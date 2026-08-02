"""Code of Conduct catalogue + sanction ladder logic (pure, unit-testable).

The rule texts below are the alliance's REAL Code of Conduct, verbatim as
supplied by the alliance admin — do not "fix" wording or spelling here,
the register must quote the rules exactly as members read them.

The escalation ladder follows CoC section 5 faithfully:

* 1st offense  — warning; the member is 'under warning' for 30 days
  (a visible badge, NOT a reset: per the CoC, older offenses keep
  counting — "we will take your older offenses into account even if you
  are currently not 'under warning'").
* 2nd offense  — warning plus a temporary mute or a kick.
* 3rd offense  — removal from the alliance for at least 60 days
  (temp-ban), possibly permanent.

Offense counting therefore never expires; leniency is expressed by
revoking/dismissing records, and only timed mutes have a real expiry
moment (``mute_expiry``).
"""

from __future__ import annotations

import datetime as dt
import difflib
from dataclasses import dataclass

#: CoC 5.1: how long a member is visibly 'under warning' after an offense.
UNDER_WARNING_DAYS = 30

#: CoC 5.3: a kicked/banned member may only return after this many days.
REAPPLY_BLOCK_DAYS = 60


@dataclass(frozen=True)
class CocRule:
    code: str
    title: str
    text: str
    category: str


_MC = "Member Conduct"
_GE = "General Etiquette"
_BV = "Buildings and Vehicles"
_OT = "Other"

