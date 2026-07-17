"""Web console: the FAQ knowledge base.

List, add, edit and soft-delete go through :class:`FaqRepo` exactly
like the ``!faq`` commands — FAQ entries live only in our own database,
so these are the rare web mutations that touch no game state and need
no pacing or dry-run gate. Soft-deleted rows keep their history but
never surface again (repo semantics).

The search box is a preview, not a second search engine: it runs the
SAME scorer and thresholds as the Discord command
(:mod:`fra_bot.services.faq_search`), so the page shows verbatim which
of the three ``!faq <query>`` outcomes the bot would pick — answer,
"did you mean" suggestions, or no match.

The Discord FAQ commands do not write the member-action log (FAQ
entries are knowledge, not member records), so the web mutations do
not either.
"""

from __future__ import annotations

from aiohttp import web

from ..db.repos import FaqRepo
from ..services.faq_search import SUGGESTION_THRESHOLD, rank_faqs
from .handlers import WEB_ACTOR, _bot, _flash, _redirect
from .html import badge, esc, page

NAV_ENTRY = ("/faq", "FAQ")

_PREVIEW_CHARS = 110


def _preview(text) -> str:
    """One collapsed line of the answer for the listing table."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= _PREVIEW_CHARS:
        return esc(flat)
    return esc(flat[: _PREVIEW_CHARS - 1]) + "…"


async def _search_panel(repo: FaqRepo, query: str) -> str:
    """What ``!faq <query>`` would do, via the command's own ranking."""
    ranked = rank_faqs(query, await repo.all_active())
    if not ranked:
        inner = (
            "<p class='muted'>No entry scores above the minimum — the bot "
            "would answer that nothing in the FAQ matches.</p>"
        )
    elif ranked[0].score >= SUGGESTION_THRESHOLD:
        row = await repo.get(ranked[0].faq_id)
        inner = (
            f"<p>{badge('would answer', 'ok')} "
            f"<a href='/faq/{row['id']}'><b>#{row['id']} "
            f"{esc(row['question'])}</b></a> "
            f"<span class='muted'>score {ranked[0].score:.0f}</span></p>"
            f"<p style='white-space:pre-wrap'>{esc(row['answer'])}</p>"
        )
    else:
        options = "".join(
            f"<li><a href='/faq/{s.faq_id}'>#{s.faq_id} {esc(s.question)}</a>"
            f" <span class='muted'>score {s.score:.0f}</span></li>"
            for s in ranked[:5]
        )
        inner = (
            f"<p>{badge('did you mean', 'dim')} the best match scores under "
            f"{SUGGESTION_THRESHOLD}, so the bot would suggest instead of "
            f"answering:</p><ul class='timeline'>{options}</ul>"
        )
    return (
        f"<div class='panel'><h2>Bot answer preview for "
        f"“{esc(query)}”</h2>{inner}</div>"
    )


