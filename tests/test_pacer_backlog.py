"""The pacer's congestion gauge (backlog of requests waiting their turn)."""

import asyncio

from fra_bot.core.pacing import HumanPacer


async def test_backlog_counts_waiting_requests():
    pacer = HumanPacer(min_delay=0.0, max_delay=0.0, max_per_minute=1000)
    assert pacer.backlog == 0
    await pacer.wait_turn()
    assert pacer.backlog == 0              # back to zero after the turn


async def test_backlog_visible_while_queued():
    # A slow pacer with two concurrent callers: while one waits for its
    # delay, the gauge shows the queue depth.
    pacer = HumanPacer(min_delay=0.15, max_delay=0.15, max_per_minute=1000)
    await pacer.wait_turn()                # arms the 0.15s gap
    task = asyncio.create_task(pacer.wait_turn())
    await asyncio.sleep(0.05)              # task is now waiting its turn
    assert pacer.backlog == 1
    await task
    assert pacer.backlog == 0
