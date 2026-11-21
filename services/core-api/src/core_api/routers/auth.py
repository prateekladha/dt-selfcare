from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import get_engine, subscribers
from ..deps import RateLimiter, get_valkey
from ..security import issue_token, verify_password

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    subscriber_id: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


@router.post("/login")
def login(body: LoginIn, rl: RateLimiter = Depends(lambda: RateLimiter(get_valkey()))) -> dict:
    if not rl.allow(f"login:{body.subscriber_id}", limit=10, window_s=900):
        raise HTTPException(429, "too many attempts, try again later")
    with get_engine().connect() as cx:
        row = cx.execute(select(subscribers).where(subscribers.c.id == body.subscriber_id)).first()
    if row is None or not verify_password(row.password_hash, body.password):
        raise HTTPException(401, "invalid credentials")
    token, ttl = issue_token(row.id, row.country)
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl}
