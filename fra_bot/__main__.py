"""Entrypoint: ``python -m fra_bot [config.yaml]``."""

from __future__ import annotations

import asyncio
import logging
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
