"""Session login for the dashboard."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import security
from ..config import COOKIE_NAME, SESSION_MAX_AGE
from ..notify import telegram

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

    response.set_cookie(
        COOKIE_NAME, security.issue_session(),
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=True, path="/",
    )
    return {"ok": True}


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
    }
