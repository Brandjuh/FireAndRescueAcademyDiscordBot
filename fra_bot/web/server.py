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
    return app


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
