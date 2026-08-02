"""CoC catalogue, fuzzy reason search, advice table, real mute expiry,
derived status, and the CoC section 5 ladder helpers."""

import datetime as dt

from fra_bot.cogs.sanctions import SANCTION_TYPE_KEYS
from fra_bot.services.sanction_rules import (
    COC_RULES,
    RULE_ADVICE,
    RULE_ALIASES,
    UNDER_WARNING_DAYS,
    advice_for,
    effective_status,
    find_reason_matches,
    is_countable_offense,
    ladder_advice,
    ladder_step,
    mute_duration,
    mute_expiry,
    under_warning_until,
)

NOW = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - dt.timedelta(days=days_ago)).isoformat(timespec="seconds")


def test_catalogue_covers_the_full_code_of_conduct():
    expected = {
        "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9",
        "2.1", "2.2", "2.3", "2.4", "3.1", "3.2", "4.1", "4.2",
    }
    assert set(COC_RULES) == expected
    # Verbatim spot checks — the register must quote the real CoC.
    assert "5%" in COC_RULES["4.1"].text
    assert "1.7.1" in COC_RULES["1.7"].text  # subrules folded in
    assert COC_RULES["1.6"].category == "Member Conduct"
    assert COC_RULES["2.2"].category == "General Etiquette"
    assert COC_RULES["3.1"].category == "Buildings and Vehicles"
    assert COC_RULES["4.2"].category == "Other"
    # Every alias key refers to a real rule.
    assert set(RULE_ALIASES) <= set(COC_RULES)


def test_advice_table_uses_only_real_sanction_types():
    valid = set(SANCTION_TYPE_KEYS.values())
    for code, (advised, note) in RULE_ADVICE.items():
        assert code in COC_RULES
        assert advised in valid, f"{code} advises unknown type {advised}"
        assert note
    assert advice_for("1.6")[0] == "Warning - Official 1st warning"
    assert advice_for("1.7")[0] == "Kick"
    assert advice_for(None) is None
    assert advice_for("9.9") is None


def test_find_reason_matches_exact_alias_and_fuzzy():
    assert find_reason_matches("1.6")[0][1].code == "1.6"
    assert find_reason_matches("racism")[0][1].code == "1.6"
    assert find_reason_matches("drama")[0][1].code == "1.4"
    assert find_reason_matches("donation")[0][1].code == "4.1"
    # Typo still lands via difflib.
    assert any(r.code == "1.7" for _, r in find_reason_matches("inactivty"))
    assert find_reason_matches("") == []
    assert len(find_reason_matches("warning", limit=3)) <= 3


def test_mute_expiry_is_real_and_only_for_timed_mutes():
    assert mute_duration("Mute 1d") == dt.timedelta(days=1)
    assert mute_duration("Mute 5m") == dt.timedelta(minutes=5)
    assert mute_duration("Mute") is None       # untimed
    assert mute_duration("Kick") is None
    assert mute_expiry("Mute 6h", NOW) == (
        (NOW + dt.timedelta(hours=6)).isoformat(timespec="seconds")
    )
    assert mute_expiry("Mute", NOW) is None
    assert mute_expiry("Warning - Verbal warning", NOW) is None


def test_effective_status_derives_expired_as_safety_net():
    base = {"sanction_type": "Mute 1d", "status": "active"}
    assert effective_status({**base, "expires_at": _iso(0.5)}, NOW) == "expired"
    future = (NOW + dt.timedelta(hours=2)).isoformat(timespec="seconds")
    assert effective_status({**base, "expires_at": future}, NOW) == "active"
    assert effective_status(
        {**base, "expires_at": _iso(0.5), "status": "revoked"}, NOW
    ) == "revoked"
    # Non-mutes never derive an expiry.
    assert effective_status(
        {"sanction_type": "Kick", "status": "active", "expires_at": _iso(1)},
        NOW,
    ) == "active"


def test_countable_offenses_follow_the_admin_decisions():
    def row(**kw):
        base = {
            "sanction_type": "Warning - Official 1st warning",
            "status": "active", "source": "manual", "created_at": _iso(1),
        }
        base.update(kw)
        return base

    assert is_countable_offense(row())
    assert is_countable_offense(row(sanction_type="Mute 1d", status="expired"))
    assert is_countable_offense(row(sanction_type="Warning - Verbal warning"))
    assert not is_countable_offense(row(source="tax"))
    assert not is_countable_offense(row(source="escalation"))
    assert not is_countable_offense(row(status="revoked"))
    assert not is_countable_offense(row(status="dismissed"))
    assert not is_countable_offense(row(status="unverified"))
    assert not is_countable_offense(row(sanction_type="Kick"))


def test_ladder_follows_coc_section_5():
    assert ladder_step(1) == "first"
    assert ladder_step(2) == "second"
    assert ladder_step(3) == "final"
    assert ladder_step(5) == "final"
    # A raised threshold stretches the 5.2 step.
    assert ladder_step(3, threshold=4) == "second"
    assert "CoC 5.1" in ladder_advice(1)
    assert "CoC 5.2" in ladder_advice(2)
    assert "CoC 5.3" in ladder_advice(3)
    assert "2nd" in ladder_advice(2)


def test_under_warning_badge_is_display_only():
    recent = {"sanction_type": "Warning - Verbal warning", "status": "active",
              "source": "manual", "created_at": _iso(5)}
    old = {**recent, "created_at": _iso(UNDER_WARNING_DAYS + 5)}
    tax = {**recent, "source": "tax", "created_at": _iso(1)}
    until = under_warning_until([recent, old], NOW)
    assert until == (
        NOW - dt.timedelta(days=5) + dt.timedelta(days=UNDER_WARNING_DAYS)
    ).isoformat(timespec="seconds")
    assert under_warning_until([old], NOW) is None
    # Tax records never light the badge.
    assert under_warning_until([tax, old], NOW) is None
    assert under_warning_until([], NOW) is None
