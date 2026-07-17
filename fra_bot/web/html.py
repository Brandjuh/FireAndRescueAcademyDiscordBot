"""Server-rendered HTML for the web console: one shared dark layout in
the infographic's palette, plus small helpers. Every piece of user or
game data goes through :func:`esc` — nothing is trusted."""

from __future__ import annotations

import html as html_mod

#: Mutable on purpose: server.build_app appends each discovered
#: handlers_<domain> module's NAV_ENTRY here.
NAV: list[tuple[str, str]] = [
    ("/", "Dashboard"),
    ("/members", "Members"),
    ("/settings", "Settings"),
]

_CSS = """
:root { --bg:#17181d; --panel:#23242b; --panel2:#2b2c34; --ink:#ececf1;
        --soft:#a6a8b4; --muted:#6e7080; --accent:#f0521f; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.5 system-ui, "Segoe UI", sans-serif; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
header { display:flex; align-items:center; gap:24px; padding:14px 32px;
         background:var(--panel); border-bottom:1px solid #000; }
header .brand { font-weight:700; letter-spacing:.08em; color:var(--soft);
                font-size:13px; }
header nav a { color:var(--soft); margin-right:18px; font-weight:600; }
header nav a.active { color:var(--ink); border-bottom:2px solid var(--accent);
                      padding-bottom:4px; }
main { max-width:1100px; margin:28px auto; padding:0 24px; }
h1 { font-size:26px; margin:0 0 18px; }
h2 { font-size:15px; margin:0 0 12px; color:var(--muted);
     text-transform:uppercase; letter-spacing:.06em; }
.panel { background:var(--panel); border-radius:14px; padding:20px 24px;
         margin-bottom:22px; }
.tiles { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:22px; }
.tile { background:var(--panel); border-radius:14px; padding:18px 22px;
        min-width:170px; border-left:4px solid var(--accent); }
.tile .n { font-size:30px; font-weight:700; }
.tile .l { color:var(--muted); font-size:12px; text-transform:uppercase;
           letter-spacing:.06em; }
table { width:100%; border-collapse:collapse; }
th { text-align:left; color:var(--muted); font-size:12px;
     text-transform:uppercase; letter-spacing:.05em; padding:6px 10px;
     border-bottom:1px solid var(--panel2); }
td { padding:8px 10px; border-bottom:1px solid var(--panel2); }
tr:hover td { background:var(--panel2); }
.badge { display:inline-block; padding:1px 9px; border-radius:9px;
         font-size:12px; font-weight:600; }
.badge.ok { background:#1d3a2a; color:#7be0a3; }
.badge.off { background:#3a1d1d; color:#e07b7b; }
.badge.dim { background:var(--panel2); color:var(--soft); }
input, select, textarea { background:var(--bg); color:var(--ink);
    border:1px solid var(--panel2); border-radius:8px; padding:7px 10px;
    font:inherit; width:100%; }
textarea { min-height:70px; }
label { display:block; color:var(--muted); font-size:12px;
        text-transform:uppercase; letter-spacing:.05em; margin:10px 0 4px; }
button { background:var(--accent); color:#fff; border:0; border-radius:8px;
         padding:8px 18px; font:inherit; font-weight:700; cursor:pointer;
         margin-top:12px; }
button.small { padding:3px 12px; margin:0; font-weight:600; font-size:13px; }
button.ghost { background:var(--panel2); color:var(--soft); }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:0 22px; }
.muted { color:var(--muted); } .soft { color:var(--soft); }
.searchbar { display:flex; gap:10px; margin-bottom:16px; }
.searchbar input { max-width:340px; }
.searchbar button { margin-top:0; }
img.card { max-width:100%; border-radius:14px; display:block; }
.flash { background:#1d3a2a; color:#7be0a3; border-radius:10px;
         padding:10px 16px; margin-bottom:18px; }
.flash.err { background:#3a1d1d; color:#e07b7b; }
.timeline li { margin-bottom:6px; } .timeline { padding-left:18px; }
form.inline { display:inline; }
"""


def esc(value) -> str:
    """HTML-escape anything (None → empty string)."""
    return html_mod.escape(str(value)) if value is not None else ""


def page(title: str, body: str, *, active: str = "/",
         flash: str | None = None, flash_error: bool = False) -> str:
    nav = "".join(
        "<a href='{href}'{cls}>{label}</a>".format(
            href=href,
            cls=" class='active'" if href == active else "",
            label=label,
        )
        for href, label in NAV
    )
    notice = ""
    if flash:
        notice = (
            f'<div class="flash{" err" if flash_error else ""}">'
            f"{esc(flash)}</div>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{esc(title)} — FRA console</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<style>{_CSS}</style></head><body>"
        "<header><span class='brand'>FIRE &amp; RESCUE ACADEMY</span>"
        f"<nav>{nav}</nav></header>"
        f"<main>{notice}<h1>{esc(title)}</h1>{body}</main>"
        # Double-click guard: the browser still submits the form once, but
        # the button greys out so a nervous second click can't enqueue a
        # duplicate real action.
        "<script>document.addEventListener('submit',function(e){"
        "var b=e.target.querySelector('button');"
        "if(b){setTimeout(function(){b.disabled=true;},0);}});"
        "</script></body></html>"
    )


def tile(label: str, value) -> str:
    return (
        f"<div class='tile'><div class='n'>{esc(value)}</div>"
        f"<div class='l'>{esc(label)}</div></div>"
    )


def badge(text: str, kind: str = "dim") -> str:
    return f"<span class='badge {kind}'>{esc(text)}</span>"
