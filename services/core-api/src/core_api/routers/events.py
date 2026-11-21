"""Server-sent events: pushes new notifications (e.g. "80% of your data used") to the open app.

The notifier publishes to Valkey channel `notif:<subscriber>`; each API pod relays only the
channels of the clients connected to it, so the push path scales horizontally with the API.
"""
import asyncio

import valkey.asyncio as avalkey
from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from ..security import Principal, current_principal
from ..settings import get_settings

router = APIRouter(prefix="/v1/me", tags=["events"])


@router.get("/events")
async def events(request: Request, p: Principal = Depends(current_principal)):
    async def stream():
        r = avalkey.Valkey.from_url(get_settings().valkey_url)
        ps = r.pubsub()
        await ps.subscribe(f"notif:{p.subscriber_id}")
        try:
            while not await request.is_disconnected():
                msg = await ps.get_message(ignore_subscribe_messages=True, timeout=15)
                if msg:
                    yield {"event": "notification", "data": msg["data"].decode()}
                else:
                    yield {"event": "ping", "data": ""}
                await asyncio.sleep(0)
        finally:
            await ps.unsubscribe()
            await r.aclose()

    return EventSourceResponse(stream(), headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
