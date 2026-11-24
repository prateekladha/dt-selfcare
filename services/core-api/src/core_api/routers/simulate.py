from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from telco_common.billing import rate
from telco_common.catalog import get_plan
from telco_common.explainer import explain
from telco_common.models import cycle_of

from ..db import bills, get_engine, subscribers
from ..deps import get_valkey
from ..security import Principal, current_principal
from ..services.bills import row_to_bill
from ..services.simulate import (
    MAX_BYTES,
    SCENARIOS,
    UsageFileError,
    apply_scenario,
    custom_scenario,
    parse_upload,
)

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


WHAT_IF_AI_PER_DAY = 10   # fresh what-if explanations per customer per day (own quota: never the real bill's)


@router.post("/simulate")
def simulate(p: Principal = Depends(current_principal), scenario: str | None = Form(default=None),
             file: UploadFile | None = File(default=None),
             custom: str | None = Form(default=None), start: str = Form(default="usual"),
             data_gb: float | None = Form(default=None), voice_min: float | None = Form(default=None),
             intl_min: float | None = Form(default=None), sms: float | None = Form(default=None),
             trip_zone: str | None = Form(default=None), trip_data_gb: float | None = Form(default=None),
             trip_voice_min: float | None = Form(default=None),
             lang: str = Query("en", pattern="^(en|de|pl)$")) -> dict:
    """Plain def (not async): the LLM call below is blocking, so it runs in the threadpool instead of freezing
    every other request on this worker. Never touches real usage or bills."""
    import json as _json

    from telco_common.flags import flag_bool

    engine = get_engine()
    with engine.connect() as cx:
        sub = cx.execute(select(subscribers).where(subscribers.c.id == p.subscriber_id)).one()
        last = cx.execute(select(bills).where(bills.c.subscriber_id == p.subscriber_id)
                          .order_by(bills.c.cycle.desc()).limit(1)).first()
    # baseline = exactly what the last bill was priced from (late records arriving afterwards aren't blamed on
    # the scenario, and an expired live counter can't silently empty it)
    base = {k: int(v) for k, v in _json.loads(last.rated_counters).items()} if last and last.rated_counters else {}
    try:
        if file is not None:
            content = file.file.read(MAX_BYTES + 1)   # bounded: a huge upload is never read whole
            counters = parse_upload(file.filename or "usage.csv", content)
            source, baseline = f"upload:{file.filename}", None
        elif custom:
            counters = custom_scenario(base, start, {"data_gb": data_gb, "voice_min": voice_min, "intl_min": intl_min,
                                                     "sms": sms, "trip_data_gb": trip_data_gb,
                                                     "trip_voice_min": trip_voice_min}, trip_zone)
            source, baseline = "scenario:custom", ("your-last-cycle" if base and start == "usual" else None)
        elif scenario:
            counters = apply_scenario(scenario, base)
            source, baseline = f"scenario:{scenario}", ("your-last-cycle" if base else None)
        else:
            raise HTTPException(422, "provide a usage file or a scenario")
    except UsageFileError as e:
        raise HTTPException(422, str(e)) from e

    # priced as "this cycle" (YYYY-MM) so it fits every cycle-typed column and renders like a real bill
    simulated = rate(sub.id, cycle_of(datetime.now(UTC)), get_plan(sub.plan_code), counters)
    ex = explain(simulated, row_to_bill(last) if last else None)
    llm_out, status, sources = None, "UNAVAILABLE", []
    if flag_bool("ai-explanations", p.country, p.subscriber_id):   # the AI kill switch applies here too
        try:
            from ..agent.explain_llm import explain_bill
            from ..agent.factory import build_llm

            title = (SCENARIOS[scenario]["title"] if scenario in SCENARIOS
                     else "the customer's own scenario" if custom else "the uploaded usage")
            res = explain_bill(build_llm(p.country, p.subscriber_id), get_valkey(), simulated,
                               row_to_bill(last) if last else None, lang,
                               quota=(f"rl:ai-whatif:{p.subscriber_id}", WHAT_IF_AI_PER_DAY),
                               subject=f"a WHAT-IF estimate for '{title}' (a simulation, not a real bill)")
            llm_out, status, sources = res["explanation"], res["status"], res["sources"]
        except Exception:  # noqa: BLE001, S110 - LLM unavailable: the priced breakdown is still shown
            pass
    scenario_title = (SCENARIOS[scenario]["title"] if scenario in SCENARIOS and not custom
                      else "Your own scenario" if custom else None)
    return {"source": source, "scenario_title": scenario_title,
            "compared_to": last.cycle if last else None, "baseline": baseline,
            "bill": simulated.model_dump(),
            "explanation": ex.model_dump(), "llm": llm_out, "status": status, "sources": sources}
