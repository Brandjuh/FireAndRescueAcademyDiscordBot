"""In-game PM opt-out by PM: a member who can't (or won't) use Discord
replies ``PM OFF`` / ``PM ON`` to the bot account, in any conversation.

The DM-mirror scan sees every incoming message and hands it to
:func:`parse_pm_command`; the notification PMs carry
:data:`PM_OPTOUT_FOOTER` so members know the keyword exists.
"""

from __future__ import annotations

import re

#: The whole message must be the command (an "!" prefix and a trailing
#: full stop are tolerated) — a sentence that merely contains "pm off"
#: is a normal message for staff, not a setting change.
_COMMAND_RE = re.compile(
    r"^\s*!?\s*(?:in-?game\s+)?(?:pm|pms|dm|dms|messages)\s*[:=]?\s*"
    r"(on|off|stop|start)\s*[.!]?\s*$",
    re.IGNORECASE,
)

PM_OPTOUT_FOOTER = (
    "Don't want these messages? Reply PM OFF (PM ON turns them back on)."
)


def parse_pm_command(body: str | None) -> bool | None:
    """True for "PM ON", False for "PM OFF", None for anything else."""
    match = _COMMAND_RE.match(str(body or ""))
    if match is None:
        return None
    return match.group(1).lower() in ("on", "start")


def confirmation_text(enabled: bool) -> str:
    if enabled:
        return (
            "In-game messages are ON: the bot will message you when your "
            "training, event or building request is handled. Reply PM OFF "
            "to stop them."
        )
    return (
        "In-game messages are OFF: the bot will no longer message you about "
        "your requests. Alliance donation warnings are still sent. Reply "
        "PM ON to turn them back on."
    )
