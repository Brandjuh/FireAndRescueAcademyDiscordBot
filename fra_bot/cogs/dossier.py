"""Staff console: the member dossier.

`/member <query>` (staff-gated) and a persistent panel button in the
member-management channel open one member's full dossier — searched
across BOTH Discord and MissionChief, including members who are not in
Discord at all. All dossier output is ephemeral (private to the staff
member), like the reference bot.
"""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..services.dossier import Dossier, DossierService
from .admin import is_fra_admin_ctx

log = logging.getLogger(__name__)

PANEL_TITLE = "Member Management"
_KIND_LABEL = {
    "training": "🎓 Trainings",
    "building": "🏗️ Buildings",
    "event": "🚨 Events",
    "large": "🚒 Large missions",
}


def _staff_check(bot, member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    allowed = set(bot.cfg.discord.admin_role_ids) | set(
        getattr(bot.cfg.discord, "staff_role_ids", ())
    )
    return any(role.id in allowed for role in member.roles)


def _day(raw: str | None) -> str:
    return raw[:10] if raw else "—"


def dossier_embed(d: Dossier) -> discord.Embed:
    status = "🟢 Active in the alliance" if d.is_active else (
        f"🔴 Left the alliance ({_day(d.left_at)})"
    )
    verified = {
        "approved": f"✅ Verified — <@{d.discord_id}>",
        "denied": "❌ Link denied",
    }.get(d.link_status or "", "➖ Not linked to Discord")
    embed = discord.Embed(
        title=f"📇 {d.name}",
        colour=discord.Colour.green() if d.is_active else discord.Colour.red(),
        timestamp=dt.datetime.now(dt.timezone.utc),
        description=(
            f"{status}\n{verified}\n"
            f"[MissionChief profile](https://www.missionchief.com/users/{d.mc_user_id})"
        ),
    )
    embed.add_field(
        name="Identity",
        value=(
            f"MC id: `{d.mc_user_id}`\n"
            f"Rank: {d.role or '—'}\n"
            f"Member since: {d.member_since or '—'}\n"
            f"First seen by bot: {_day(d.first_seen_at)}"
        ),
    )
    earned = f"{d.earned_credits:,}" if d.earned_credits is not None else "—"
    rate = f"{d.contribution_rate:g}%" if d.contribution_rate is not None else "—"
    contributed = []
    if d.contributed_daily is not None:
        contributed.append(f"today: {d.contributed_daily:,}")
    if d.contributed_monthly is not None:
        contributed.append(f"this month: {d.contributed_monthly:,}")
    embed.add_field(
        name="Credits",
        value=(
            f"Earned: {earned}\n"
            f"Contribution rate: {rate}\n"
            "Contributed " + ", ".join(contributed) if contributed
            else f"Earned: {earned}\nContribution rate: {rate}\nContributed: —"
        ),
    )
    merged: dict[str, dict] = dict(d.requests)
    for kind, entry in d.missions.items():
        merged.setdefault(kind, entry)
    lines = [
        f"{_KIND_LABEL.get(kind, kind)}: {entry['count']}× "
        f"(last: {entry.get('last_status', '—')}, {_day(entry.get('last_at'))})"
        for kind, entry in sorted(merged.items())
    ]
    embed.add_field(
        name="Requests",
        value="\n".join(lines) if lines else "No requests on record.",
        inline=False,
    )
    embed.set_footer(text=f"MC {d.mc_user_id} · dossier is private to you")
    return embed


class DossierSearchModal(discord.ui.Modal, title="Member lookup"):
    query = discord.ui.TextInput(
        label="Discord @mention/id, MC name or MC id",
        placeholder="e.g. DutchFireFighter or 123456",
        max_length=100,
    )

    def __init__(self, cog: "DossierCog") -> None:
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._cog.open_dossier(interaction, str(self.query))


class DossierPanelView(discord.ui.View):
    def __init__(self, cog: "DossierCog") -> None:
        super().__init__(timeout=None)
        self._cog = cog

    @discord.ui.button(
        label="Open Member Management",
        style=discord.ButtonStyle.primary,
        custom_id="fra:dossier:open",
        emoji="📇",
    )
    async def open_panel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not _staff_check(interaction.client, interaction.user):
            await interaction.response.send_message(
                "You don't have permission to do this.", ephemeral=True
            )
            return
        await interaction.response.send_modal(DossierSearchModal(self._cog))


class DossierCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.service = DossierService(bot.db)

    # -- shared open path ---------------------------------------------------

    async def open_dossier(self, interaction: discord.Interaction, raw: str) -> None:
        """Resolve a query (mention / Discord id / MC name / MC id) to one
        member and show the ephemeral dossier, or a disambiguation list."""
        query = raw.strip().lstrip("<@!&").rstrip(">")
        mc_id: int | None = None

        # A mention or id that belongs to a GUILD member resolves through
        # the verified link first; a bare number that is not a guild member
        # falls through to the MC-id search.
        if query.isdigit() and interaction.guild is not None:
            member = interaction.guild.get_member(int(query))
            if member is not None:
                mc_id = await self.service.resolve_discord(member.id)
                if mc_id is None:
                    await interaction.response.send_message(
                        f"{member.mention} has no verified MissionChief link — "
                        "search by their MC name instead.", ephemeral=True,
                    )
                    return
        if mc_id is None:
            candidates = await self.service.search(query)
            if not candidates:
                await interaction.response.send_message(
                    f"No member found for `{raw}` — try the exact MC name or id.",
                    ephemeral=True,
                )
                return
            if len(candidates) > 1 and candidates[0].score <= candidates[1].score:
                lines = [
                    f"- **{c.name}** (MC `{c.mc_user_id}`"
                    + (f", <@{c.discord_id}>" if c.discord_id else "")
                    + ("" if c.is_active else ", left")
                    + ")"
                    for c in candidates
                ]
                await interaction.response.send_message(
                    "Multiple matches — search again with the exact name or MC id:\n"
                    + "\n".join(lines),
                    ephemeral=True,
                )
                return
            mc_id = candidates[0].mc_user_id

        dossier = await self.service.build(mc_id)
        if dossier is None:
            await interaction.response.send_message(
                f"No roster data for MC `{mc_id}`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=dossier_embed(dossier), ephemeral=True
        )

    # -- entry points -----------------------------------------------------------

    @app_commands.command(name="member", description="Open a member's dossier (staff)")
    @app_commands.describe(query="Discord @mention/id, MC name or MC id")
    async def member_slash(
        self, interaction: discord.Interaction, query: str
    ) -> None:
        if not _staff_check(self.bot, interaction.user):
            await interaction.response.send_message(
                "You don't have permission to do this.", ephemeral=True
            )
            return
        await self.open_dossier(interaction, query)

    @commands.command(name="memberpanel")
    async def member_panel(self, ctx: commands.Context) -> None:
        """(Re)post the member-management panel in its channel."""
        if not is_fra_admin_ctx(ctx):
            await ctx.send("⛔ You don't have permission to use that command.")
            return
        channel_id = getattr(self.bot.cfg.discord.channels, "member_panel", 0)
        channel = self.bot.get_channel(channel_id) if channel_id else ctx.channel
        if channel is None:
            await ctx.send("⚠️ Set the panel channel first: `!fra set member_panel <id>`.")
            return
        embed = discord.Embed(
            title=f"📇 {PANEL_TITLE}",
            colour=discord.Colour.blurple(),
            description=(
                "Look up everything we know about an alliance member — "
                "requests, contribution, credits, verification.\n\n"
                "Search works for Discord **and** MissionChief members "
                "(also members who are not in Discord)."
            ),
        )
        await channel.send(embed=embed, view=DossierPanelView(self))
        if channel is not ctx.channel:
            await ctx.send(f"✅ Panel posted in {channel.mention}.")
