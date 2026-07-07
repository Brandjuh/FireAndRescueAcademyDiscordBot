"""Logging: console + rotating file, safe defaults for a Raspberry Pi."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .config import LoggingConfig

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(cfg: LoggingConfig) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.level, logging.INFO))

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path = Path(cfg.path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Discord.py is chatty on INFO; keep its noise down without hiding warnings.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
