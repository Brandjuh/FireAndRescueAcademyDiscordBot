"""Member audit timeline (reference bot: MemberManager audit helpers).

One chronological view per member, merged from data the bot already
stores — nothing is scraped for this:

* roster change events (joined/left/role/contribution/name),
* person-level alliance log rows (joins, kicks, chat bans, admin-role
  changes) matched on either identity side,
* the sanctions register,
* the verified-link approval.

The reference bot's load-bearing exclusion is kept: course completions
are course-level alliance logs, NOT personal records — including them
fabricates false personal activity."""

from __future__ import annotations

from dataclasses import dataclass

from ..db.database import Database
from ..db.repos import LinksRepo, MemberActionsRepo, SanctionsRepo

#: Alliance-log action keys that belong in a PERSON's audit timeline
#: (the reference set, plus this bot's more granular admin-role keys).
PERSON_AUDIT_ACTION_KEYS = frozenset({
    "added_to_alliance",
    "left_alliance",
    "kicked_from_alliance",
    "chat_ban_removed",
    "chat_ban_set",
    "set_admin",
    "removed_admin",
    "set_co_admin",
    "removed_co_admin",
    "set_mod_action_admin",
    "removed_mod_action_admin",
    "set_as_staff",
    "removed_as_staff",
    "promoted_to_event_manager",
    "removed_event_manager",
    "set_transport_admin",
    "removed_transport_admin",
    "set_education_admin",
    "removed_education_admin",
    "set_finance_admin",
    "removed_finance_admin",
    "allowed_to_apply",
    "not_allowed_to_apply",
    "application_denied",
    # Mission/event starts carry the starter's name in the alliance log —
    # also the MANUAL ones done outside the bot, which admins explicitly
    # want visible in a member's history.
    "large_mission_started",
    "alliance_event_started",
})

#: Never in a member timeline (reference exclusion — course completions
#: are course-level rows, not personal activity).
EXCLUDED_ACTION_KEYS = frozenset({"course_completed", "contributed_to_alliance"})

_MEMBER_EVENT_ICONS = {
    "joined": "✅", "left": "👋", "role_changed": "👔",
    "contribution_changed": "📊", "name_changed": "🏷️",
}


@dataclass(frozen=True)
class TimelineEvent:
    at: str          # ISO timestamp (sorting key; may be date-only precision)
    icon: str
    title: str
    detail: str = ""
    source: str = ""  # roster | logs | sanctions | links


async def build_timeline(
    db: Database, *, mc_user_id: int | None = None, name: str | None = None,
    discord_user_id: int | None = None, limit: int = 25,
) -> list[TimelineEvent]:
    """The member's merged audit timeline, newest first."""
    events: list[TimelineEvent] = []

    # Roster change events.
    clauses, params = [], []
    if mc_user_id is not None:
        clauses.append("mc_user_id = ?")
        params.append(mc_user_id)
    if name:
        clauses.append("name = ? COLLATE NOCASE")
        params.append(name)
    if clauses:
        async with db.conn.execute(
            f"SELECT * FROM member_events WHERE {' OR '.join(clauses)} "
            "ORDER BY id DESC LIMIT 200",
            params,
        ) as cur:
            for row in await cur.fetchall():
                change = ""
                if row["old_value"] or row["new_value"]:
                    change = f": {row['old_value'] or '—'} → {row['new_value'] or '—'}"
                events.append(TimelineEvent(
                    at=row["occurred_at"],
                    icon=_MEMBER_EVENT_ICONS.get(row["event_type"], "ℹ️"),
                    title=row["event_type"].replace("_", " "),
                    detail=change,
                    source="roster",
                ))

    # Person-level alliance log rows, matched on either identity side.
    log_clauses, log_params = [], []
    if mc_user_id is not None:
        log_clauses += ["executed_mc_id = ?", "affected_mc_id = ?"]
        log_params += [mc_user_id, mc_user_id]
    if name:
        log_clauses += [
            "executed_name = ? COLLATE NOCASE", "affected_name = ? COLLATE NOCASE"
        ]
        log_params += [name, name]
    if log_clauses:
        keys = ",".join("?" for _ in PERSON_AUDIT_ACTION_KEYS)
        async with db.conn.execute(
            f"SELECT * FROM alliance_logs WHERE ({' OR '.join(log_clauses)}) "
            f"AND action_key IN ({keys}) "
            "ORDER BY id DESC LIMIT 200",
            (*log_params, *PERSON_AUDIT_ACTION_KEYS),
        ) as cur:
            for row in await cur.fetchall():
                if row["action_key"] in EXCLUDED_ACTION_KEYS:
                    continue
                events.append(TimelineEvent(
                    at=row["event_at"] or row["scraped_at"],
                    icon="🎮",
                    title=row["action_key"].replace("_", " "),
                    detail=(row["description"] or "")[:120],
                    source="logs",
                ))

    # Sanctions register.
    for row in await SanctionsRepo(db).for_member(
        mc_user_id=mc_user_id, discord_user_id=discord_user_id,
        name=name, limit=100,
    ):
        suffix = " (revoked)" if row["status"] != "active" else ""
        events.append(TimelineEvent(
            at=row["created_at"],
            icon="🚨",
            title=f"{row['sanction_type']}{suffix}",
            detail=f"#{row['id']} — {row['reason'][:100]}",
            source="sanctions",
        ))

    # Bot-side member actions (requests, profile edits, clicks). Actions
    # that MIRROR a richer source above (sanctions register, link
    # approval) are skipped — they would show the same event twice and
    # waste the event budget on duplicates.
    _mirrored = {"sanction_received", "sanction_revoked", "verified"}
    for row in await MemberActionsRepo(db).for_member(
        discord_user_id=discord_user_id, mc_user_id=mc_user_id,
        name=name, limit=100,
    ):
        if row["action"] in _mirrored:
            continue
        events.append(TimelineEvent(
            at=row["created_at"],
            icon="🤖",
            title=row["action"].replace("_", " "),
            detail=(row["detail"] or "")[:120],
            source="bot",
        ))

    # Verified link.
    link = None
    if discord_user_id is not None:
        link = await LinksRepo(db).get_by_discord(discord_user_id)
    elif mc_user_id is not None:
        link = await LinksRepo(db).get_by_mc(mc_user_id)
    if link is not None and link["status"] == "approved":
        events.append(TimelineEvent(
            at=link["updated_at"] or link["created_at"],
            icon="🔗",
            title="Discord link approved",
            source="links",
        ))

    events.sort(key=lambda e: e.at or "", reverse=True)
    return events[:limit]


def render_timeline(name: str, events: list[TimelineEvent]) -> str:
    """A compact text rendering (one line per event, newest first)."""
    if not events:
        return f"No recorded history for **{name}**."
    lines = [f"📜 Timeline for **{name}** (newest first):"]
    for event in events:
        day = (event.at or "")[:10] or "????-??-??"
        detail = f" — {event.detail}" if event.detail else ""
        lines.append(f"`{day}` {event.icon} {event.title}{detail}")
    return "\n".join(lines)[:1900]
