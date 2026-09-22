"""One-tap sign-in, and commands, over the chat the app already talks to.

Built because a password needs somewhere to type it. A corporate network that
routes unrecognised domains through browser isolation streams the dashboard as
pixels and blocks text input, so the password field is unusable while
everything behind it works. The bot is reachable because it is not a browser.

Most of this file is about the ways a sign-in link must NOT work.
"""
import asyncio
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from monitor import db, security, telegram_bot

CHAT = "12345"


@pytest.fixture(autouse=True)
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr("monitor.config.SESSION_SECRET", "s" * 32)
    monkeypatch.setattr("monitor.security.SESSION_SECRET", "s" * 32)
    monkeypatch.setattr("monitor.security.PASSWORD_HASH",
                        security.hash_password("pw"))
    monkeypatch.setattr("monitor.telegram_bot.TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setattr("monitor.telegram_bot.TELEGRAM_BOT_TOKEN", "bot:token")
    monkeypatch.setattr("monitor.telegram_bot.PUBLIC_URL", "")
    db.kv_set("public_origin", "https://dash.example")


@pytest.fixture
def client():
    from monitor.main import app
    # https, because the session cookie is Secure and a client on http would
    # silently decline to send it back — which would look like a login bug.
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def token_from(reply: str) -> str:
    return reply.split("?t=")[1].split('"')[0]


# --- the token ------------------------------------------------------------

def test_a_link_signs_you_in():
    token = security.issue_login_token()
    assert security.consume_login_token(token) is True


def test_a_link_works_exactly_once():
    """The whole point of the nonce. A link sits in a chat log forever."""
    token = security.issue_login_token()

    assert security.consume_login_token(token) is True
    assert security.consume_login_token(token) is False


def test_minting_a_new_link_kills_the_previous_one():
    first = security.issue_login_token()
    second = security.issue_login_token()

    assert security.consume_login_token(first) is False
    assert security.consume_login_token(second) is True


def test_a_stale_link_does_not_destroy_the_live_one():
    """The realistic case, and the one that actually reaches the nonce check.

    A forged token dies at the signature and never touches storage. A token we
    really did issue and have since superseded does reach it — and old links
    sit in a chat log forever, so one being opened must not invalidate the
    current one. Read-then-delete would do exactly that, locking you out of
    your own dashboard by re-tapping yesterday's message.
    """
    stale = security.issue_login_token()
    live = security.issue_login_token()

    assert security.consume_login_token(stale) is False
    assert security.consume_login_token(live) is True, \
        "the current link must survive an old one being opened"


def test_a_forged_token_is_refused():
    assert security.consume_login_token("not-a-token") is False
    assert security.consume_login_token("") is False
    assert security.consume_login_token(None) is False


def test_a_token_signed_with_another_secret_is_refused(monkeypatch):
    token = security.issue_login_token()
    monkeypatch.setattr("monitor.security.SESSION_SECRET", "different" * 4)

    assert security.consume_login_token(token) is False


def test_an_expired_link_is_refused(monkeypatch):
    token = security.issue_login_token()
    monkeypatch.setattr(security, "LOGIN_TOKEN_MAX_AGE", -1)

    assert security.consume_login_token(token) is False


# --- redeeming it ---------------------------------------------------------

def test_redeeming_sets_a_session_and_lands_on_the_dashboard(client):
    token = security.issue_login_token()

    r = client.get(f"/api/auth/telegram?t={token}", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie
    assert client.get("/api/me").json()["authenticated"] is True


def test_a_spent_link_does_not_sign_anyone_in(client):
    token = security.issue_login_token()
    client.get(f"/api/auth/telegram?t={token}", follow_redirects=False)

    again = TestClient(client.app, base_url="https://testserver")
    r = again.get(f"/api/auth/telegram?t={token}", follow_redirects=False)

    assert r.status_code == 401
    assert again.get("/api/me").json()["authenticated"] is False


def test_the_redeem_route_needs_no_session_to_reach(client):
    """Requiring a session to reach the thing that grants one is a closed
    loop, and would make this feature useless precisely when it is needed."""
    r = client.get("/api/auth/telegram?t=nonsense", follow_redirects=False)

    assert r.status_code == 401, "refused, but reachable"
    assert "Not authenticated" not in r.text, "refused by the route, not the gate"


# --- who the bot listens to -----------------------------------------------

def update(uid, text, chat=CHAT):
    return {"update_id": uid,
            "message": {"chat": {"id": int(chat)}, "text": text}}


@respx.mock
async def test_only_the_configured_chat_is_obeyed():
    """Anyone can message a bot whose username they know."""
    sent = []
    respx.get(url__startswith="https://api.telegram.org/botbot:token/getUpdates"
              ).mock(return_value=httpx.Response(200, json={
                  "ok": True, "result": [update(1, "/login", chat="99999")]}))
    respx.post(url__startswith="https://api.telegram.org/botbot:token/sendMessage"
               ).mock(side_effect=lambda req: sent.append(req) or httpx.Response(
                   200, json={"ok": True}))

    await telegram_bot._poll_once()

    assert sent == [], "a stranger gets silence, not an error"


@respx.mock
async def test_a_stranger_cannot_mint_a_link_for_someone_else():
    respx.get(url__startswith="https://api.telegram.org/botbot:token/getUpdates"
              ).mock(return_value=httpx.Response(200, json={
                  "ok": True, "result": [update(1, "/login", chat="99999")]}))
    respx.post(url__startswith="https://api.telegram.org/botbot:token/sendMessage"
               ).mock(return_value=httpx.Response(200, json={"ok": True}))

    await telegram_bot._poll_once()

    assert db.kv_get("login_nonce") is None, "no token was ever created"


# --- polling behaviour ----------------------------------------------------

@respx.mock
async def test_a_command_that_throws_is_not_replayed_forever():
    """The offset moves before the command runs. Otherwise one bad message
    jams the queue for the life of the process."""
    async def boom(text):
        raise RuntimeError("nope")

    respx.get(url__startswith="https://api.telegram.org/botbot:token/getUpdates"
              ).mock(return_value=httpx.Response(200, json={
                  "ok": True, "result": [update(7, "/status")]}))
    respx.post(url__startswith="https://api.telegram.org/botbot:token/sendMessage"
               ).mock(return_value=httpx.Response(200, json={"ok": True}))

    import monitor.telegram_bot as bot
    original, bot.handle = bot.handle, boom
    try:
        await bot._poll_once()
    finally:
        bot.handle = original

    assert db.kv_get("telegram_offset") == "8"


@respx.mock
async def test_plain_chat_is_ignored():
    sent = []
    respx.get(url__startswith="https://api.telegram.org/botbot:token/getUpdates"
              ).mock(return_value=httpx.Response(200, json={
                  "ok": True, "result": [update(1, "hello there")]}))
    respx.post(url__startswith="https://api.telegram.org/botbot:token/sendMessage"
               ).mock(side_effect=lambda req: sent.append(req) or httpx.Response(
                   200, json={"ok": True}))

    await telegram_bot._poll_once()

    assert sent == []


# --- the commands ---------------------------------------------------------

async def test_login_replies_with_a_working_link(client):
    reply = await telegram_bot.handle("/login")

    assert "dash.example/api/auth/telegram?t=" in reply
    r = client.get(f"/api/auth/telegram?t={token_from(reply)}",
                   follow_redirects=False)
    assert r.status_code == 303


async def test_login_says_so_when_it_does_not_know_its_own_address():
    db.kv_set("public_origin", None)

    reply = await telegram_bot.handle("/login")

    assert "do not know this app's public address" in reply
    assert "http" not in reply.replace("PUBLIC_URL", "")


async def test_an_unknown_command_is_answered_with_silence():
    assert await telegram_bot.handle("/nonsense") is None


async def test_the_command_suffix_a_group_chat_adds_is_stripped():
    assert await telegram_bot.handle("/help@restockbot") is not None
