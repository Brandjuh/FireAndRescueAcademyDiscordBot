"""Intake-time checks for Discord-sourced requests.

Every Discord request flow (training, building, mission, event) runs the
same gate BEFORE anything is queued: resolve the member's MissionChief
identity from their approved verification link, look up their alliance
contribution rate on the roster, and compare it against the feature's
minimum. The board flows have always had this check at execute time via
the post author's ``requester_mc_id`` — a Discord interaction carries no
MC identity of its own, so without the link lookup the check would be
silently skipped for panel and slash requests.

The verdict carries the resolved identity so accepted requests can store
``requester_mc_id``, which keeps the services' execute-time contribution
gates working as a second line of defence.

The contribution rate can NOT be checked live per member: it only exists
on the game's paginated alliance members list (dozens of pages), which
the hourly roster sweep walks. A member who just fixed their alliance
tax is therefore told WHEN the roster refreshes and to retry after that.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..db.database import Database
from ..db.repos import LinksRepo, MembersRepo, RunsRepo

#: Payload flag marking a request that was refused at intake. The member
#: already got the reason ephemerally, so the publisher must not DM them
#: again — the admin-log embed is the (required) log entry.
INTAKE_REJECTED_FLAG = "intake_rejected"


def donation_instructions(min_rate: float) -> str:
    """The click path to the setting the gate reads, as a numbered block.

    Telling a member their donation is too low without telling them where
    to change it just moves the question to an admin. The wording matches
    the tax-warning PMs (:mod:`fra_bot.services.tax_warnings`, ported from
    the reference bot) so a member who gets both reads one instruction,
    not two that disagree.
    """
    return (
        "How to update your alliance donation:\n"
        "1. Open the menu.\n"
        "2. Click on Show Alliance.\n"
        "3. Go to Alliance Funds.\n"
        f"4. Set your donation percentage to at least {min_rate:g}%."
    )


def donation_hint(min_rate: float) -> str:
    """One-line version of :func:`donation_instructions` for Discord, where
    the refusal is already a paragraph and a numbered list would bury it."""
    return (
        "You set this in the game: **menu → Show Alliance → Alliance "
        f"Funds** → donation percentage, at least {min_rate:g}%."
    )


@dataclass(frozen=True)
class IntakeVerdict:
    ok: bool
    #: Machine-ish reason key when rejected: "not_linked" | "low_contribution".
    reason: str | None
    mc_user_id: int | None
    mc_name: str | None
    rate: float | None
    min_rate: float
    #: For low-contribution rejections: epoch when the next roster sweep
    #: should have fresh numbers, so a member who just raised their
    #: alliance tax knows when to retry.
    retry_at: int | None = None

    @property
    def rejection_text(self) -> str:
        """Member-facing explanation of a rejection (English, like every
        member-facing text)."""
        if self.reason == "low_contribution":
            text = (
                f"your alliance contribution is **{self.rate:g}%**, the "
                f"minimum for requests is **{self.min_rate:g}%**."
            )
            text += "\n" + donation_hint(self.min_rate)
            if self.retry_at:
                text += (
                    " Just raised your alliance tax in the game? My roster "
                    f"data refreshes <t:{self.retry_at}:R> - please try "
                    "again after that."
                )
            else:
                text += " Then try again."
            return text
        return (
            "I couldn't find your MissionChief account. Set your Discord "
            "nickname to your exact MissionChief name and run `!verify` "
            "first, then request again."
        )

    @property
    def log_detail(self) -> str:
        """The status_detail for the request's log row."""
        if self.reason == "low_contribution":
            return (
                f"rejected at intake: contribution {self.rate:g}% below the "
                f"required {self.min_rate:g}%"
            )
        return "rejected at intake: requester has no verified MissionChief link"


async def _roster_refresh_eta(db: Database, interval_minutes: int) -> int:
    """Epoch when the NEXT members sweep should have finished — the same
    honest formula the verify flow uses (last sweep + interval with jitter
    headroom + the sweep's own runtime, never promised sooner than 5 min)."""
    now = dt.datetime.now(dt.timezone.utc)
    base = now
    last = await RunsRepo(db).last_success("members")
    if last is not None and last["started_at"]:
        try:
            base = dt.datetime.fromisoformat(last["started_at"])
            if base.tzinfo is None:
                base = base.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            base = now
    eta = base + dt.timedelta(minutes=interval_minutes * 1.15 + 10)
    return int(max(eta, now + dt.timedelta(minutes=5)).timestamp())


async def contribution_gate(
    db: Database, discord_id: int, min_rate: float,
    *, members_interval_minutes: int = 60,
) -> IntakeVerdict:
    """The always-on contribution check for Discord requests.

    * no approved link → rejected (anyone could dodge the check otherwise),
    * linked, on the roster, rate below ``min_rate`` → rejected with the
      numbers AND when the roster refreshes (retry moment),
    * linked but not (yet) on the active roster → allowed with an unknown
      rate, exactly like the board flows treat an unknown rate — the
      services re-check at execute time once the roster sweep catches up.
    """
    link = await LinksRepo(db).get_by_discord(discord_id)
    if link is None or link["status"] != "approved":
        return IntakeVerdict(False, "not_linked", None, None, None, min_rate)
    mc_user_id = int(link["mc_user_id"])
    row = (await MembersRepo(db).active_members()).get(mc_user_id)
    if row is None:
        return IntakeVerdict(True, None, mc_user_id, None, None, min_rate)
    # An empty contribution column on the roster IS 0% (a never-set
    # donation shows no rate at all) — treating it as unknown waved 0%
    # contributors straight through the gate.
    rate = float(row["contribution_rate"] or 0.0)
    if rate < min_rate:
        retry_at = await _roster_refresh_eta(db, members_interval_minutes)
        return IntakeVerdict(
            False, "low_contribution", mc_user_id, row["name"], rate, min_rate,
            retry_at=retry_at,
        )
    return IntakeVerdict(True, None, mc_user_id, row["name"], rate, min_rate)
