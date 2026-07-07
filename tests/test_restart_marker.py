"""Tests for the self-update restart marker.

After !fra update the process re-execs; the marker is how the fresh
process knows to post a "restarted" confirmation in the right channel.
"""

import json
import time

from fra_bot.selfupdate import (
    _marker_path,
    read_and_clear_restart_marker,
    write_restart_marker,
)


def test_write_read_roundtrip(tmp_path):
    db = tmp_path / "data" / "fra.sqlite3"
    write_restart_marker(db, channel_id=999, old_rev="aaa", new_rev="bbb")
    assert _marker_path(db).exists()

    marker = read_and_clear_restart_marker(db)
    assert marker["channel_id"] == 999
    assert marker["old_rev"] == "aaa"
    assert marker["new_rev"] == "bbb"


def test_marker_cleared_after_read(tmp_path):
    db = tmp_path / "fra.sqlite3"
    write_restart_marker(db, channel_id=1, old_rev="a", new_rev="b")
    assert read_and_clear_restart_marker(db) is not None
    # Second read: gone, so no double-post on a reconnect.
    assert read_and_clear_restart_marker(db) is None


def test_absent_marker_returns_none(tmp_path):
    assert read_and_clear_restart_marker(tmp_path / "fra.sqlite3") is None


def test_stale_marker_ignored_but_cleared(tmp_path):
    db = tmp_path / "fra.sqlite3"
    _marker_path(db).parent.mkdir(parents=True, exist_ok=True)
    _marker_path(db).write_text(
        json.dumps(
            {
                "channel_id": 1,
                "old_rev": "x",
                "new_rev": "y",
                "written_at": time.time() - 700,  # older than the 600s window
            }
        )
    )
    assert read_and_clear_restart_marker(db) is None
    assert not _marker_path(db).exists()


def test_corrupt_marker_is_safe(tmp_path):
    db = tmp_path / "fra.sqlite3"
    _marker_path(db).parent.mkdir(parents=True, exist_ok=True)
    _marker_path(db).write_text("not json {{{")
    assert read_and_clear_restart_marker(db) is None
    assert not _marker_path(db).exists()