#: The alliance's Code of Conduct, verbatim (categories 1-4).
COC_RULES: dict[str, CocRule] = {
    rule.code: rule
    for rule in (
        CocRule(
            "1.1", "Forums and In-Game Chat",
            "All members are expected to treat others as they wish to be "
            "treated. If a member is being abusive, discriminating or "
            "participating in conduct unbecoming of a representative of "
            "Fire & Rescue Academy, please report the offense in question "
            "to an admin. The alliance will not tolerate flaming, "
            "discrimination based on sex, politics, religion, racism, or "
            "excessive use of foul language. This also includes "
            "spamming/flooding",
            _MC,
        ),
        CocRule(
            "1.2", "Helping Others",
            "All members are encouraged to help other guild members and "
            "in-game players whenever possible.",
            _MC,
        ),
        CocRule(
            "1.3", "Respect others. We earn respect by giving respect",
            "The Alliance is barrier free which requires all members to be "
            "aware and respectful of our diversity and the "
            "cultures/background we represent. There is no elevation "
            "between the officers and regular members other than "
            "responsibility. Do not cause undue stress or be disruptive "
            "within the alliance.",
            _MC,
        ),
        CocRule(
            "1.4", "No drama",
            "Don’t cause it and don’t make yourself the subject "
            "of it. Please do not ruin the playtime of others or be the "
            "cause of other members not wanting to logon.",
            _MC,
        ),
        CocRule(
            "1.5", "Religion/Politics",
            "We forbid discussions pertaining to politics and religion in "
            "the in-game chat and forum. If ever there were two subjects "
            "bound to cause intense conflict, it would be those. If you "
            "must discuss those subjects, please take it to a private chat.",
            _MC,
        ),
        CocRule(
            "1.6", "Racism/Bullying",
            "Derogatory racist remarks and any type of bullying or "
            "insulting behavior toward another member of your game's "
            "global community will NOT be tolerated.",
            _MC,
        ),
        CocRule(
            "1.7", "Non-Active Community Members",
            "Members will be removed from the community after 60 days of "
            "inactivity.\n"
            "1.7.1. Except if they are active on discord.\n"
            "1.7.2. Except if the member has shared buildings and these "
            "are of great value to the alliance.\n"
            "1.7.3. Except the member has reported his or her absence to "
            "an alliance admin.",
            _MC,
        ),
        CocRule(
            "1.8", "No offensive Nicknames",
            "All the rules in the COC are also applied to the nicknames.",
            _MC,
        ),
        CocRule(
            "1.9", "No advertisement",
            "Advertising / alliance poaching is not allowed within the "
            "alliance or via PM's, including from and to other alliances.",
            _MC,
        ),
        CocRule(
            "2.1", "Foul language",
            "In general the occasional swear word is acceptable in the "
            "in-game chat or spoken during Discord communication. However, "
            "gratuitous and excessive use of vulgar foul language is not. "
            "Also foul language used to bully, sexually or racially "
            "discriminate or harass members is not tolerated. A member "
            "that engages in this type of behavior can be subject to "
            "immediate review and could face dismissal from the Alliance.",
            _GE,
        ),
        CocRule(
            "2.2", "Personal Privacy",
            "Members are welcome to share personal information based on "
            "their own discretion but The Alliance does not require you "
            "to do so. If as a Member you receive unwelcome, continued "
            "requests for personal information such as pictures, home "
            "address or your real name, this is considered harassment and "
            "is a violation of the COC. Furthermore, there is zero "
            "tolerance for requests for personal financial information, "
            "engaging in this type of request will result in your "
            "immediate removal from the Alliance.",
            _GE,
        ),
        CocRule(
            "2.3", "Live Streaming",
            "Members are allowed to live stream/record the game, however "
            "due to privacy issues, you are required to mask the in-game "
            "chat so that it is not visible to the general public. The "
            "method used to mask chat is up to the individual but it must "
            "cover the majority of the chat window.",
            _GE,
        ),
        CocRule(
            "2.4", "Common sense",
            "Use a common sense, in real life you are not "
            "spamming/flooding or yelling to everyone the whole day.",
            _GE,
        ),
        CocRule(
            "3.1", "Placement",
            "The placing of buildings should take place at locations "
            "where this is also possible in real life. Buildings do not "
            "float on water. Hospitals are not build on railroad tracks.",
            _BV,
        ),
        CocRule(
            "3.2", "Naming",
            "You are free to name the building and vehicles as you want, "
            "as long as this is done according to the Code of Conduct.",
            _BV,
        ),
        CocRule(
            "4.1", "5% donation to alliance",
            "The minimum amount to be donated to the alliance is 5% a "
            "higher percentage is strongly recommended. This setting can "
            "be found in the alliance menu under fundings. With these "
            "small contributions, the alliance can build hospitals and "
            "prisons for you and other members.",
            _OT,
        ),
        CocRule(
            "4.2", "Mission sharing",
            "Mission sharing in the chat is allowed within a set of "
            "rules. Sharing a mission is always allowed, even if you do "
            "not need the vehicle but want to share the mission money.\n"
            "Allowed with a mission share: the amount of vehicles needed "
            "or a specific vehicle you need, the amount of victims, the "
            "location, and the amount of credits.\n"
            "Not allowed: the name of the mission (already shared "
            "automatic) and the vehicles you are sending.",
            _OT,
        ),
    )
}

#: Search synonyms per rule code — what admins actually type.
RULE_ALIASES: dict[str, tuple[str, ...]] = {
    "1.1": ("flaming", "discrimination", "spam", "flooding", "chat abuse",
            "forum abuse"),
    "1.2": ("helping others",),
    "1.3": ("respect", "disrespect", "disruptive"),
    "1.4": ("drama",),
    "1.5": ("religion", "politics", "political", "religious"),
    "1.6": ("racism", "racist", "bullying", "bully", "insult",
            "harassment"),
    "1.7": ("inactive", "inactivity", "idle", "60 days", "absence",
            "non-active"),
    "1.8": ("nickname", "offensive name", "username"),
    "1.9": ("advertising", "advertisement", "poaching", "recruiting"),
    "2.1": ("foul language", "swearing", "vulgar", "cursing", "profanity"),
    "2.2": ("privacy", "personal information", "doxxing", "financial info",
            "address"),
    "2.3": ("stream", "streaming", "recording", "twitch", "youtube"),
    "2.4": ("common sense", "yelling"),
    "3.1": ("placement", "building placement", "floating building",
            "unrealistic"),
    "3.2": ("building name", "vehicle name", "naming"),
    "4.1": ("donation", "tax", "5%", "contribution", "funds", "fundings"),
    "4.2": ("mission share", "mission sharing", "share rules"),
}

