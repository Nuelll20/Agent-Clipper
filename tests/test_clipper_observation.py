import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from core.event_store import EventStore
from core.observation import RunObserver
from scripts import podcast_clipper as clipper


def ingest_args(tmp_path, handler):
    return argparse.Namespace(
        command="ingest", job="job-test", event_db=str(tmp_path / "events.db"), handler=handler,
    )


def snapshots(path):
    store = EventStore(path)
    try:
        return store.list_runs(), store.read_events()
    finally:
        store.close()


@pytest.mark.parametrize("code,state", [(0, "COMPLETED"), (1, "FAILED")])
def test_exit_status_is_persisted(tmp_path, code, state):
    args = ingest_args(tmp_path, lambda _: code)
    assert clipper.execute_observed(args) == code
    runs, events = snapshots(tmp_path / "events.db")
    assert runs[0]["state"] == state
    assert [e["state"] for e in events] == ["STARTING", state]
    assert args.observer is None


@pytest.mark.parametrize("error", [clipper.WorkflowError("private-detail"), KeyboardInterrupt()])
def test_exception_and_interrupt_are_recorded_then_reraised(tmp_path, error):
    def fail(args):
        clipper.observe(args, "transcribing", "Transcribing")
        raise error
    with pytest.raises(type(error)):
        clipper.execute_observed(ingest_args(tmp_path, fail))
    runs, events = snapshots(tmp_path / "events.db")
    assert runs[0]["state"] == "FAILED"
    assert events[1]["stage"] == "transcribing"
    assert "private-detail" not in json.dumps(events)


def test_unavailable_store_does_not_block_pipeline(tmp_path, capsys):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    calls = []
    args = ingest_args(tmp_path, lambda _: calls.append("ran"))
    args.event_db = str(blocked / "events.db")
    assert clipper.execute_observed(args) == 0
    assert calls == ["ran"]
    assert "events unavailable" in capsys.readouterr().err


def test_store_failure_mid_run_disables_observer(tmp_path, monkeypatch, capsys):
    observer = RunObserver(tmp_path / "events.db", "job", "CLIP_RENDER")
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(observer.store, "record", broken)
    observer.emit("rendering", "Working")
    observer.emit("completed", "Done", state="COMPLETED")
    assert observer.store is None
    assert "stored progress may be incomplete" in capsys.readouterr().err
    runs, _ = snapshots(tmp_path / "events.db")
    assert runs[0]["state"] == "STARTING"  # Never fabricate a completion.


def test_render_batch_counts_successes_and_preserves_failure_manifest(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    transcript = tmp_path / "transcript.json"
    transcript.write_text('{"segments": []}')
    plan_path = tmp_path / "clip-plan.json"
    plan_path.write_text(json.dumps({
        "job_id": "batch", "source_path": str(source), "transcript_path": str(transcript),
        "clips": [
            {"id": "clip-01", "start": 0, "end": 10, "hook": "First hook"},
            {"id": "clip-02", "start": 10, "end": 20, "hook": "Second hook"},
        ],
    }))
    monkeypatch.setattr(clipper, "ffprobe", lambda _: {"duration": 60})
    monkeypatch.setattr(clipper, "prepare_campaign", lambda *a: (None, None, None))
    def render(plan, item, source, transcript, job_dir, **kwargs):
        kwargs["observer"].emit("rendering", "Rendering", clip_id=item["id"])
        if item["id"] == "clip-01":
            raise clipper.WorkflowError("fixture failure")
        return {"clip_id": item["id"]}
    monkeypatch.setattr(clipper, "render_one_clip", render)
    args = clipper.build_parser().parse_args([
        "render", "--plan", str(plan_path), "--no-send", "--continue-on-error",
        "--event-db", str(tmp_path / "events.db"),
    ])
    assert clipper.execute_observed(args) == 1
    runs, events = snapshots(tmp_path / "events.db")
    assert (runs[0]["state"], runs[0]["current"], runs[0]["total"]) == ("FAILED", 1, 2)
    assert [e["clip_id"] for e in events if e["stage"] == "clip_failed"] == ["clip-01"]
    manifest = json.loads((tmp_path / "render-manifest.json").read_text())
    assert [c["status"] for c in manifest["clips"]] == ["failed", "completed"]


def test_cli_can_read_persisted_events_from_another_process(tmp_path):
    db = tmp_path / "events.db"
    clipper.execute_observed(ingest_args(tmp_path, lambda _: 0))
    script = Path(clipper.__file__).resolve()
    result = subprocess.run(
        [sys.executable, str(script), "events", "--event-db", str(db)],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    events = json.loads(result.stdout)
    assert events[-1]["state"] == "COMPLETED"
    result = subprocess.run(
        [sys.executable, str(script), "runs", "--job", "job-test", "--event-db", str(db)],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout)[0] == events[-1]
