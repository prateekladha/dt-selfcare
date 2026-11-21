"""Operator endpoints (admin role): bill run trigger and pipeline health snapshot."""
from fastapi import APIRouter, Depends, HTTPException

from ..db import get_engine
from ..deps import get_usage_state
from ..security import Principal, require_admin
from ..services.bills import close_cycle
from ..settings import get_settings

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.post("/bill-runs/{cycle}")
def bill_run(cycle: str, settle_before: str | None = None, _: Principal = Depends(require_admin)) -> dict:
    try:
        return close_cycle(get_engine(), get_usage_state(), cycle, settle_before, get_settings().bill_due_days)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/pipeline")
def pipeline(_: Principal = Depends(require_admin)) -> dict:
    st = get_usage_state()
    return {"partitions": {p: st.committed_offset(p) for p in range(st.partitions)},
            "outbox_depth": {p: st.r.xlen(f"alerts:{{p{p}}}") for p in range(st.partitions)}}
