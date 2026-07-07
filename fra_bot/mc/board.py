"""Alliance board operations: read threads, reply, delete posts.

All generated replies start with the ``[FRA]`` marker so the pollers
can recognize (and never re-process) the bot's own posts.
"""

from __future__ import annotations

import logging

from .client import MissionChiefClient
from .errors import ParseError
from .parsers.board import BoardThreadPage, parse_board_thread_page

log = logging.getLogger(__name__)

REPLY_MARKER = "[FRA]"


class BoardClient:
    def __init__(self, client: MissionChiefClient) -> None:
        self._client = client

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
        page = await self.fetch_latest_page(thread_id)
        if page.reply_token is None:
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
            log.warning("Board reply to thread %s failed with HTTP %s", thread_id, status)
            return False
        return True
