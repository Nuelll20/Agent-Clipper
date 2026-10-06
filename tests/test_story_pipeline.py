import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from core.artifacts import sha256_file
from core.event_store import EventStore
from core.story import (
    FixtureStoryProvider,
    OpenAICompatibleStoryProvider,
    StoryGateError,
    StoryPipeline,
    assert_story_approved,
    review_story,
    safe_story_id,
    validate_story_revision,
    write_story_brief,
)
from core.supervisor import SupervisorStore, SupervisorWorker
from validators.artifact_validator import ArtifactValidationError


def make_brief(tmp_path, *, story_id="story-01"):
    path = tmp_path / "brief.json"
    payload = {
        "artifact_type": "story-brief",
        "schema_version": "2.0",
        "story_id": story_id,
        "title": "Janji Ari",
        "premise": "Ari belajar mempertahankan janji ketika keadaan berubah.",
        "language": "id",
        "audience": "remaja",
        "format": "short-animation",
        "target_duration_seconds": 60,
        "constraints": ["Tanpa kekerasan grafis"],
    }
    write_story_brief(path, payload)
    return path, payload


def develop(tmp_path, *, revision="rev-01", provider=None):
    brief, _ = make_brief(tmp_path)
    pipeline = StoryPipeline(tmp_path / "stories", provider or FixtureStoryProvider())
    return pipeline.run(brief, revision_id=revision)


def test_fixture_pipeline_creates_six_linked_artifacts_and_manifest(tmp_path):
    brief, _ = make_brief(tmp_path)
    progress = []

    result = StoryPipeline(tmp_path / "stories", FixtureStoryProvider()).run(
        brief,
        revision_id="rev-01",
        on_stage=lambda *values: progress.append(values),
    )

    assert result.manifest["status"] == "REVIEW_REQUIRED"
    assert [item["stage"] for item in result.manifest["artifacts"]] == [
        "universe", "outline", "screenplay", "critique", "continuity", "shot-list",
    ]
    assert [item[0] for item in progress] == [item["stage"] for item in result.manifest["artifacts"]]
    assert all(item[-1] is False for item in progress)
    assert validate_story_revision(result.manifest_path) == result.manifest


def test_retry_same_revision_reuses_validated_stages_without_provider_calls(tmp_path):
    brief, _ = make_brief(tmp_path)
    pipeline = StoryPipeline(tmp_path / "stories", FixtureStoryProvider())
    first = pipeline.run(brief, revision_id="stable-revision")

    class MustNotRun(FixtureStoryProvider):
        def generate(self, *args, **kwargs):
            pytest.fail("validated stage should be reused")

    reused = []
    second = StoryPipeline(tmp_path / "stories", MustNotRun()).run(
        brief,
        revision_id="stable-revision",
        on_stage=lambda *values: reused.append(values[-1]),
    )
    assert second.manifest == first.manifest
    assert reused == [True] * 6


def test_same_revision_blocks_changed_brief(tmp_path):
    brief, payload = make_brief(tmp_path)
    pipeline = StoryPipeline(tmp_path / "stories", FixtureStoryProvider())
    pipeline.run(brief, revision_id="fixed-revision")
    payload["premise"] = "Premise yang telah diubah."
    brief.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StoryGateError, match="gunakan revision_id baru"):
        pipeline.run(brief, revision_id="fixed-revision")


def test_critique_revise_stops_continuity_and_shot_list(tmp_path):
    brief, _ = make_brief(tmp_path)

    class RevisingProvider(FixtureStoryProvider):
        def generate(self, stage, **kwargs):
            if stage == "critique":
                return {
                    "verdict": "REVISE",
                    "summary": "Motivasi perlu diperkuat.",
                    "issues": [{
                        "severity": "MAJOR",
                        "category": "character",
                        "message": "Motivasi belum terlihat.",
                        "target_id": "char-protagonist",
                        "recommendation": "Tambahkan keputusan yang jelas.",
                    }],
                }
            return super().generate(stage, **kwargs)

    with pytest.raises(StoryGateError, match="meminta revisi"):
        StoryPipeline(tmp_path / "stories", RevisingProvider()).run(
            brief,
            revision_id="needs-work",
        )
    revision = tmp_path / "stories" / "story-01" / "revisions" / "needs-work"
    assert (revision / "critique.json").is_file()
    assert not (revision / "continuity.json").exists()
    assert not (revision / "story-manifest.json").exists()


