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


async def test_bulk_traffic_yields_to_interactive():
    """Priority ordering: while interactive requests are waiting, a bulk
    request (backfill) holds back — even one that arrived earlier."""
    from fra_bot.core.pacing import bulk_traffic

    pacer = HumanPacer(min_delay=0.1, max_delay=0.1, max_per_minute=1000)
    await pacer.wait_turn()                     # arm the 0.1s gap
    order: list[str] = []

    async def interactive(name):
        await pacer.wait_turn()
        order.append(name)

    async def bulk():
        with bulk_traffic():
            await pacer.wait_turn()
        order.append("bulk")

    # P1 goes first and occupies the slot; BULK arrives before P2 but must
    # let P2 (interactive) through first.
    p1 = asyncio.create_task(interactive("p1"))
    await asyncio.sleep(0.02)                   # p1 is inside, waiting its slot
    b = asyncio.create_task(bulk())
    await asyncio.sleep(0.02)                   # bulk parked behind the gate
    p2 = asyncio.create_task(interactive("p2"))
    await asyncio.gather(p1, b, p2)
    assert order == ["p1", "p2", "bulk"]


async def test_bulk_backlog_counter():
    from fra_bot.core.pacing import bulk_traffic

    pacer = HumanPacer(min_delay=0.15, max_delay=0.15, max_per_minute=1000)
    await pacer.wait_turn()                     # arm the gap
    with bulk_traffic():
        task = asyncio.create_task(pacer.wait_turn())
    # The task inherits the bulk flag via its context snapshot.
    await asyncio.sleep(0.03)
    assert pacer.backlog == 1 and pacer.backlog_bulk == 1
    await task
    assert pacer.backlog == 0 and pacer.backlog_bulk == 0


async def test_bulk_proceeds_when_no_interactive_waiting():
    from fra_bot.core.pacing import bulk_traffic

    pacer = HumanPacer(min_delay=0.0, max_delay=0.0, max_per_minute=1000)
    with bulk_traffic():
        await pacer.wait_turn()                 # no gate to wait on
    assert pacer.backlog == 0


async def test_interactive_preempts_a_bulk_waiter_mid_sleep():
    """Sleeps happen OUTSIDE the pacing lock: a bulk request that is
    already waiting out a delay must NOT block an interactive request
    that arrives later — with the sleep inside the lock, the interactive
    caller sat out the bulk sleeper's whole delay first (head-of-line
    blocking, the recurring slow-poll-tick ingredient)."""
    from fra_bot.core.pacing import bulk_traffic

    pacer = HumanPacer(min_delay=0.25, max_delay=0.25, max_per_minute=1000)
    await pacer.wait_turn()                       # arms the 0.25s gap
    order = []

    async def bulk_job():
        with bulk_traffic():
            await pacer.wait_turn()
        order.append("bulk")

    async def interactive_job():
        await pacer.wait_turn()
        order.append("interactive")

    b = asyncio.create_task(bulk_job())
    await asyncio.sleep(0.05)                     # bulk is mid-sleep now
    p = asyncio.create_task(interactive_job())
    await asyncio.wait_for(asyncio.gather(b, p), timeout=5.0)
    assert order == ["interactive", "bulk"]


async def test_circuit_opening_interrupts_a_sleeping_waiter():
    """A circuit that opens while a request waits its turn must be seen
    when the waiter re-checks — not sailed past because the check
    happened before the sleep."""
    import pytest

    from fra_bot.core.pacing import CircuitOpenError

    pacer = HumanPacer(
        min_delay=0.3, max_delay=0.3, max_per_minute=1000,
        failure_threshold=1, cooldown_seconds=60.0,
    )
    await pacer.wait_turn()                       # arms the 0.3s gap
    task = asyncio.create_task(pacer.wait_turn())
    await asyncio.sleep(0.05)                     # task is mid-sleep
    pacer.record_failure()                        # breaker trips (threshold 1)
    with pytest.raises(CircuitOpenError):
        await asyncio.wait_for(task, timeout=5.0)
