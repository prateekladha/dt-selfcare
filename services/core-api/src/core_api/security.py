"""Authentication: argon2id password hashes + short-lived signed JWTs.

Every customer endpoint derives the subscriber from the token (`/v1/me/...`) and never from the
URL, so one customer cannot address another customer's data (no IDOR by construction).
In DT production the token would come from the group IdP (OIDC); only `issue_token` changes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .settings import get_settings

_ph = PasswordHasher()
_bearer = HTTPBearer(auto_error=False)
ISSUER = "dt-selfcare-core-api"


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(hashed: str, pw: str) -> bool:
    try:
        return _ph.verify(hashed, pw)
    except VerifyMismatchError:
        return False


@dataclass(frozen=True)
class Principal:
    subscriber_id: str
    country: str
    role: str = "customer"


def issue_token(subscriber_id: str, country: str, role: str = "customer") -> tuple[str, int]:
    s = get_settings()
    now = int(time.time())
    claims = {"sub": subscriber_id, "country": country, "role": role, "iat": now, "exp": now + s.jwt_ttl_s,
              "iss": ISSUER}
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256"), s.jwt_ttl_s


REFRESH_TTL_S = 30 * 24 * 3600
ROTATION_GRACE_S = 30


def _rt_key(token: str) -> str:
    import hashlib

    return "rt:" + hashlib.sha256(token.encode()).hexdigest()  # only a hash is stored


def issue_refresh(r, subscriber_id: str, country: str, role: str = "customer") -> str:
    import json
    import secrets

    token = secrets.token_urlsafe(32)
    r.set(_rt_key(token), json.dumps({"sub": subscriber_id, "country": country, "role": role}), ex=REFRESH_TTL_S)
    return token


def rotate_refresh(r, token: str) -> dict | None:
    """One-time use with a short grace window: parallel requests refreshing with the same token at the
    same moment all receive the same new pair instead of logging the user out."""
    import json

    key = _rt_key(token)
    grace = r.get(key + ":next")
    if grace:
        return json.loads(grace)
    raw = r.get(key)
    if raw is None:
        return None
    claims = json.loads(raw)
    access, ttl = issue_token(claims["sub"], claims["country"], claims.get("role", "customer"))
    new_refresh = issue_refresh(r, claims["sub"], claims["country"], claims.get("role", "customer"))
    pair = {"access_token": access, "expires_in": ttl, "refresh_token": new_refresh, "token_type": "bearer"}
    pipe = r.pipeline()
    pipe.delete(key)
    pipe.set(key + ":next", json.dumps(pair), ex=ROTATION_GRACE_S)
    pipe.execute()
    return pair


def revoke_refresh(r, token: str) -> None:
    r.delete(_rt_key(token))


def decode_token(token: str) -> Principal:
    try:
        c = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"], issuer=ISSUER)
    except jwt.PyJWTError as e:
        raise HTTPException(401, "invalid or expired token") from e
    return Principal(c["sub"], c["country"], c.get("role", "customer"))


def current_principal(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if cred is None:
        raise HTTPException(401, "missing bearer token")
    return decode_token(cred.credentials)


def require_admin(p: Principal = Depends(current_principal)) -> Principal:
    if p.role != "admin":
        raise HTTPException(403, "admin only")
    return p
