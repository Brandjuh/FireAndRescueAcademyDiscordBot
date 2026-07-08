

async def test_backlog_counts_waiting_requests():
    from fra_bot.core.pacing import HumanPacer

    pacer = HumanPacer(min_delay=0.0, max_delay=0.0, max_per_minute=1000)
    assert pacer.backlog == 0
    await pacer.wait_turn()
    assert pacer.backlog == 0          # returns to zero after the turn
