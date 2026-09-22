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
import secrets

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


# --- one-tap sign-in over Telegram ---------------------------------------
#
# The dashboard sits behind a password, and a password needs a text input to
# type it into — which is not always available. A corporate network that routes
# unrecognised domains through browser isolation streams the page as pixels and
# blocks typing entirely, so the password field becomes unusable while the app
# behind it works perfectly.
#
# The link is signed, expires in minutes, and is good exactly once: the token
# carries a nonce that is also stored server-side, and redeeming it deletes the
# nonce. Minting a new link invalidates the previous one, so at most one live
# link exists at a time.

LOGIN_TOKEN_MAX_AGE = 600          # seconds
_LOGIN_SALT = "monitor-login-link"
_NONCE_KEY = "login_nonce"


def _login_serializer() -> URLSafeTimedSerializer:
    if not SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET is not set")
    return URLSafeTimedSerializer(SESSION_SECRET, salt=_LOGIN_SALT)


def issue_login_token() -> str:
    from . import db

    nonce = secrets.token_urlsafe(16)
    db.kv_set(_NONCE_KEY, nonce)
    return _login_serializer().dumps({"n": nonce})


def consume_login_token(token: str | None) -> bool:
    """True exactly once per issued token, and only before it expires."""
    from . import db

    if not token:
        return False
    try:
        data = _login_serializer().loads(token, max_age=LOGIN_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired, RuntimeError):
        return False
    # Claim it, rather than read-compare-delete: a wrong or replayed token must
    # not disturb the live one. The signature is already verified above, so
    # reaching here with a value at all means it came from us.
    return db.kv_claim(_NONCE_KEY, str(data.get("n") or ""))


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