#: Advised sanction per rule (default suggestion — the admin always
#: decides). One editable place; the wizard preselects, embeds display.
#: value = (advised sanction type label, escalation note).
RULE_ADVICE: dict[str, tuple[str, str]] = {
    "1.1": ("Warning - Official 1st warning",
            "Repeat: Mute 1d, then the CoC ladder."),
    "1.3": ("Warning - Verbal warning", "Repeat: official warning."),
    "1.4": ("Warning - Verbal warning",
            "Repeat: official warning, then Mute 6h."),
    "1.5": ("Warning - Verbal warning",
            "Repeat: Mute 1h, then official warning."),
    "1.6": ("Warning - Official 1st warning",
            "Zero tolerance — combine with Mute 1d; repeat or severe: "
            "Kick/Ban."),
    "1.7": ("Kick",
            "Administrative removal after 60 days of inactivity — not a "
            "punishment; check the exceptions in 1.7.1-1.7.3 first."),
    "1.8": ("Warning - Verbal warning",
            "Ask for a rename; repeat: official warning."),
    "1.9": ("Warning - Official 1st warning", "Repeat: Kick."),
    "2.1": ("Warning - Verbal warning",
            "Repeat: official warning + Mute 6h; severe cases: Kick."),
    "2.2": ("Warning - Official 1st warning",
            "Requests for financial information: immediate Kick "
            "(CoC: zero tolerance)."),
    "2.3": ("Warning - Verbal warning", "Repeat: official warning."),
    "2.4": ("Warning - Verbal warning", "Repeat: official warning."),
    "3.1": ("Warning - Verbal warning",
            "Ask to fix the placement; repeat: official warning."),
    "3.2": ("Warning - Verbal warning",
            "Ask for a rename; see 1.8."),
    "4.1": ("Warning - Official 1st warning",
            "Handled automatically by the tax ladder "
            "(warning 1 → 2 → 3 → Kick)."),
    "4.2": ("Warning - Verbal warning", "Repeat: official warning."),
}


def advice_for(code: str | None) -> tuple[str, str] | None:
    if not code:
        return None
    return RULE_ADVICE.get(code)


def find_reason_matches(
    query: str, limit: int = 5
) -> list[tuple[float, CocRule]]:
    """Fuzzy CoC rule search (reference: find_sanction_reason_matches).

    exact code = 1.0, alias/title substring = 0.9, otherwise the best
    ``difflib`` ratio against title + aliases (cut off below 0.5)."""
    wanted = (query or "").strip().lower()
    if not wanted:
        return []
    scored: list[tuple[float, CocRule]] = []
    for code, rule in COC_RULES.items():
        haystacks = [rule.title.lower()] + [
            a.lower() for a in RULE_ALIASES.get(code, ())
        ]
        if wanted == code:
            score = 1.0
        elif code.startswith(wanted):
            score = 0.95
        elif any(wanted in h or h in wanted for h in haystacks):
            score = 0.9
        else:
            score = max(
                difflib.SequenceMatcher(None, wanted, h).ratio()
                for h in haystacks
            )
            if score < 0.5:
                continue
        scored.append((score, rule))
    scored.sort(key=lambda pair: (-pair[0], pair[1].code))
    return scored[:limit]


# -- timed mutes -----------------------------------------------------------

#: Duration suffix of a timed mute type → real duration. The game lifts a
#: timed chat ban by itself; ``expires_at`` mirrors that moment so the
#: register can book the transition to 'expired'.
MUTE_DURATIONS: dict[str, dt.timedelta] = {
    "5m": dt.timedelta(minutes=5),
    "15m": dt.timedelta(minutes=15),
    "30m": dt.timedelta(minutes=30),
    "1h": dt.timedelta(hours=1),
    "6h": dt.timedelta(hours=6),
    "12h": dt.timedelta(hours=12),
    "1d": dt.timedelta(days=1),
    "7d": dt.timedelta(days=7),
    "14d": dt.timedelta(days=14),
}


