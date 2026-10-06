import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.artifacts import validate_transcript_contract
from core.event_store import EventStore
from scripts import podcast_clipper as clipper
from validators.artifact_validator import (
    ArtifactValidationError,
    validate_artifact,
)


def legacy_transcript(text="Transcript fixture yang valid.", *, end=10):
    return {
        "schema_version": 2,
        "language": "id",
        "duration": end,
        "segments": [{
            "id": 1,
            "start": 0,
            "end": end,
            "text_original": text,
            "words": [
                {"word": "Transcript", "start": 0, "end": 0.5},
                {"word": "fixture", "start": 0.5, "end": 1.0},
            ],
        }],
    }


def test_legacy_transcript_is_adapted_without_losing_content(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-content")
    original = legacy_transcript()

    adapted = validate_transcript_contract(
        original,
        source_path=source,
        artifact_path=tmp_path / "transcript.json",
        config={"model": "medium", "language": "id"},
    )

    assert adapted["schema_version"] == "2.0"
    assert adapted["artifact_type"] == "transcript"
    assert adapted["segments"] == original["segments"]
    assert adapted["source_fingerprint"].startswith("sha256:")
    assert adapted["config_fingerprint"].startswith("sha256:")
    assert adapted["contract_adapter"]["from_schema_version"] == 2


def test_schema_validator_raises_instead_of_printing_only(tmp_path):
    artifact = tmp_path / "invalid.json"
    artifact.write_text('{"schema_version": 2}', encoding="utf-8")
    schema = Path(clipper.project_root()) / "artifacts" / "schemas" / "transcript.schema.json"

    with pytest.raises(ArtifactValidationError, match="tidak sesuai kontrak"):
        validate_artifact(artifact, schema)

    result = subprocess.run(
        [sys.executable, str(Path(clipper.project_root()) / "validators" / "artifact_validator.py"),
         str(artifact), str(schema)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "[BLOCK]" in result.stdout


@pytest.mark.parametrize(
    "payload, message",
    [
        (
            {
                "segments": [
                    {"start": 5, "end": 4, "text": "Waktu terbalik."},
                ]
            },
            "rentang waktu tidak valid",
        ),
        (
            {
                "segments": [
                    {"start": 0, "end": 1, "text": "{\\pos(1,2)}raw tag"},
                ]
            },
            "raw ASS override tag",
        ),
    ],
)
def test_semantic_gate_blocks_unsafe_transcript(tmp_path, payload, message):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    with pytest.raises(ArtifactValidationError, match=message):
        validate_transcript_contract(payload, source_path=source)


def test_render_blocks_invalid_transcript_before_renderer(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    transcript = tmp_path / "transcript.json"
    transcript.write_text(
        json.dumps({"schema_version": "2.0", "segments": []}),
        encoding="utf-8",
    )
    plan = tmp_path / "clip-plan.json"
    plan.write_text(
        json.dumps({
            "job_id": "blocked-job",
            "source_path": str(source),
            "transcript_path": str(transcript),
            "clips": [{"id": "clip-01", "start": 0, "end": 10, "hook": "Hook"}],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(clipper, "ffprobe", lambda _: {"duration": 30})
    monkeypatch.setattr(
        clipper,
        "render_one_clip",
        lambda *args, **kwargs: pytest.fail("renderer tidak boleh dijalankan"),
    )
    event_db = tmp_path / "events.db"
    args = clipper.build_parser().parse_args([
        "render", "--plan", str(plan), "--no-send", "--event-db", str(event_db),
    ])

    with pytest.raises(clipper.WorkflowError, match="Artifact transcript diblokir"):
        clipper.execute_observed(args)

    store = EventStore(event_db)
    try:
        events = store.read_events()
    finally:
        store.close()
    assert events[-1]["state"] == "FAILED"
    assert any(event["stage"] == "validating_artifact" for event in events)


def test_ingest_upgrades_legacy_transcript_and_validates_job_manifest(
    tmp_path,
    monkeypatch,
):
    jobs_root = tmp_path / "jobs"
    job_dir = jobs_root / "upgrade-job"
    job_dir.mkdir(parents=True)
    source = job_dir / "source.mp4"
    source.write_bytes(b"source")
    transcript = job_dir / "transcript-full.json"
    transcript.write_text(json.dumps(legacy_transcript(end=12)), encoding="utf-8")
    monkeypatch.setattr(
        clipper,
        "ffprobe",
        lambda _: {"width": 1920, "height": 1080, "duration": 12},
    )
    args = clipper.build_parser().parse_args([
        "ingest",
        "https://example.invalid/video",
        "--job",
        "upgrade-job",
        "--jobs-root",
        str(jobs_root),
    ])

    assert clipper.command_ingest(args) == 0
    upgraded = json.loads(transcript.read_text(encoding="utf-8"))
    manifest_path = job_dir / "job.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == "2.0"
    assert manifest["schema_version"] == "2.0"
    assert manifest["artifact_type"] == "job-manifest"
    assert manifest["upstream_artifacts"]
    validate_artifact(
        manifest_path,
        Path(clipper.project_root()) / "artifacts" / "schemas" / "job-manifest.schema.json",
    )
