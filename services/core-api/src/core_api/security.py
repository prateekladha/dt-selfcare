"""Authentication: argon2id password hashes + short-lived signed JWTs (RS256).

Tokens are signed with an RSA private key that only core-api (the issuer) holds. Every other service
verifies with the public key, which can check a token but never create one, so a compromised web or
payments pod cannot forge a session for another customer. Only RS256 is accepted (no `alg` confusion).

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


_dev_keys: tuple[str, str] | None = None


def _dev_keypair() -> tuple[str, str]:
    """Process-local keypair for unit tests and local runs only (never used when keys are configured)."""
    global _dev_keys
    if get_settings().require_jwt_keys:
        raise RuntimeError("JWT keys are not configured (APP_JWT_PUBLIC_KEY / APP_JWT_PRIVATE_KEY)")
    if _dev_keys is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _dev_keys = (k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode(),
                     k.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo).decode())
    return _dev_keys


def _signing_key() -> str:
    s = get_settings()
    if s.jwt_private_key:
        return s.jwt_private_key
    if s.jwt_public_key:  # a verifier-only service (no private key) must never issue tokens
        raise RuntimeError("this service holds only the public key and cannot issue tokens")
    return _dev_keypair()[0]


def _verify_key() -> str:
    s = get_settings()
    return s.jwt_public_key or _dev_keypair()[1]


def issue_token(subscriber_id: str, country: str, role: str = "customer") -> tuple[str, int]:
    s = get_settings()
    now = int(time.time())
    claims = {"sub": subscriber_id, "country": country, "role": role, "iat": now, "exp": now + s.jwt_ttl_s,
              "iss": ISSUER}
    return jwt.encode(claims, _signing_key(), algorithm="RS256"), s.jwt_ttl_s


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
        c = jwt.decode(token, _verify_key(), algorithms=["RS256"], issuer=ISSUER)
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
