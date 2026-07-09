"""Alliance board operations: read threads, reply, delete posts.

All generated replies start with the ``[FRA]`` marker so the pollers
can recognize (and never re-process) the bot's own posts.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from bs4 import BeautifulSoup

from .client import MissionChiefClient
from .errors import MissionChiefError, ParseError
from .parsers.board import BoardThreadPage, parse_board_thread_page

log = logging.getLogger(__name__)

REPLY_MARKER = "[FRA]"


def _normalize_marker_text(text: str) -> str:
    """Reduce text to plain ASCII with single spaces, for marker matching.

    The forum re-renders posts (emoji can become images and disappear from
    the text, whitespace gets reflowed), so an exact prefix match on what we
    POSTED will not reliably match what we READ BACK. Both sides are
    normalized before comparing."""
    text = re.sub(r"[^\x20-\x7E\n]", "", text or "")    # drop emoji/unicode
    return re.sub(r"[ \t]+", " ", text).strip()


def _marker_in(content: str, marker: str) -> bool:
    """True when the normalized marker occurs in the normalized content."""
    needle = _normalize_marker_text(marker)
    return bool(needle) and needle in _normalize_marker_text(content)


def guide_now() -> float:
    """Current UTC epoch seconds — the guide's refresh clock."""
    return dt.datetime.now(dt.timezone.utc).timestamp()


def guide_updated_line(now_epoch: float | None = None) -> str:
    """A human 'last updated' line for the bottom of a guide post."""
    moment = (
        dt.datetime.fromtimestamp(now_epoch, dt.timezone.utc)
        if now_epoch is not None
        else dt.datetime.now(dt.timezone.utc)
    )
    return f"Last updated: {moment.strftime('%Y-%m-%d %H:%M UTC')}"


