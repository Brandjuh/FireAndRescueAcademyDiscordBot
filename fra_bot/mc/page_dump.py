"""Helpers for the `!fra dump` diagnostic: fetch a MissionChief page's raw
HTML so the real form markup can be inspected when building/adjusting a
parser (e.g. the mission-creation or building form).

Two safety concerns live here, both pure and unit-tested:

* ``sanitize_dump_path`` keeps the command on-site (no ``http://evil`` —
  ``urljoin`` would otherwise happily fetch an absolute URL).
* ``redact_html`` strips per-session CSRF tokens before the HTML is shared
  in Discord. Only the field *names* and structure matter for parsing.
"""

from __future__ import annotations

import re

# authenticity_token <input> — both attribute orders (name/value swapped).
_TOKEN_NAME_FIRST = re.compile(
    r'(name="authenticity_token"[^>]*?\svalue=")[^"]*(")', re.IGNORECASE
)
_TOKEN_VALUE_FIRST = re.compile(
    r'(\svalue=")[^"]*("[^>]*?\sname="authenticity_token")', re.IGNORECASE
)
# <meta name="csrf-token" content="..."> — both attribute orders.
_META_NAME_FIRST = re.compile(
    r'(<meta[^>]*?\sname="csrf-token"[^>]*?\scontent=")[^"]*(")', re.IGNORECASE
)
_META_CONTENT_FIRST = re.compile(
    r'(<meta[^>]*?\scontent=")[^"]*("[^>]*?\sname="csrf-token")', re.IGNORECASE
)

_REDACTION = r"\1REDACTED\2"


def redact_html(html: str) -> str:
    """Blank out per-session CSRF tokens; keep everything else intact."""
    for pattern in (
        _TOKEN_NAME_FIRST,
        _TOKEN_VALUE_FIRST,
        _META_NAME_FIRST,
        _META_CONTENT_FIRST,
    ):
        html = pattern.sub(_REDACTION, html)
    return html


def sanitize_dump_path(path: str) -> str:
    """Return an on-site path (leading '/'), or raise ValueError.

    Rejects absolute URLs / scheme-relative paths so the dump can only ever
    hit the MissionChief host the client is already authenticated against.
    """
    path = (path or "").strip()
    if not path:
        raise ValueError("give a path, e.g. `/missionAllianceNew`")
    if "://" in path or path.startswith("//"):
        raise ValueError("only on-site paths are allowed (start with `/`)")
    if not path.startswith("/"):
        path = "/" + path
    return path