def test_invalid_cross_reference_is_blocked_before_artifact_write(tmp_path):
    brief, _ = make_brief(tmp_path)

    class BrokenOutlineProvider(FixtureStoryProvider):
        def generate(self, stage, **kwargs):
            value = super().generate(stage, **kwargs)
            if stage == "outline":
                value["acts"][0]["beats"][0]["location_id"] = "unknown-location"
            return value

    with pytest.raises(ArtifactValidationError, match="location_id yang tidak dikenal"):
        StoryPipeline(tmp_path / "stories", BrokenOutlineProvider()).run(
            brief,
            revision_id="bad-reference",
        )
    revision = tmp_path / "stories" / "story-01" / "revisions" / "bad-reference"
    assert (revision / "universe.json").is_file()
    assert not (revision / "outline.json").exists()


def test_review_is_required_and_bound_to_exact_manifest(tmp_path):
    result = develop(tmp_path)
    with pytest.raises(ArtifactValidationError, match="tidak ditemukan"):
        assert_story_approved(result.manifest_path)

    review, _ = review_story(
        result.manifest_path,
        decision="APPROVED",
        reviewer="operator-test",
    )
    assert assert_story_approved(result.manifest_path) == review

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = "2026-10-06T10:00:00+00:00"
    result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(StoryGateError, match="berubah setelah review"):
        assert_story_approved(result.manifest_path)


def test_changes_requested_never_opens_production_gate(tmp_path):
    result = develop(tmp_path)
    review_story(
        result.manifest_path,
        decision="CHANGES_REQUESTED",
        reviewer="operator-test",
        note="Perbaiki tempo adegan pertama.",
    )
    with pytest.raises(StoryGateError, match="belum disetujui"):
        assert_story_approved(result.manifest_path)


def test_stage_tampering_is_detected_by_manifest_fingerprint(tmp_path):
    result = develop(tmp_path)
    shot_list = result.revision_dir / "shot-list.json"
    payload = json.loads(shot_list.read_text(encoding="utf-8"))
    payload["content"]["shots"][0]["action"] = "Isi diubah setelah manifest dibuat."
    shot_list.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StoryGateError, match="Fingerprint artifact shot-list berubah"):
        validate_story_revision(result.manifest_path)


def test_provider_configuration_never_contains_api_key():
    provider = OpenAICompatibleStoryProvider(
        base_url="http://127.0.0.1:20128/v1",
        model="local-model",
        api_key="very-secret",
    )
    assert "very-secret" not in json.dumps(provider.configuration())
    assert provider.configuration()["model"] == "local-model"


def test_openai_compatible_provider_requests_json_and_decodes_fenced_response(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {
                        "content": "```json\n{\"verdict\":\"PASS\",\"summary\":\"OK\",\"issues\":[]}\n```"
                    }
                }]
            }).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    provider = OpenAICompatibleStoryProvider(
        base_url="http://127.0.0.1:20128/v1",
        model="local-model",
        api_key="secret",
        timeout_seconds=12,
    )
    result = provider.generate("critique", brief={"title": "Test"}, context={})
    assert result == {"verdict": "PASS", "summary": "OK", "issues": []}
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["timeout"] == 12
    assert captured["authorization"] == "Bearer secret"
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_story_id_rejects_path_traversal():
    with pytest.raises(ArtifactValidationError):
        safe_story_id("../outside")


