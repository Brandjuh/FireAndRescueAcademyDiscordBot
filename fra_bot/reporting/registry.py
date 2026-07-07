"""Report registry: modules register named reports; the Discord layer
renders whatever is registered.

A report is a pure(ish) async builder that takes a resolved
:class:`~fra_bot.reporting.period.Period` and returns a structured
:class:`ReportResult`. Rendering to a Discord embed is done elsewhere,
so reports stay testable and output-agnostic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .period import Period


@dataclass
class ReportField:
    name: str
    value: str
    inline: bool = False


@dataclass
class ReportResult:
    title: str
    description: str = ""
    fields: list[ReportField] = field(default_factory=list)
    # 0 = let the renderer pick a default colour.
    colour: int = 0

    def add(self, name: str, value: str, *, inline: bool = False) -> None:
        self.fields.append(ReportField(name, value, inline))


Builder = Callable[[Period], Awaitable[ReportResult]]


@dataclass
class Report:
    name: str            # slug, e.g. "members"
    description: str
    builder: Builder
    # Periods this report understands; the first is its default.
    periods: tuple[str, ...] = ("today", "week", "month")

    @property
    def default_period(self) -> str:
        return self.periods[0]


class ReportRegistry:
    def __init__(self) -> None:
        self._reports: dict[str, Report] = {}

    def register(self, report: Report) -> None:
        if report.name in self._reports:
            raise ValueError(f"Report '{report.name}' already registered")
        self._reports[report.name] = report

    def get(self, name: str) -> Report | None:
        return self._reports.get(name.lower())

    def all(self) -> list[Report]:
        return sorted(self._reports.values(), key=lambda r: r.name)

    def names(self) -> list[str]:
        return sorted(self._reports)
