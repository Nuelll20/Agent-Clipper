import sqlite3

import pytest

from core.event_store import EventStore


def test_reopen_and_cursor_replay_preserve_snapshot(tmp_path):
    path = tmp_path / "events.db"
    store = EventStore(path)
    run = store.start_run("episode-01", "CLIP_RENDER")
    first = store.read_events(run)[0]
    store.record(run, "WORKING", "rendering", "Rendering clip", current=0, total=2)
    final = store.record(run, "COMPLETED", "completed", "Done", current=2, total=2)
    store.close()
    reopened = EventStore(path)
    try:
        assert reopened.list_runs("episode-01") == [final]
        page = reopened.read_events(run, after=first["sequence"], limit=1)
        assert page[0]["stage"] == "rendering"
        assert reopened.read_events(run, after=page[0]["sequence"]) == [final]
    finally:
        reopened.close()


def test_event_and_snapshot_rollback_together(tmp_path):
    store = EventStore(tmp_path / "events.db")
    try:
        run = store.start_run("job", "CLIP_RENDER")
        original = store.list_runs()[0]
        store.connection.execute("""
            CREATE TRIGGER reject_snapshot BEFORE UPDATE ON runs
            BEGIN SELECT RAISE(ABORT, 'test snapshot failure'); END;
        """)
        with pytest.raises(sqlite3.IntegrityError):
            store.record(run, "WORKING", "rendering", "Rendering")
        assert store.list_runs() == [original]
        assert store.read_events(run) == [original]
    finally:
        store.close()


def test_two_connections_and_reruns_are_isolated(tmp_path):
    path = tmp_path / "events.db"
    one, two = EventStore(path), EventStore(path)
    try:
        a = one.start_run("same-job", "CLIP_RENDER")
        b = two.start_run("same-job", "CLIP_RENDER")
        one.record(a, "FAILED", "rendering", "Failed")
        two.record(b, "COMPLETED", "completed", "Done")
        assert a != b
        assert [r["state"] for r in one.list_runs("same-job")] == ["COMPLETED", "FAILED"]
        with pytest.raises(ValueError, match="finished run"):
            two.record(a, "WORKING", "rendering", "Stale worker")
        assert all(e["run_id"] == b for e in one.read_events(b))
    finally:
        one.close()
        two.close()


@pytest.mark.parametrize("current,total", [(1, None), (-1, 2), (3, 2), (1.5, 2), (True, 2)])
def test_invalid_progress_does_not_change_history(tmp_path, current, total):
    store = EventStore(tmp_path / "events.db")
    try:
        run = store.start_run("job", "CLIP_RENDER")
        with pytest.raises(ValueError):
            store.record(run, "WORKING", "rendering", "Work", current=current, total=total)
        assert len(store.read_events()) == 1
    finally:
        store.close()
