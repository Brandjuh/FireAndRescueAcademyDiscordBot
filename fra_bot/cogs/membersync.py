"""Discord layer for MemberSync: ``!verify``, the retry queue and role
upkeep. All texts mirror the reference bot verbatim.

The verified role is granted when a member proves alliance membership
(nickname matches the roster, or a supplied MC id) and removed by the
hourly prune once a linked member leaves the alliance.
"""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord.ext import commands, tasks

from ..services.membersync import QUEUE_MAX_ATTEMPTS, MemberSyncService
from .admin import is_fra_admin

log = logging.getLogger(__name__)

QUEUE_INTERVAL_SECONDS = 120
PRUNE_INTERVAL_HOURS = 1

_EXPIRED_DM = (
    "❌ **Verification Failed**\n\n"
    "We couldn't find your account in the alliance roster after 1.5 hours "
    "of attempts.\n\n"
    "**To fix this:**\n"
    "1. Make sure your **Discord server nickname** matches your "
    "**MissionChief name exactly** (including capitalization)\n"
    "2. If you just joined the alliance, wait a few minutes for the roster "
    "to update\n"
    "3. Run the command `!verify` again to restart the verification process\n"
    "4. If you still have issues, you can provide your MC User ID: "
    "`!verify <your_mc_user_id>`\n\n"
    "**Need help?** Contact an administrator if you continue to have issues."
)


def _queued_reply(name: str, eta: dt.datetime | None) -> str:
    expected = eta or (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1, minutes=45)
    )
    return (
        "⏳ **Not found in the roster yet.**\n\n"
        "You've been added to the verification queue.\n"
        "The system will automatically check every 2 minutes.\n\n"
        "**Expected completion (next roster update):** "
        f"<t:{int(expected.timestamp())}:R>\n\n"
        "**JUST WAIT** - you don't need to do anything else.\n"
        "Once found, you'll be automatically verified.\n\n"
        "**Tips:**\n"
        f"• Your current display name is: **{name}**\n"
        "• Make sure this matches your MissionChief name exactly "
        "(including capitalization)\n"
        "• You can provide your MC User ID by running `!verify <your_mc_id>`"
    )


_NAME_MISMATCH = (
    "❌ **We couldn't verify you.**\n\n"
    "Your name is not in the alliance roster **and** the alliance logs show "
    "no recent join under **{name}** — so your Discord nickname almost "
    "certainly doesn't match your MissionChief name.\n\n"
    "**To fix this:**\n"
    "1. Set your **Discord server nickname** to your **MissionChief name "
    "exactly** (including capitalization)\n"
    "2. Run `!verify` again\n"
    "3. Or skip the name matching entirely: `!verify <your_mc_user_id>`\n\n"
    "**Need help?** Contact an administrator."
)

_CONTRIBUTION_REMINDER = (
    "💰 **One more thing:** your alliance donation is currently below the "
    "required minimum of 5% (Code of Conduct rule 4.1).\n\n"
    "**How to set it:**\n"
    "1. Open the menu → **Show Alliance**\n"
    "2. Go to **Alliance Funds**\n"
    "3. Set your donation to at least **5%**\n\n"
    "These funds build the hospitals, prisons and academies everyone uses. "
    "Thank you! 🚒"
)


class MemberSyncCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.service = MemberSyncService(bot.db, mc=bot.mc, cfg=bot.cfg)
        self.queue_loop.start()
        self.prune_loop.start()

    def cog_unload(self) -> None:
        self.queue_loop.cancel()
        self.prune_loop.cancel()

    # -- helpers -----------------------------------------------------------

    def _guild(self) -> discord.Guild | None:
        guild_id = self.bot.cfg.discord.guild_id
        if guild_id:
            return self.bot.get_guild(guild_id)
        return self.bot.guilds[0] if self.bot.guilds else None

    def _role(self, guild: discord.Guild | None) -> discord.Role | None:
        role_id = getattr(self.bot.cfg.discord, "verified_role_id", 0)
        if guild is None or not role_id:
            return None
        return guild.get_role(role_id)

    async def _grant_role(self, member: discord.Member, *, reason: str) -> None:
        role = self._role(member.guild)
        if role is not None and role not in member.roles:
            try:
                await member.add_roles(role, reason=reason)
            except discord.HTTPException as exc:
                log.warning("membersync: could not add role to %s: %s", member, exc)

    async def _dm(self, user: discord.abc.User, text: str) -> None:
        try:
            await user.send(text[:1900])
        except (discord.Forbidden, discord.HTTPException):
            pass

    # -- member-facing command ----------------------------------------------

    @commands.hybrid_command(name="verify")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def verify(self, ctx: commands.Context, mc_id: int | None = None) -> None:
        """Link your Discord account to your MissionChief account.

        Your server nickname must match your MissionChief name exactly, or
        pass your MC user id: `!verify 123456`.
        """
        if ctx.guild is None:
            await ctx.send("❌ This can only be used in a server.")
            return
        outcome = await self.service.request_verification(
            ctx.author.id, ctx.author.display_name, mc_id, ctx.guild.id
        )
        if outcome.outcome == "already_verified":
            await self._grant_role(ctx.author, reason="MemberSync: ensure verified role")
            await ctx.send("✅ You are already verified.")
            return
        if outcome.outcome == "already_queued":
            await ctx.send(
                "⏳ **You're already in the verification queue!**\n\n"
                f"Attempts so far: {outcome.attempts}/{QUEUE_MAX_ATTEMPTS}\n\n"
                "**PLEASE BE PATIENT** - the system is working automatically.\n"
                "You don't need to run this command again. Just wait."
            )
            return
        if outcome.outcome in ("approved", "approved_from_logs"):
            await self._grant_role(ctx.author, reason="MemberSync auto verified")
            await ctx.send(
                "✅ **Verified!** Your account has been linked and you've "
                "been granted the Verified role."
            )
            dm = (
                f"✅ Your MissionChief account `{outcome.mc_user_id}` has been "
                "verified and linked to your Discord account!"
            )
            # Fresh joins (verified straight from the join logs) always
            # start at 0% donation; roster members below the minimum get
            # the same friendly nudge.
            rate = outcome.contribution_rate
            min_rate = self.bot.cfg.automation.tax_warnings.min_rate
            if outcome.outcome == "approved_from_logs" or (
                rate is not None and rate < min_rate
            ):
                dm += "\n\n" + _CONTRIBUTION_REMINDER
            await self._dm(ctx.author, dm)
            return
        if outcome.outcome == "name_mismatch":
            await self._dm(
                ctx.author,
                _NAME_MISMATCH.format(name=ctx.author.display_name),
            )
            await ctx.send(
                "❌ Not found in the roster **or** the recent join logs — "
                "your nickname probably doesn't match your MissionChief "
                "name. Check your DMs for how to fix it."
            )
            return
        # queued (logs unreachable, or an unknown MC id was supplied)
        await self._dm(
            ctx.author,
            _queued_reply(ctx.author.display_name, outcome.roster_eta),
        )
        await ctx.send(
            "🔍 Not found in the roster yet — you've been added to the "
            "verification queue. Check your DMs for details."
        )

    @verify.error
    async def verify_error(self, ctx: commands.Context, error) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                f"⏳ Please wait {error.retry_after:.0f}s before trying again."
            )
        else:
            raise error

    # -- staff commands -------------------------------------------------------

    @commands.command(name="link")
    @is_fra_admin()
    async def link(
        self, ctx: commands.Context, member: discord.Member, mc_id: int
    ) -> None:
        """Manually link a Discord member to an MC account and verify them."""
        await self.service.approve_manual(member.id, mc_id, ctx.author.id)
        await self._grant_role(member, reason=f"MemberSync manual link by {ctx.author}")
        await self._dm(
            member,
            f"✅ Your MissionChief account `{mc_id}` has been verified and "
            "linked to your Discord account!",
        )
        await ctx.send(f"✅ Linked {member.mention} to MC `{mc_id}` and granted the role.")

    @commands.command(name="verifyall")
    @is_fra_admin()
    async def verify_all(self, ctx: commands.Context) -> None:
        """Backfill verification for the whole server: every Discord member
        whose nickname matches an active roster name is linked and gets the
        Verified role — nobody has to run `!verify` themselves. No DMs are
        sent (a server-wide sweep must not spam hundreds of inboxes)."""
        import asyncio

        if ctx.guild is None:
            await ctx.send("❌ This can only be used in a server.")
            return
        role = self._role(ctx.guild)
        if role is None:
            await ctx.send(
                "⚠️ No verified role configured — "
                "`!fra set verified_role @role` first."
            )
            return
        names = {
            m.id: m.display_name for m in ctx.guild.members if not m.bot
        }
        message = await ctx.send(
            f"⏳ Matching {len(names)} server members against the roster…"
        )
        matches = await self.service.backfill_matches(names)
        linked = 0
        for discord_id, roster_row in matches:
            member = ctx.guild.get_member(discord_id)
            if member is None:
                continue
            await self.service.approve_manual(
                discord_id, roster_row["mc_user_id"], reviewer_id=ctx.author.id
            )
            await self._grant_role(member, reason="MemberSync backfill (!verifyall)")
            linked += 1
            if linked % 25 == 0:
                await message.edit(
                    content=f"⏳ Backfill running… {linked}/{len(matches)} linked"
                )
            await asyncio.sleep(1.0)  # be gentle with the role-add rate limit
        unmatched = len(names) - len(matches)
        await message.edit(
            content=(
                f"✅ Backfill done: **{linked}** member(s) linked and verified, "
                f"{unmatched} had no roster match or were already linked. "
                "The rest can still use `!verify` (e.g. after fixing their "
                "nickname)."
            )
        )

    @commands.command(name="unlink")
    @is_fra_admin()
    async def unlink(self, ctx: commands.Context, member: discord.Member) -> None:
        """Remove a member's MC link and the verified role."""
        removed = await self.service.links.delete(member.id)
        role = self._role(ctx.guild)
        if role is not None and role in member.roles:
            try:
                await member.remove_roles(role, reason=f"MemberSync unlink by {ctx.author}")
            except discord.HTTPException:
                pass
        await ctx.send(
            f"{'✅ Unlinked' if removed else 'ℹ️ No link found for'} {member.mention}."
        )

    # -- background loops -------------------------------------------------------

    @tasks.loop(seconds=QUEUE_INTERVAL_SECONDS)
    async def queue_loop(self) -> None:
        try:
            guild = self._guild()
            if guild is None:
                return
            queued = await self.service.links.queue_all()
            if not queued:
                return
            names: dict[int, str | None] = {}
            for row in queued:
                member = guild.get_member(row["discord_id"])
                names[row["discord_id"]] = member.display_name if member else None
            for discord_id, outcome, mc_user_id in await self.service.process_queue(names):
                member = guild.get_member(discord_id)
                if member is None:
                    continue
                if outcome == "approved":
                    await self._grant_role(member, reason="MemberSync auto verified")
                    await self._dm(
                        member,
                        f"✅ Your MissionChief account `{mc_user_id}` has been "
                        "verified and linked to your Discord account!",
                    )
                elif outcome == "expired":
                    await self._dm(member, _EXPIRED_DM)
        except Exception:
            log.exception("membersync queue loop failed")

    @tasks.loop(hours=PRUNE_INTERVAL_HOURS)
    async def prune_loop(self) -> None:
        try:
            guild = self._guild()
            role = self._role(guild)
            if guild is None or role is None:
                return
            for discord_id, mc_user_id in await self.service.prune_candidates():
                member = guild.get_member(discord_id)
                if member is None or role not in member.roles:
                    continue
                try:
                    await member.remove_roles(
                        role, reason="MemberSync: left the alliance"
                    )
                except discord.HTTPException as exc:
                    log.warning("membersync: could not remove role from %s: %s",
                                member, exc)
                    continue
                log.info("membersync: removed verified role from %s (MC %s)",
                         member, mc_user_id)
                await self.bot.notify_admin(
                    f"👋 Removed Verified from {member.mention} — "
                    f"MC `{mc_user_id}` left the alliance."
                )
        except Exception:
            log.exception("membersync prune loop failed")

    @queue_loop.before_loop
    @prune_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()
