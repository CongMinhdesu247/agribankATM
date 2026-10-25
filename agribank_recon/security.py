import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import config

bearer_scheme = HTTPBearer(auto_error=False)
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LENGTH = 32


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LENGTH
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded_hash: str) -> bool:
    if not isinstance(encoded_hash, str) or len(encoded_hash) > 256:
        return False
    try:
        # The hash comes from the local database. Still validate its work factors
        # before passing them to scrypt so a damaged/tampered DB cannot request
        # arbitrarily large CPU or memory allocations during login.
        algorithm, n, r, p, salt_value, digest_value = encoded_hash.split("$", 5)
        if (algorithm != "scrypt" or int(n) != SCRYPT_N or int(r) != SCRYPT_R
                or int(p) != SCRYPT_P):
            return False
        expected = _b64decode(digest_value)
        salt = _b64decode(salt_value)
        if len(expected) != SCRYPT_LENGTH or len(salt) != 16:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LENGTH,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError, MemoryError, OverflowError):
        return False


def create_access_token(username: str) -> tuple[str, int]:
    if len(config.JWT_SECRET) < 32:
        raise RuntimeError("JWT_SECRET must contain at least 32 characters; run python -m scripts.setup_admin first.")
    ttl_seconds = config.ACCESS_TOKEN_TTL_MINUTES * 60
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + ttl_seconds,
        "iss": "agribank-reconciliation",
        "aud": "agribank-reconciliation-api",
    }
    unsigned = f"{_b64encode(json.dumps(header, separators=(',', ':')).encode())}.{_b64encode(json.dumps(payload, separators=(',', ':')).encode())}"
    signature = hmac.new(config.JWT_SECRET.encode(), unsigned.encode("ascii"), hashlib.sha256).digest()
    return f"{unsigned}.{_b64encode(signature)}", ttl_seconds


def verify_access_token(token: str) -> str | None:
    try:
        if not token or len(token) > 4096:
            return None
        header_part, payload_part, signature_part = token.split(".", 2)
        unsigned = f"{header_part}.{payload_part}"
        expected = hmac.new(config.JWT_SECRET.encode(), unsigned.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature_part)):
            return None
        header = json.loads(_b64decode(header_part))
        payload = json.loads(_b64decode(payload_part))
        if header.get("alg") != "HS256" or header.get("typ") != "JWT":
            return None
        now = int(time.time())
        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        if not isinstance(issued_at, int) or issued_at > now + 60:
            return None
        if not isinstance(expires_at, int) or expires_at <= now or expires_at - issued_at > config.ACCESS_TOKEN_TTL_MINUTES * 60 + 60:
            return None
        if payload.get("iss") != "agribank-reconciliation" or payload.get("aud") != "agribank-reconciliation-api":
            return None
        subject = payload.get("sub")
        return subject if isinstance(subject, str) and subject else None
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return None


def require_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    username = verify_access_token(credentials.credentials)
    if username is None or not hmac.compare_digest(
        username.encode("utf-8"), config.APP_USERNAME.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return username

