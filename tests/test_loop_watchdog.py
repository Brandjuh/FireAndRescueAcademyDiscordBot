"""The stall watchdog names what holds the loop, and academy clicks that
Discord already expired are dropped instead of built."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace

import discord
import pytest

from fra_bot.cogs.academy import AcademyCog
from fra_bot.core.loop_watchdog import LoopStallWatchdog
from fra_bot.mc.parsers import academy as parsers

pytestmark = pytest.mark.asyncio


def _hold_the_loop(seconds: float) -> None:
    time.sleep(seconds)


async def test_watchdog_logs_the_stack_of_a_stalled_loop(caplog):
    caplog.set_level(logging.WARNING, logger="fra_bot.core.loop_watchdog")
    dog = LoopStallWatchdog(threshold=0.1, interval=0.02)
    dog.start()
    try:
        await asyncio.sleep(0.05)
        _hold_the_loop(0.3)
        await asyncio.sleep(0.05)
    finally:
        dog.stop()
    assert "_hold_the_loop" in caplog.text
    assert "event loop stall ended" in caplog.text


async def test_watchdog_quiet_when_loop_is_free(caplog):
    caplog.set_level(logging.WARNING, logger="fra_bot.core.loop_watchdog")
    dog = LoopStallWatchdog(threshold=0.2, interval=0.02)
    dog.start()
    try:
        await asyncio.sleep(0.3)
    finally:
        dog.stop()
    assert caplog.text == ""


async def test_alliance_buildings_parse_runs_off_the_loop(monkeypatch):
    threads = []

    def fake_parse(html):
        threads.append(threading.get_ident())
        return ["listing"]

    monkeypatch.setattr(parsers, "parse_alliance_buildings_page", fake_parse)
    monkeypatch.setattr(parsers, "find_next_page_path", lambda html: "/next")
    assert await parsers.parse_alliance_buildings_off_loop("<html>") == (["listing"], "/next")
    assert threads and threads[0] != threading.get_ident()


async def test_expired_academy_click_builds_nothing(monkeypatch):
    async def expired(**kwargs):
        response = SimpleNamespace(status=404, reason="Not Found")
        raise discord.NotFound(response, {"code": 10062, "message": "Unknown interaction"})

    async def must_not_enqueue(*args, **kwargs):
        raise AssertionError("an expired click must not build")

    monkeypatch.setattr("fra_bot.cogs.academy._role_ok", lambda interaction: True)
    bot = SimpleNamespace(academy=SimpleNamespace(enqueue=must_not_enqueue))
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=expired),
        created_at=discord.utils.utcnow(),
    )
    await AcademyCog(bot).build(interaction, "police")
