"""Session login for the dashboard."""
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .. import security
from ..config import COOKIE_NAME, SESSION_MAX_AGE
from ..notify import ntfy, telegram

router = APIRouter()


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
def login(body: LoginRequest, response: Response):
    if not security.configured():
        raise HTTPException(
            503,
            "Dashboard auth is not configured. On the server run "
            "`python -m monitor.hashpw` and put both lines in monitor/.env.",
        )
    if not security.verify_password(body.password):
        raise HTTPException(401, "Incorrect password")

    response.set_cookie(COOKIE_NAME, security.issue_session(), **SESSION_COOKIE)
    return {"ok": True}


SESSION_COOKIE = dict(max_age=SESSION_MAX_AGE, httponly=True,
                      samesite="lax", secure=True, path="/")


@router.get("/auth/telegram")
def telegram_login(t: str = ""):
    """Redeem a one-tap sign-in link sent to the bot's own chat.

    Redirects rather than returning JSON, because this is opened by tapping a
    link in a chat app and what should happen next is the dashboard.
    """
    if not security.consume_login_token(t):
        # Deliberately one message for expired, already-used and forged. The
        # difference is only useful to someone who did not send the link.
        return HTMLResponse(
            "<p>That sign-in link has expired or has already been used. "
            "Send <code>/login</code> to the bot for a fresh one.</p>",
            status_code=401)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE_NAME, security.issue_session(), **SESSION_COOKIE)
    return response


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    return {
        "authenticated": security.session_valid(request.cookies.get(COOKIE_NAME)),
        "auth_configured": security.configured(),
        "telegram_configured": telegram.configured(),
        "ntfy_configured": ntfy.configured(),
    }
