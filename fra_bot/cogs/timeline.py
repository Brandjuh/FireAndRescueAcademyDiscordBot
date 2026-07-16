"""`!timeline <lid>` — a member's merged audit timeline for admins
(reference bot: MemberManager). Data comes from what the bot already
stores; nothing is scraped on demand."""

from __future__ import annotations

import logging

from discord.ext import commands

from ..services.timeline import build_timeline, render_timeline
from .admin import is_fra_admin
from .sanctions import resolve_member_target

log = logging.getLogger(__name__)


class TimelineCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="timeline", aliases=["audit"])
    @is_fra_admin()
    async def timeline(self, ctx: commands.Context, *, target: str) -> None:
        """Show a member's audit timeline: `!timeline SomeMember`."""
        mc_user_id, name, discord_id = await resolve_member_target(
            self.bot, ctx, target
        )
        events = await build_timeline(
            self.bot.db, mc_user_id=mc_user_id, name=name,
            discord_user_id=discord_id,
        )
        await ctx.send(render_timeline(name or target, events))


async def setup(bot) -> None:
    await bot.add_cog(TimelineCog(bot))