class BoardClient:
    def __init__(self, client: MissionChiefClient) -> None:
        self._client = client
        #: Human-readable reason for the most recent post/edit failure —
        #: surfaced by `!fra guides` so a silent False never hides WHY.
        self.last_error: str | None = None

    # Safety cap on how many pages we'll walk back in one poll.
    MAX_PAGES_PER_POLL = 5

    async def fetch_latest_page(self, thread_id: int) -> BoardThreadPage:
        """Fetch the newest page of a thread (reply form + last_page)."""
        base_path = f"/alliance_threads/{thread_id}"
        html = await self._client.fetch_page(base_path)
        page = parse_board_thread_page(html)
        if not page.posts and page.reply_action is None:
            raise ParseError(
                f"Thread {thread_id} page contains no posts and no reply "
                "form — layout change or no access"
            )
        if page.last_page > 1:
            html = await self._client.fetch_page(f"{base_path}?page={page.last_page}")
            page = parse_board_thread_page(html)
        return page

    async def fetch_new_posts(
        self, thread_id: int, last_seen_post_id: int | None
    ) -> tuple[BoardThreadPage, list]:
        """Return (newest_page, posts newer than last_seen across pages).

        Walks backward from the last page so a burst that spilled onto a
        new page can't leave posts stranded on an earlier one. The newest
        page is returned separately because it carries the reply form and
        our current user id.
        """
        base_path = f"/alliance_threads/{thread_id}"
        newest = await self.fetch_latest_page(thread_id)
        last_page = newest.last_page

        collected: dict[int, object] = {p.post_id: p for p in newest.posts}
        # If the oldest post on the last page is still newer than what we
        # have, earlier pages may hold unseen posts too — walk back.
        if last_seen_post_id is not None and newest.posts:
            page_number = last_page - 1
            walked = 1
            while page_number >= 1 and walked < self.MAX_PAGES_PER_POLL:
                oldest_on_seen = min(p.post_id for p in collected.values())
                if oldest_on_seen <= last_seen_post_id:
                    break  # we've reached posts we already know
                html = await self._client.fetch_page(f"{base_path}?page={page_number}")
                prev = parse_board_thread_page(html)
                if not prev.posts:
                    break
                for post in prev.posts:
                    collected.setdefault(post.post_id, post)
                page_number -= 1
                walked += 1

        if last_seen_post_id is None:
            fresh = list(collected.values())
        else:
            fresh = [p for p in collected.values() if p.post_id > last_seen_post_id]
        fresh.sort(key=lambda p: p.post_id)
        return newest, fresh

    async def post_reply(self, thread_id: int, content: str) -> bool:
        """Post a reply to a thread. Content gets the [FRA] marker."""
        self.last_error = None
        page = await self.fetch_latest_page(thread_id)
        if page.reply_token is None:
            self.last_error = (
                "no reply form/token on the thread — can the bot's "
                "MissionChief account post there?"
            )
            log.warning("No reply token on thread %s; cannot reply", thread_id)
            return False
        action = page.reply_action or f"/alliance_posts?alliance_thread_id={thread_id}"
        body = content if content.startswith(REPLY_MARKER) else f"{REPLY_MARKER} {content}"
        status, _, _ = await self._client.post_form(
            action,
            {
                "utf8": "✓",
                "authenticity_token": page.reply_token,
                "alliance_post[content]": body[:4000],
                "commit": "Save",
            },
            referer=self._client.url(f"/alliance_threads/{thread_id}"),
        )
        if status >= 400:
            self.last_error = f"the forum rejected the post with HTTP {status}"
            log.warning("Board reply to thread %s failed with HTTP %s", thread_id, status)
            return False
        return True

    async def find_bot_post(
        self, thread_id: int, marker: str, *, max_pages: int | None = None
    ) -> int | None:
        """Newest post authored by us whose content carries ``marker``.

        Used to locate an existing guide post so it can be EDITED instead of
        duplicated. Matching is a normalized SUBSTRING check (like the old
        bot), not a prefix check: the forum may re-render emoji/whitespace,
        so both sides are reduced to plain ASCII before comparing. Walks back
        from the last page up to ``max_pages``."""
        base_path = f"/alliance_threads/{thread_id}"
        newest = await self.fetch_latest_page(thread_id)
        uid = newest.current_user_id
        found: int | None = None

        def _scan(page: BoardThreadPage) -> None:
            nonlocal found
            for post in page.posts:
                if uid is not None and post.author_mc_id != uid:
                    continue
                if not _marker_in(post.content, marker):
                    continue
                if found is None or post.post_id > found:
                    found = post.post_id

        _scan(newest)
        limit = max_pages or self.MAX_PAGES_PER_POLL
        page_number = newest.last_page - 1
        walked = 1
        while found is None and page_number >= 1 and walked < limit:
            html = await self._client.fetch_page(f"{base_path}?page={page_number}")
            _scan(parse_board_thread_page(html))
            page_number -= 1
            walked += 1
        return found

    async def create_post_get_id(self, thread_id: int, content: str) -> int | None:
        """Post a reply and return its new post id (found by matching the
        first line back on the thread), or None (see ``last_error``)."""
        if not await self.post_reply(thread_id, content):
            return None
        body = content if content.startswith(REPLY_MARKER) else f"{REPLY_MARKER} {content}"
        marker = body.splitlines()[0][:60] if body else REPLY_MARKER
        found = await self.find_bot_post(thread_id, marker)
        if found is None:
            self.last_error = (
                "the post was accepted but I can't find it back on the "
                "thread — did the forum drop or transform it?"
            )
        return found

    async def edit_post(self, post_id: int, content: str) -> bool:
        """Edit one of our posts in place (Rails ``_method=patch``).

        Returns False (rather than raising) when the post can't be edited —
        e.g. it was deleted, so its edit page 404s. Callers treat False as
        "stale id: forget it and find/create the post again"."""
        self.last_error = None
        try:
            html = await self._client.fetch_page(f"/alliance_posts/{post_id}/edit")
        except MissionChiefError as exc:
            self.last_error = f"edit page for post {post_id} unavailable ({exc})"
            log.warning("Edit page for post %s unavailable (%s)", post_id, exc)
            return False
        soup = BeautifulSoup(html, "lxml")
        form = None
        for candidate in soup.find_all("form"):
            action = str(candidate.get("action") or "")
            if "/alliance_posts" in action or candidate.find(
                "textarea", attrs={"name": "alliance_post[content]"}
            ):
                form = candidate
                break
        if form is None:
            log.warning("Edit form for post %s not found", post_id)
            return False
        token_el = form.find("input", attrs={"name": "authenticity_token"})
        action = form.get("action") or f"/alliance_posts/{post_id}"
        body = content if content.startswith(REPLY_MARKER) else f"{REPLY_MARKER} {content}"
        data = {
            "utf8": "✓",
            "_method": "patch",
            "alliance_post[content]": body[:4000],
            "commit": "Save",
        }
        if token_el is not None and token_el.get("value"):
            data["authenticity_token"] = token_el.get("value")
        status, _, _ = await self._client.post_form(
            action, data,
            referer=self._client.url(f"/alliance_posts/{post_id}/edit"),
        )
        if status >= 400:
            log.warning("Editing post %s failed with HTTP %s", post_id, status)
            return False
        return True

    async def delete_post(self, thread_id: int, post_id: int) -> bool:
        """Delete one of our posts (Rails ``_method=delete``)."""
        page = await self.fetch_latest_page(thread_id)
        data = {"utf8": "✓", "_method": "delete", "commit": "Delete"}
        if page.reply_token:
            data["authenticity_token"] = page.reply_token
        status, _, _ = await self._client.post_form(
            f"/alliance_posts/{post_id}", data,
            referer=self._client.url(f"/alliance_threads/{thread_id}"),
        )
        if status in (404, 410):
            return True  # already gone — the desired end state
        if status >= 400:
            log.warning("Deleting post %s failed with HTTP %s", post_id, status)
            return False
        return True


