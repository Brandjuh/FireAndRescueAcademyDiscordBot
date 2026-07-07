"""Parsers for the alliance treasury page (/verband/kasse).

The page contains, depending on query parameters:

* the total alliance funds figure (header area),
* an income top list (2 columns: NAME | CREDITS) — daily by default,
  monthly with ``?type=monthly``,
* the paginated expense ledger (4 columns: CREDITS | NAME | DESCRIPTION
  | DATE) via ``?page=N``.

Tables are recognized by their header shape, never by position on the
page. Identical-looking expense rows are REAL distinct events (several
identical payouts can land in the same minute) — the parser keeps every
row and computes a content signature; dedup is the sync layer's job.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from .common import extract_user_id, normalize_mc_timestamp, parse_int, signature_of

_FUNDS_PATTERNS = [
    re.compile(r"Alliance\s+Funds\s*:?\s*([\d.,\s]+)\s+Credits", re.IGNORECASE),
    re.compile(r"Alliance\s+Treasury\s*:?\s*([\d.,\s]+)\s+Credits", re.IGNORECASE),
    re.compile(r"([\d.,\s]+)\s+Credits", re.IGNORECASE),
]
_FUNDS_MARKERS = ("alliance funds", "alliance fund", "alliance treasury")


def parse_total_funds(html: str) -> int | None:
    """Total alliance funds from the kasse page header."""
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"\s+", " ", text)
    lowered = text.casefold()
    for marker in _FUNDS_MARKERS:
        pos = lowered.find(marker)
        if pos >= 0:
            window = text[max(0, pos - 200) : pos + 600]
            for pattern in _FUNDS_PATTERNS:
                match = pattern.search(window)
                if match:
                    return parse_int(match.group(1))
    return None


def parse_income_table(html: str) -> list[dict[str, Any]]:
    """Income top list rows (NAME | CREDITS), in display order."""
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers:
            continue
        # Skip the expense ledger (credits|name|description|date).
        if "description" in headers and "date" in headers:
            continue
        name_idx = credits_idx = None
        for idx, header in enumerate(headers):
            if name_idx is None and any(k in header for k in ("name", "user", "member")):
                name_idx = idx
            elif credits_idx is None and any(
                k in header for k in ("credit", "amount", "contribution")
            ):
                credits_idx = idx
        if name_idx is None or credits_idx is None:
            continue

        body = table.find("tbody") or table
        entries: list[dict[str, Any]] = []
        for tr in body.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) <= max(name_idx, credits_idx):
                continue
            name_cell = cells[name_idx]
            link = name_cell.find("a", href=True)
            username = (link or name_cell).get_text(strip=True)
            mc_user_id = extract_user_id(link["href"]) if link else None
            amount = parse_int(cells[credits_idx].get_text(strip=True))
            if username and amount is not None and amount > 0:
                entries.append(
                    {"username": username, "mc_user_id": mc_user_id, "amount": amount}
                )
        if entries:
            return entries
    return []


@dataclass
class ExpensesPage:
    rows: list[dict[str, Any]]  # newest first, as displayed
    has_table: bool


_EXPENSE_HEADERS = ["credits", "name", "description", "date"]


def parse_expenses_page(html: str) -> ExpensesPage:
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if headers[:4] != _EXPENSE_HEADERS:
            continue

        body = table.find("tbody") or table
        rows: list[dict[str, Any]] = []
        for tr in body.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            amount = parse_int(cells[0].get_text(strip=True))
            name_cell = cells[1]
            link = name_cell.find("a", href=True)
            username = (link or name_cell).get_text(strip=True)
            description = cells[2].get_text(" ", strip=True)
            raw_date = cells[3].get_text(strip=True)
            if amount is None or not username:
                continue
            rows.append(
                {
                    "amount": amount,
                    "username": username,
                    "description": description,
                    "raw_date": raw_date,
                    "event_at": normalize_mc_timestamp(raw_date),
                    "signature": signature_of(raw_date, username, amount, description),
                }
            )
        return ExpensesPage(rows=rows, has_table=True)
    return ExpensesPage(rows=[], has_table=False)


_LAST_PAGE_RE = re.compile(r"[?&]page=(\d+)")


def parse_last_page_number(html: str) -> int | None:
    """Highest page number linked in the pagination widget."""
    soup = BeautifulSoup(html, "lxml")
    best: int | None = None
    pagination = soup.find(class_="pagination") or soup
    for link in pagination.find_all("a", href=True):
        match = _LAST_PAGE_RE.search(link["href"])
        if match:
            page = int(match.group(1))
            if best is None or page > best:
                best = page
    return best
