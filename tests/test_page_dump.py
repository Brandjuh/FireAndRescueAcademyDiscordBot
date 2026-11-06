"""Tests for the `!fra dump` helpers: path safety and CSRF redaction."""

import pytest

from fra_bot.mc.page_dump import redact_html, sanitize_dump_path


def test_sanitize_adds_leading_slash():
    assert sanitize_dump_path("missionAllianceNew") == "/missionAllianceNew"
    assert sanitize_dump_path("/buildings/new") == "/buildings/new"
    assert sanitize_dump_path("  /verband/kasse  ") == "/verband/kasse"


def test_sanitize_keeps_query_string():
    assert (
        sanitize_dump_path("/missionAllianceNew?tlat=40.7&tlng=-74")
        == "/missionAllianceNew?tlat=40.7&tlng=-74"
    )


def test_sanitize_rejects_absolute_and_scheme_relative():
    for bad in ("http://evil.com/x", "https://evil.com", "//evil.com/x", ""):
        with pytest.raises(ValueError):
            sanitize_dump_path(bad)


def test_redact_token_input_name_first():
    html = '<input type="hidden" name="authenticity_token" value="SECRET123==" />'
    out = redact_html(html)
    assert "SECRET123" not in out
    assert 'name="authenticity_token"' in out
    assert 'value="REDACTED"' in out


def test_redact_token_input_value_first():
    html = '<input value="SECRET123==" name="authenticity_token" type="hidden">'
    out = redact_html(html)
    assert "SECRET123" not in out
    assert 'name="authenticity_token"' in out


def test_redact_meta_csrf_token():
    html = '<meta name="csrf-token" content="abc.def.ghi">'
    out = redact_html(html)
    assert "abc.def.ghi" not in out
    assert "REDACTED" in out


def test_redact_leaves_other_fields_intact():
    html = (
        '<input name="mission_position[latitude]" value="40.7">'
        '<input name="authenticity_token" value="SECRET">'
    )
    out = redact_html(html)
    assert 'name="mission_position[latitude]" value="40.7"' in out
    assert "SECRET" not in out


def test_redact_discord_webhook_token():
    # The alliance text page (/veband/text/edit) carries the alliance's
    # Discord webhook; its token must never reach a Discord upload.
    html = (
        '<input type="text" name="discord_webhook" value="https://ptb.discordapp.com'
        '/api/webhooks/743939319122886657/AbC-123_xyz.Q" />'
    )
    out = redact_html(html)
    assert "AbC-123_xyz" not in out
    assert "/api/webhooks/743939319122886657/REDACTED" in out