async def faq_page(request: web.Request) -> web.Response:
    bot = _bot(request)
    repo = FaqRepo(bot.db)
    query = (request.query.get("q") or "").strip()
    search_html = await _search_panel(repo, query) if query else ""

    rows = await repo.all_active()
    lines = "".join(
        "<tr>"
        f"<td><a href='/faq/{row['id']}'>#{row['id']}</a></td>"
        f"<td><a href='/faq/{row['id']}'>{esc(row['question'])}</a></td>"
        f"<td>{_preview(row['answer'])}</td>"
        f"<td>{esc(row['keywords'] or '—')}</td>"
        f"<td>{esc(row['category'] or '—')}</td>"
        f"<td>{esc(str(row['updated_at'])[:10])}</td>"
        "<td><form class='inline' method='post' "
        f"action='/faq/{row['id']}/delete'>"
        "<button class='small ghost'>Delete</button></form></td>"
        "</tr>"
        for row in rows
    ) or "<tr><td colspan='7' class='muted'>The FAQ is empty.</td></tr>"

    add_form = (
        "<form method='post' action='/faq/add'>"
        "<label>Question</label><input name='question' required>"
        "<label>Answer</label><textarea name='answer' required></textarea>"
        "<label>Category (optional)</label><input name='category'>"
        "<label>Keywords (optional, comma separated — extra search terms)"
        "</label><input name='keywords'>"
        "<button>Add entry</button></form>"
    )

    body = (
        "<form class='searchbar' method='get'>"
        "<input name='q' placeholder='Preview what the bot would answer' "
        f"value='{esc(query)}'>"
        "<button>Preview</button></form>"
        + search_html
        + f"<div class='panel'><h2>Entries ({len(rows)})</h2>"
        "<table><tr><th>#</th><th>Question</th><th>Answer</th>"
        "<th>Keywords</th><th>Category</th><th>Updated</th><th></th></tr>"
        f"{lines}</table></div>"
        f"<div class='panel'><h2>Add entry</h2>{add_form}</div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("FAQ", body, active="/faq", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


async def faq_detail(request: web.Request) -> web.Response:
    bot = _bot(request)
    faq_id = int(request.match_info["faq_id"])
    row = await FaqRepo(bot.db).get(faq_id)
    if row is None:
        raise web.HTTPNotFound(text="Unknown FAQ entry")

    edit_form = (
        f"<form method='post' action='/faq/{faq_id}/edit'>"
        "<label>Question</label>"
        f"<input name='question' value='{esc(row['question'])}' required>"
        "<label>Answer</label>"
        f"<textarea name='answer' required>{esc(row['answer'])}</textarea>"
        "<label>Category</label>"
        f"<input name='category' value='{esc(row['category'] or '')}'>"
        "<label>Keywords (comma separated)</label>"
        f"<input name='keywords' value='{esc(row['keywords'] or '')}'>"
        "<button>Save changes</button></form>"
    )
    body = (
        f"<p class='muted'>Added by {esc(row['created_by'])} on "
        f"{esc(str(row['created_at'])[:10])} · last updated "
        f"{esc(str(row['updated_at'])[:16])} · "
        "<a href='/faq'>back to the FAQ</a></p>"
        "<div class='grid2'>"
        "<div class='panel'><h2>Answer as the bot posts it</h2>"
        f"<p style='white-space:pre-wrap'>{esc(row['answer'])}</p></div>"
        f"<div class='panel'><h2>Edit entry</h2>{edit_form}</div>"
        "</div>"
        "<div class='panel'><h2>Remove</h2>"
        f"<form method='post' action='/faq/{faq_id}/delete'>"
        "<button class='ghost'>Soft-delete this entry</button></form>"
        "<p class='muted'>The row stays in the database for history but "
        "never surfaces in search or listings again.</p></div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page(f"FAQ #{faq_id} — {row['question']}", body, active="/faq",
                  flash=flash, flash_error=is_err),
        content_type="text/html",
    )


async def post_add(request: web.Request) -> web.Response:
    bot = _bot(request)
    form = await request.post()
    question = str(form.get("question") or "").strip()
    answer = str(form.get("answer") or "").strip()
    if not question or not answer:
        _redirect("/faq", err="Question and answer are both required.")
    # Same insert the `!faq add` command performs; category/keywords ride
    # along in the one repo call instead of a second `!faq keywords` step.
    faq_id = await FaqRepo(bot.db).add(
        question=question, answer=answer, created_by=WEB_ACTOR,
        category=str(form.get("category") or "").strip() or None,
        keywords=str(form.get("keywords") or "").strip() or None,
    )
    _redirect("/faq", ok=f"FAQ #{faq_id} added.")


async def post_edit(request: web.Request) -> web.Response:
    bot = _bot(request)
    faq_id = int(request.match_info["faq_id"])
    form = await request.post()
    question = str(form.get("question") or "").strip()
    answer = str(form.get("answer") or "").strip()
    if not question or not answer:
        _redirect(f"/faq/{faq_id}",
                  err="Question and answer are both required.")
    # category/keywords go in as "" (not None) so clearing the field in
    # the form really clears the column — repo.update skips None values.
    updated = await FaqRepo(bot.db).update(
        faq_id, question=question, answer=answer,
        category=str(form.get("category") or "").strip(),
        keywords=str(form.get("keywords") or "").strip(),
    )
    if not updated:
        _redirect("/faq", err=f"FAQ #{faq_id} does not exist.")
    _redirect(f"/faq/{faq_id}", ok=f"FAQ #{faq_id} updated.")


async def post_delete(request: web.Request) -> web.Response:
    bot = _bot(request)
    faq_id = int(request.match_info["faq_id"])
    if not await FaqRepo(bot.db).remove(faq_id):
        _redirect("/faq", err=f"FAQ #{faq_id} does not exist.")
    _redirect("/faq", ok=f"FAQ #{faq_id} removed.")


ROUTES = [
    web.get("/faq", faq_page),
    web.get("/faq/{faq_id:\\d+}", faq_detail),
    web.post("/faq/add", post_add),
    web.post("/faq/{faq_id:\\d+}/edit", post_edit),
    web.post("/faq/{faq_id:\\d+}/delete", post_delete),
]
