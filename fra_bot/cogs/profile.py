"""Member profiles: ``/profile`` to view (everyone), ``/profile-edit``
to manage your own sections via modals (admins may edit anyone's).

Sections: timezone + playtimes, bio, specialties, birthday, vehicles,
buildings. Every edit lands in the central member-action log, so admins
see profile changes in the per-member history and the action feed.
"""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from ..db.repos import GameSyncRepo, LinksRepo, MemberProfilesRepo, MembersRepo

log = logging.getLogger(__name__)

_BIRTHDAY_RE = re.compile(r"^(\d{1,2})-(\d{1,2})(?:-(\d{4}))?$")

#: section key -> (title, [(field, label, style, placeholder), ...])
SECTIONS: dict[str, tuple[str, list[tuple[str, str, discord.TextStyle, str]]]] = {
    "time": ("Timezone & playtimes", [
        ("timezone", "Timezone", discord.TextStyle.short,
         "e.g. America/New_York, EST, UTC-5"),
        ("playtimes", "Playtimes", discord.TextStyle.short,
         "e.g. weekdays 7-11 pm, weekends during the day"),
    ]),
    "bio": ("About me", [
        ("bio", "About me", discord.TextStyle.paragraph,
         "Introduce yourself to the alliance."),
    ]),
    "specialties": ("Specialties", [
        ("specialties", "Specialties", discord.TextStyle.paragraph,
         "e.g. fire, EMS, water rescue, mission planning"),
    ]),
    "birthday": ("Birthday", [
        ("birthday", "Birthday (DD-MM or DD-MM-YYYY)",
         discord.TextStyle.short, "e.g. 17-07 or 17-07-1990"),
    ]),
    "vehicles": ("Vehicles", [
        ("vehicles", "Your fleet", discord.TextStyle.paragraph,
         "e.g. 42 engines, 12 ladders, 8 ambulances — or your setup"),
    ]),
    "buildings": ("Buildings", [
        ("buildings", "Your buildings", discord.TextStyle.paragraph,
         "e.g. 30 fire stations, 4 hospitals, 2 academies"),
    ]),
}

_FIELD_LABELS = {
    "timezone": "🕐 Timezone", "playtimes": "🎮 Playtimes",
    "bio": "💬 About me", "specialties": "⭐ Specialties",
    "birthday": "🎂 Birthday", "vehicles": "🚒 Vehicles",
    "buildings": "🏢 Buildings",
}


def validate_birthday(raw: str) -> str | None:
    """Normalized 'DD-MM[-YYYY]' or None when invalid. Empty passes
    through as '' (clears the field)."""
    text = raw.strip()
    if not text:
        return ""
    match = _BIRTHDAY_RE.match(text)
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    suffix = f"-{match.group(3)}" if match.group(3) else ""
    return f"{day:02d}-{month:02d}{suffix}"


