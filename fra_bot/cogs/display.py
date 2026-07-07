"""Presentation mapping for alliance log actions.

Keys MUST match the action keys emitted by
:mod:`fra_bot.mc.parsers.logs` — a startup check in the notifications
cog warns when the two drift apart (a real bug in the previous bot).
"""

from __future__ import annotations

import discord

_GREEN = discord.Colour.green()
_RED = discord.Colour.red()
_ORANGE = discord.Colour.orange()
_BLUE = discord.Colour.blue()
_PURPLE = discord.Colour.purple()
_GOLD = discord.Colour.gold()
_GREY = discord.Colour.light_grey()

# action_key -> (title, colour, emoji)
ACTION_DISPLAY: dict[str, tuple[str, discord.Colour, str]] = {
    "added_to_alliance": ("Added to the alliance", _GREEN, "✅"),
    "application_denied": ("Application denied", _RED, "🚫"),
    "left_alliance": ("Left the alliance", _ORANGE, "👋"),
    "kicked_from_alliance": ("Kicked from the alliance", _RED, "🥾"),
    "set_admin": ("Set as admin", _BLUE, "🛡️"),
    "removed_admin": ("Removed admin", _ORANGE, "🛡️"),
    "set_co_admin": ("Set as co-admin", _BLUE, "🛡️"),
    "removed_co_admin": ("Removed co-admin", _ORANGE, "🛡️"),
    "set_transport_admin": ("Set as transport admin", _BLUE, "🚑"),
    "removed_transport_admin": ("Removed transport admin", _ORANGE, "🚑"),
    "set_education_admin": ("Set as education admin", _BLUE, "🎓"),
    "removed_education_admin": ("Removed education admin", _ORANGE, "🎓"),
    "set_finance_admin": ("Set as finance admin", _BLUE, "💼"),
    "removed_finance_admin": ("Removed finance admin", _ORANGE, "💼"),
    "set_mod_action_admin": ("Set as mod action admin", _BLUE, "⚖️"),
    "removed_mod_action_admin": ("Removed mod action admin", _ORANGE, "⚖️"),
    "chat_ban_set": ("Chat ban set", _RED, "🔇"),
    "chat_ban_removed": ("Chat ban removed", _GREEN, "🔊"),
    "allowed_to_apply": ("Allowed to apply", _GREEN, "📝"),
    "not_allowed_to_apply": ("Not allowed to apply", _RED, "📝"),
    "created_course": ("Course created", _BLUE, "🎓"),
    "course_completed": ("Course completed", _GREEN, "🎓"),
    "building_constructed": ("Building constructed", _GREEN, "🏗️"),
    "building_destroyed": ("Building destroyed", _RED, "💥"),
    "extension_started": ("Extension started", _BLUE, "🧱"),
    "expansion_finished": ("Expansion finished", _GREEN, "🧱"),
    "large_mission_started": ("Large scale mission started", _PURPLE, "🚨"),
    "alliance_event_started": ("Alliance event started", _PURPLE, "🎉"),
    "set_as_staff": ("Set as staff", _BLUE, "👤"),
    "removed_as_staff": ("Removed as staff", _ORANGE, "👤"),
    "promoted_to_event_manager": ("Promoted to event manager", _BLUE, "🎪"),
    "removed_event_manager": ("Removed event manager", _ORANGE, "🎪"),
    "removed_custom_large_scale_mission": (
        "Removed custom large scale mission", _ORANGE, "🚨",
    ),
    "contributed_to_alliance": ("Contributed to the alliance", _GOLD, "💰"),
}

FALLBACK_DISPLAY = ("Alliance log", _GREY, "ℹ️")

MEMBER_EVENT_DISPLAY: dict[str, tuple[str, discord.Colour, str]] = {
    "joined": ("Member joined", _GREEN, "🟢"),
    "left": ("Member left", _ORANGE, "🔴"),
    "role_changed": ("Role changed", _BLUE, "🔁"),
    "contribution_changed": ("Contribution rate changed", _GOLD, "💱"),
    "name_changed": ("Name changed", _BLUE, "✏️"),
}


def profile_url(mc_user_id: int | None) -> str | None:
    if not mc_user_id:
        return None
    return f"https://www.missionchief.com/profile/{mc_user_id}"
