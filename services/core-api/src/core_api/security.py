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
