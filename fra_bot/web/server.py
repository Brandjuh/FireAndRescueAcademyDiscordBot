"""The web console server: an aiohttp app inside the bot process.

Runs on the bot's event loop against the bot's own DB handle and paced
MissionChief client — one process, one database writer. Listens on all
interfaces by default so the operator can browse to the Pi's LAN IP;
access is guarded by the operator password (``WEB_PASSWORD`` in .env,
or the generated one shown by ``!fra web``) plus the Host / same-origin
middleware below.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket

from aiohttp import web

from . import handlers

log = logging.getLogger(__name__)

#: State key for the generated operator password (WEB_PASSWORD overrides).
PASSWORD_STATE_KEY = "web:password"

#: Non-IP hostnames the console answers to. IP-literal Hosts (the Pi's
#: LAN address, 127.0.0.1, ::1) are always accepted — DNS rebinding, the
#: attack this blocks, needs a DOMAIN in the Host header. Combined with
#: the same-origin POST check this closes the holes a LAN bind opens:
#: rebinding (attacker domain resolving to the Pi) and cross-site form
#: POSTs (no CORS preflight needed).
_LOCAL_NAMES = frozenset({"localhost"})


def _host_allowed(host: str | None, extra: str | None) -> bool:
    if not host:
        return False
    if host in _LOCAL_NAMES or (extra and host == extra):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _security_middleware(extra_host: str | None):
    # A wildcard bind is not a hostname — never allow "0.0.0.0" literally.
    extra = None if extra_host in (None, "", "0.0.0.0", "::") else extra_host

    @web.middleware
    async def middleware(request: web.Request, handler):
        if not _host_allowed(request.url.host, extra):
            raise web.HTTPForbidden(text="Unknown Host")
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("Origin")
            if origin:
                from yarl import URL

                origin_url = URL(origin)
                if (origin_url.host != request.url.host
                        or origin_url.port != request.url.port):
                    raise web.HTTPForbidden(text="Cross-origin POST refused")
            else:
                fetch_site = request.headers.get("Sec-Fetch-Site")
                if fetch_site not in (None, "same-origin", "none"):
                    raise web.HTTPForbidden(text="Cross-site POST refused")
        return await handler(request)

    return middleware


def build_app(bot, *, password: str | None = None) -> web.Application:
    """The console app. With a ``password`` every page requires a login
    session; without one (unit tests) the app is open and safety rests on
    the caller binding to loopback."""
    from .auth import (
        SessionStore,
        auth_middleware_factory,
        login_page,
        post_login,
        post_logout,
    )

    web_cfg = getattr(bot.cfg, "web", None)
    middlewares = [_security_middleware(getattr(web_cfg, "host", None))]
    store = SessionStore()
    if password:
        middlewares.append(auth_middleware_factory(password, store))
    app = web.Application(middlewares=middlewares)
    app["bot"] = bot
    app["web_password"] = password
    app["web_sessions"] = store
    if password:
        app.add_routes([
            web.get("/login", login_page),
            web.post("/login", post_login),
        ])
    app.add_routes([
        web.post("/logout", post_logout),
        web.get("/", handlers.index),
        web.get("/health", handlers.health),
        web.get("/members", handlers.members),
        web.get("/members/{mc_id:\\d+}", handlers.member_detail),
        web.post("/members/{mc_id:\\d+}/profile", handlers.post_profile),
        web.post("/members/{mc_id:\\d+}/sanctions", handlers.post_sanction),
        web.post("/members/{mc_id:\\d+}/note", handlers.post_note),
        web.post("/sanctions/{sanction_id:\\d+}/revoke",
                 handlers.post_sanction_revoke),
        web.get("/settings", handlers.settings_page),
        web.post("/settings", handlers.post_settings),
        web.get("/images/infographic.png", handlers.infographic_png),
        web.get("/images/fleet.png", handlers.fleet_png),
    ])
    _register_domain_modules(app)
    return app


def _register_domain_modules(app: web.Application) -> None:
    """Auto-register every ``handlers_<domain>`` module in this package:
    its ``ROUTES`` list joins the app, its optional ``NAV_ENTRY``
    ``(path, label)`` joins the shared nav. One broken module is skipped
    (and logged) rather than taking the whole console down — its own
    tests catch the breakage."""
    import importlib
    import pkgutil

    from . import __path__ as pkg_path
    from .html import NAV

    nav = list(NAV)
    for module_info in sorted(pkgutil.iter_modules(pkg_path),
                              key=lambda m: m.name):
        if not module_info.name.startswith("handlers_"):
            continue
        try:
            module = importlib.import_module(
                f".{module_info.name}", package=__package__
            )
        except Exception:  # noqa: BLE001
            log.warning("web console: module %s failed to load",
                        module_info.name, exc_info=True)
            continue
        app.add_routes(getattr(module, "ROUTES", []))
        entry = getattr(module, "NAV_ENTRY", None)
        if entry and tuple(entry) not in nav:
            nav.append(tuple(entry))
    # html.page() reads NAV at render time; settings stays last.
    nav.sort(key=lambda item: item[0] == "/settings")
    NAV[:] = nav


def lan_ip() -> str | None:
    """Best-effort LAN address of this machine (no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET; UDP connect only
            return probe.getsockname()[0]
    except OSError:
        return None


async def resolve_password(db) -> str:
    """The operator password: WEB_PASSWORD from the environment wins;
    otherwise a generated one, persisted so it survives restarts."""
    import secrets

    from ..db.repos import StateRepo

    from_env = (os.environ.get("WEB_PASSWORD") or "").strip()
    if from_env:
        return from_env
    state = StateRepo(db)
    stored = await state.get(PASSWORD_STATE_KEY)
    if stored:
        return stored
    generated = secrets.token_urlsafe(9)
    await state.set(PASSWORD_STATE_KEY, generated)
    log.info("web console: generated an operator password "
             "(see `!fra web` in Discord)")
    return generated


class WebConsole:
    """Lifecycle wrapper the bot owns: start on setup, stop on close."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        cfg = self.bot.cfg.web
        password = await resolve_password(self.bot.db)
        self._runner = web.AppRunner(
            build_app(self.bot, password=password), access_log=None
        )
        await self._runner.setup()
        site = web.TCPSite(self._runner, cfg.host, cfg.port)
        await site.start()
        address = lan_ip() or "127.0.0.1"
        log.info("web console: http://%s:%d/ (bound to %s; password via "
                 "`!fra web`)", address, cfg.port, cfg.host)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
