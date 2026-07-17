"""Offline smoke driver for the FRA Discord bot.

The bot's two external surfaces (Discord, missionchief.com) are
unreachable from the dev container, so this driver exercises the layer
every PR actually touches — config loading, migrations, repos, parsers
and the service state machines — against canned HTML, end to end:

    .venv/bin/python .claude/skills/run-fra-discord-bot/driver.py

Flows (all run by default; pick one with e.g. ``--flow training``):
  config        load config.example.yaml through the real loader
  migrations    connect a fresh DB, run every migration
  training      Discord training request -> execute -> class opened
  applications  application auto-accept -> annehmen GET -> resolved
  faq           add an entry, fuzzy-search it via a synonym
  chat          parse the alliance-chat form + history fixtures
  settings      resolve/parse a `!fra set` key round-trip

Exit code 0 = every flow passed. Non-zero = the printed flow failed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

ACADEMY_LIST = (
    "<table><tr search_attribute='Fire Academy'>"
    "<td><img building_id='4951748' src='/img/fire.png' alt='Fire'/></td>"
    "<td><a href='/buildings/4951748' class='btn btn-success'>"
    "Start a new training course</a></td></tr></table>"
)
ACADEMY_PAGE = (
    "<form action='/buildings/4951748/education' method='post'>"
    "<input type='hidden' name='authenticity_token' value='tok'/>"
    "<select name='building_rooms_use'><option value='1'>1</option>"
    "<option value='2'>2</option></select>"
    "<select name='alliance[cost]'><option value='0'>Free</option></select>"
    "<select name='education_select'><option value='12'>HazMat</option></select>"
    "<input type='submit' value='Educate'/></form>"
)
CHAT_FORM = (
    "<form action='/alliance_chats' id='new_alliance_chat' method='post'>"
    "<input name='utf8' type='hidden' value='x'/>"
    "<input name='authenticity_token' type='hidden' value='secret'/>"
    "<input name='alliance_chat[message]' type='text'/></form>"
)
CHAT_HISTORY = (
    "<div id='chat_message_42' data-message-time='2026-07-17T10:00:00-04:00'>"
    "<strong><a href='/profile/814047'>Mtycofire</a></strong>"
    "<div class='message-content'><p>hello alliance</p></div></div>"
)


class FakeClient:
    """MissionChiefClient stand-in serving canned pages."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.fetched: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def url(self, path: str) -> str:
        return "https://www.missionchief.com/" + path.lstrip("/")

    async def fetch_page(self, path, *, referer=None, ajax=False):
        self.fetched.append(path)
        return self.pages.get(path, "<html></html>")

    async def post_form(self, path, data, **kwargs):
        self.posts.append((path, dict(data)))
        return 200, "", ""


async def flow_config() -> str:
    from fra_bot.config import load_config

    os.environ.setdefault("DISCORD_TOKEN", "smoke")
    os.environ.setdefault("MC_EMAIL", "smoke@example.com")
    os.environ.setdefault("MC_PASSWORD", "smoke")
    cfg = load_config(REPO / "config.example.yaml")
    assert cfg.missionchief.alliance_id == 1621
    assert cfg.automation.dry_run is True  # the example ships SAFE
    return f"alliance {cfg.missionchief.alliance_id}, dry_run={cfg.automation.dry_run}"


async def _fresh_db(tmp: Path):
    from fra_bot.db.database import Database

    db = Database(tmp / "smoke.sqlite3")
    await db.connect()
    return db


async def flow_migrations(tmp: Path) -> str:
    db = await _fresh_db(tmp)
    try:
        async with db.conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'"
        ) as cur:
            n = (await cur.fetchone())["n"]
        assert n >= 25, f"expected 25+ tables after migrations, got {n}"
        return f"{n} tables migrated"
    finally:
        await db.close()


