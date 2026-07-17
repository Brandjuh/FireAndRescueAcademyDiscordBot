"""Web console authentication: a single operator password, a per-boot
session cookie, and a brute-force throttle.

The password comes from (in order) the ``WEB_PASSWORD`` environment
variable or a generated one persisted in state (shown by ``!fra web``).
Sessions are in-memory only, so a bot restart logs everyone out.
The cookie is HttpOnly + SameSite=Lax — Lax also means cross-site form
POSTs arrive without the cookie, which layers on top of the
same-origin middleware. No password configured (``build_app`` without
one, as in most tests) means no auth — the console then relies on the
loopback bind alone, which is exactly the pre-auth behaviour.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time

from aiohttp import web

from .html import esc, page

log = logging.getLogger(__name__)

COOKIE_NAME = "fra_session"
#: Paths served without a session (login itself + the bare health probe).
_OPEN_PATHS = frozenset({"/login", "/health"})

#: Brute-force throttle: after this many failures within the window the
#: login is locked for LOCKOUT_SECONDS. In-memory — LAN-scale is enough.
MAX_FAILURES = 8
FAILURE_WINDOW_SECONDS = 900.0
LOCKOUT_SECONDS = 60.0


class SessionStore:
    def __init__(self) -> None:
        self.tokens: set[str] = set()
        self.failures: list[float] = []
        self.locked_until = 0.0

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        self.tokens.add(token)
        return token

    def valid(self, token: str | None) -> bool:
        return bool(token) and token in self.tokens

    def drop(self, token: str | None) -> None:
        self.tokens.discard(token or "")

    # -- throttle ----------------------------------------------------------

    def locked(self) -> bool:
        return time.monotonic() < self.locked_until

    def record_failure(self) -> None:
        now = time.monotonic()
        self.failures = [
            at for at in self.failures if now - at < FAILURE_WINDOW_SECONDS
        ]
        self.failures.append(now)
        if len(self.failures) >= MAX_FAILURES:
            self.locked_until = now + LOCKOUT_SECONDS
            self.failures.clear()
            log.warning("web console: login locked for %.0f s after "
                        "repeated failures", LOCKOUT_SECONDS)


def auth_middleware_factory(password: str, store: SessionStore):
    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.path in _OPEN_PATHS:
            return await handler(request)
        if store.valid(request.cookies.get(COOKIE_NAME)):
            return await handler(request)
        raise web.HTTPFound("/login")

    return middleware


def _login_body(error: str | None = None) -> str:
    notice = f"<div class='flash err'>{esc(error)}</div>" if error else ""
    return (
        f"{notice}<div class='panel' style='max-width:420px'>"
        "<form method='post' action='/login'>"
        "<label>Operator password</label>"
        "<input type='password' name='password' autofocus>"
        "<button>Log in</button></form>"
        "<p class='muted'>The password comes from WEB_PASSWORD in the "
        "bot's .env, or was generated at first start — run "
        "<code>!fra web</code> in Discord to see it.</p></div>"
    )


async def login_page(request: web.Request) -> web.Response:
    return web.Response(
        text=page("Log in", _login_body(), active=""),
        content_type="text/html",
    )


async def post_login(request: web.Request) -> web.Response:
    password: str = request.app["web_password"]
    store: SessionStore = request.app["web_sessions"]
    if store.locked():
        return web.Response(
            status=429,
            text=page("Log in", _login_body(
                "Too many attempts — locked for a minute."), active=""),
            content_type="text/html",
        )
    form = await request.post()
    attempt = str(form.get("password") or "")
    if not hmac.compare_digest(attempt, password):
        store.record_failure()
        await asyncio.sleep(0.5)  # flat cost per wrong guess
        return web.Response(
            status=403,
            text=page("Log in", _login_body("Wrong password."), active=""),
            content_type="text/html",
        )
    response = web.HTTPFound("/")
    response.set_cookie(
        COOKIE_NAME, store.issue(), httponly=True, samesite="Lax", path="/",
    )
    return response


async def post_logout(request: web.Request) -> web.Response:
    store: SessionStore = request.app["web_sessions"]
    store.drop(request.cookies.get(COOKIE_NAME))
    # Without a password there is no /login route (open test mode).
    response = web.HTTPFound(
        "/login" if request.app.get("web_password") else "/"
    )
    response.del_cookie(COOKIE_NAME, path="/")
    return response
