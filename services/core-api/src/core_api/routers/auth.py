from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import get_engine, subscribers
from ..deps import RateLimiter, get_valkey
from ..security import issue_refresh, issue_token, revoke_refresh, rotate_refresh, verify_password

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
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl,
            "refresh_token": issue_refresh(get_valkey(), row.id, row.country)}


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=200)


@router.post("/refresh")
def refresh(body: RefreshIn) -> dict:
    """Short-lived access token + rotating refresh token: sessions survive deploys and restarts."""
    pair = rotate_refresh(get_valkey(), body.refresh_token)
    if pair is None:
        raise HTTPException(401, "refresh token expired or revoked")
    return pair


@router.post("/logout", status_code=204)
def logout(body: RefreshIn) -> None:
    revoke_refresh(get_valkey(), body.refresh_token)
