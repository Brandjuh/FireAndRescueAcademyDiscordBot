"""New-member welcome: post a one-time greeting in the game's alliance
chat when someone joins the alliance.

The join is detected from the roster diff (a ``member_events`` row of
type ``joined``), so it fires for EVERY route in — auto-accept, an admin
accepting in-game, or the Discord accept button. Each member is greeted
exactly once (a ``welcomed_at`` marker on the event), the greeting posts
through the chat bridge's paced sender with echo memory (so it never
bounces back into Discord), and everything honours the global
``dry_run`` switch.

Always registered on the scheduler; the ``automation.welcome.enabled``
switch is read live, so the job is a cheap no-op while off.
"""

from __future__ import annotations

import logging

from ..config import Config
from ..db.database import Database
from ..db.repos import MembersRepo
from ..mc.errors import MissionChiefError
from .chat_sync import ChatSyncService

log = logging.getLogger(__name__)

#: A member joined more than this long ago is not greeted retroactively —
#: guards against a first-enable or a long outage dumping a burst of late
#: welcomes into the chat. (Fresh joins are welcomed within a poll or two.)
MAX_WELCOME_AGE_HOURS = 48
#: At most this many welcomes per tick, so a rush of joins paces out.
MAX_PER_TICK = 3


class WelcomeService:
    def __init__(self, cfg: Config, db: Database, chat: ChatSyncService) -> None:
        self.cfg = cfg
        self.chat = chat
        self.members = MembersRepo(db)

    @property
    def enabled(self) -> bool:
        return self.cfg.automation.welcome.enabled

    def _render(self, name: str) -> str:
        template = self.cfg.automation.welcome.message or ""
        try:
            return template.format(name=name)
        except (KeyError, IndexError, ValueError):
            # A malformed template must never wedge the welcome — fall back
            # to a plain mention + the raw template.
            return f"@{name} {template}".strip()

    async def run(self) -> int:
        """Greet freshly joined members; returns how many were welcomed."""
        if not self.enabled:
            return 0
        import datetime as dt

        pending = await self.members.pending_welcomes(limit=MAX_PER_TICK * 3)
        if not pending:
            return 0
        now = dt.datetime.now(dt.timezone.utc)
        welcomed = 0
        for event in pending:
            if welcomed >= MAX_PER_TICK:
                break
            name = event["name"] or ""
            age = self._age_hours(event["occurred_at"], now)
            if age is not None and age > MAX_WELCOME_AGE_HOURS:
                # Too old to greet, but still mark it so it doesn't linger.
                await self.members.mark_welcomed(event["id"])
                log.info("welcome: skipped stale join for %s (%.0fh old)",
                         name, age)
                continue
            if self.cfg.automation.dry_run:
                await self.members.mark_welcomed(event["id"])
                log.info("welcome: [dry-run] would greet %s", name)
                welcomed += 1
                continue
            try:
                await self.chat.post_message(self._render(name))
            except MissionChiefError as exc:
                # Leave welcomed_at NULL so the next tick retries — a
                # transient chat-post failure must not skip the greeting.
                log.warning("welcome: chat post for %s failed (%s); will retry",
                            name, exc)
                break
            await self.members.mark_welcomed(event["id"])
            welcomed += 1
            log.info("welcome: greeted new member %s in the game chat", name)
        return welcomed

    @staticmethod
    def _age_hours(raw: str | None, now) -> float | None:
        if not raw:
            return None
        import datetime as dt

        try:
            then = dt.datetime.fromisoformat(raw)
        except ValueError:
            return None
        if then.tzinfo is None:
            then = then.replace(tzinfo=dt.timezone.utc)
        return (now - then).total_seconds() / 3600.0
