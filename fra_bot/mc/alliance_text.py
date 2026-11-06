"""The alliance text page (``/veband/text/edit``): read it, change one or
more fields, save, and prove the save landed.

The page is ONE form holding every alliance text — the public description,
the rules, the welcome PM (subject + text), the "Important Alliance
Announcements" line shown above the alliance chat, and the Discord webhook
the game posts announcements through. Saving sends all of them at once, so
every save here resubmits each field exactly as the game rendered it and
only swaps the values that were asked for. A field we did not mean to touch
that reads differently after the save is reported loudly.

Two browser behaviours are reproduced so an untouched textarea survives a
save byte-for-byte:

* Rails emits a newline right after ``<textarea>`` that browsers drop (the
  HTML spec says so); lxml keeps it. Without dropping it, every save would
  prepend one more blank line to the rules and description.
* Browsers submit textarea line breaks as CRLF; lxml hands us LF.

``/veband`` is MissionChief's own spelling for this page (the rest of the
alliance pages use ``/verband``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .errors import MissionChiefError, ParseError

log = logging.getLogger(__name__)

EDIT_PATH = "/veband/text/edit"
#: Saved by the edit form; only a fallback when the form has no action.
SAVE_PATH = "/veband/text/speichern"


@dataclass(frozen=True)
class TextField:
    name: str        # the form control's name
    label: str       # what the game calls it
    multiline: bool  # textarea (True) or a single-line input


#: The fields the bot can write. Only "announcements" is wired to commands
#: today; the rest are here so later features reuse the same safe save.
FIELDS: dict[str, TextField] = {
    "announcements": TextField(
        "alliance_text[chat_header]", "Important Alliance Announcements", False
    ),
    "description": TextField("alliance_text[content]", "Description Text", True),
    "rules": TextField("alliance_text[rules]", "Rules", True),
    "welcome_subject": TextField(
        "alliance_text[welcome_subject]", "Welcome PM subject", False
    ),
    "welcome_text": TextField(
        "alliance_text[welcome_text]", "Welcome PM message", True
    ),
}

#: Controls whose VALUE must never be logged or shown (only their name).
SECRET_FIELDS = frozenset({"discord_webhook"})
#: Per-request plumbing — differs between two page loads by design.
_VOLATILE_FIELDS = frozenset({"authenticity_token", "utf8", "_method"})

_LABELS_BY_NAME = {f.name: f.label for f in FIELDS.values()}
_LABELS_BY_NAME["discord_webhook"] = "Discord webhook"


@dataclass
class AllianceTextForm:
    action: str
    #: Every named control in page order, values as a browser would send
    #: them EXCEPT textarea line breaks, which are kept as LF (see payload).
    fields: list[tuple[str, str]] = field(default_factory=list)
    textareas: set[str] = field(default_factory=set)
    maxlength: dict[str, int] = field(default_factory=dict)
    submit: tuple[str, str] | None = None

    def value(self, name: str) -> str | None:
        for key, value in self.fields:
            if key == name:
                return value
        return None

    def get(self, key: str) -> str | None:
        """A :data:`FIELDS` key's current value (``"announcements"`` …)."""
        return self.value(FIELDS[key].name)

    def max_chars(self, key: str) -> int | None:
        return self.maxlength.get(FIELDS[key].name)


def _textarea_value(tag) -> str:
    value = tag.get_text() or ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    # The newline Rails puts after <textarea> is not part of the value.
    if value.startswith("\n"):
        value = value[1:]
    return value


def parse_alliance_text_form(html: str) -> AllianceTextForm:
    soup = BeautifulSoup(html or "", "lxml")
    form = None
    for candidate in soup.find_all("form"):
        if candidate.find(attrs={"name": FIELDS["announcements"].name}):
            form = candidate
            break
    if form is None:
        raise ParseError(
            "No alliance text form on /veband/text/edit — layout change, or "
            "the bot account lost the right to edit alliance texts?"
        )

    parsed = AllianceTextForm(action=form.get("action") or SAVE_PATH)
    for tag in form.find_all(("input", "textarea", "select")):
        name = tag.get("name")
        kind = (tag.get("type") or "").lower()
        if not name or kind in ("button", "reset", "image", "file"):
            continue
        if kind == "submit":
            if parsed.submit is None:
                parsed.submit = (name, tag.get("value") or "")
            continue
        if kind in ("checkbox", "radio") and not tag.has_attr("checked"):
            continue  # a browser only sends checked boxes
        if tag.name == "textarea":
            value = _textarea_value(tag)
            parsed.textareas.add(name)
        elif tag.name == "select":
            chosen = tag.find("option", selected=True) or tag.find("option")
            value = chosen.get("value", chosen.get_text()) if chosen else ""
        else:
            value = tag.get("value") or ""
        parsed.fields.append((name, value))
        limit = tag.get("maxlength")
        if limit and str(limit).isdigit():
            parsed.maxlength[name] = int(limit)
    return parsed