async def ensure_guide_post(
    board: BoardClient,
    state,
    thread_id: int,
    *,
    id_key: str,
    hash_key: str,
    refreshed_key: str,
    marker: str,
    desired,
    signature: str,
    now_epoch: float,
    min_refresh_seconds: float = 3600.0,
) -> None:
    """Keep exactly one guide post on a board: find our existing one and EDIT
    it in place, else create it — never duplicate.

    ``signature`` is a hash of the guide's *stable* text (its instructions),
    while ``desired`` is the full post including volatile bits like a
    "last updated" timestamp or live availability. The board is re-written when
    the stable text changes, or at most once per ``min_refresh_seconds`` to
    freshen the volatile bits — so a timestamp never triggers an edit on every
    poll. ``state`` (a :class:`StateRepo`) remembers the post id, the stable
    signature and when it was last refreshed.

    ``desired`` may be a plain string, or an (async) zero-arg callable that is
    invoked ONLY once a write is actually going to happen — so building
    expensive live content (e.g. classroom availability, which costs many
    rate-limited page fetches) is skipped entirely on the throttled polls.

    This is an informational forum post (not a game action), so callers
    maintain it even in dry-run. Board errors propagate as
    :class:`MissionChiefError` for the caller to catch.
    """
    resolved: str | None = desired if isinstance(desired, str) else None

    async def _content() -> str:
        nonlocal resolved
        if resolved is None:
            value = desired()
            if hasattr(value, "__await__"):
                value = await value
            resolved = str(value)
        return resolved

    async def _record(post_id: int) -> None:
        await state.set(id_key, str(post_id))
        await state.set(hash_key, signature)
        await state.set(refreshed_key, repr(now_epoch))

    stored = await state.get(id_key)
    if stored:
        same_signature = await state.get(hash_key) == signature
        raw_refreshed = await state.get(refreshed_key)
        try:
            last_refreshed = float(raw_refreshed) if raw_refreshed else 0.0
        except (TypeError, ValueError):
            last_refreshed = 0.0
        if same_signature and (now_epoch - last_refreshed) < min_refresh_seconds:
            return  # unchanged and refreshed recently — leave it be
        if await board.edit_post(int(stored), await _content()):
            await _record(int(stored))
            return
        await state.delete(id_key)  # stale id — fall through and re-find
    existing = await board.find_bot_post(thread_id, marker)
    if existing is not None:
        if await board.edit_post(existing, await _content()):
            await _record(existing)
        return
    new_id = await board.create_post_get_id(thread_id, await _content())
    if new_id is not None:
        await _record(new_id)
