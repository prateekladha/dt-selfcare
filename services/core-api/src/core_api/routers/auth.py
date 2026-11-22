import ipaddress

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import get_engine, subscribers
from ..deps import RateLimiter, get_valkey
from ..security import issue_refresh, issue_token, revoke_refresh, rotate_refresh, verify_password

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Brute force: at most 10 attempts per account per 15 min (guessing one customer's password).
# Credential stuffing / password spraying (one password against many accounts): at most 20 FAILED
# logins per client IP per 15 min. Only failures count, so many customers behind one NAT/office IP
# can still sign in; a spraying bot fails almost every attempt and is stopped after 20.
ACCOUNT_ATTEMPTS, IP_FAILURES, WINDOW_S = 10, 20, 900


def client_net(raw: str | None) -> str | None:
    """The caller's address as the web BFF saw it (X-Client-IP, from the ingress' X-Real-Ip).
    IPv6 is keyed by /64: one customer line gets a whole /64, so a single address is no limit."""
    try:
        ip = ipaddress.ip_address((raw or "").strip())
    except ValueError:
        return None
    return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else str(ip)


class LoginIn(BaseModel):
    subscriber_id: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


@router.post("/login")
def login(body: LoginIn, rl: RateLimiter = Depends(lambda: RateLimiter(get_valkey())),
          x_client_ip: str | None = Header(None)) -> dict:
    r = get_valkey()
    net = client_net(x_client_ip)
    ip_key = f"rl:login-fail-ip:{net}" if net else None
    if ip_key and int(r.get(ip_key) or 0) >= IP_FAILURES:
        raise HTTPException(429, "too many attempts, try again later")
    if not rl.allow(f"login:{body.subscriber_id}", limit=ACCOUNT_ATTEMPTS, window_s=WINDOW_S):
        raise HTTPException(429, "too many attempts, try again later")
    with get_engine().connect() as cx:
        row = cx.execute(select(subscribers).where(subscribers.c.id == body.subscriber_id)).first()
    if row is None or not verify_password(row.password_hash, body.password):
        if ip_key and r.incr(ip_key) == 1:
            r.expire(ip_key, WINDOW_S)
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
