"""The web console server: an aiohttp app inside the bot process.

Runs on the bot's event loop against the bot's own DB handle and paced
MissionChief client — one process, one database writer. Binds to
localhost by default; there is NO authentication yet, so ``web.host``
must only be widened on a trusted network.
"""

from __future__ import annotations

import logging

from aiohttp import web

from . import handlers

log = logging.getLogger(__name__)


def build_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.add_routes([
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


class WebConsole:
    """Lifecycle wrapper the bot owns: start on setup, stop on close."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        cfg = self.bot.cfg.web
        self._runner = web.AppRunner(build_app(self.bot), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, cfg.host, cfg.port)
        await site.start()
        log.info("web console: http://%s:%d/", cfg.host, cfg.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
