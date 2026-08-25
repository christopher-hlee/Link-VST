"""Password hashing and signed session cookies.

The dashboard is reachable from the public internet, and a static Bearer key is
not enough on its own there: a browser cannot hold a secret, so anything shipped
to the page is readable by anyone who loads it. Sessions are signed cookies
instead; the password is stored only as an scrypt hash, generated on the server
via `python -m monitor.hashpw`. The Bearer key is kept for scripts.
"""
import hashlib
import hmac
import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import PASSWORD_HASH, SESSION_MAX_AGE, SESSION_SECRET

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
_SALT = "monitor-session"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N,
                            r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str = "") -> bool:
    stored = stored or PASSWORD_HASH
    try:
        scheme, n, r, p, salt_hex, want_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                             n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(got.hex(), want_hex)


def _serializer() -> URLSafeTimedSerializer:
    if not SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET is not set")
    return URLSafeTimedSerializer(SESSION_SECRET, salt=_SALT)


def issue_session() -> str:
    return _serializer().dumps({"v": 1})


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    try:
        _serializer().loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired, RuntimeError):
        return False


def configured() -> bool:
    return bool(PASSWORD_HASH and SESSION_SECRET)
