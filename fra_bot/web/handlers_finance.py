"""Web console: alliance finance — funds, income top lists, expenses.

Entirely read-only. Every figure comes from data the treasury sync
already stores (``treasury_balance``, ``income_snapshots``, ``expenses``)
— nothing here talks to MissionChief. The income panels run the same
registered report builders as ``!fra report income-daily/-monthly`` so
the NY-game-day key selection and its pre-reset fallback live in ONE
place; spend totals reuse ``TreasuryRepo.expense_summary`` with the
shared report periods for the same reason.

The expense ledger may still be mid-backfill (3150+ pages ingested in
resumable chunks); the page says so instead of presenting partial spend
totals as complete.
"""

from __future__ import annotations

import logging
import re

from aiohttp import web

from ..db.repos import StateRepo, TreasuryRepo
from ..reporting.period import resolve_period
from ..services.treasury_sync import (
    STATE_BACKFILL_DONE,
    STATE_BACKFILL_NEXT_PAGE,
)
from .handlers import _bot, _flash
from .html import esc, page, tile

log = logging.getLogger(__name__)

_EXPENSE_LIMIT = 50

# Report descriptions are Discord-markdown ("**bold**", "`code`"); the
# patterns are applied AFTER esc() so no unescaped input can slip in.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")


def _md_html(text: str) -> str:
    out = esc(text)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _CODE_RE.sub(r"<code>\1</code>", out)
    return out.replace("\n", "<br>")


def _income_reports(bot):
    """The registered income report builders — from the live bot's
    registry when it has one, otherwise (tests, partial boots) a fresh
    registry built by the SAME ``register_builtin_reports``."""
    registry = getattr(bot, "reports", None)
    if registry is None or registry.get("income-daily") is None:
        from ..reporting import ReportRegistry
        from ..reporting.reports import register_builtin_reports

        registry = ReportRegistry()
        register_builtin_reports(registry, bot.db)
    return registry.get("income-daily"), registry.get("income-monthly")


def _report_panel(result) -> str:
    return (
        f"<div class='panel'><h2>{esc(result.title)}</h2>"
        f"<p>{_md_html(result.description)}</p></div>"
    )


def _when(row) -> str:
    # event_at is inferred during backfill and can be NULL for ambiguous
    # ledger dates — fall back to the raw in-game date text.
    if row["event_at"]:
        return esc(str(row["event_at"])[:16])
    return esc(row["raw_date"])


async def finance_page(request: web.Request) -> web.Response:
    bot = _bot(request)
    treasury = TreasuryRepo(bot.db)
    state = StateRepo(bot.db)
    query = (request.query.get("q") or "").strip()

    balance = await treasury.latest_balance()
    today = resolve_period("today")
    month = resolve_period("month")
    spent_today = await treasury.expense_summary(today.start_iso, today.end_iso)
    spent_month = await treasury.expense_summary(month.start_iso, month.end_iso)
    total_expenses = await treasury.expense_count()

    daily_report, monthly_report = _income_reports(bot)
    daily = await daily_report.builder(today)
    monthly = await monthly_report.builder(month)

    notes = []
    if balance is not None:
        notes.append(
            f"Funds as of {esc(str(balance['scraped_at'])[:16])} UTC."
        )
    else:
        notes.append("No balance recorded yet — the treasury sync has not run.")
    if await state.get(STATE_BACKFILL_DONE) != "1":
        staged = await treasury.staging_count()
        next_page = await state.get(STATE_BACKFILL_NEXT_PAGE, "1")
        notes.append(
            f"Expense backfill in progress (at page {esc(next_page)}, "
            f"{staged:,} rows staged) — spend totals cover only the part "
            "of the ledger ingested so far."
        )

    where = ""
    params: list = []
    if query:
        where = "WHERE (username LIKE ? OR description LIKE ?) "
        like = f"%{query}%"
        params = [like, like]
    async with bot.db.conn.execute(
        f"SELECT COUNT(*) AS n FROM expenses {where}", params
    ) as cur:
        matched = (await cur.fetchone())["n"]
    async with bot.db.conn.execute(
        "SELECT raw_date, event_at, username, amount, description "
        f"FROM expenses {where}ORDER BY id DESC LIMIT ?",
        (*params, _EXPENSE_LIMIT),
    ) as cur:
        rows = await cur.fetchall()

    expense_lines = "".join(
        "<tr>"
        f"<td>{_when(row)}</td>"
        f"<td>{esc(row['username'])}</td>"
        f"<td>{row['amount']:,}</td>"
        f"<td>{esc(row['description'] or '—')}</td>"
        "</tr>"
        for row in rows
    ) or "<tr><td colspan='4' class='muted'>No expenses recorded.</td></tr>"

    spender_lines = "".join(
        f"<li><strong>{esc(name)}</strong> — {spent:,} credits</li>"
        for name, spent in spent_month["top"]
    ) or "<li class='muted'>No spend recorded this month.</li>"

    funds = f"{balance['total_funds']:,}" if balance is not None else "—"
    tiles = (
        tile("Alliance funds", funds)
        + tile("Spent today", f"{spent_today['total']:,}")
        + tile("Spent this month", f"{spent_month['total']:,}")
        + tile("Expenses recorded", f"{total_expenses:,}")
    )
    body = (
        f"<div class='tiles'>{tiles}</div>"
        f"<p class='muted'>{' '.join(notes)}</p>"
        f"<div class='grid2'>{_report_panel(daily)}{_report_panel(monthly)}"
        "</div>"
        "<div class='panel'><h2>Top spenders — this month</h2>"
        f"<ul class='timeline'>{spender_lines}</ul></div>"
        "<form class='searchbar' method='get' action='/finance'>"
        f"<input name='q' placeholder='Member or description' "
        f"value='{esc(query)}'>"
        "<button>Filter</button></form>"
        f"<div class='panel'><h2>Recent expenses (last {_EXPENSE_LIMIT})</h2>"
        "<table><tr><th>When</th><th>Member</th><th>Amount</th>"
        f"<th>Description</th></tr>{expense_lines}</table>"
        f"<p class='muted'>{len(rows)} shown · {matched:,} matching · "
        f"{total_expenses:,} recorded in total.</p></div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Finance", body, active="/finance", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


ROUTES = [
    web.get("/finance", finance_page),
]
NAV_ENTRY = ("/finance", "Finance")