def _is_admin(bot, member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    allowed = set(bot.cfg.discord.admin_role_ids)
    return any(role.id in allowed for role in member.roles)


class SectionModal(discord.ui.Modal):
    """One modal per profile section, prefilled with the current values."""

    def __init__(self, cog: "ProfileCog", target: discord.abc.User,
                 section: str, current: dict) -> None:
        title, fields = SECTIONS[section]
        super().__init__(title=title[:45])
        self._cog = cog
        self._target = target
        self._section = section
        self._inputs: list[tuple[str, discord.ui.TextInput]] = []
        for field, label, style, placeholder in fields:
            text_input = discord.ui.TextInput(
                label=label[:45], style=style, required=False,
                default=(current.get(field) or "")[:1000],
                placeholder=placeholder[:100], max_length=1000,
            )
            self._inputs.append((field, text_input))
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values: dict[str, str] = {}
        for field, text_input in self._inputs:
            value = str(text_input.value or "")
            if field == "birthday":
                normalized = validate_birthday(value)
                if normalized is None:
                    await interaction.response.send_message(
                        "⚠️ Birthday not saved — use DD-MM or "
                        "DD-MM-YYYY (e.g. 17-07 or 17-07-1990).",
                        ephemeral=True,
                    )
                    return
                value = normalized
            values[field] = value
        await self._cog.profiles.set_fields(self._target.id, **values)
        changed = ", ".join(field for field, _ in self._inputs)
        # The action files under the TARGET (their history, their feed
        # line); an admin editing someone else's profile is named in the
        # detail so neither surface reads as a self-edit.
        by_admin = interaction.user.id != self._target.id
        await self._cog.bot.log_member_action(
            action="profile_updated",
            detail=f"section: {SECTIONS[self._section][0]}" + (
                f" (edited by {interaction.user.display_name})"
                if by_admin else ""
            ),
            discord_user_id=self._target.id,
            actor_name=self._target.display_name,
        )
        suffix = (
            "" if interaction.user.id == self._target.id
            else f" (for {self._target.display_name})"
        )
        await interaction.response.send_message(
            f"✅ Profile updated: **{SECTIONS[self._section][0]}**"
            f"{suffix} ({changed}).",
            ephemeral=True,
        )


class ProfileCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.profiles = MemberProfilesRepo(bot.db)

    # -- shared embed builder (also used by the dossier browse view) -------

    async def profile_embed(self, user: discord.abc.User) -> discord.Embed:
        row = await self.profiles.get(user.id)
        embed = discord.Embed(
            title=f"👤 Profile — {user.display_name}",
            colour=discord.Colour.blurple(),
        )
        try:
            embed.set_thumbnail(url=user.display_avatar.url)
        except Exception:  # noqa: BLE001 — avatars are cosmetic
            pass
        # MissionChief identity through the verified link + roster.
        link = await LinksRepo(self.bot.db).get_by_discord(user.id)
        if link is not None and link["status"] == "approved":
            roster = await MembersRepo(self.bot.db).active_members()
            member = roster.get(int(link["mc_user_id"]))
            if member is not None:
                # Roster columns can be NULL mid-sweep — never crash on them.
                credits = member["earned_credits"]
                rate = member["contribution_rate"]
                parts = [f"**{member['name']}** — {member['role'] or 'Member'}"]
                stats = []
                if credits is not None:
                    stats.append(f"Credits: {credits:,}")
                if rate is not None:
                    stats.append(f"contribution: {rate:g}%")
                if stats:
                    parts.append(" · ".join(stats))
                embed.add_field(
                    name="🎖️ MissionChief",
                    value="\n".join(parts)[:1024],
                    inline=False,
                )
            else:
                embed.add_field(
                    name="🎖️ MissionChief",
                    value=f"linked (id {link['mc_user_id']})", inline=False,
                )
        else:
            embed.add_field(
                name="🎖️ MissionChief",
                value="not linked — run `!verify`", inline=False,
            )
        # Game data the member's own userscript reported (real counts, in
        # contrast to the self-written vehicles/buildings notes below).
        sync = await GameSyncRepo(self.bot.db).get_by_discord(user.id)
        if sync is not None:
            import json as _json

            from ..services.game_sync import summarize_buildings

            try:
                by_type = (
                    _json.loads(sync["buildings_json"] or "{}").get("by_type")
                    or {}
                )
            except ValueError:
                by_type = {}
            value = (
                f"{sync['building_count']} buildings · "
                f"{sync['vehicle_count']} vehicles"
            )
            summary = summarize_buildings(by_type)
            if summary:
                value += f"\n{summary}"
            value += f"\n*synced {str(sync['synced_at'])[:16]}*"
            embed.add_field(name="🎮 Game data", value=value[:1024], inline=False)
        filled = 0
        if row is not None:
            # Discord caps the WHOLE embed at 6000 chars — seven fields of
            # 1000 (the modal limit) would blow past it and 400 every view.
            # Budget the remaining space across the filled fields.
            budget = 5400 - sum(
                len(str(part or ""))
                for part in (embed.title, embed.description)
            ) - sum(len(f.name or "") + len(f.value or "") for f in embed.fields)
            for field, label in _FIELD_LABELS.items():
                value = row[field]
                if not value:
                    continue
                room = min(1024, budget - len(label))
                if room <= 3:
                    break  # embed is full; later sections are cut, not crashed
                text = str(value)
                if len(text) > room:
                    text = text[: room - 1] + "…"
                filled += 1
                budget -= len(label) + len(text)
                embed.add_field(
                    name=label, value=text,
                    inline=field in ("timezone", "playtimes", "birthday"),
                )
            embed.set_footer(text=f"Last updated: {row['updated_at'][:16]}")
        if filled == 0:
            embed.description = (
                "*No profile information yet — fill in your profile with "
                "`/profile-edit`.*"
            )
        return embed

    # -- slash commands ------------------------------------------------------

    @app_commands.command(name="profile", description="View a profile")
    @app_commands.describe(member="Whose profile (empty = your own)")
    async def profile_view(
        self, interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        embed = await self.profile_embed(target)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="profile-edit", description="Edit your profile (per section)"
    )
    @app_commands.describe(
        section="Which section do you want to edit?",
        member="ADMINS ONLY: edit this member's profile",
    )
    @app_commands.choices(section=[
        app_commands.Choice(name=title, value=key)
        for key, (title, _) in SECTIONS.items()
    ])
    async def profile_edit(
        self, interaction: discord.Interaction,
        section: app_commands.Choice[str],
        member: discord.Member | None = None,
    ) -> None:
        target = interaction.user
        if member is not None and member.id != interaction.user.id:
            if not _is_admin(self.bot, interaction.user):
                await interaction.response.send_message(
                    "Only admins can edit another member's profile.",
                    ephemeral=True,
                )
                return
            target = member
        current_row = await self.profiles.get(target.id)
        current = dict(current_row) if current_row is not None else {}
        await interaction.response.send_modal(
            SectionModal(self, target, section.value, current)
        )


async def setup(bot) -> None:
    await bot.add_cog(ProfileCog(bot))
