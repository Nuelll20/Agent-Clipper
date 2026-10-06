import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from core.event_store import EventStore
from scripts import podcast_clipper as clipper
from scripts import telegram_approval as approval


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def event_history(path):
    store = EventStore(path)
    try:
        return store.read_events()
    finally:
        store.close()


def build_render_fixture(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    transcript = tmp_path / "transcript.json"
    transcript.write_text('{"segments": []}', encoding="utf-8")
    plan = tmp_path / "clip-plan.json"
    plan.write_text(
        json.dumps({
            "job_id": "delivery-job",
            "source_path": str(source),
            "transcript_path": str(transcript),
            "clips": [{"id": "clip-01", "start": 0, "end": 10, "hook": "Hook"}],
        }),
        encoding="utf-8",
    )
    video = tmp_path / "clip-final.mp4"
    video.write_bytes(b"rendered")
    caption = tmp_path / "caption.txt"
    caption.write_text("Caption", encoding="utf-8")
    return plan, video, caption


def test_delivery_failure_preserves_render_and_retry_does_not_render(tmp_path, monkeypatch):
    plan, video, caption = build_render_fixture(tmp_path)
    event_db = tmp_path / "events.db"
    render_calls = []

    monkeypatch.setattr(clipper, "ffprobe", lambda _: {"duration": 60})
    monkeypatch.setattr(clipper, "prepare_campaign", lambda *args: (None, None, None))

    def fake_render(*args, **kwargs):
        render_calls.append("render")
        return {
            "clip_id": "clip-01",
            "output_video": str(video),
            "caption_path": str(caption),
            "rendered_at": "2026-10-06T00:00:00+00:00",
            "telegram_sent": False,
        }

    monkeypatch.setattr(clipper, "render_one_clip", fake_render)
    monkeypatch.setattr(
        clipper,
        "run_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(clipper.WorkflowError("offline")),
    )
    args = clipper.build_parser().parse_args([
        "render", "--plan", str(plan), "--event-db", str(event_db),
    ])
    assert clipper.execute_observed(args) == 1

    manifest_path = tmp_path / "render-manifest.json"
    rendered = read_json(manifest_path)["clips"][0]
    assert rendered["status"] == "completed"
    assert rendered["delivery"]["status"] == "failed"
    assert rendered["delivery"]["attempts"] == 1
    assert rendered["telegram_sent"] is False
    stages = [event["stage"] for event in event_history(event_db)]
    assert "clip_completed" in stages
    assert "delivery_failed" in stages
    assert "clip_failed" not in stages

    commands = []
    monkeypatch.setattr(clipper, "run_command", lambda command, **kwargs: commands.append(command))
    retry = clipper.build_parser().parse_args([
        "deliver", "--manifest", str(manifest_path), "--event-db", str(event_db),
    ])
    assert clipper.execute_observed(retry) == 0
    delivered = read_json(manifest_path)["clips"][0]
    assert delivered["status"] == "completed"
    assert delivered["delivery"]["status"] == "sent"
    assert delivered["delivery"]["attempts"] == 2
    assert delivered["telegram_sent"] is True
    assert len(render_calls) == 1
    assert len(commands) == 1
    assert "--delivery-key" in commands[0]

    assert clipper.execute_observed(retry) == 0
    assert len(commands) == 1  # A sent artifact is not delivered twice.


def test_no_send_records_local_render_without_delivery(tmp_path, monkeypatch):
    plan, video, caption = build_render_fixture(tmp_path)
    monkeypatch.setattr(clipper, "ffprobe", lambda _: {"duration": 60})
    monkeypatch.setattr(clipper, "prepare_campaign", lambda *args: (None, None, None))
    monkeypatch.setattr(
        clipper,
        "render_one_clip",
        lambda *args, **kwargs: {
            "clip_id": "clip-01",
            "output_video": str(video),
            "caption_path": str(caption),
            "rendered_at": "2026-10-06T00:00:00+00:00",
            "telegram_sent": False,
        },
    )
    monkeypatch.setattr(
        clipper,
        "run_command",
        lambda *args, **kwargs: pytest.fail("Telegram must not run with --no-send"),
    )
    args = clipper.build_parser().parse_args([
        "render", "--plan", str(plan), "--no-send",
        "--event-db", str(tmp_path / "events.db"),
    ])
    assert clipper.execute_observed(args) == 0
    item = read_json(tmp_path / "render-manifest.json")["clips"][0]
    assert item["status"] == "completed"
    assert item["delivery"]["status"] == "not_requested"
    assert item["delivery"]["attempts"] == 0


def test_delivery_resolves_legacy_relative_artifact_paths(tmp_path, monkeypatch):
    _, video, caption = build_render_fixture(tmp_path)
    manifest_path = tmp_path / "render-manifest.json"
    manifest_path.write_text(
        json.dumps({
            "job_id": "delivery-job",
            "clips": [{
                "status": "completed",
                "clip_id": "clip-01",
                "output_video": video.name,
                "caption_path": caption.name,
                "rendered_at": "2026-10-06T00:00:00+00:00",
                "telegram_sent": False,
            }],
        }),
        encoding="utf-8",
    )
    commands = []
    monkeypatch.setattr(clipper, "run_command", lambda command, **kwargs: commands.append(command))
    args = clipper.build_parser().parse_args([
        "deliver", "--manifest", str(manifest_path),
        "--event-db", str(tmp_path / "events.db"),
    ])
    assert clipper.execute_observed(args) == 0
    assert str(video.resolve()) in commands[0]
    assert str(caption.resolve()) in commands[0]


def test_force_delivery_intentionally_resends_without_idempotency_key(tmp_path, monkeypatch):
    _, video, caption = build_render_fixture(tmp_path)
    manifest_path = tmp_path / "render-manifest.json"
    item = {
        "status": "completed",
        "clip_id": "clip-01",
        "output_video": str(video),
        "caption_path": str(caption),
        "rendered_at": "2026-10-06T00:00:00+00:00",
        "telegram_sent": True,
    }
    item["delivery"] = clipper.new_delivery("delivery-job", item, requested=True)
    item["delivery"].update({"status": "sent", "sent_at": "earlier", "attempts": 1})
    manifest_path.write_text(
        json.dumps({"job_id": "delivery-job", "clips": [item]}), encoding="utf-8"
    )
    commands = []
    monkeypatch.setattr(clipper, "run_command", lambda command, **kwargs: commands.append(command))
    args = clipper.build_parser().parse_args([
        "deliver", "--manifest", str(manifest_path), "--force",
        "--event-db", str(tmp_path / "events.db"),
    ])
    assert clipper.execute_observed(args) == 0
    assert len(commands) == 1
    assert "--delivery-key" not in commands[0]


def telegram_args(tmp_path, delivery_key):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    return argparse.Namespace(
        video=str(video), caption="caption", caption_file=None,
        env=str(tmp_path / ".env"), state=str(tmp_path / "approval.db"),
        job="job", clip="clip", timeout=1, delivery_key=delivery_key,
    )


def test_telegram_delivery_key_prevents_duplicate_upload(tmp_path, monkeypatch):
    args = telegram_args(tmp_path, "stable-delivery-key")
    uploads = []
    monkeypatch.setattr(
        approval,
        "merged_config",
        lambda _: {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_CHAT_ID": "1",
            "TELEGRAM_OWNER_USER_ID": "1",
        },
    )
    monkeypatch.setattr(
        approval,
        "telegram_upload_video",
        lambda *a, **k: uploads.append("upload") or {
            "result": {"chat": {"id": 1}, "message_id": 2}
        },
    )
    assert approval.command_send(args) == 0
    assert approval.command_send(args) == 0
    assert uploads == ["upload"]
    connection = approval.connect_db(Path(args.state))
    try:
        assert connection.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 1
        row = connection.execute("SELECT * FROM clips").fetchone()
        assert row["status"] == approval.STATUS_PENDING
        assert row["message_id"] == 2
    finally:
        connection.close()


def test_uncertain_telegram_upload_is_not_retried_automatically(tmp_path, monkeypatch):
    args = telegram_args(tmp_path, "uncertain-delivery-key")
    uploads = []
    monkeypatch.setattr(
        approval,
        "merged_config",
        lambda _: {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_CHAT_ID": "1",
            "TELEGRAM_OWNER_USER_ID": "1",
        },
    )

    def uncertain_upload(*args, **kwargs):
        uploads.append("upload")
        raise approval.DeliveryUncertainError("connection dropped")

    monkeypatch.setattr(approval, "telegram_upload_video", uncertain_upload)
    with pytest.raises(approval.DeliveryUncertainError):
        approval.command_send(args)
    with pytest.raises(approval.ApprovalError, match="belum pasti"):
        approval.command_send(args)
    assert uploads == ["upload"]
    connection = approval.connect_db(Path(args.state))
    try:
        row = connection.execute("SELECT * FROM clips").fetchone()
        assert row["status"] == approval.STATUS_SEND_UNCERTAIN
    finally:
        connection.close()


def test_explicit_telegram_failure_retries_the_same_record(tmp_path, monkeypatch):
    args = telegram_args(tmp_path, "retry-delivery-key")
    uploads = []
    monkeypatch.setattr(
        approval,
        "merged_config",
        lambda _: {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_CHAT_ID": "1",
            "TELEGRAM_OWNER_USER_ID": "1",
        },
    )

    def upload(*args, **kwargs):
        uploads.append("upload")
        if len(uploads) == 1:
            raise approval.ApprovalError("Telegram rejected the request")
        return {"result": {"chat": {"id": 1}, "message_id": 3}}

    monkeypatch.setattr(approval, "telegram_upload_video", upload)
    with pytest.raises(approval.ApprovalError):
        approval.command_send(args)
    assert approval.command_send(args) == 0
    assert uploads == ["upload", "upload"]
    connection = approval.connect_db(Path(args.state))
    try:
        rows = connection.execute("SELECT * FROM clips").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == approval.STATUS_PENDING
        assert rows[0]["message_id"] == 3
    finally:
        connection.close()


def test_existing_approval_database_is_migrated(tmp_path):
    path = tmp_path / "approval.db"
    connection = sqlite3.connect(path)
    connection.execute("""
        CREATE TABLE clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            job_id TEXT NOT NULL,
            clip_id TEXT NOT NULL,
            video_path TEXT NOT NULL,
            caption TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            chat_id TEXT,
            message_id INTEGER,
            decision_by TEXT,
            decision_at TEXT,
            error_message TEXT
        )
    """)
    connection.commit()
    connection.close()
    migrated = approval.connect_db(path)
    try:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(clips)")}
        assert "delivery_key" in columns
    finally:
        migrated.close()
