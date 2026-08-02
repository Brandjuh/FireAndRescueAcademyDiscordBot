"""Sanctions register (reference bot: sanctionmanager, rebuilt).

Records sanctions with full history and statistics, announces them,
tells the member — and, unlike the reference cog, a Mute sanction sets
the REAL in-game chat ban (via SanctionService; behind the
``mute_execution_enabled`` switch until the route is verified live).
Escalation follows CoC section 5 in three modes: ``advisory`` (admin
text), ``button`` (admin embed with Mute/Kick/Dismiss buttons, the
default) and ``auto`` (the service acts after the configured gap).

Commands (admins): ``!sanction add <type> <lid> <reden>``, ``list``,
``stats``, ``revoke``. Types are the reference bot's labels, addressed
by short key (verbal, w1, w2, w3, kick, ban, mute, mute5m … mute14d).
The target may be a Discord @mention (the MC identity resolves through
the verified link) or a MissionChief name (resolved via the roster).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from ..db.repos import LinksRepo, MembersRepo, SanctionsRepo
from ..services.sanction_rules import (
    COC_RULES,
    advice_for,
    effective_status,
    find_reason_matches,
    ladder_advice,
    ladder_step,
    under_warning_until,
)
from .admin import is_fra_admin, is_fra_admin_ctx
from .display import profile_url
from .dossier import _staff_check

log = logging.getLogger(__name__)

PANEL_TITLE = "Sanction Management"

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


def _iso_unix(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        moment = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return int(moment.timestamp())


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
            label="Approve",
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


class ReviewEditButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fra:sreview:edit:(?P<field>type|reason|notes):(?P<sid>[0-9]+)",
):
    """Edit one field of a sanction in place (review notices and
    ``!sanction edit``). The sanction id travels in the custom_id — never
    in footer text (the reference bot's footer-parsing bug)."""

    _LABELS = {"type": "Edit type", "reason": "Edit reason", "notes": "Edit notes"}

    def __init__(self, field: str, sanction_id: int) -> None:
        super().__init__(discord.ui.Button(
            label=self._LABELS[field],
            style=discord.ButtonStyle.secondary,
            custom_id=f"fra:sreview:edit:{field}:{sanction_id}",
        ))
        self.field = field
        self.sanction_id = sanction_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["field"], int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("SanctionsCog")
        if cog is not None:
            await cog.handle_review_edit(interaction, self.field, self.sanction_id)


class EditTextModal(discord.ui.Modal, title="Edit sanction"):
    value = discord.ui.TextInput(
        label="Value", style=discord.TextStyle.paragraph, max_length=500,
    )

    def __init__(self, cog: "SanctionsCog", field: str, sanction_id: int,
                 current: str | None) -> None:
        super().__init__(title=f"Edit {field} — sanction #{sanction_id}")
        self._cog = cog
        self._field = field
        self._sanction_id = sanction_id
        self.value.label = field.capitalize()
        self.value.default = (current or "")[:500]
        self.value.required = field == "reason"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._cog.apply_review_edit(
            interaction, self._field, self._sanction_id, str(self.value),
        )


class _EditTypeSelect(discord.ui.Select):
    def __init__(self, cog: "SanctionsCog", sanction_id: int,
                 origin: discord.Message | None) -> None:
        self._cog = cog
        self._sanction_id = sanction_id
        self._origin = origin
        super().__init__(
            placeholder="New sanction type…",
            options=[
                discord.SelectOption(label=label[:100], value=label)
                for label in SANCTION_TYPE_KEYS.values()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._cog.apply_review_edit(
            interaction, "type", self._sanction_id, self.values[0],
            origin=self._origin,
        )


def _review_view(sanction_id: int, *, include_resolution: bool = True) -> discord.ui.View:
    """The full review button set: Approve / Edit type / Edit reason /
    Edit notes / Dismiss. ``include_resolution=False`` gives just the
    edit buttons (``!sanction edit`` on an already-settled record)."""
    view = discord.ui.View(timeout=None)
    if include_resolution:
        view.add_item(ReviewConfirmButton(sanction_id))
    view.add_item(ReviewEditButton("type", sanction_id))
    view.add_item(ReviewEditButton("reason", sanction_id))
    view.add_item(ReviewEditButton("notes", sanction_id))
    if include_resolution:
        view.add_item(ReviewDismissButton(sanction_id))
    return view


class EscalationActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fra:sesc:(?P<verb>mute|kick|dismiss):(?P<sid>[0-9]+)",
):
    """One button on an escalation notice (button mode). ``sid`` is the
    sanction that triggered the step — identity resolves through it, so
    the button survives restarts without any in-memory state."""

    _STYLES = {
        "mute": ("Mute now", discord.ButtonStyle.primary),
        "kick": ("Kick from alliance", discord.ButtonStyle.danger),
        "dismiss": ("Dismiss", discord.ButtonStyle.secondary),
    }

    def __init__(self, verb: str, sanction_id: int) -> None:
        label, style = self._STYLES[verb]
        super().__init__(discord.ui.Button(
            label=label, style=style,
            custom_id=f"fra:sesc:{verb}:{sanction_id}",
        ))
        self.verb = verb
        self.sanction_id = sanction_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["verb"], int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("SanctionsCog")
        if cog is not None:
            await cog.handle_escalation_action(
                interaction, self.verb, self.sanction_id,
            )


def _escalation_view(step: str, sanction_id: int) -> discord.ui.View:
    """CoC 5.2 (second) offers Mute/Kick/Dismiss; the 5.3 removal step
    (final) offers Kick/Dismiss."""
    view = discord.ui.View(timeout=None)
    if step != "final":
        view.add_item(EscalationActionButton("mute", sanction_id))
    view.add_item(EscalationActionButton("kick", sanction_id))
    view.add_item(EscalationActionButton("dismiss", sanction_id))
    return view


# -- the sanction wizard (panel/context-menu flow; all steps ephemeral) -----

@dataclass
class SanctionDraft:
    """State carried through the wizard steps."""
    mc_user_id: int | None = None
    name: str | None = None
    discord_id: int | None = None
    rule_code: str | None = None      # None = free-text reason
    reason: str | None = None
    sanction_type: str | None = None
    notes: str | None = None


async def _wizard_guard(interaction: discord.Interaction) -> bool:
    if _staff_check(interaction.client, interaction.user):
        return True
    await interaction.response.send_message(
        "You don't have permission to do this.", ephemeral=True
    )
    return False


def type_options(rule_code: str | None) -> list[discord.SelectOption]:
    """The 16 reference types; the CoC-advised one (if any) first and
    marked, so the wizard preselects the advice without forcing it."""
    advice = advice_for(rule_code)
    advised = advice[0] if advice else None
    ordered = ([advised] if advised in SANCTION_TYPE_KEYS.values() else []) + [
        label for label in SANCTION_TYPE_KEYS.values() if label != advised
    ]
    return [
        discord.SelectOption(
            label=(f"{label} (advised)" if label == advised else label)[:100],
            value=label,
        )
        for label in ordered[:25]
    ]


def repeat_banner(rows, rule_code: str | None, threshold: int = 3) -> str | None:
    """The reference bot's repeated-offense banner: several warnings for
    the SAME CoC rule is a strong escalation signal."""
    if not rule_code:
        return None
    same = sum(
        1 for r in rows
        if r["status"] in ("active", "expired")
        and (r["reason_category"] or "") == rule_code
    )
    if same < threshold:
        return None
    return (
        f"⚠️ **{same}× rule {rule_code} already on record** — consider "
        "escalating instead of another warning (CoC section 5)."
    )


class SanctionMemberModal(discord.ui.Modal, title="Sanction a member"):
    query = discord.ui.TextInput(
        label="MC name, MC id or Discord id",
        placeholder="e.g. DutchFireFighter or 123456",
        max_length=100,
    )

    def __init__(self, cog: "SanctionsCog", mode: str) -> None:
        super().__init__(
            title="Sanction a member" if mode == "create" else "Member history"
        )
        self._cog = cog
        self._mode = mode

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self._mode == "create":
            await self._cog.start_wizard(interaction, str(self.query))
        else:
            await self._cog.show_history(interaction, str(self.query))


class _MemberSelect(discord.ui.Select):
    def __init__(self, cog: "SanctionsCog", candidates, mode: str) -> None:
        self._cog = cog
        self._mode = mode
        self._by_id = {str(c.mc_user_id): c for c in candidates[:25]}
        super().__init__(
            placeholder="Select the member…",
            options=[
                discord.SelectOption(
                    label=c.name[:100], value=str(c.mc_user_id),
                    description=(
                        f"MC {c.mc_user_id}"
                        + ("" if c.is_active else " · left the alliance")
                    )[:100],
                )
                for c in candidates[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _wizard_guard(interaction):
            return
        c = self._by_id[self.values[0]]
        if self._mode == "history":
            await self._cog.show_history_for(
                interaction, mc_user_id=c.mc_user_id, name=c.name,
                discord_id=c.discord_id, edit=True,
            )
            return
        draft = SanctionDraft(
            mc_user_id=c.mc_user_id, name=c.name, discord_id=c.discord_id,
        )
        await self._cog.wizard_reason_step(interaction, draft)


class _ReasonSelect(discord.ui.Select):
    def __init__(self, cog: "SanctionsCog", draft: SanctionDraft, rules) -> None:
        self._cog = cog
        self._draft = draft
        super().__init__(
            placeholder="Pick the CoC rule…",
            options=[
                discord.SelectOption(
                    label=f"{rule.code} — {rule.title}"[:100],
                    value=rule.code,
                    description=(
                        (advice_for(rule.code) or (rule.text,))[0]
                    )[:100],
                )
                for rule in rules[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _wizard_guard(interaction):
            return
        rule = COC_RULES[self.values[0]]
        self._draft.rule_code = rule.code
        self._draft.reason = f"{rule.code}. {rule.title}"
        await self._cog.wizard_type_step(interaction, self._draft)


class ReasonSearchModal(discord.ui.Modal, title="Search CoC rules"):
    query = discord.ui.TextInput(
        label="Keyword (e.g. racism, drama, donation)", max_length=60,
    )

    def __init__(self, cog: "SanctionsCog", draft: SanctionDraft) -> None:
        super().__init__()
        self._cog = cog
        self._draft = draft

    async def on_submit(self, interaction: discord.Interaction) -> None:
        matches = [rule for _, rule in find_reason_matches(str(self.query), 10)]
        if not matches:
            matches = list(COC_RULES.values())
        await self._cog.wizard_reason_step(
            interaction, self._draft, rules=matches, edit=True,
        )


class FreeReasonModal(discord.ui.Modal, title="Custom reason"):
    reason = discord.ui.TextInput(
        label="Reason (free text)", style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, cog: "SanctionsCog", draft: SanctionDraft) -> None:
        super().__init__()
        self._cog = cog
        self._draft = draft

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._draft.rule_code = None
        self._draft.reason = str(self.reason)
        await self._cog.wizard_type_step(interaction, self._draft)


class ReasonStepView(discord.ui.View):
    def __init__(self, cog: "SanctionsCog", draft: SanctionDraft, rules) -> None:
        super().__init__(timeout=600)
        self._cog = cog
        self._draft = draft
        self.add_item(_ReasonSelect(cog, draft, rules))

    @discord.ui.button(label="Search reason", style=discord.ButtonStyle.secondary)
    async def search_reason(self, interaction, button) -> None:
        if not await _wizard_guard(interaction):
            return
        await interaction.response.send_modal(
            ReasonSearchModal(self._cog, self._draft)
        )

    @discord.ui.button(label="Other reason", style=discord.ButtonStyle.secondary)
    async def other_reason(self, interaction, button) -> None:
        if not await _wizard_guard(interaction):
            return
        await interaction.response.send_modal(
            FreeReasonModal(self._cog, self._draft)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button) -> None:
        await interaction.response.edit_message(
            content="Wizard cancelled.", embed=None, view=None,
        )


class _TypeSelect(discord.ui.Select):
    def __init__(self, cog: "SanctionsCog", draft: SanctionDraft) -> None:
        self._cog = cog
        self._draft = draft
        super().__init__(
            placeholder="Pick the sanction type…",
            options=type_options(draft.rule_code),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _wizard_guard(interaction):
            return
        self._draft.sanction_type = self.values[0]
        await self._cog.wizard_summary_step(interaction, self._draft)


class TypeStepView(discord.ui.View):
    def __init__(self, cog: "SanctionsCog", draft: SanctionDraft) -> None:
        super().__init__(timeout=600)
        self.add_item(_TypeSelect(cog, draft))
        self._cog = cog
        self._draft = draft

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button) -> None:
        await interaction.response.edit_message(
            content="Wizard cancelled.", embed=None, view=None,
        )


class NotesModal(discord.ui.Modal, title="Add notes"):
    notes = discord.ui.TextInput(
        label="Internal notes (staff only)",
        style=discord.TextStyle.paragraph, max_length=500, required=False,
    )

    def __init__(self, cog: "SanctionsCog", draft: SanctionDraft) -> None:
        super().__init__()
        self._cog = cog
        self._draft = draft
        self.notes.default = draft.notes or ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._draft.notes = str(self.notes) or None
        await self._cog.wizard_summary_step(interaction, self._draft)


class SummaryView(discord.ui.View):
    def __init__(self, cog: "SanctionsCog", draft: SanctionDraft) -> None:
        super().__init__(timeout=600)
        self._cog = cog
        self._draft = draft

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.danger)
    async def submit(self, interaction, button) -> None:
        if not await _wizard_guard(interaction):
            return
        await self._cog.wizard_submit(interaction, self._draft)

    @discord.ui.button(label="Add notes", style=discord.ButtonStyle.secondary)
    async def add_notes(self, interaction, button) -> None:
        if not await _wizard_guard(interaction):
            return
        await interaction.response.send_modal(
            NotesModal(self._cog, self._draft)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button) -> None:
        await interaction.response.edit_message(
            content="Wizard cancelled — nothing recorded.",
            embed=None, view=None,
        )


class SanctionPanelView(discord.ui.View):
    def __init__(self, cog: "SanctionsCog") -> None:
        super().__init__(timeout=None)
        self._cog = cog

    @discord.ui.button(
        label="Create sanction", style=discord.ButtonStyle.danger,
        custom_id="fra:spanel:create", emoji="⚖️",
    )
    async def create(self, interaction, button) -> None:
        if not await _wizard_guard(interaction):
            return
        await interaction.response.send_modal(
            SanctionMemberModal(self._cog, "create")
        )

    @discord.ui.button(
        label="Member history", style=discord.ButtonStyle.secondary,
        custom_id="fra:spanel:history", emoji="📋",
    )
    async def history(self, interaction, button) -> None:
        if not await _wizard_guard(interaction):
            return
        await interaction.response.send_modal(
            SanctionMemberModal(self._cog, "history")
        )

    @discord.ui.button(
        label="Statistics", style=discord.ButtonStyle.secondary,
        custom_id="fra:spanel:stats", emoji="📊",
    )
    async def stats(self, interaction, button) -> None:
        if not await _wizard_guard(interaction):
            return
        cog = self._cog
        embed = await cog.stats_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)


class SanctionsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.repo = SanctionsRepo(bot.db)
        bot.add_dynamic_items(
            ReviewConfirmButton, ReviewDismissButton, ReviewEditButton,
            EscalationActionButton,
        )
        service = getattr(bot, "sanction_service", None)
        if service is not None:
            # Escalation/auto actions notify through the same DM→in-game
            # fallback path as command-issued sanctions.
            service.notify_member = self._notify_member

    async def _resolve_target(
        self, ctx: commands.Context, target: str
    ) -> tuple[int | None, str | None, int | None]:
        return await resolve_member_target(self.bot, ctx, target)

    # -- wizard steps (ephemeral; entered from panel/context menu) ----------

    async def _search_candidates(self, query: str):
        from ..services.dossier import DossierService

        return await DossierService(self.bot.db).search(query)

    async def start_wizard(
        self, interaction: discord.Interaction, query: str, *,
        draft: SanctionDraft | None = None,
    ) -> None:
        """Entry: resolve the member, then walk reason → type → summary."""
        if draft is not None and draft.mc_user_id is not None:
            await self.wizard_reason_step(interaction, draft, edit=False)
            return
        candidates = await self._search_candidates(query)
        if not candidates:
            await interaction.response.send_message(
                f"No member found for `{query}` — try the exact MC name or id.",
                ephemeral=True,
            )
            return
        if len(candidates) > 1 and candidates[0].score <= candidates[1].score:
            view = discord.ui.View(timeout=600)
            view.add_item(_MemberSelect(self, candidates, "create"))
            await interaction.response.send_message(
                "Multiple matches — pick the member:", view=view,
                ephemeral=True,
            )
            return
        c = candidates[0]
        await self.wizard_reason_step(
            interaction,
            SanctionDraft(
                mc_user_id=c.mc_user_id, name=c.name, discord_id=c.discord_id,
            ),
            edit=False,
        )

    def _wizard_embed(self, draft: SanctionDraft, step: str) -> discord.Embed:
        embed = discord.Embed(
            title=f"⚖️ Sanction wizard — {draft.name}",
            colour=discord.Colour.orange(),
            description=step,
        )
        if draft.reason:
            embed.add_field(name="Reason", value=draft.reason[:1024], inline=False)
        if draft.sanction_type:
            embed.add_field(name="Type", value=draft.sanction_type)
        if draft.notes:
            embed.add_field(name="Notes", value=draft.notes[:1024], inline=False)
        return embed

    async def wizard_reason_step(
        self, interaction: discord.Interaction, draft: SanctionDraft, *,
        rules=None, edit: bool = True,
    ) -> None:
        advice_hint = (
            "Pick the Code of Conduct rule (the select shows the advised "
            "sanction per rule), search it, or write a free-text reason."
        )
        embed = self._wizard_embed(draft, advice_hint)
        view = ReasonStepView(self, draft, rules or list(COC_RULES.values()))
        if edit:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True,
            )

    async def wizard_type_step(
        self, interaction: discord.Interaction, draft: SanctionDraft,
    ) -> None:
        advice = advice_for(draft.rule_code)
        hint = "Pick the sanction type."
        if advice:
            hint += f"\n**CoC advice:** {advice[0]} — {advice[1]}"
        await interaction.response.edit_message(
            embed=self._wizard_embed(draft, hint),
            view=TypeStepView(self, draft),
        )

    async def wizard_summary_step(
        self, interaction: discord.Interaction, draft: SanctionDraft,
    ) -> None:
        rows = await self.repo.for_member(
            mc_user_id=draft.mc_user_id, discord_user_id=draft.discord_id,
            name=draft.name, limit=200,
        )
        offenses = await self.repo.offense_count(
            mc_user_id=draft.mc_user_id, discord_user_id=draft.discord_id,
            name=draft.name,
        )
        lines = ["Check the summary, then **Submit**."]
        if offenses:
            lines.append(
                f"Current CoC offense position: **{offenses}** — next: "
                f"{ladder_advice(offenses + 1, self.bot.cfg.automation.sanctions.escalation_offense_threshold)}"
            )
        banner = repeat_banner(rows, draft.rule_code)
        if banner:
            lines.append(banner)
        embed = self._wizard_embed(draft, "\n".join(lines))
        await interaction.response.edit_message(
            embed=embed, view=SummaryView(self, draft),
        )

    async def wizard_submit(
        self, interaction: discord.Interaction, draft: SanctionDraft,
    ) -> None:
        await interaction.response.defer()
        summary = await self._issue_full(
            mc_user_id=draft.mc_user_id, name=draft.name,
            discord_id=draft.discord_id, admin_id=interaction.user.id,
            admin_name=interaction.user.display_name,
            sanction_type=draft.sanction_type or "Warning - Verbal warning",
            reason=draft.reason or "(no reason given)",
            reason_category=draft.rule_code, notes=draft.notes,
            source="panel",
        )
        await interaction.edit_original_response(
            content=summary, embed=None, view=None,
        )

    # -- member history (panel button) --------------------------------------

    async def show_history(
        self, interaction: discord.Interaction, query: str,
    ) -> None:
        candidates = await self._search_candidates(query)
        if not candidates:
            await interaction.response.send_message(
                f"No member found for `{query}`.", ephemeral=True,
            )
            return
        if len(candidates) > 1 and candidates[0].score <= candidates[1].score:
            view = discord.ui.View(timeout=600)
            view.add_item(_MemberSelect(self, candidates, "history"))
            await interaction.response.send_message(
                "Multiple matches — pick the member:", view=view,
                ephemeral=True,
            )
            return
        c = candidates[0]
        await self.show_history_for(
            interaction, mc_user_id=c.mc_user_id, name=c.name,
            discord_id=c.discord_id, edit=False,
        )

    async def show_history_for(
        self, interaction: discord.Interaction, *, mc_user_id: int | None,
        name: str | None, discord_id: int | None, edit: bool,
    ) -> None:
        rows = await self.repo.for_member(
            mc_user_id=mc_user_id, discord_user_id=discord_id, name=name,
            limit=25,
        )
        offenses = await self.repo.offense_count(
            mc_user_id=mc_user_id, discord_user_id=discord_id, name=name,
        )
        under = under_warning_until(rows)
        lines = [
            f"`#{r['id']}` {r['created_at'][:10]} — "
            f"{r['sanction_type']} — {r['reason'][:60]}"
            + (
                f" *({effective_status(r)})*"
                if effective_status(r) != "active" else ""
            )
            for r in rows
        ] or ["*No sanctions on record.*"]
        badge = f"\n🟠 Under warning until {under[:10]} (CoC 5.1)" if under else ""
        embed = discord.Embed(
            title=f"📋 Sanctions — {name}",
            colour=discord.Colour.orange(),
            description=(
                f"CoC offense position: **{offenses}**{badge}\n\n"
                + "\n".join(lines)
            )[:4096],
        )
        if edit:
            await interaction.response.edit_message(
                content=None, embed=embed, view=None,
            )
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- statistics ---------------------------------------------------------

    async def stats_embed(self) -> discord.Embed:
        by_status = await self.repo.status_summary()
        type_rows = await self.repo.stats()
        admins = await self.repo.admin_leaderboard()
        members = await self.repo.member_leaderboard()
        embed = discord.Embed(
            title="📊 Sanction statistics",
            colour=discord.Colour.blurple(),
            description=" · ".join(
                f"{status}: **{n}**"
                for status, n in sorted(by_status.items())
            ) or "No sanctions recorded yet.",
        )
        if type_rows:
            embed.add_field(
                name="By type",
                value="\n".join(
                    f"- {r['sanction_type']}"
                    + (f" ({r['status']})" if r["status"] != "active" else "")
                    + f": **{r['n']}**"
                    for r in type_rows[:12]
                )[:1024],
                inline=False,
            )
        if admins:
            embed.add_field(
                name="Top recording admins",
                value="\n".join(
                    f"- {r['admin_name']}: {r['n']}" for r in admins
                )[:1024],
            )
        if members:
            embed.add_field(
                name="Most sanctioned",
                value="\n".join(
                    f"- {r['name']}: {r['n']}" for r in members
                )[:1024],
            )
        return embed

    # -- panel (posted/maintained by the panel keeper) ----------------------

    def panel_embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"⚖️ {PANEL_TITLE}",
            colour=discord.Colour.dark_red(),
            description=(
                "Record and manage member sanctions against the alliance "
                "Code of Conduct.\n\n"
                "**Create sanction** walks through member → CoC rule → "
                "type (with the advised sanction preselected) → summary.\n"
                "**Member history** shows one member's record and CoC "
                "offense position.\n**Statistics** shows the register "
                "totals. Staff only; everything opens privately."
            ),
        )

    def panel_view(self) -> discord.ui.View:
        return SanctionPanelView(self)

    @commands.command(name="sanctionpanel")
    async def sanction_panel(self, ctx: commands.Context) -> None:
        """(Re)post the sanction-management panel in its channel."""
        if not is_fra_admin_ctx(ctx):
            await ctx.send("⛔ You don't have permission to use that command.")
            return
        keeper = self.bot.get_cog("PanelKeeperCog")
        if keeper is None:
            await ctx.send("Panel keeper not loaded.")
            return
        channel_id = getattr(self.bot.cfg.discord.channels, "sanction_panel", 0)
        channel = self.bot.get_channel(channel_id) if channel_id else ctx.channel
        if channel is None:
            await ctx.send(
                "⚠️ Set the panel channel first: `!fra set sanction_panel <id>`."
            )
            return
        outcome = await keeper.ensure("sanctions", channel=channel, force=True)
        await ctx.send(f"✅ Sanction panel {outcome} in {channel.mention}.")

    # -- context menu -------------------------------------------------------

    async def sanction_member_menu(
        self, interaction: discord.Interaction, member: discord.Member,
    ) -> None:
        """Right-click → Apps → Sanction member: the wizard with the
        member preselected (MC identity via the verified link)."""
        if not await _wizard_guard(interaction):
            return
        link = await LinksRepo(self.bot.db).get_by_discord(member.id)
        mc_user_id = (
            int(link["mc_user_id"])
            if link is not None and link["status"] == "approved" else None
        )
        name = member.display_name
        if mc_user_id is not None:
            roster = await MembersRepo(self.bot.db).active_members()
            row = roster.get(mc_user_id)
            if row is not None:
                name = row["name"]
        await self.start_wizard(
            interaction, name,
            draft=SanctionDraft(
                mc_user_id=mc_user_id, name=name, discord_id=member.id,
            ) if mc_user_id is not None else None,
        )

    # -- commands ------------------------------------------------------------

    @commands.group(name="sanction", aliases=["sanctions"], invoke_without_command=True)
    @is_fra_admin()
    async def sanction(self, ctx: commands.Context) -> None:
        keys = ", ".join(sorted(SANCTION_TYPE_KEYS))
        await ctx.send(
            "Sanctions register — subcommands: `add <type> <lid> <reden>`, "
            "`list <lid>`, `recent`, `view <id>`, `edit <id>`, `stats`, "
            "`revoke <id>`, `reviewscan`. Panel: `!sanctionpanel`.\n"
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
        summary = await self._issue_full(
            mc_user_id=mc_user_id, name=name, discord_id=discord_id,
            admin_id=ctx.author.id, admin_name=ctx.author.display_name,
            sanction_type=sanction_type, reason=reason,
        )
        await ctx.send(summary)

    async def _issue_full(
        self, *, mc_user_id: int | None, name: str | None,
        discord_id: int | None, admin_id: int, admin_name: str,
        sanction_type: str, reason: str, reason_category: str | None = None,
        notes: str | None = None, source: str = "manual",
    ) -> str:
        """The one issue path shared by the command, the wizard and the
        context menu: register (+ real mute), announce, notify the
        member, dossier action, escalation follow-up. Returns the
        summary line for the invoker."""
        result = await self.bot.sanction_service.issue(
            mc_user_id=mc_user_id, mc_username=name, discord_user_id=discord_id,
            admin_discord_id=admin_id, admin_name=admin_name,
            sanction_type=sanction_type, reason=reason,
            reason_category=reason_category, notes=notes, source=source,
        )
        sanction_id = result["sanction_id"]
        offenses = result["offense_count"]
        under_until = None
        if offenses:
            rows = await self.repo.for_member(
                mc_user_id=mc_user_id, discord_user_id=discord_id, name=name,
                limit=100,
            )
            under_until = under_warning_until(rows)
        await self._announce(
            sanction_id, sanction_type, name, reason, admin_name, offenses,
            expires_at=result["expires_at"], mute_note=result["mute_note"],
            under_until=under_until,
        )
        await self._notify_member(discord_id, name, sanction_type, reason)
        await self.bot.log_member_action(
            action="sanction_received",
            detail=f"#{sanction_id} {sanction_type} — {reason[:120]} "
                   f"(by {admin_name})",
            discord_user_id=discord_id, mc_user_id=mc_user_id,
            actor_name=name,
        )
        await self._post_escalation(
            result["escalation"], sanction_id=sanction_id, name=name,
        )
        note = f" — CoC offense position **{offenses}**" if offenses else ""
        unknown = "" if mc_user_id or discord_id else " (⚠️ not on the roster)"
        mute_line = f"\n{result['mute_note']}" if result["mute_note"] else ""
        return (
            f"✅ Sanction **#{sanction_id}** recorded: {sanction_type} for "
            f"**{name}**{unknown}.{note}{mute_line}"
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
        await ctx.send(embed=await self.stats_embed())

    @sanction.command(name="view")
    @is_fra_admin()
    async def sanction_view(self, ctx: commands.Context, sanction_id: int) -> None:
        """Full detail of one sanction, including notes and history."""
        row = await self.repo.get(sanction_id)
        if row is None:
            await ctx.send(f"⚠️ Sanction #{sanction_id} does not exist.")
            return
        status = effective_status(row)
        embed = discord.Embed(
            title=f"⚖️ Sanction #{sanction_id} — {row['sanction_type']}"[:256],
            colour=type_colour(row["sanction_type"]),
            description=(
                f"**Member:** {row['mc_username'] or '?'}"
                + (f" (<@{row['discord_user_id']}>)"
                   if row["discord_user_id"] else "")
                + f"\n**Status:** {status}"
                + f"\n**Source:** {row['source']}"
            )[:4096],
        )
        embed.add_field(name="Type", value=row["sanction_type"])
        embed.add_field(
            name="Reason",
            value=(
                (f"[{row['reason_category']}] " if row["reason_category"] else "")
                + (row["reason"] or "—")
            )[:1024],
            inline=False,
        )
        if row["notes"]:
            embed.add_field(name="Notes", value=row["notes"][:1024], inline=False)
        detail = [f"Recorded by {row['admin_name']} on {row['created_at'][:16]}"]
        if row["expires_at"]:
            detail.append(f"Expires: {row['expires_at'][:16]}")
        if row["edited_at"]:
            detail.append(f"Edited by {row['edited_by']} on {row['edited_at'][:16]}")
        if row["revoked_at"]:
            detail.append(f"Settled by {row['revoked_by']} on {row['revoked_at'][:16]}")
        embed.add_field(name="Record", value="\n".join(detail)[:1024], inline=False)
        history = await self.repo.history(sanction_id)
        if history:
            embed.add_field(
                name="History",
                value="\n".join(
                    f"`{h['created_at'][:16]}` {h['action']} — {h['actor']}"
                    + (f": {h['detail'][:60]}" if h["detail"] else "")
                    for h in history[-10:]
                )[:1024],
                inline=False,
            )
        await ctx.send(embed=embed)

    @sanction.command(name="edit")
    @is_fra_admin()
    async def sanction_edit(self, ctx: commands.Context, sanction_id: int) -> None:
        """Post the edit buttons for a sanction (same set as the review
        flow; Approve/Dismiss included while it is still unverified)."""
        row = await self.repo.get(sanction_id)
        if row is None:
            await ctx.send(f"⚠️ Sanction #{sanction_id} does not exist.")
            return
        embed = discord.Embed(
            title=f"✏️ Edit sanction #{sanction_id}"[:256],
            colour=type_colour(row["sanction_type"]),
            description=f"**Member:** {row['mc_username'] or '?'}"[:4096],
        )
        embed.add_field(name="Type", value=row["sanction_type"])
        embed.add_field(name="Reason", value=(row["reason"] or "—")[:1024],
                        inline=False)
        if row["notes"]:
            embed.add_field(name="Notes", value=row["notes"][:1024], inline=False)
        await ctx.send(
            embed=embed,
            view=_review_view(
                sanction_id,
                include_resolution=row["status"] == "unverified",
            ),
        )

    @sanction.command(name="revoke")
    @is_fra_admin()
    async def sanction_revoke(
        self, ctx: commands.Context, sanction_id: int
    ) -> None:
        row = await self.repo.get(sanction_id)
        if row is None:
            await ctx.send(f"⚠️ Sanction #{sanction_id} does not exist.")
            return
        # The service lifts a still-running mute in the game first; when
        # that fails the sanction stays active (register = game).
        ok, note = await self.bot.sanction_service.revoke(
            sanction_id, revoked_by=ctx.author.display_name,
        )
        if not ok:
            await ctx.send(f"⚠️ Sanction #{sanction_id}: {note}")
            return
        await ctx.send(
            f"↩️ Sanction **#{sanction_id}** ({row['sanction_type']} for "
            f"**{row['mc_username']}**) revoked.{note}"
        )
        await self.bot.notify_admin(
            f"↩️ Sanction #{sanction_id} ({row['sanction_type']} for "
            f"**{row['mc_username']}**) revoked by "
            f"{ctx.author.display_name}.{note}"
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
        verb = "Approved" if confirm else "Dismissed"
        if confirm:
            # Dossier entry on APPROVE only — parity with `!sanction add`.
            await self.bot.log_member_action(
                action="sanction_received",
                detail=f"#{sanction_id} {row['sanction_type']} — "
                       f"{row['reason'][:120]} (game log, approved by "
                       f"{interaction.user.display_name})",
                discord_user_id=row["discord_user_id"],
                mc_user_id=row["mc_user_id"],
                actor_name=row["mc_username"],
            )
            # An approved game-log warning/mute is a real CoC offense —
            # run the same escalation follow-up as a fresh sanction.
            if str(row["sanction_type"]).startswith(("Warning", "Mute")):
                count = await self.repo.offense_count(
                    mc_user_id=row["mc_user_id"],
                    discord_user_id=row["discord_user_id"],
                    name=row["mc_username"],
                )
                if count >= 2:
                    threshold = (
                        self.bot.cfg.automation.sanctions
                        .escalation_offense_threshold
                    )
                    await self._post_escalation(
                        {
                            "count": count,
                            "step": ladder_step(count, threshold),
                            "advice": ladder_advice(count, threshold),
                            "mode": self.bot.cfg.automation.sanctions
                                    .escalation_mode,
                        },
                        sanction_id=sanction_id, name=row["mc_username"],
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

    # -- review edits (Edit type / Edit reason / Edit notes) -------------------

    async def handle_review_edit(
        self, interaction: discord.Interaction, field: str, sanction_id: int,
    ) -> None:
        from .automation import _is_admin_interaction

        if not _is_admin_interaction(interaction):
            await interaction.response.send_message(
                "You don't have permission to do this.", ephemeral=True
            )
            return
        row = await self.repo.get(sanction_id)
        if row is None:
            await interaction.response.send_message(
                f"⚠️ Sanction #{sanction_id} no longer exists.", ephemeral=True
            )
            return
        if field == "type":
            view = discord.ui.View(timeout=600)
            view.add_item(_EditTypeSelect(self, sanction_id, interaction.message))
            await interaction.response.send_message(
                f"New type for sanction #{sanction_id} "
                f"(now: {row['sanction_type']}):",
                view=view, ephemeral=True,
            )
            return
        current = row["reason"] if field == "reason" else row["notes"]
        await interaction.response.send_modal(
            EditTextModal(self, field, sanction_id, current)
        )

    async def apply_review_edit(
        self, interaction: discord.Interaction, field: str, sanction_id: int,
        value: str, *, origin: discord.Message | None = None,
    ) -> None:
        from ..services.sanction_rules import mute_expiry

        kwargs: dict = {}
        if field == "type":
            kwargs["sanction_type"] = value
            # A new timed-mute type gets a real expiry from now; other
            # types keep whatever was stored (edit() ignores None).
            expires = mute_expiry(value)
            if expires is not None:
                kwargs["expires_at"] = expires
        elif field == "reason":
            kwargs["reason"] = value
        else:
            kwargs["notes"] = value
        ok = await self.repo.edit(
            sanction_id, actor=interaction.user.display_name, **kwargs,
        )
        if not ok:
            await interaction.response.send_message(
                f"⚠️ Could not edit sanction #{sanction_id}.", ephemeral=True
            )
            return
        row = await self.repo.get(sanction_id)
        # Refresh the message the button lived on (the review notice or
        # the `!sanction edit` card); for the type-select the origin was
        # captured at click time, for modals it IS interaction.message.
        target = origin or interaction.message
        refreshed = False
        if target is not None and target.embeds:
            embed = self._apply_review_edits(target.embeds[0], row)
            try:
                if origin is not None:
                    await origin.edit(embed=embed)
                    await interaction.response.edit_message(
                        content=f"✅ Type updated for sanction #{sanction_id}.",
                        view=None,
                    )
                else:
                    await interaction.response.edit_message(embed=embed)
                refreshed = True
            except discord.HTTPException as exc:
                log.warning("review edit refresh failed: %s", exc)
        if not refreshed and not interaction.response.is_done():
            await interaction.response.send_message(
                f"✅ Sanction #{sanction_id} updated ({field}).",
                ephemeral=True,
            )

    @staticmethod
    def _apply_review_edits(embed: discord.Embed, row) -> discord.Embed:
        """Reflect the stored values on an existing notice embed."""
        def set_field(name: str, value: str) -> None:
            for i, field in enumerate(embed.fields):
                if field.name == name:
                    embed.set_field_at(i, name=name, value=value[:1024],
                                       inline=field.inline)
                    return
            embed.add_field(name=name, value=value[:1024], inline=False)

        type_field = (
            "Detected action"
            if any(f.name == "Detected action" for f in embed.fields)
            else "Type"
        )
        set_field(type_field, row["sanction_type"])
        set_field("Reason", row["reason"] or "—")
        if row["notes"]:
            set_field("Notes", row["notes"])
        return embed

    # -- escalation (CoC section 5) --------------------------------------------

    async def _post_escalation(
        self, esc: dict | None, *, sanction_id: int, name: str | None,
    ) -> None:
        """Post the CoC-5 follow-up for a member on/over an escalation
        step, in the configured mode."""
        if not esc:
            return
        cfg = self.bot.cfg.automation.sanctions
        if esc["mode"] == "advisory":
            await self.bot.notify_admin(
                f"⚖️ **{name}** — {esc['advice']} (manual action required)"
            )
            return
        if esc["mode"] == "auto":
            await self.bot.notify_admin(
                f"⚖️ **{name}** — {esc['advice']}\n"
                f"Auto mode: the bot acts in ~{cfg.escalation_gap_hours}h "
                "unless the offense is revoked or dismissed first."
            )
            return
        channel = (
            self.bot.channel_for("sanctions") or self.bot.channel_for("admin_log")
        )
        if channel is None:
            return
        final = esc["step"] == "final"
        embed = discord.Embed(
            title="⚖️ CoC escalation step reached",
            colour=discord.Colour.red() if final else discord.Colour.orange(),
            description=f"**Member:** {name}\n{esc['advice']}"[:4096],
        )
        if not final:
            embed.add_field(
                name="Mute button",
                value=f"Issues **{cfg.escalation_mute_type}** — a real "
                      "in-game chat ban once execution is enabled.",
                inline=False,
            )
        embed.set_footer(text=f"Triggered by sanction #{sanction_id}")
        try:
            await channel.send(
                embed=embed, view=_escalation_view(esc["step"], sanction_id),
            )
        except discord.HTTPException as exc:
            log.warning("escalation notice failed: %s", exc)

    async def handle_escalation_action(
        self, interaction: discord.Interaction, verb: str, sanction_id: int,
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
        by = interaction.user.display_name
        mc_user_id = (
            int(row["mc_user_id"]) if row["mc_user_id"] is not None else None
        )
        discord_id = (
            int(row["discord_user_id"])
            if row["discord_user_id"] is not None else None
        )
        if verb == "dismiss":
            outcome = (
                "👌 Escalation dismissed — no action taken (the offenses "
                "stay on record)."
            )
        else:
            count = await self.repo.offense_count(
                mc_user_id=mc_user_id, discord_user_id=discord_id,
                name=row["mc_username"],
            )
            service = self.bot.sanction_service
            if verb == "mute":
                outcome = await service.execute_escalation_mute(
                    mc_user_id=mc_user_id, name=row["mc_username"],
                    discord_user_id=discord_id, count=count, by=by,
                )
            else:
                outcome = await service.execute_escalation_kick(
                    mc_user_id=mc_user_id, name=row["mc_username"],
                    discord_user_id=discord_id, count=count, by=by,
                )
            outcome = outcome or "⚠️ No action was possible."
            await self.bot.notify_admin(f"{outcome} (button by {by})")
        try:
            message = interaction.message
            embed = message.embeds[0] if message and message.embeds else None
            if message is not None and embed is not None:
                embed.colour = discord.Colour.light_grey()
                embed.set_footer(
                    text=f"Resolved ({verb}) by {by} — "
                         f"triggered by sanction #{sanction_id}"
                )
                await message.edit(embed=embed, view=None)
        except discord.HTTPException as exc:
            log.warning("could not update escalation embed: %s", exc)
        await interaction.followup.send(outcome[:1900], ephemeral=True)

    # -- announcements ---------------------------------------------------------

    async def _announce(
        self, sanction_id: int, sanction_type: str, name: str | None,
        reason: str, admin_name: str, offenses: int, *,
        expires_at: str | None = None, mute_note: str | None = None,
        under_until: str | None = None,
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
        if offenses:
            embed.add_field(name="CoC offense position", value=str(offenses))
        unix = _iso_unix(expires_at)
        if unix is not None:
            embed.add_field(name="Expires", value=f"<t:{unix}:f> (<t:{unix}:R>)")
        under_unix = _iso_unix(under_until)
        if under_unix is not None:
            embed.add_field(
                name="Under warning (CoC 5.1)",
                value=f"until <t:{under_unix}:D>",
            )
        if mute_note:
            embed.add_field(
                name="In-game chat ban", value=mute_note[:1024], inline=False,
            )
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
    cog = SanctionsCog(bot)
    await bot.add_cog(cog)
    tree = getattr(bot, "tree", None)
    if tree is not None:
        menu = app_commands.ContextMenu(
            name="Sanction member", callback=cog.sanction_member_menu,
        )
        try:
            tree.add_command(menu)
        except app_commands.CommandAlreadyRegistered:
            pass
