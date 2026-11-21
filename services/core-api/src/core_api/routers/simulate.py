from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from telco_common.billing import rate
from telco_common.catalog import get_plan
from telco_common.explainer import explain
from telco_common.models import cycle_of

from ..db import bills, get_engine, subscribers
from ..deps import get_usage_state, get_valkey
from ..security import Principal, current_principal
from ..services.bills import row_to_bill
from ..services.simulate import SCENARIOS, UsageFileError, parse_upload, scenario_counters

router = APIRouter(prefix="/v1/me/usage", tags=["usage-insights"])


@router.get("/scenarios")
def scenarios() -> list[dict]:
    return [{"key": k, "title": v["title"]} for k, v in SCENARIOS.items()]


@router.get("/sample.csv", include_in_schema=False)
def sample_csv():
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse("usage_type,quantity,unit,roaming_zone\nDATA,6,GB,\nVOICE,180,min,\n"
                             "ROAMING_DATA,900,MB,Z2\nROAMING_VOICE,40,min,Z2\nINTL_VOICE,30,min,\nSMS,12,count,\n",
                             media_type="text/csv")


@router.post("/simulate")
async def simulate(p: Principal = Depends(current_principal), scenario: str | None = Form(default=None),
                   file: UploadFile | None = File(default=None),
                   lang: str = Query("en", pattern="^(en|de|pl)$")) -> dict:
    personal = False
    try:
        if file is not None:
            counters = parse_upload(file.filename or "usage.csv", await file.read())
            source = f"upload:{file.filename}"
        elif scenario:
            counters = scenario_counters(scenario)
            source = f"scenario:{scenario}"
            personal = True
        else:
            raise HTTPException(422, "provide a usage file or a scenario")
    except (UsageFileError, ValueError, KeyError, UnicodeDecodeError) as e:
        raise HTTPException(422, f"invalid usage: {e}") from e

    engine = get_engine()
    with engine.connect() as cx:
        sub = cx.execute(select(subscribers).where(subscribers.c.id == p.subscriber_id)).one()
        last = cx.execute(select(bills).where(bills.c.subscriber_id == p.subscriber_id)
                          .order_by(bills.c.cycle.desc()).limit(1)).first()
    # priced as "this cycle" (YYYY-MM) so it fits every cycle-typed column and renders like a real bill
    base: dict[str, int] = {}
    if personal and last:
        # scenario on top of YOUR actual usage (CDR totals) from your latest billed cycle
        base = {k: v for k, v in get_usage_state().counters(sub.id, last.cycle).items()}
        for k, v in base.items():
            counters[k] = counters.get(k, 0) + v
    # priced as "this cycle" (YYYY-MM) so it fits every cycle-typed column and renders like a real bill
    simulated = rate(sub.id, cycle_of(datetime.now(UTC)), get_plan(sub.plan_code), counters)
    ex = explain(simulated, row_to_bill(last) if last else None)
    llm_out, status, sources = None, "UNAVAILABLE", []
    try:
        from ..agent.explain_llm import explain_bill
        from ..agent.factory import build_llm

        res = explain_bill(build_llm(p.country, p.subscriber_id), get_valkey(), simulated,
                           row_to_bill(last) if last else None, lang)
        llm_out, status, sources = res["explanation"], res["status"], res["sources"]
    except Exception:  # noqa: BLE001, S110 - LLM unavailable: the priced breakdown is still shown
        pass
    return {"source": source, "compared_to": last.cycle if last else None, "baseline": "your-last-cycle" if (personal and last) else None,
            "bill": simulated.model_dump(),
            "explanation": ex.model_dump(), "llm": llm_out, "status": status, "sources": sources}
