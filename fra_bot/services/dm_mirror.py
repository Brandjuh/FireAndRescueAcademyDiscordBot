"""In-game DM mirror: every MissionChief PM conversation ↔ one forum thread.

Both directions land in the forum:

* **incoming** — the inbox scan picks up conversations flagged "New";
* **outgoing** — a conversation the bot account started (tax warnings,
  requester DMs, or a manual PM from the game UI) shows up in the inbox
  list without a "New" badge; any conversation the mirror doesn't know yet
  is fetched and mirrored too, so those appear within one scan interval.

Replying happens in Discord: a staff message typed in a mirrored thread is
POSTed into the game conversation (see the DmMirrorCog listener, which
calls :meth:`DmMirrorService.reply_from_thread`).

Dedup: the game's conversation page carries an ISO timestamp per message
(``data-message-time``); the newest mirrored timestamp is persisted per
conversation and only strictly newer messages are posted on a rescan. A
reply sent via the forum thread is remembered in-memory as an "echo" so
the next scan does not mirror it back (it is already visible as the staff
member's own Discord message).

The scan itself is read-only on the game side (plus the implicit
mark-as-read of opening a conversation), so it runs in dry-run too;
replies are real actions and honour the global dry_run switch.
"""

from __future__ import annotations

import datetime as dt
import logging

import discord

from ..config import Config
from ..db.database import Database
from ..db.repos import DmMirrorRepo
from ..mc import mailbox
from ..mc.client import MissionChiefClient

log = logging.getLogger(__name__)

# Discord message limit is 2000; keep headroom for the header line.
CHUNK_LIMIT = 1900


def _parse_ts(value: str | None) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _discord_ts(value: str | None) -> str:
    parsed = _parse_ts(value)
    if parsed is None:
        return str(value or "")
    return f"<t:{int(parsed.timestamp())}:f>"


def _norm_body(body: str) -> str:
    return " ".join(str(body or "").split()).casefold()