def test_supervisor_story_adapter_uses_task_as_revision_and_no_secret(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    brief, payload = make_brief(tmp_path)
    store = SupervisorStore(tmp_path / "supervisor.db")
    task, _ = store.enqueue(
        job_id=payload["story_id"],
        kind="DEVELOP_STORY",
        payload={
            "story_id": payload["story_id"],
            "brief_path": str(brief),
            "brief_fingerprint": sha256_file(brief),
            "provider": "openai-compatible",
            "output_root": str(tmp_path / "stories"),
            "base_url": "http://127.0.0.1:20128/v1",
            "model": "local-model",
            "api_key_env": "STORY_TEST_KEY",
            "timeout_seconds": 30,
        },
        idempotency_key="story-key",
        resources=["story:story-01", "story-llm"],
    )
    calls = []

    def runner(command, cwd, heartbeat, heartbeat_seconds):
        calls.append(command)
        heartbeat()
        return 0

    worker = SupervisorWorker(
        store,
        project_root=project_root,
        event_db=tmp_path / "events.db",
        worker_id="story-worker",
        lease_seconds=30,
        heartbeat_seconds=5,
        command_runner=runner,
        python_executable="python-fixture",
    )
    try:
        result = worker.run_once()
        command = calls[0]
        assert result.status == "completed"
        assert command[1].endswith("story_studio.py")
        assert command[2] == "develop"
        assert command[command.index("--revision-id") + 1] == task["task_id"]
        assert command[command.index("--task-id") + 1] == task["task_id"]
        assert "STORY_TEST_KEY" in command
        assert "very-secret" not in command
    finally:
        store.close()


def test_story_review_gate_exit_is_not_retried_by_supervisor(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    brief, payload = make_brief(tmp_path)
    store = SupervisorStore(tmp_path / "supervisor.db")
    task, _ = store.enqueue(
        job_id=payload["story_id"],
        kind="DEVELOP_STORY",
        payload={
            "story_id": payload["story_id"],
            "brief_path": str(brief),
            "brief_fingerprint": sha256_file(brief),
            "provider": "fixture",
            "output_root": str(tmp_path / "stories"),
        },
        idempotency_key="review-gate",
        resources=["story:story-01"],
        max_attempts=3,
    )
    worker = SupervisorWorker(
        store,
        project_root=project_root,
        event_db=tmp_path / "events.db",
        worker_id="story-worker",
        lease_seconds=30,
        heartbeat_seconds=5,
        command_runner=lambda *args: 3,
    )
    try:
        result = worker.run_once()
        snapshot = store.get_task(task["task_id"])
        assert result.status == "review_required"
        assert snapshot["state"] == "REVIEW_REQUIRED"
        assert snapshot["attempt_count"] == 1
        assert snapshot["last_error"] is None
        assert "review gate" in snapshot["result"]["review_reason"]
        next_task, _ = store.enqueue(
            job_id=payload["story_id"],
            kind="DEVELOP_STORY",
            payload=task["payload"],
            idempotency_key="after-review-gate",
            resources=["story:story-01"],
        )
        claimed, _ = store.claim_next("next-worker", lease_seconds=30)
        assert claimed["task_id"] == next_task["task_id"]
    finally:
        store.close()


def test_supervisor_blocks_story_brief_changed_after_enqueue(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    brief, payload = make_brief(tmp_path)
    store = SupervisorStore(tmp_path / "supervisor.db")
    task, _ = store.enqueue(
        job_id=payload["story_id"],
        kind="DEVELOP_STORY",
        payload={
            "story_id": payload["story_id"],
            "brief_path": str(brief),
            "brief_fingerprint": sha256_file(brief),
            "provider": "fixture",
            "output_root": str(tmp_path / "stories"),
        },
        idempotency_key="changed-brief",
        resources=["story:story-01"],
    )
    payload["premise"] = "Premise changed after queueing."
    brief.write_text(json.dumps(payload), encoding="utf-8")
    worker = SupervisorWorker(
        store,
        project_root=project_root,
        event_db=tmp_path / "events.db",
        worker_id="story-worker",
        lease_seconds=30,
        heartbeat_seconds=5,
        command_runner=lambda *args: pytest.fail("changed brief must not execute"),
    )
    try:
        result = worker.run_once()
        snapshot = store.get_task(task["task_id"])
        assert result.status == "failed"
        assert snapshot["state"] == "FAILED"
        assert "enqueue task baru" in snapshot["last_error"]
    finally:
        store.close()


def test_supervisor_story_cli_enqueue_is_idempotent(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    brief, _ = make_brief(tmp_path)
    database = tmp_path / "supervisor.db"
    command = [
        sys.executable,
        str(project_root / "scripts" / "supervisor.py"),
        "--db", str(database),
        "enqueue", "story",
        "--brief", str(brief),
        "--provider", "fixture",
        "--output-root", str(tmp_path / "stories"),
    ]
    first = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
    second = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
    assert first["created"] is True
    assert second["created"] is False
    assert first["task"]["task_id"] == second["task"]["task_id"]


def test_story_cli_records_each_agent_and_stage_progress(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    brief, _ = make_brief(tmp_path)
    event_db = tmp_path / "events.db"
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "story_studio.py"),
            "develop",
            "--brief", str(brief),
            "--story-id", "story-01",
            "--revision-id", "observed-revision",
            "--provider", "fixture",
            "--output-root", str(tmp_path / "stories"),
            "--event-db", str(event_db),
            "--task-id", "task-observed",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    store = EventStore(event_db)
    try:
        events = store.read_events()
    finally:
        store.close()
    working = [event for event in events if event["state"] == "WORKING"]
    assert [event["stage"] for event in working] == [
        "universe", "outline", "screenplay", "critique", "continuity", "shot-list",
    ]
    assert [event["agent_id"] for event in working] == [
        "worldbuilder", "plotter", "screenwriter", "story-critic",
        "continuity-editor", "shot-planner",
    ]
    assert all(event["unit"] == "stages" for event in working)
    assert events[-1]["state"] == "COMPLETED"
