from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from telco_common.billing import rate
from telco_common.catalog import get_plan
from telco_common.explainer import explain

from ..db import bills, get_engine, subscribers
from ..deps import RateLimiter, get_valkey
from ..security import Principal, current_principal
from ..services.ai import Narrator
from ..services.bills import row_to_bill
from ..services.simulate import SCENARIOS, UsageFileError, parse_upload, scenario_counters
from ..settings import get_settings

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
    try:
        if file is not None:
            counters = parse_upload(file.filename or "usage.csv", await file.read())
            source = f"upload:{file.filename}"
        elif scenario:
            counters = scenario_counters(scenario)
            source = f"scenario:{scenario}"
        else:
            raise HTTPException(422, "provide a usage file or a scenario")
    except (UsageFileError, ValueError, KeyError, UnicodeDecodeError) as e:
        raise HTTPException(422, f"invalid usage: {e}") from e

    engine = get_engine()
    with engine.connect() as cx:
        sub = cx.execute(select(subscribers).where(subscribers.c.id == p.subscriber_id)).one()
        last = cx.execute(select(bills).where(bills.c.subscriber_id == p.subscriber_id)
                          .order_by(bills.c.cycle.desc()).limit(1)).first()
    simulated = rate(sub.id, "simulated", get_plan(sub.plan_code), counters)
    ex = explain(simulated, row_to_bill(last) if last else None)
    s = get_settings()
    allowed = RateLimiter(get_valkey()).allow(f"ai:{p.subscriber_id}", s.ai_daily_limit_per_subscriber, 86400)
    text, provider = Narrator(s, engine).narrate(ex, p.subscriber_id, p.country, lang, allow_llm=allowed)
    return {"source": source, "compared_to": last.cycle if last else None, "bill": simulated.model_dump(),
            "explanation": ex.model_dump(), "narrative": text, "provider": provider}