def clean_value(key: str, value: str) -> str:
    """What we will store for ``key``: single-line fields lose their line
    breaks (an <input> can't hold them), every field loses outer
    whitespace."""
    value = str(value or "")
    if not FIELDS[key].multiline:
        value = re.sub(r"\s*[\r\n]+\s*", " ", value)
    return value.strip()


def _canon(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def build_payload(
    form: AllianceTextForm, changes: dict[str, str]
) -> list[tuple[str, str]]:
    """The full form submission with ``changes`` (control name → value)
    applied; every other control is sent back untouched."""
    payload: list[tuple[str, str]] = []
    for name, value in form.fields:
        value = changes.get(name, value)
        if name in form.textareas:
            value = value.replace("\r\n", "\n").replace("\n", "\r\n")
        payload.append((name, value))
    if form.submit:
        payload.append(form.submit)
    return payload


def check_changes(form: AllianceTextForm, changes: dict[str, str]) -> list[str]:
    """Problems that make a save pointless (empty list = fine)."""
    problems: list[str] = []
    for key, value in changes.items():
        spec = FIELDS[key]
        if form.value(spec.name) is None:
            problems.append(f"the page has no {spec.label} field any more")
            continue
        limit = form.maxlength.get(spec.name)
        if limit is not None and len(value) > limit:
            problems.append(
                f"{spec.label} is too long: {len(value)}/{limit} characters"
            )
    return problems


@dataclass
class SaveResult:
    ok: bool
    #: True when a save was actually submitted.
    saved: bool
    detail: str
    #: FIELDS key → value before / after (secrets never included).
    before: dict[str, str] = field(default_factory=dict)
    after: dict[str, str] = field(default_factory=dict)
    #: Labels of fields we did NOT ask to change that read differently
    #: after the save — should always be empty.
    collateral: list[str] = field(default_factory=list)


def _snapshot(form: AllianceTextForm) -> dict[str, str]:
    return {key: form.get(key) or "" for key in FIELDS if form.get(key) is not None}


def _collateral(
    before: AllianceTextForm, after: AllianceTextForm, changed: set[str]
) -> list[str]:
    labels: list[str] = []
    for name, old in before.fields:
        if name in changed or name in _VOLATILE_FIELDS:
            continue
        if _canon(after.value(name)) != _canon(old):
            labels.append(_LABELS_BY_NAME.get(name, name))
    return labels


async def save_alliance_texts(
    client, changes: dict[str, str], *, dry_run: bool = False
) -> SaveResult:
    """Change the given :data:`FIELDS` (key → new value) and verify.

    Reads the page, skips the save when every value is already there,
    submits the whole form once (POSTs are never retried blindly), then
    re-reads the page: ``ok`` means the game now SHOWS the new values.
    A lost response to the save is fine — the re-read decides.
    """
    unknown = [key for key in changes if key not in FIELDS]
    if unknown:
        raise KeyError(f"unknown alliance text field(s): {', '.join(unknown)}")
    wanted = {key: clean_value(key, value) for key, value in changes.items()}

    try:
        form = parse_alliance_text_form(await client.fetch_page(EDIT_PATH))
    except MissionChiefError as exc:
        return SaveResult(False, False, f"could not read the alliance text page: {exc}")
    before = _snapshot(form)

    problems = check_changes(form, wanted)
    if problems:
        return SaveResult(False, False, "; ".join(problems), before=before)

    by_name = {FIELDS[key].name: value for key, value in wanted.items()}
    if all(_canon(form.value(name)) == _canon(value) for name, value in by_name.items()):
        return SaveResult(True, False, "already up to date", before=before, after=before)
    if dry_run:
        return SaveResult(
            True, False, "dry run — would have saved it", before=before
        )

    post_problem = ""
    try:
        status, html, _final_url = await client.post_form(
            form.action, build_payload(form, by_name),
            referer=client.url(EDIT_PATH),
        )
        if status >= 400:
            post_problem = f"HTTP {status}"
    except MissionChiefError as exc:
        # Possibly landed (lost response) — the re-read below decides.
        post_problem = str(exc)
        log.warning("alliance text save: %s — verifying by re-reading", exc)

    try:
        after_form = parse_alliance_text_form(await client.fetch_page(EDIT_PATH))
    except MissionChiefError as exc:
        return SaveResult(
            False, True,
            f"saved, but could not re-read the page to confirm it ({exc})"
            + (f"; the save itself reported: {post_problem}" if post_problem else ""),
            before=before,
        )
    after = _snapshot(after_form)
    collateral = _collateral(form, after_form, set(by_name))
    if collateral:
        log.error("alliance text save changed untouched fields: %s", collateral)

    missing = [
        FIELDS[key].label for key, value in wanted.items()
        if _canon(after_form.get(key)) != _canon(value)
    ]
    if missing:
        reason = post_problem or "the game kept the old text"
        return SaveResult(
            False, True, f"not stored: {', '.join(missing)} ({reason})",
            before=before, after=after, collateral=collateral,
        )
    log.info("alliance texts saved: %s", ", ".join(sorted(wanted)))
    return SaveResult(
        True, True, "saved and confirmed", before=before, after=after,
        collateral=collateral,
    )
