import re
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def password_policy_error(password: str) -> Optional[str]:
    """Return a human-readable error if the password fails the complexity policy, else None.

    Policy: min 8 chars, at least 1 uppercase, 1 lowercase, 1 number, 1 special character.
    """
    if not password or len(password) < 8:
        return "Password must be at least 8 characters long."
    if not re.search(r"[A-Z]", password):
        return "Password must contain at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Password must contain at least one lowercase letter."
    if not re.search(r"\d", password):
        return "Password must contain at least one number."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must contain at least one special character."
    return None


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None


# ── Token revocation (SEC-002) ────────────────────────────────────────────────────────────
# A session token carries the user's `token_version` as a `ver` claim. Authentication compares
# the claim against the live column and rejects a mismatch, so incrementing the column revokes
# every token previously issued to that user. Without this a JWT is valid until it expires —
# ten years for Admin and Super Admin — and cannot be withdrawn.
#
# `token_claim_version` is the ONE place that decides how a token's version is read, so the two
# independent authentication paths (get_current_user and the support WebSocket) cannot drift
# apart. A missing claim reads as 0, which is what keeps tokens minted before this feature valid.

TOKEN_VERSION_CLAIM = "ver"


def token_claim_version(payload: dict | None) -> int:
    """Version a token asserts. Absent (pre-feature token) or malformed → 0."""
    try:
        return int((payload or {}).get(TOKEN_VERSION_CLAIM, 0) or 0)
    except (TypeError, ValueError):
        return 0


def token_version_matches(payload: dict | None, user) -> bool:
    """True when the token may still be used for `user`.

    Deliberately tolerant of a NULL column: a row that predates the migration reads as 0 and
    matches a token with no claim, so an incomplete migration cannot lock anyone out.
    """
    return token_claim_version(payload) == int(getattr(user, "token_version", 0) or 0)
