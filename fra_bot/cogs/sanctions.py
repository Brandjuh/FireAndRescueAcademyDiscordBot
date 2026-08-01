"""Sanctions register (reference bot: sanctionmanager).

Records sanctions against alliance members with full history and
statistics, announces them, and tells the member — the bot never
executes a kick/ban itself (same as the reference cog: enforcement
stays a human act; on a 3rd official warning it posts the configured
ADVISORY to the admin log).

Commands (admins): ``!sanction add <type> <lid> <reden>``, ``list``,
``stats``, ``revoke``. Types are the reference bot's labels, addressed
by short key (verbal, w1, w2, w3, kick, ban, mute, mute5m … mute14d).
The target may be a Discord @mention (the MC identity resolves through
the verified link) or a MissionChief name (resolved via the roster).
"""

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from ..db.repos import LinksRepo, MembersRepo, SanctionsRepo
from .admin import is_fra_admin
from .display import profile_url

log = logging.getLogger(__name__)

#: Short key → the reference bot's sanction type label (kept verbatim so
#: old and new records read the same).
SANCTION_TYPE_KEYS: dict[str, str] = {
    "verbal": "Warning - Verbal warning",
    "w1": "Warning - Official 1st warning",
    "w2": "Warning - Official 2nd warning",
    "w3": "Warning - Official 3rd and last warning",
    "kick": "Kick",
    "ban": "Ban",
    "mute": "Mute",
    "mute5m": "Mute 5m",
    "mute15m": "Mute 15m",
    "mute30m": "Mute 30m",
    "mute1h": "Mute 1h",
    "mute6h": "Mute 6h",
    "mute12h": "Mute 12h",
    "mute1d": "Mute 1d",
    "mute7d": "Mute 7d",
    "mute14d": "Mute 14d",
}

_WARNING_TYPES = frozenset(SanctionsRepo.OFFICIAL_WARNINGS)


def resolve_type(key: str) -> str | None:
    return SANCTION_TYPE_KEYS.get(key.strip().lower())


def type_colour(sanction_type: str) -> discord.Colour:
    if sanction_type in ("Kick", "Ban"):
        return discord.Colour.red()
    if sanction_type.startswith("Mute"):
        return discord.Colour.dark_orange()
    if sanction_type in _WARNING_TYPES:
        return discord.Colour.orange()
    return discord.Colour.yellow()  # verbal


async def resolve_member_target(
    bot, ctx: commands.Context, target: str
) -> tuple[int | None, str | None, int | None]:
    """(mc_user_id, mc_username, discord_user_id) for a @mention or an
    MC name. A mention resolves MC identity through the verified link;
    a name resolves through the roster (case-insensitive). Shared by the
    sanction and timeline commands."""
    member = None
    try:
        member = await commands.MemberConverter().convert(ctx, target)
    except commands.BadArgument:
        member = None
    if member is not None:
        link = await LinksRepo(bot.db).get_by_discord(member.id)
        mc_user_id = (
            int(link["mc_user_id"])
            if link is not None and link["status"] == "approved" else None
        )
        name = member.display_name
        if mc_user_id is not None:
            roster = await MembersRepo(bot.db).active_members()
            row = roster.get(mc_user_id)
            if row is not None:
                name = row["name"]
        return mc_user_id, name, member.id
    # Plain MC name: roster lookup, else record the name as given.
    wanted = target.strip().casefold()
    for mc_id, row in (await MembersRepo(bot.db).active_members()).items():
        if str(row["name"]).casefold() == wanted:
            link = await LinksRepo(bot.db).get_by_mc(mc_id)
            discord_id = (
                int(link["discord_id"])
                if link is not None and link["status"] == "approved" else None
            )
            return mc_id, row["name"], discord_id
    return None, target.strip(), None


class ReviewConfirmButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fra:sreview:confirm:(?P<sid>[0-9]+)",
):
    def __init__(self, sanction_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Confirm sanction",
            style=discord.ButtonStyle.success,
            custom_id=f"fra:sreview:confirm:{sanction_id}",
        ))
        self.sanction_id = sanction_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("SanctionsCog")
        if cog is not None:
            await cog.handle_review(interaction, self.sanction_id, confirm=True)


class ReviewDismissButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fra:sreview:dismiss:(?P<sid>[0-9]+)",
):
    def __init__(self, sanction_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Dismiss",
            style=discord.ButtonStyle.secondary,
            custom_id=f"fra:sreview:dismiss:{sanction_id}",
        ))
        self.sanction_id = sanction_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("SanctionsCog")
        if cog is not None:
            await cog.handle_review(interaction, self.sanction_id, confirm=False)


