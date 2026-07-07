"""Parser for alliance board threads (/alliance_threads/<id>?page=N).

Structure of a thread page (server-rendered Rails HTML):

* each post lives in ``<div id="post-on-page-N">``,
* the post id comes from its ``/alliance_posts/<id>`` permalink,
* the author from the ``/profile/<id>`` link,
* the timestamp from the first ``<span title="...">``,
* the body from the ``col-md-11`` column (with <br> as newlines),
* the reply form is ``form#new_alliance_post`` (action + CSRF token),
* our own MC user id appears in inline JS as ``user_id = N``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

_POST_ID_RE = re.compile(r"/alliance_posts/(\d+)")
_PROFILE_RE = re.compile(r"/profile/(\d+)")
_PAGE_RE = re.compile(r"[?&]page=(\d+)")
_USER_ID_RE = re.compile(r"user_id\s*=\s*(\d+)")


@dataclass
class BoardPost:
    post_id: int
    author_name: str | None
    author_mc_id: int | None
    raw_timestamp: str | None
    content: str


@dataclass
class BoardThreadPage:
    posts: list[BoardPost] = field(default_factory=list)
    last_page: int = 1
    current_user_id: int | None = None
    reply_action: str | None = None
    reply_token: str | None = None


def _extract_content(post_div) -> str:
    column = post_div.find(
        "div", class_=lambda c: c and "col-md-11" in c.split()
    )
    node = column or post_div
    for br in node.find_all("br"):
        br.replace_with("\n")
    text = node.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def parse_board_thread_page(html: str) -> BoardThreadPage:
    soup = BeautifulSoup(html, "lxml")
    page = BoardThreadPage()

    for div in soup.find_all("div", id=re.compile(r"^post-on-page-")):
        link = div.find("a", href=_POST_ID_RE)
        if link is None:
            continue
        post_id = int(_POST_ID_RE.search(link["href"]).group(1))

        author_name = author_mc_id = None
        profile_link = div.find("a", href=_PROFILE_RE)
        if profile_link is not None:
            author_mc_id = int(_PROFILE_RE.search(profile_link["href"]).group(1))
            author_name = profile_link.get_text(strip=True) or None

        span = div.find("span", title=True)
        raw_timestamp = span["title"] if span else None

        page.posts.append(
            BoardPost(
                post_id=post_id,
                author_name=author_name,
                author_mc_id=author_mc_id,
                raw_timestamp=raw_timestamp,
                content=_extract_content(div),
            )
        )

    # Pagination: highest page number linked anywhere + the active page.
    last = 1
    for link in soup.find_all("a", href=True):
        match = _PAGE_RE.search(link["href"])
        if match:
            last = max(last, int(match.group(1)))
    active = soup.find("li", class_="active")
    if active:
        text = active.get_text(strip=True)
        if text.isdigit():
            last = max(last, int(text))
    page.last_page = last

    # Our own MC user id (from inline JS) — used to skip our own posts.
    for script in soup.find_all("script"):
        match = _USER_ID_RE.search(script.get_text() or "")
        if match:
            page.current_user_id = int(match.group(1))
            break

    form = soup.find("form", id="new_alliance_post")
    if form is not None:
        page.reply_action = form.get("action")
        token = form.find("input", attrs={"name": "authenticity_token"})
        if token is not None:
            page.reply_token = token.get("value")

    return page


def extract_form_fields(html: str, *, action_contains: str) -> dict[str, Any] | None:
    """Generic helper: find the first form whose action contains a
    substring and return {'action', 'fields': {name: value}} including
    hidden inputs (authenticity_token) and select options metadata."""
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if action_contains not in action:
            continue
        fields: dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                fields[name] = inp.get("value", "")
        selects: dict[str, list[tuple[str, str]]] = {}
        for select in form.find_all("select"):
            name = select.get("name")
            if not name:
                continue
            selects[name] = [
                (opt.get("value", ""), opt.get_text(strip=True))
                for opt in select.find_all("option")
            ]
        return {"action": action, "fields": fields, "selects": selects}
    return None
