"""The class-availability panel: free training classes per agency.

One self-maintaining panel (channel ``discord.channels.class_panel``)
whose embed lists how many free classrooms every academy type has, with a
last-updated stamp. The numbers come from the availability cache the
trainings service writes; an hourly job (``class-availability`` in the
bot) refreshes that cache — reusing the guide walk's result when it is
recent, so the panel never doubles the game traffic — and then asks the
panel keeper to re-render this panel. The keeper's digest covers the
description, so the message is edited in place whenever the counts (or
the timestamp) change and left alone otherwise.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..services.trainings import _AGENCY_ORDER, _AGENCY_TITLES

log = logging.getLogger(__name__)

PANEL_TITLE = "🎓 Training classes available right now"


class ClassesPanelCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._data: dict | None = None  # {"counts": {...}, "at": epoch}

    async def cog_load(self) -> None:
        # Load the cached numbers before the keeper's first sweep, so a
        # restart re-renders real counts instead of the placeholder.
        await self.reload_snapshot()

    async def reload_snapshot(self) -> None:
        """Pull the latest cached walk into memory (panel_embed is sync)."""
        try:
            self._data = await self.bot.trainings.cached_availability()
        except Exception:
            log.exception("could not load the availability cache")

    # -- panel (posted/maintained by the panel keeper) ---------------------

    def panel_embed(self) -> discord.Embed:
        lines: list[str] = []
        counts = (self._data or {}).get("counts") or {}
        if counts:
            for key in _AGENCY_ORDER:
                count = int(counts.get(key, 0))
                unit = "class" if count == 1 else "classes"
                lines.append(f"{_AGENCY_TITLES[key]}: **{count}** {unit}")
            at = (self._data or {}).get("at")
            if at:
                lines.append("")
                lines.append(f"Last updated <t:{int(at)}:R> — refreshed hourly.")
        else:
            lines.append(
                "The first availability check hasn't finished yet — the "
                "numbers appear here shortly."
            )
        lines.append(
            "Request a class with **/training** or the request panel."
        )
        return discord.Embed(
            title=PANEL_TITLE,
            colour=discord.Colour.green(),
            description="\n".join(lines),
        )

    def panel_view(self) -> discord.ui.View | None:
        return None  # informational panel, no buttons
