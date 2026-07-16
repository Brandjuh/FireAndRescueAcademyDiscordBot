"""Timeouts must stay inside the MissionChief error taxonomy.

The session's ClientTimeout raises asyncio.TimeoutError, which is NOT an
aiohttp.ClientError — uncaught it would skip fetch_page's retry/backoff
path and post_form's FetchError wrapping (which the callers' ambiguous
"the action may have landed" verification keys on).
"""

import asyncio
from types import SimpleNamespace

import pytest

from fra_bot.mc.client import MissionChiefClient
from fra_bot.mc.errors import FetchError, MissionChiefError

pytestmark = pytest.mark.asyncio


class _Pacer:
    def __init__(self):
        self.failures = 0
        self.successes = 0

    async def wait_turn(self):
        pass

    def record_failure(self):
        self.failures += 1

    def record_success(self):
        self.successes += 1


class _TimeoutSession:
    """Every request times out (the ClientTimeout way)."""

    closed = False

    def get(self, *a, **k):
        raise asyncio.TimeoutError()

    def post(self, *a, **k):
        raise asyncio.TimeoutError()


def _client() -> tuple[MissionChiefClient, _Pacer]:
    cfg = SimpleNamespace(
        base_url="https://www.missionchief.com",
        cookie_path="",
        username="",
        password="",
    )
    pacer = _Pacer()
    client = MissionChiefClient(cfg, pacer)
    client._session = _TimeoutSession()
    return client, pacer


async def test_fetch_page_timeout_retries_then_raises_fetch_error(monkeypatch):
    client, pacer = _client()
    sleeps = []

    async def _no_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    with pytest.raises(FetchError):
        await client.fetch_page("/buildings")
    # Every attempt was a recorded failure and backed off — the timeout went
    # through the retry path, it did not escape as a bare TimeoutError.
    assert pacer.failures == 3
    assert len(sleeps) == 3


async def test_post_form_timeout_wraps_into_fetch_error():
    client, pacer = _client()
    with pytest.raises(MissionChiefError) as exc:
        await client.post_form("/buildings/1/education", {"a": "b"})
    assert isinstance(exc.value, FetchError)
    assert pacer.failures == 1