def split_chunks(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Discord-safe chunks, splitting at paragraph > line > space."""
    remaining = str(text or "").strip()
    if not remaining:
        return ["(empty message)"]
    chunks = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        split_at = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if split_at < max(1, limit // 2):
            split_at = limit
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()
    return chunks


def thread_title(username: str, subject: str, conversation_id: str) -> str:
    """``"{user} · {subject} · #{id}"`` within Discord's 100-char limit;
    the #id suffix is preserved when the subject is truncated."""
    suffix = f" · #{conversation_id}"
    head = f"{str(username or 'Unknown').strip()} · {str(subject or 'No subject').strip()}"
    head = " ".join(head.split())
    room = max(1, 100 - len(suffix))
    if len(head) > room:
        head = head[: room - 1].rstrip() + "…"
    return f"{head}{suffix}"


class DmMirrorService:
    def __init__(
        self, cfg: Config, mc: MissionChiefClient, db: Database, bot
    ) -> None:
        self._cfg = cfg
        self._mc = mc
        self._bot = bot
        self._repo = DmMirrorRepo(db)
        # Bodies we just POSTed into the game from a forum thread: the next
        # scan skips mirroring them back (they are already in the thread as
        # the staff member's message). Memory-only — after a restart such a
        # reply may be mirrored once, which is harmless.
        self._echoes: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Channel plumbing
    # ------------------------------------------------------------------

    def forum(self) -> discord.ForumChannel | None:
        channel_id = self._cfg.discord.channels.dm_mirror
        if not channel_id:
            return None
        channel = self._bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "create_thread"):
            return None
        return channel

    async def _get_thread(self, thread_id: int):
        thread = self._bot.get_channel(thread_id)
        if thread is not None:
            return thread
        try:
            return await self._bot.fetch_channel(thread_id)
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            log.warning("Could not fetch DM-mirror thread %s: %s", thread_id, exc)
            return None

    # ------------------------------------------------------------------
    # Scan (game → Discord)
    # ------------------------------------------------------------------

    async def scan(self) -> dict:
        """One mirror pass over the inbox. Fetches a conversation page when
        the conversation is unknown (catches ones WE started), flagged
        "New", or known-but-threadless; mirrors only messages newer than
        the stored marker."""
        forum = self.forum()
        if forum is None:
            return self._summary(
                error="DM-mirror forum is not configured — set it with "
                      "`!fra set dm_mirror <forum channel id>`."
            )
        rows = await mailbox.fetch_inbox(self._mc)
        threads_created = mirrored = skipped = failed = 0
        for row in rows:
            known = await self._repo.get(row.conversation_id)
            needs_fetch = (
                known is None
                or row.is_new
                or known["thread_id"] is None
            )
            if not needs_fetch:
                skipped += 1
                continue
            try:
                new_thread, posted = await self._mirror_conversation(forum, row, known)
            except discord.HTTPException as exc:
                failed += 1
                log.error(
                    "Mirroring conversation %s failed: %s", row.conversation_id, exc
                )
                continue
            threads_created += 1 if new_thread else 0
            mirrored += posted
        return self._summary(
            conversations=len(rows), threads_created=threads_created,
            mirrored=mirrored, skipped=skipped, failed=failed,
        )

    async def _mirror_conversation(self, forum, row, known) -> tuple[bool, int]:
        """Returns (created_new_thread, messages_posted)."""
        messages = await mailbox.fetch_conversation(self._mc, row.conversation_id)
        # Page order is newest-first; mirror chronologically.
        messages = list(reversed(messages))

        last_seen = _parse_ts(known["last_activity"]) if known is not None else None
        fresh: list[mailbox.ConversationMessage] = []
        for message in messages:
            ts = _parse_ts(message.timestamp)
            if last_seen is not None and (ts is None or ts <= last_seen):
                continue
            fresh.append(message)
        if known is not None and not fresh:
            # Nothing new (e.g. the "New" badge was our own mark-as-read
            # race); just refresh the marker bookkeeping.
            await self._record(row, known["thread_id"], messages)
            return False, 0

        thread = None
        created = False
        if known is not None and known["thread_id"] is not None:
            thread = await self._get_thread(int(known["thread_id"]))
        if thread is None:
            thread = await self._create_thread(forum, row, messages)
            created = True
            # The starter already carries the conversation header; all
            # messages (both directions) follow below.
            fresh = messages

        posted = 0
        echoes = self._echoes.get(row.conversation_id, [])
        for message in fresh:
            body_norm = _norm_body(message.body)
            if body_norm in echoes:
                echoes.remove(body_norm)
                continue
            await self._post_message(thread, row, message)
            posted += 1
        if not echoes:
            self._echoes.pop(row.conversation_id, None)

        await self._record(row, thread.id, messages)
        return created, posted

    async def _create_thread(self, forum, row, messages):
        embed = discord.Embed(
            title="📬 MissionChief conversation",
            colour=discord.Colour.blurple(),
            description=(
                f"**Member:** {discord.utils.escape_markdown(row.sender or '?')}\n"
                f"**Subject:** {discord.utils.escape_markdown(row.subject or '—')}\n"
                f"**Conversation:** #{row.conversation_id}\n\n"
                "Reply here to answer in the game."
            ),
        )
        result = await forum.create_thread(
            name=thread_title(row.sender, row.subject, row.conversation_id),
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
            reason="DM mirror",
        )
        return getattr(result, "thread", result)

    async def _post_message(self, thread, row, message) -> None:
        incoming = _norm_body(message.author) == _norm_body(row.sender)
        arrow = "📥" if incoming else "📤"
        header = f"{arrow} **{discord.utils.escape_markdown(message.author)}**"
        stamp = _discord_ts(message.timestamp)
        if stamp:
            header += f" · {stamp}"
        chunks = split_chunks(message.body)
        await thread.send(
            content=f"{header}\n{chunks[0]}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        for chunk in chunks[1:]:
            await thread.send(
                content=chunk, allowed_mentions=discord.AllowedMentions.none()
            )

    async def _record(self, row, thread_id, messages) -> None:
        newest = None
        for message in messages:
            ts = _parse_ts(message.timestamp)
            if ts is not None and (newest is None or ts > newest):
                newest = ts
        await self._repo.record(
            row.conversation_id,
            username=row.sender,
            subject=row.subject,
            thread_id=int(thread_id) if thread_id is not None else None,
            last_activity=newest.isoformat() if newest else None,
            mirrored_count=len(messages),
        )

    # ------------------------------------------------------------------
    # Reply (Discord → game)
    # ------------------------------------------------------------------

    async def reply_from_thread(self, thread_id: int, body: str) -> tuple[bool, str]:
        """Send a staff reply typed in a mirrored thread into the game."""
        row = await self._repo.by_thread(thread_id)
        if row is None:
            return False, "this thread is not linked to a game conversation"
        if self._cfg.automation.dry_run:
            return False, (
                "dry-run is on — reply NOT sent to the game "
                "(`!fra set dry_run off` to go live)"
            )
        ok, detail = await mailbox.send_reply(
            self._mc, row["conversation_id"], body
        )
        if ok:
            self._echoes.setdefault(row["conversation_id"], []).append(
                _norm_body(body)
            )
        return ok, detail

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def status_lines(self) -> list[str]:
        auto = self._cfg.automation.dm_mirror
        channels = self._cfg.discord.channels
        forum = self.forum()
        return [
            "forum: "
            + (
                f"<#{channels.dm_mirror}>"
                + ("" if forum else " (⚠️ not reachable / not a forum)")
                if channels.dm_mirror else "not set (`!fra set dm_mirror <id>`)"
            ),
            f"conversations tracked: {await self._repo.count()}",
            f"inbox scan: {'every ' + str(auto.interval) + ' min' if auto.enabled else 'OFF'}",
            "replies from threads: "
            + ("dry-run (NOT sent)" if self._cfg.automation.dry_run else "live"),
        ]

    @staticmethod
    def _summary(*, error: str | None = None, **counts) -> dict:
        if error:
            return {"error": error, "lines": [error], "changed": False}
        lines = [
            f"{counts.get('conversations', 0)} conversation(s) in the inbox — "
            f"{counts.get('threads_created', 0)} new thread(s), "
            f"{counts.get('mirrored', 0)} message(s) mirrored, "
            f"{counts.get('skipped', 0)} unchanged, {counts.get('failed', 0)} failed"
        ]
        changed = bool(
            counts.get("threads_created") or counts.get("mirrored")
            or counts.get("failed")
        )
        return {**counts, "error": None, "lines": lines, "changed": changed}
