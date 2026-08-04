"""MissionChief outage announcements for the members' status channel.

The bot is the alliance's most reliable witness of whether the game is
up: every few minutes something of ours talks to missionchief.com. This
service turns that into two messages members care about — "the game
looks down" and "it is back" — and nothing else.

Deliberately quiet by design:

* it announces only after the site has been unreachable for
  ``outage_minutes`` (default 15), so a server hiccup, a deploy or one
  timed-out page never reaches the channel;
* it reads :class:`~fra_bot.mc.health.MissionChiefHealth`, which counts
  only site-is-down signals (5xx, connection errors, timeouts) — a 4xx,
  rate limiting or a sign-in redirect proves the site is UP and clears
  the outage clock;
* each transition is announced once. The open outage is persisted, so a
  bot restart mid-outage neither repeats the notice nor loses the start
  time the recovery message reports.

The check itself makes NO MissionChief request: it only reads state the
normal traffic already produced, so it is safe at any interval and in
dry-run.
"""

from __future__ import annotations

import datetime as dt
import logging

import discord

from ..config import Config
from ..db.database import Database
from ..db.repos import StateRepo

log = logging.getLogger(__name__)

#: Persisted start of the currently announced outage (epoch seconds).
STATE_DOWN_SINCE = "mc_status:down_since"


def _human_duration(seconds: float) -> str:
    minutes = max(1, int(max(0.0, seconds) // 60))
    if minutes < 60:
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        text = f"{hours} hour" + ("" if hours == 1 else "s")
        return text if minutes == 0 else f"{text} {minutes} min"
    days, hours = divmod(hours, 24)
    text = f"{days} day" + ("" if days == 1 else "s")
    return text if hours == 0 else f"{text} {hours} h"


class MissionChiefStatusService:
    def __init__(self, cfg: Config, client, db: Database, bot) -> None:
        self.cfg = cfg
        self.client = client
        self.state = StateRepo(db)
        self.bot = bot

    @property
    def _auto(self):
        return self.cfg.automation.mc_status

    @property
    def channel_id(self) -> int:
        return int(getattr(self.cfg.discord.channels, "mc_status", 0) or 0)

    def channel(self):
        return self.bot.channel_for("mc_status") if self.channel_id else None

    # ------------------------------------------------------------------

    async def check(self) -> str | None:
        """One pass. Returns a line for the admin log on a transition."""
        if not self._auto.enabled:
            return None
        health = getattr(self.client, "health", None)
        if health is None:  # pragma: no cover - defensive
            return None

        stored = await self.state.get(STATE_DOWN_SINCE)
        down_since = float(stored) if stored else None

        if down_since is not None:
            # While an outage is running we look for ONE thing: positive
            # proof the site answered again. Not "no failures lately" — a
            # bot restarted mid-outage starts with an empty health record,
            # and reading that as "up" would announce a recovery in the
            # middle of the outage. Strictly newer than the outage start,
            # because that start IS the last reachable moment.
            reachable_at = health.last_reachable_at
            if reachable_at is None or reachable_at <= down_since:
                return None
            await self.state.delete(STATE_DOWN_SINCE)
            lasted = max(0.0, reachable_at - down_since)
            await self._announce_recovery(down_since, lasted)
            return (
                f"🟢 MissionChief reachable again after "
                f"{_human_duration(lasted)} — recovery announced"
            )

        outage = health.outage_seconds()
        threshold = max(1, int(self._auto.outage_minutes)) * 60
        if outage >= threshold:
            started = health.down_since() or 0.0
            # Store BEFORE announcing: a failed post must not turn into a
            # fresh notice every single pass.
            await self.state.set(STATE_DOWN_SINCE, repr(started))
            await self._announce_outage(started, health.last_reason)
            return (
                f"🔴 MissionChief unreachable for {_human_duration(outage)} "
                f"({health.last_reason or 'no answer'}) — outage announced"
            )
        return None

    # ------------------------------------------------------------------

    async def _announce_outage(self, started: float, reason: str | None) -> None:
        embed = discord.Embed(
            title="🔴 MissionChief appears to be down",
            colour=discord.Colour.red(),
            description=(
                "The bot has not been able to reach missionchief.com since "
                f"<t:{int(started)}:t> (<t:{int(started)}:R>).\n\n"
                "This is the game itself, not your connection — alliance "
                "automation is paused until it is back. You will get a "
                "message here the moment it is."
            ),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        await self._post(embed)

    async def _announce_recovery(self, started: float, lasted: float) -> None:
        embed = discord.Embed(
            title="🟢 MissionChief is back online",
            colour=discord.Colour.green(),
            description=(
                "The game is reachable again and alliance automation has "
                f"resumed.\n\nThe outage lasted about **{_human_duration(lasted)}** "
                f"(since <t:{int(started)}:t>)."
            ),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        await self._post(embed)

    async def _post(self, embed: discord.Embed) -> None:
        channel = self.channel()
        if channel is None:
            log.warning(
                "MissionChief status: channel %s is not configured or not "
                "reachable — notice not posted", self.channel_id,
            )
            return
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("MissionChief status notice failed: %s", exc)

    # ------------------------------------------------------------------

    async def status_lines(self) -> list[str]:
        """`!fra mcstatus`: what the watcher currently believes."""
        health = getattr(self.client, "health", None)
        channel_id = self.channel_id
        outage = health.outage_seconds() if health is not None else 0.0
        stored = await self.state.get(STATE_DOWN_SINCE)
        lines = [
            "game: " + (
                f"⚠️ unreachable for {_human_duration(outage)} "
                f"({health.last_reason or 'no answer'})"
                if outage > 0 else "✅ reachable"
            ),
            "channel: " + (
                f"<#{channel_id}>"
                + ("" if self.channel() else " (⚠️ not reachable)")
                if channel_id else "not set (`!fra set mc_status <channel id>`)"
            ),
            f"announce after: {self._auto.outage_minutes} min"
            + ("" if self._auto.enabled else " (watcher OFF)"),
            "announced outage: " + (
                f"since <t:{int(float(stored))}:f>" if stored else "none"
            ),
        ]
        return lines