def mute_duration(sanction_type: str) -> dt.timedelta | None:
    """The real duration of a timed mute type ("Mute 1d" → 1 day); None
    for anything else, including the untimed bare "Mute"."""
    if not sanction_type.startswith("Mute "):
        return None
    return MUTE_DURATIONS.get(sanction_type.removeprefix("Mute ").strip())


def mute_expiry(
    sanction_type: str, now: dt.datetime | None = None
) -> str | None:
    """UTC ISO expiry for a timed mute issued now; None when untimed.
    (The reference bot stored the duration as a label and nothing ever
    expired — the durations were cosmetic. Here they are real.)"""
    duration = mute_duration(sanction_type)
    if duration is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now + duration).isoformat(timespec="seconds")


def _field(row, key: str):
    """Tolerant field access for aiosqlite.Row / dict test doubles."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def effective_status(row, now: dt.datetime | None = None) -> str:
    """Status for display. The expiry sweep STORES the active→expired
    transition; this derives it as a safety net for the window before
    the sweep has run."""
    status = _field(row, "status") or "active"
    if status != "active":
        return status
    expires = _field(row, "expires_at")
    if not expires or not str(_field(row, "sanction_type") or "").startswith("Mute"):
        return status
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        expiry = dt.datetime.fromisoformat(expires)
    except ValueError:
        return status
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=dt.timezone.utc)
    return "expired" if expiry <= now else status


# -- the CoC section 5 ladder ----------------------------------------------

def is_countable_offense(row) -> bool:
    """Does this record advance the member's CoC offense position?

    Warnings and mutes count; kicks/bans are consequences, not ladder
    input. Tax records mirror the automated 4.1 ladder (kept out per the
    admin's decision) and escalation records ARE the consequence — both
    excluded, or the engine would escalate on its own output.
    NB: the repo mirrors this filter in SQL (``offense_count``)."""
    status = _field(row, "status")
    if status not in ("active", "expired"):
        return False
    if (_field(row, "source") or "manual") in ("tax", "escalation"):
        return False
    stype = str(_field(row, "sanction_type") or "")
    return stype.startswith(("Warning", "Mute"))


def ladder_step(count: int, threshold: int = 3) -> str:
    """'first' | 'second' | 'final' for a member with ``count`` offenses."""
    if count >= threshold:
        return "final"
    return "second" if count >= 2 else "first"


def ladder_advice(count: int, threshold: int = 3) -> str:
    """The CoC-prescribed follow-up for the member's offense position."""
    step = ladder_step(count, threshold)
    if step == "first":
        return (
            "1st offense — CoC 5.1: warning; the member is 'under "
            f"warning' for {UNDER_WARNING_DAYS} days."
        )
    if step == "second":
        return (
            f"{count}{_ordinal(count)} offense — CoC 5.2: warning plus a "
            "temporary mute or a kick from the alliance."
        )
    return (
        f"{count}{_ordinal(count)} offense — CoC 5.3: removal from the "
        f"alliance for at least {REAPPLY_BLOCK_DAYS} days (temp-ban), "
        "possibly permanent."
    )


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def under_warning_until(rows, now: dt.datetime | None = None) -> str | None:
    """CoC 5.1's 30-day 'under warning' badge: the ISO moment it ends,
    or None when the member's latest countable offense is older than
    that. Display only — it never resets the offense count."""
    now = now or dt.datetime.now(dt.timezone.utc)
    latest: dt.datetime | None = None
    for row in rows:
        if not is_countable_offense(row):
            continue
        raw = _field(row, "created_at")
        if not raw:
            continue
        try:
            created = dt.datetime.fromisoformat(raw)
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        if latest is None or created > latest:
            latest = created
    if latest is None:
        return None
    until = latest + dt.timedelta(days=UNDER_WARNING_DAYS)
    if until <= now:
        return None
    return until.isoformat(timespec="seconds")
