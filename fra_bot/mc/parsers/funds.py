"""Parse the total alliance funds figure from /verband/kasse.

Shared with the treasury scraper's parser, kept here as a thin wrapper
so the building service has an explicit dependency.
"""

from __future__ import annotations

from .treasury import parse_total_funds

__all__ = ["parse_total_funds"]
