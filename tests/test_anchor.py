from fra_bot.services.anchor import count_overlap


def test_full_overlap():
    assert count_overlap(["a", "b", "c"], ["a", "b", "c"]) == 3


def test_partial_overlap():
    # stored ...x,y ; scraped starts with x,y then new rows
    assert count_overlap(["w", "x", "y"], ["x", "y", "n1", "n2"]) == 2


def test_no_overlap():
    assert count_overlap(["a", "b"], ["c", "d"]) == 0


def test_empty_inputs():
    assert count_overlap([], ["a"]) == 0
    assert count_overlap(["a"], []) == 0


def test_identical_rows_align_as_sequence():
    # Three identical signatures are aligned as a run, not collapsed.
    tail = ["p", "dup", "dup", "dup"]
    scraped = ["dup", "dup", "dup", "new"]
    assert count_overlap(tail, scraped) == 3


def test_prefers_longest_match():
    tail = ["a", "b", "a", "b"]
    scraped = ["a", "b", "z"]
    # tail ends with [a, b] which equals scraped[:2]
    assert count_overlap(tail, scraped) == 2