def _review_view(sanction_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewConfirmButton(sanction_id))
    view.add_item(ReviewDismissButton(sanction_id))
    return view


class SanctionsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.repo = SanctionsRepo(bot.db)
        bot.add_dynamic_items(ReviewConfirmButton, ReviewDismissButton)

    async def _resolve_target(
        self, ctx: commands.Context, target: str
    ) -> tuple[int | None, str | None, int | None]:
        return await resolve_member_target(self.bot, ctx, target)

    # -- commands ------------------------------------------------------------

    @commands.group(name="sanction", aliases=["sanctions"], invoke_without_command=True)
    @is_fra_admin()
    async def sanction(self, ctx: commands.Context) -> None:
        keys = ", ".join(sorted(SANCTION_TYPE_KEYS))
        await ctx.send(
            "Sanctions register — subcommands: `add <type> <lid> <reden>`, "
            "`list <lid>`, `recent`, `stats`, `revoke <id>`.\n"
            f"Types: {keys}"
        )

    @sanction.command(name="add")
    @is_fra_admin()
    async def sanction_add(
        self, ctx: commands.Context, type_key: str, target: str, *,
        reason: str,
    ) -> None:
        """Record a sanction: `!sanction add w1 SomeMember spamming chat`."""
        sanction_type = resolve_type(type_key)
        if sanction_type is None:
            await ctx.send(
                f"⚠️ Unknown type `{type_key}` — use one of: "
                + ", ".join(sorted(SANCTION_TYPE_KEYS))
            )
            return
        mc_user_id, name, discord_id = await self._resolve_target(ctx, target)
        sanction_id = await self.repo.add(
            mc_user_id=mc_user_id, mc_username=name, discord_user_id=discord_id,
            admin_discord_id=ctx.author.id, admin_name=ctx.author.display_name,
            sanction_type=sanction_type, reason=reason,
        )
        warnings = 0
        if sanction_type in _WARNING_TYPES:
            warnings = await self.repo.official_warning_count(
                mc_user_id=mc_user_id, discord_user_id=discord_id, name=name,
            )
        await self._announce(sanction_id, sanction_type, name, reason,
                             ctx.author.display_name, warnings)
        await self._notify_member(discord_id, name, sanction_type, reason)
        await self.bot.log_member_action(
            action="sanction_received",
            detail=f"#{sanction_id} {sanction_type} — {reason[:120]} "
                   f"(by {ctx.author.display_name})",
            discord_user_id=discord_id, mc_user_id=mc_user_id,
            actor_name=name,
        )
        note = f" — official warning **{warnings}/3**" if warnings else ""
        unknown = "" if mc_user_id or discord_id else " (⚠️ not on the roster)"
        await ctx.send(
            f"✅ Sanction **#{sanction_id}** recorded: {sanction_type} for "
            f"**{name}**{unknown}.{note}"
        )
        if (
            warnings >= 3
            and self.bot.cfg.automation.sanctions.auto_action_enabled
        ):
            # ADVISORY only, like the reference bot — no automatic kick.
            action = self.bot.cfg.automation.sanctions.third_warning_action
            await self.bot.notify_admin(
                f"⚠️ **3rd official warning** for **{name}** — configured "
                f"follow-up: **{action}** (manual action required)."
            )

    @sanction.command(name="list")
    @is_fra_admin()
    async def sanction_list(self, ctx: commands.Context, *, target: str) -> None:
        mc_user_id, name, discord_id = await self._resolve_target(ctx, target)
        rows = await self.repo.for_member(
            mc_user_id=mc_user_id, discord_user_id=discord_id, name=name,
        )
        if not rows:
            await ctx.send(f"No sanctions recorded for **{name}**.")
            return
        lines = [
            f"`#{r['id']}` {r['created_at'][:10]} — {r['sanction_type']} — "
            f"{r['reason'][:80]}"
            + (f" *({r['status']})*" if r["status"] != "active" else "")
            for r in rows
        ]
        warnings = await self.repo.official_warning_count(
            mc_user_id=mc_user_id, discord_user_id=discord_id, name=name,
        )
        await ctx.send(
            f"📋 Sanctions for **{name}** (official warnings: {warnings}/3):\n"
            + "\n".join(lines)[:1800]
        )

    @sanction.command(name="recent")
    @is_fra_admin()
    async def sanction_recent(self, ctx: commands.Context) -> None:
        rows = await self.repo.recent()
        if not rows:
            await ctx.send("No sanctions recorded yet.")
            return
        lines = [
            f"`#{r['id']}` {r['created_at'][:10]} — **{r['mc_username']}** — "
            f"{r['sanction_type']}"
            + (f" *({r['status']})*" if r["status"] != "active" else "")
            for r in rows
        ]
        await ctx.send("🕐 Recent sanctions:\n" + "\n".join(lines)[:1800])

    @sanction.command(name="stats")
    @is_fra_admin()
    async def sanction_stats(self, ctx: commands.Context) -> None:
        rows = await self.repo.stats()
        if not rows:
            await ctx.send("No sanctions recorded yet.")
            return
        lines = [
            f"- {r['sanction_type']}: **{r['n']}**"
            + (" (revoked)" if r["status"] != "active" else "")
            for r in rows
        ]
        await ctx.send("📊 Sanction statistics:\n" + "\n".join(lines)[:1800])

    @sanction.command(name="revoke")
    @is_fra_admin()
    async def sanction_revoke(
        self, ctx: commands.Context, sanction_id: int
    ) -> None:
        row = await self.repo.get(sanction_id)
        if row is None:
            await ctx.send(f"⚠️ Sanction #{sanction_id} does not exist.")
            return
        if not await self.repo.revoke(
            sanction_id, revoked_by=ctx.author.display_name
        ):
            await ctx.send(f"⚠️ Sanction #{sanction_id} was already revoked.")
            return
        await ctx.send(
            f"↩️ Sanction **#{sanction_id}** ({row['sanction_type']} for "
            f"**{row['mc_username']}**) revoked."
        )
        await self.bot.notify_admin(
            f"↩️ Sanction #{sanction_id} ({row['sanction_type']} for "
            f"**{row['mc_username']}**) revoked by {ctx.author.display_name}."
        )
        await self.bot.log_member_action(
            action="sanction_revoked",
            detail=f"#{sanction_id} {row['sanction_type']} "
                   f"(by {ctx.author.display_name})",
            discord_user_id=row["discord_user_id"],
            mc_user_id=row["mc_user_id"],
            actor_name=row["mc_username"],
        )

    @sanction.command(name="reviewscan")
    @is_fra_admin()
    async def sanction_reviewscan(self, ctx: commands.Context) -> None:
        """Run the game-log sanction review pass right now."""
        result = await self.bot.sanction_review.scan()
        if result["bootstrapped"]:
            await ctx.send(
                "✅ Review checkpoint initialised at the current log tail — "
                "new kicks/chat bans are picked up from here."
            )
            return
        await self.post_reviews(result["created"])
        await ctx.send(
            f"🔍 Game-log review: **{len(result['created'])}** imported for "
            f"review, {result['skipped_own']} own tax-kick(s) skipped, "
            f"{result['skipped_recorded']} already recorded manually."
        )

    # -- game-log review notices (posted by the scheduler job) -----------------

    async def post_reviews(self, created: list[dict]) -> None:
        """Post review notices for freshly imported game-log sanctions —
        one embed each with Confirm/Dismiss, or a single bulk notice when
        a pass imported a pile (reference behaviour)."""
        from ..services.sanction_review import BULK_THRESHOLD

        if not created:
            return
        channel = (
            self.bot.channel_for("sanctions") or self.bot.channel_for("admin_log")
        )
        if channel is None:
            return
        if len(created) >= BULK_THRESHOLD:
            sample = ", ".join(
                str(item["name"] or item["mc_user_id"] or "?")
                for item in created[:8]
            )
            embed = discord.Embed(
                title="⚖️ Bulk game-log sanction review",
                colour=discord.Colour.orange(),
                description=(
                    f"**{len(created)}** moderation entries from the game log "
                    "were imported as *unverified* sanctions.\n"
                    f"Sample: {sample}\n\n"
                    "Review them with `!sanction recent` (Confirm/Dismiss "
                    "per member via `!sanction list <member>`)."
                )[:4096],
            )
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as exc:
                log.warning("bulk review notice failed: %s", exc)
            return
        for item in created:
            try:
                await channel.send(
                    embed=self._review_embed(item),
                    view=_review_view(item["sanction_id"]),
                )
            except discord.HTTPException as exc:
                log.warning("review notice for sanction #%s failed: %s",
                            item["sanction_id"], exc)

    def _review_embed(self, item: dict) -> discord.Embed:
        embed = discord.Embed(
            title="⚖️ Sanction review required",
            colour=discord.Colour.orange(),
            description=(
                "A moderation entry appeared in the game log without a "
                "matching record in the sanctions register. Confirm to "
                "record it, or dismiss if it needs no follow-up."
            ),
        )
        name = item["name"] or "Unknown"
        url = profile_url(item["mc_user_id"])
        embed.add_field(
            name="Member",
            value=f"[{name}]({url})" if url else name,
            inline=False,
        )
        if item["discord_user_id"]:
            embed.add_field(
                name="Discord", value=f"<@{item['discord_user_id']}>",
                inline=True,
            )
        embed.add_field(
            name="Detected action", value=item["sanction_type"], inline=True
        )
        embed.add_field(name="Executed by", value=item["executor"], inline=True)
        embed.add_field(
            name="Game log time",
            value=str(item["event_at"] or item["raw_timestamp"]),
            inline=False,
        )
        if item["description"]:
            embed.add_field(
                name="Log entry", value=str(item["description"])[:1024],
                inline=False,
            )
        embed.set_footer(
            text=f"Sanction #{item['sanction_id']} (unverified) — "
                 f"game log #{item['log_id']}"
        )
        return embed

    async def handle_review(
        self, interaction: discord.Interaction, sanction_id: int, *,
        confirm: bool,
    ) -> None:
        from .automation import _is_admin_interaction

        if not _is_admin_interaction(interaction):
            await interaction.response.send_message(
                "You don't have permission to do this.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        row = await self.repo.get(sanction_id)
        if row is None:
            await interaction.followup.send(
                f"⚠️ Sanction #{sanction_id} no longer exists.", ephemeral=True
            )
            return
        if not await self.repo.resolve_review(
            sanction_id, confirm=confirm,
            by=interaction.user.display_name,
        ):
            await interaction.followup.send(
                f"⚠️ Sanction #{sanction_id} is already "
                f"{row['status']} — nothing changed.",
                ephemeral=True,
            )
            return
        verb = "Confirmed" if confirm else "Dismissed"
        if confirm:
            # Dossier entry on CONFIRM only — parity with `!sanction add`.
            await self.bot.log_member_action(
                action="sanction_received",
                detail=f"#{sanction_id} {row['sanction_type']} — "
                       f"{row['reason'][:120]} (game log, confirmed by "
                       f"{interaction.user.display_name})",
                discord_user_id=row["discord_user_id"],
                mc_user_id=row["mc_user_id"],
                actor_name=row["mc_username"],
            )
        try:
            message = interaction.message
            embed = message.embeds[0] if message and message.embeds else None
            if message is not None and embed is not None:
                embed.colour = (
                    discord.Colour.red() if confirm
                    else discord.Colour.light_grey()
                )
                embed.set_footer(
                    text=f"{verb} by {interaction.user.display_name} — "
                         f"sanction #{sanction_id}"
                )
                await message.edit(embed=embed, view=None)
        except discord.HTTPException as exc:
            log.warning("could not update review embed #%s: %s", sanction_id, exc)
        await interaction.followup.send(
            f"✅ {verb} sanction **#{sanction_id}** for "
            f"**{row['mc_username'] or '?'}**.",
            ephemeral=True,
        )

    # -- announcements ---------------------------------------------------------

    async def _announce(
        self, sanction_id: int, sanction_type: str, name: str | None,
        reason: str, admin_name: str, warnings: int,
    ) -> None:
        channel = (
            self.bot.channel_for("sanctions") or self.bot.channel_for("admin_log")
        )
        if channel is None:
            return
        embed = discord.Embed(
            title=f"⚖️ Sanction #{sanction_id} — {sanction_type}"[:256],
            colour=type_colour(sanction_type),
            description=f"**Member:** {name}\n**Reason:** {reason}"[:4096],
        )
        if warnings:
            embed.add_field(name="Official warnings", value=f"{warnings}/3")
        embed.set_footer(text=f"Recorded by {admin_name}")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.warning("sanction announce failed: %s", exc)

    async def _notify_member(
        self, discord_id: int | None, name: str | None,
        sanction_type: str, reason: str,
    ) -> None:
        """Tell the member: Discord DM when linked, else an in-game PM —
        the reference bot could only DM; the in-game fallback means an
        unlinked member still hears about it."""
        text = (
            f"⚖️ You have received a sanction in Fire & Rescue Academy: "
            f"**{sanction_type}**.\nReason: {reason}\n"
            "Contact an admin if you believe this is a mistake."
        )
        if discord_id:
            user = self.bot.get_user(int(discord_id))
            try:
                if user is None:
                    user = await self.bot.fetch_user(int(discord_id))
                await user.send(text)
                return
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        if not name:
            return
        try:
            plain = text.replace("**", "")
            result = await self.bot.dm_mirror.send_new(name, "Sanction", plain)
            if not result.get("ok"):
                log.warning("sanction in-game PM to %s failed: %s",
                            name, result.get("detail"))
        except Exception:  # noqa: BLE001 — a PM must never fail the command
            log.exception("sanction in-game PM to %s errored", name)


async def setup(bot) -> None:
    await bot.add_cog(SanctionsCog(bot))
