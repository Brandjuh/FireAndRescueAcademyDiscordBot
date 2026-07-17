"""Web console: the alliance log feed (read-only).

Renders the ``alliance_logs`` table — the same rows the Discord
publisher announces from — so the console can never drift from what the
bot posted. Titles, tidied descriptions and member links mirror the
embed builder in :mod:`fra_bot.cogs.notifications` (via
``ACTION_DISPLAY`` / ``format_log_description``), so the web feed reads
like the Discord feed. Strictly read-only: log rows are scraped game
facts, there is nothing safe to mutate here.
"""

from __future__ import annotations

import math
from urllib.parse import urlencode

from aiohttp import web

from ..cogs.display import (
    ACTION_DISPLAY,
    FALLBACK_DISPLAY,
    format_log_description,
)
from ..core.log_routes import UNKNOWN, known_action_keys
from .handlers import _bot, _flash
from .html import esc, page

PAGE_SIZE = 100

NAV_ENTRY = ("/logs", "Logs")


def _action_label(key: str) -> str:
    display = ACTION_DISPLAY.get(key)
    if display is not None:
        return display[0]
    if key == UNKNOWN:
        return "Unclassified log line"
    return key.replace("_", " ")


def _member_cell(name, mc_id) -> str:
    if not name:
        return "<span class='muted'>—</span>"
    if mc_id:
        return f"<a href='/members/{int(mc_id)}'>{esc(name)}</a>"
    return esc(name)


def _feed_href(query: str, action: str, page_num: int = 1) -> str:
    params: dict[str, str | int] = {}
    if query:
        params["q"] = query
    if action:
        params["action"] = action
    if page_num > 1:
        params["page"] = page_num
    return "/logs" + ("?" + urlencode(params) if params else "")


def _row_html(row) -> str:
    title, _, emoji = ACTION_DISPLAY.get(row["action_key"], FALLBACK_DISPLAY)
    if row["event_at"]:
        when = esc(str(row["event_at"])[:16].replace("T", " "))
    else:
        # Unparseable game timestamp: show the raw text, like the embed does.
        when = f"<span class='muted'>{esc(row['raw_timestamp'])}</span>"

    affected = "<span class='muted'>—</span>"
    if row["affected_name"] and row["affected_name"] != row["executed_name"]:
        # Only users have /members pages; a building/mission/vehicle id would
        # point at the wrong member ('' means user, same as affected_url()).
        if row["affected_mc_id"] and (row["affected_type"] or "user") == "user":
            affected = _member_cell(row["affected_name"], row["affected_mc_id"])
        else:
            affected = esc(row["affected_name"])

    parts = []
    detail = format_log_description(row["action_key"], row["description"] or "")
    if detail:
        parts.append(esc(detail))
    if row["contribution_amount"]:
        parts.append(f"<b>{row['contribution_amount']:+,}</b> credits")
    detail_html = " · ".join(parts) or "<span class='muted'>—</span>"

    return (
        "<tr>"
        f"<td>{when}</td>"
        f"<td>{esc(emoji)} {esc(title)}</td>"
        f"<td>{_member_cell(row['executed_name'], row['executed_mc_id'])}</td>"
        f"<td>{affected}</td>"
        f"<td>{detail_html}</td>"
        "</tr>"
    )


async def logs_page(request: web.Request) -> web.Response:
    bot = _bot(request)
    query = (request.query.get("q") or "").strip()
    action = (request.query.get("action") or "").strip()
    try:
        page_num = max(1, int(request.query.get("page", "1")))
    except ValueError:
        page_num = 1

    where, params = [], []
    if action:
        where.append("action_key = ?")
        params.append(action)
    if query:
        where.append(
            "(description LIKE ? OR executed_name LIKE ? "
            "OR affected_name LIKE ?)"
        )
        needle = f"%{query}%"
        params += [needle, needle, needle]
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    # Per-action counts over the WHOLE filtered window (not just this page);
    # their sum doubles as the pagination total.
    async with bot.db.conn.execute(
        "SELECT action_key, COUNT(*) AS n FROM alliance_logs"
        + where_sql + " GROUP BY action_key ORDER BY n DESC, action_key",
        params,
    ) as cur:
        counts = [(row["action_key"], row["n"]) for row in await cur.fetchall()]
    total = sum(n for _, n in counts)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page_num = min(page_num, pages)

    async with bot.db.conn.execute(
        "SELECT * FROM alliance_logs" + where_sql
        # Same event clock the publisher drains by, inverted to newest-first.
        + " ORDER BY COALESCE(event_at, scraped_at) DESC, id DESC"
        " LIMIT ? OFFSET ?",
        (*params, PAGE_SIZE, (page_num - 1) * PAGE_SIZE),
    ) as cur:
        rows = await cur.fetchall()

    keys = sorted(known_action_keys(), key=_action_label)
    if action and action not in known_action_keys():
        # A key stored by the scraper but not (yet) in the display map —
        # keep it selectable so a summary-chip link round-trips.
        keys.append(action)
    options = ["<option value=''>All types</option>"] + [
        f"<option value='{esc(key)}'"
        + (" selected" if key == action else "")
        + f">{esc(_action_label(key))}</option>"
        for key in keys
    ]

    chips = " ".join(
        f"<a class='badge dim' href='{esc(_feed_href(query, key))}'>"
        f"{esc(_action_label(key))} × {n}</a>"
        for key, n in counts
    ) or "<span class='muted'>No log entries match this filter.</span>"

    lines = "".join(_row_html(row) for row in rows) or (
        "<tr><td colspan='5' class='muted'>No log entries match.</td></tr>"
    )

    noun = "entry" if total == 1 else "entries"
    pager = []
    if page_num > 1:
        pager.append(
            f"<a href='{esc(_feed_href(query, action, page_num - 1))}'>"
            "← Newer</a>"
        )
    pager.append(f"Page {page_num} of {pages} · {total:,} {noun}")
    if page_num < pages:
        pager.append(
            f"<a href='{esc(_feed_href(query, action, page_num + 1))}'>"
            "Older →</a>"
        )

    body = (
        "<form class='searchbar' method='get'>"
        "<input name='q' placeholder='Search description or names' "
        f"value='{esc(query)}'>"
        "<select name='action' style='max-width:260px'>"
        + "".join(options) + "</select>"
        "<button>Filter</button></form>"
        f"<div class='panel'><h2>Counts for this filter</h2><p>{chips}</p>"
        "</div>"
        "<div class='panel'><table><tr><th>When</th><th>Action</th>"
        f"<th>By</th><th>Affected</th><th>Detail</th></tr>{lines}</table>"
        f"<p class='muted'>{' · '.join(pager)}</p></div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Alliance logs", body, active="/logs", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


ROUTES = [web.get("/logs", logs_page)]
