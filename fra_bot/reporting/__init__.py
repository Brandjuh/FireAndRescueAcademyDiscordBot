"""Reporting framework.

Every module can register named, read-only reports that summarize its
data over a time period. Reports never touch MissionChief — they query
the local database only — so they are safe to run at any time, including
while the bot is otherwise in dry-run.
"""

from .registry import Report, ReportField, ReportRegistry, ReportResult
from .period import Period, resolve_period

__all__ = [
    "Report",
    "ReportField",
    "ReportRegistry",
    "ReportResult",
    "Period",
    "resolve_period",
]
