"""Entrypoint: ``python -m fra_bot [config.yaml]``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .bot import FRABot
from .config import ConfigError, load_config
from .log_setup import setup_logging

log = logging.getLogger(__name__)


async def _run() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    setup_logging(cfg.logging)
    log.info("Starting Fire & Rescue Academy bot")

    bot = FRABot(cfg)

    # systemd sends SIGTERM on stop/restart. bot.start() (unlike run())
    # installs no handler, so without this the process is killed without
    # a graceful close (cookies unsaved, DB not cleanly closed).
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        log.info("Shutdown signal received; closing")
        loop.create_task(bot.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # e.g. Windows
            pass

    try:
        await bot.start(cfg.discord.token)
    finally:
        if not bot.is_closed():
            await bot.close()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