async def flow_training(tmp: Path) -> str:
    from fra_bot.db.repos import (
        AutomationRepo, BoardDeletionRepo, BoardRepo, MembersRepo,
        RemindersRepo, RunsRepo, StateRepo,
    )
    from fra_bot.mc.board import BoardClient
    from fra_bot.services.trainings import TrainingsService

    db = await _fresh_db(tmp)
    try:
        cfg = SimpleNamespace(automation=SimpleNamespace(
            dry_run=True, reply_to_board=False,
            training=SimpleNamespace(
                enabled=True, thread_id=5935, interval=5,
                min_contribution_rate=5.0, preferred_academies={},
            ),
        ))
        client = FakeClient({
            "/verband/gebauede": ACADEMY_LIST,
            "/buildings/4951748": ACADEMY_PAGE,
        })
        svc = TrainingsService.__new__(TrainingsService)
        svc.cfg, svc.client, svc.board = cfg, client, BoardClient(client)
        svc.board_repo, svc.requests = BoardRepo(db), AutomationRepo(db)
        svc.members, svc.runs = MembersRepo(db), RunsRepo(db)
        svc.state, svc.deletions = StateRepo(db), BoardDeletionRepo(db)
        svc.reminders = RemindersRepo(db)
        svc._auto = cfg.automation.training

        rid = await svc.requests.create(
            kind="training", thread_id=0, post_id=1,
            requester_name="SmokeTester", requester_mc_id=None,
            payload=json.dumps({
                "trainings": [{"discipline": "fire", "name": "HazMat",
                               "duration": 3, "count": 1}],
                "ambiguous": [], "discord_user_id": 1, "channel_id": None,
                "remind": False,
            }),
        )
        executed = await svc.execute_queue_now()
        row = await svc.requests.get(rid)
        assert executed == 1 and row["status"] == "done", (
            f"expected done, got {row['status']}: {row['status_detail']}"
        )
        return f"request #{rid} -> {row['status']} ({row['status_detail']})"
    finally:
        await db.close()


async def flow_applications(tmp: Path) -> str:
    from fra_bot.db.repos import ApplicationsRepo
    from fra_bot.services.applications_sync import ApplicationsSyncService

    db = await _fresh_db(tmp)
    try:
        apps = ApplicationsRepo(db)
        await apps.upsert_seen([{
            "application_id": 77, "applicant_name": "Rookie", "mc_user_id": 5,
        }])
        client = FakeClient({})
        await ApplicationsSyncService(client, db).accept(77)
        assert "/verband/bewerbungen/annehmen/77" in client.fetched
        assert (await apps.get(77))["resolved_at"] is not None
        return "application 77 accepted and resolved"
    finally:
        await db.close()


async def flow_faq(tmp: Path) -> str:
    from fra_bot.db.repos import FaqRepo
    from fra_bot.services.faq_search import rank_faqs

    db = await _fresh_db(tmp)
    try:
        repo = FaqRepo(db)
        await repo.add(
            question="How do I configure Alarm and Response Regulations?",
            answer="Settings -> ARR, one rule per vehicle set.",
            created_by="driver",
        )
        ranked = rank_faqs("arr setup", await repo.all_active())
        assert ranked, "synonym query found nothing"
        return f"'arr setup' -> FAQ #{ranked[0].faq_id} (score {ranked[0].score:.0f})"
    finally:
        await db.close()


async def flow_chat() -> str:
    from fra_bot.mc.parsers.chat import parse_chat_form, parse_chat_history

    form = parse_chat_form(CHAT_FORM, "https://www.missionchief.com/")
    history = parse_chat_history(CHAT_HISTORY)
    assert form.hidden_fields["authenticity_token"] == "secret"
    assert history and history[0].username == "Mtycofire"
    return f"form ok, {len(history)} chat message(s) parsed"


async def flow_settings() -> str:
    from fra_bot.core import settings as rt
    from fra_bot.config import load_config

    cfg = load_config(REPO / "config.example.yaml")
    setting = rt.resolve("training.enabled")
    value = rt.parse_value(setting, "on", cfg)
    assert setting.path == "automation.training.enabled" and value is True
    return f"{setting.path} <- 'on' parsed to {value}"


FLOWS = {
    "config": lambda tmp: flow_config(),
    "migrations": flow_migrations,
    "training": flow_training,
    "applications": flow_applications,
    "faq": flow_faq,
    "chat": lambda tmp: flow_chat(),
    "settings": lambda tmp: flow_settings(),
}


async def main() -> int:
    wanted = None
    if "--flow" in sys.argv:
        wanted = sys.argv[sys.argv.index("--flow") + 1]
        if wanted not in FLOWS:
            print(f"unknown flow {wanted!r}; pick from: {', '.join(FLOWS)}")
            return 2
    failures = 0
    with tempfile.TemporaryDirectory(prefix="fra-smoke-") as tmpdir:
        tmp = Path(tmpdir)
        for name, flow in FLOWS.items():
            if wanted and name != wanted:
                continue
            try:
                detail = await flow(tmp)
                print(f"  OK  {name:<13} {detail}")
            except Exception as exc:  # noqa: BLE001 — report and continue
                failures += 1
                print(f"FAIL  {name:<13} {type(exc).__name__}: {exc}")
    print("driver:", "ALL FLOWS PASSED" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
