import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.artifacts import sha256_file
from core.event_store import EventStore
from core.production import (
    AssetPipeline,
    AssetProviderError,
    FixtureAssetProvider,
    ProductionGateError,
    create_voice_cast,
    prepare_asset_plan,
    validate_asset_production,
    validate_voice_cast,
)
from core.story import (
    FixtureStoryProvider,
    StoryPipeline,
    review_story,
    write_story_brief,
)
from core.supervisor import SupervisorStore, SupervisorWorker
from validators.artifact_validator import ArtifactValidationError


class TwoShotStoryProvider(FixtureStoryProvider):
    def generate(self, stage, **kwargs):
        value = super().generate(stage, **kwargs)
        if stage == "screenplay":
            value["scenes"].append({
                "id": "scene-02",
                "heading": "INT. RUANG UTAMA - SIANG",
                "location_id": "loc-main",
                "time_of_day": "siang",
                "summary": "Ari menyelesaikan tindakannya.",
                "duration_seconds": 20,
                "beats": [{"type": "ACTION", "text": "Ari merapikan ruangan."}],
            })
        if stage == "shot-list":
            value["shots"].append({
                "id": "shot-02",
                "scene_id": "scene-02",
                "order": 1,
                "duration_seconds": 20,
                "framing": "wide shot",
                "camera": "slow pan",
                "action": "Ari merapikan ruangan.",
                "dialogue": "",
                "characters": ["char-protagonist"],
                "location_id": "loc-main",
                "continuity_notes": "Pertahankan pakaian dan aksen warna.",
            })
        return value


def story_brief(tmp_path, *, story_id="asset-story"):
    path = tmp_path / "brief.json"
    write_story_brief(path, {
        "artifact_type": "story-brief",
        "schema_version": "2.0",
        "story_id": story_id,
        "title": "Asset Story",
        "premise": "Ari menjaga janjinya sampai akhir.",
        "language": "id",
        "audience": "remaja",
        "format": "short-animation",
        "target_duration_seconds": 60,
        "constraints": [],
    })
    return path


def story_revision(tmp_path, *, approved=True, two_shots=False):
    brief = story_brief(tmp_path)
    provider = TwoShotStoryProvider() if two_shots else FixtureStoryProvider()
    result = StoryPipeline(tmp_path / "stories", provider).run(
        brief,
        revision_id="rev-assets",
    )
    if approved:
        review_story(result.manifest_path, decision="APPROVED", reviewer="test")
    return result


def voice_assignments():
    return {
        "char-protagonist": {
            "provider": "local-tts",
            "provider_voice_id": "voice-ari-v1",
            "language": "id",
            "style": "warm and calm",
        }
    }


def prepared_plan(tmp_path, *, two_shots=False, provider_mode="fixture", provider_config=None):
    story = story_revision(tmp_path, two_shots=two_shots)
    cast = tmp_path / "voice-cast.json"
    create_voice_cast(story.manifest_path, voice_assignments(), cast)
    plan = prepare_asset_plan(
        story.manifest_path,
        cast,
        production_root=tmp_path / "productions",
        production_id="prod-01",
        provider_mode=provider_mode,
        provider_config_path=provider_config,
    )
    return story, cast, plan


def command_provider_config(tmp_path, helper_path, *, fail_kind=None):
    config = {
        "artifact_type": "asset-provider-config",
        "schema_version": "1.0",
        "providers": {},
    }
    for kind, extension in {
        "reference": ".png",
        "animation": ".mp4",
        "voice": ".wav",
        "sfx": ".wav",
    }.items():
        config["providers"][kind] = {
            "name": "test-" + kind,
            "command": [sys.executable, str(helper_path), "--kind", kind],
            "output_extension": extension,
            "timeout_seconds": 30,
        }
        if kind == fail_kind:
            config["providers"][kind]["command"].append("--fail")
    path = tmp_path / "provider-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_story_approval_is_required_before_voice_cast(tmp_path):
    story = story_revision(tmp_path, approved=False)
    with pytest.raises((ArtifactValidationError, ProductionGateError), match="review|disetujui"):
        create_voice_cast(
            story.manifest_path,
            voice_assignments(),
            tmp_path / "voice-cast.json",
        )


def test_voice_cast_is_complete_and_bound_to_story_revision(tmp_path):
    story = story_revision(tmp_path)
    cast_path = tmp_path / "voice-cast.json"
    create_voice_cast(story.manifest_path, voice_assignments(), cast_path)
    cast = json.loads(cast_path.read_text(encoding="utf-8"))
    assert cast["voices"][0]["provider_voice_id"] == "voice-ari-v1"
    assert validate_voice_cast(cast, manifest_path=story.manifest_path) == cast

    cast["voices"][0]["character_id"] = "unknown"
    with pytest.raises(ProductionGateError, match="tepat semua karakter"):
        validate_voice_cast(cast, manifest_path=story.manifest_path)


def test_fixture_production_generates_and_reuses_all_assets(tmp_path):
    _, _, plan = prepared_plan(tmp_path)
    pipeline = AssetPipeline(plan)
    first = pipeline.generate()
    assert first.generated == 4
    assert first.reused == 0
    assert first.manifest["status"] == "COMPLETED"
    assert validate_asset_production(plan, require_complete=True)["status"] == "COMPLETED"

    second = AssetPipeline(plan).generate()
    assert second.generated == 0
    assert second.reused == 4
    assert second.manifest["status"] == "COMPLETED"


def test_validation_without_generation_does_not_create_manifest(tmp_path):
    _, _, plan = prepared_plan(tmp_path)
    manifest_path = plan.parent / "asset-manifest.json"
    with pytest.raises(ProductionGateError, match="belum dibuat"):
        validate_asset_production(plan)
    assert not manifest_path.exists()


def test_reference_regeneration_only_stales_animation_for_selected_shot(tmp_path):
    _, _, plan = prepared_plan(tmp_path, two_shots=True)
    initial = AssetPipeline(plan).generate().manifest
    before = copy.deepcopy({shot["shot_id"]: shot for shot in initial["shots"]})

    partial = AssetPipeline(plan).generate(
        shot_ids=["shot-01"],
        kinds=["reference"],
        force=True,
    ).manifest
    partial_map = {shot["shot_id"]: shot for shot in partial["shots"]}
    assert partial["status"] == "PARTIAL"
    assert partial_map["shot-01"]["assets"]["animation"]["state"] == "STALE"
    assert len(partial_map["shot-01"]["assets"]["reference"]["attempts"]) == 2
    assert partial_map["shot-02"] == before["shot-02"]
    with pytest.raises(ProductionGateError, match="belum lengkap"):
        validate_asset_production(plan, require_complete=True)

    completed = AssetPipeline(plan).generate(
        shot_ids=["shot-01"],
        kinds=["animation"],
    ).manifest
    completed_map = {shot["shot_id"]: shot for shot in completed["shots"]}
    assert completed["status"] == "COMPLETED"
    assert len(completed_map["shot-01"]["assets"]["animation"]["attempts"]) == 2
    assert completed_map["shot-02"] == before["shot-02"]


def test_regeneration_key_creates_one_new_attempt_and_is_retry_idempotent(tmp_path):
    _, _, plan = prepared_plan(tmp_path)
    AssetPipeline(plan).generate()

    regenerated = AssetPipeline(plan).generate(
        shot_ids=["shot-01"],
        kinds=["animation"],
        regeneration_key="animation-take-02",
    )
    animation = regenerated.manifest["shots"][0]["assets"]["animation"]
    assert regenerated.generated == 1
    assert len(animation["attempts"]) == 2

    retried = AssetPipeline(plan).generate(
        shot_ids=["shot-01"],
        kinds=["animation"],
        regeneration_key="animation-take-02",
    )
    animation = retried.manifest["shots"][0]["assets"]["animation"]
    assert retried.generated == 0
    assert retried.reused == 1
    assert len(animation["attempts"]) == 2


def test_output_tampering_is_detected(tmp_path):
    _, _, plan = prepared_plan(tmp_path)
    manifest = AssetPipeline(plan).generate().manifest
    reference = manifest["shots"][0]["assets"]["reference"]
    attempt_path = plan.parent / reference["active_attempt"]
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    output = plan.parent / attempt["output"]["path"]
    output.write_bytes(b"tampered")

    with pytest.raises(ProductionGateError, match="berubah atau hilang"):
        validate_asset_production(plan)


def test_request_tampering_is_detected(tmp_path):
    _, _, plan = prepared_plan(tmp_path)
    manifest = AssetPipeline(plan).generate().manifest
    reference = manifest["shots"][0]["assets"]["reference"]
    attempt_path = plan.parent / reference["active_attempt"]
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    request = plan.parent / attempt["request_path"]
    request.write_text("{}", encoding="utf-8")

    with pytest.raises(ProductionGateError, match="Request berubah atau hilang"):
        validate_asset_production(plan)


def test_latest_story_review_can_revoke_asset_plan(tmp_path):
    story, _, plan = prepared_plan(tmp_path)
    review_story(
        story.manifest_path,
        decision="CHANGES_REQUESTED",
        reviewer="test",
        note="Perbaiki visual story lebih dahulu.",
    )
    with pytest.raises(ProductionGateError, match="belum disetujui"):
        AssetPipeline(plan)


def test_command_provider_runs_without_shell_and_creates_real_attempt_records(tmp_path):
    helper = tmp_path / "provider_helper.py"
    helper.write_text(
        """
import argparse
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--kind')
p.add_argument('--request')
p.add_argument('--output')
p.add_argument('--fail', action='store_true')
a = p.parse_args()
if a.fail:
    raise SystemExit(7)
Path(a.output).write_bytes(('provider:' + a.kind).encode())
""".strip(),
        encoding="utf-8",
    )
    config = command_provider_config(tmp_path, helper)
    _, _, plan = prepared_plan(
        tmp_path,
        provider_mode="command",
        provider_config=config,
    )
    result = AssetPipeline(plan).generate()
    assert result.manifest["status"] == "COMPLETED"
    assert result.generated == 4
    validate_asset_production(plan, require_complete=True)


def test_failed_animation_does_not_discard_other_asset_results(tmp_path):
    _, _, plan = prepared_plan(tmp_path)

    class FailingAnimationProvider(FixtureAssetProvider):
        def generate(self, kind, request, request_path, output_path):
            if kind == "animation":
                raise AssetProviderError("animation fixture failed")
            return super().generate(kind, request, request_path, output_path)

    pipeline = AssetPipeline(plan)
    pipeline.provider = FailingAnimationProvider()
    result = pipeline.generate(continue_on_error=True)
    assets = result.manifest["shots"][0]["assets"]
    assert result.failures == ("shot-01:animation",)
    assert result.manifest["status"] == "FAILED"
    assert assets["reference"]["state"] == "COMPLETED"
    assert assets["animation"]["state"] == "FAILED"
    assert assets["voice"]["state"] == "COMPLETED"
    assert assets["sfx"]["state"] == "COMPLETED"


def test_provider_config_change_after_plan_is_blocked(tmp_path):
    helper = tmp_path / "provider_helper.py"
    helper.write_text("raise SystemExit(0)", encoding="utf-8")
    config = command_provider_config(tmp_path, helper)
    _, _, plan = prepared_plan(
        tmp_path,
        provider_mode="command",
        provider_config=config,
    )
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["providers"]["reference"]["name"] = "changed"
    config.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProductionGateError, match="Provider config berubah"):
        AssetPipeline(plan)


def test_supervisor_asset_adapter_is_selective_and_task_correlated(tmp_path):
    _, _, plan = prepared_plan(tmp_path)
    store = SupervisorStore(tmp_path / "supervisor.db")
    task, _ = store.enqueue(
        job_id="asset-story",
        kind="GENERATE_ASSETS",
        payload={
            "plan_path": str(plan),
            "plan_fingerprint": sha256_file(plan),
            "shot_ids": ["shot-01"],
            "asset_kinds": ["reference"],
            "continue_on_error": False,
            "regeneration_id": "take-02",
        },
        idempotency_key="asset-task",
        resources=["asset-generation"],
    )
    calls = []

    def runner(command, cwd, heartbeat, heartbeat_seconds):
        calls.append(command)
        heartbeat()
        return 0

    worker = SupervisorWorker(
        store,
        project_root=Path(__file__).resolve().parents[1],
        event_db=tmp_path / "events.db",
        worker_id="asset-worker",
        lease_seconds=30,
        heartbeat_seconds=5,
        command_runner=runner,
        python_executable="python-fixture",
    )
    try:
        result = worker.run_once()
        command = calls[0]
        assert result.status == "completed"
        assert command[1].endswith("animation_studio.py")
        assert command[2] == "generate"
        assert command[command.index("--task-id") + 1] == task["task_id"]
        assert command[command.index("--shot") + 1] == "shot-01"
        assert command[command.index("--asset") + 1] == "reference"
        assert command[command.index("--regeneration-key") + 1] == "take-02"
    finally:
        store.close()


def test_supervisor_blocks_asset_plan_changed_after_enqueue(tmp_path):
    _, _, plan = prepared_plan(tmp_path)
    store = SupervisorStore(tmp_path / "supervisor.db")
    task, _ = store.enqueue(
        job_id="asset-story",
        kind="GENERATE_ASSETS",
        payload={
            "plan_path": str(plan),
            "plan_fingerprint": sha256_file(plan),
            "shot_ids": [],
            "asset_kinds": [],
            "continue_on_error": False,
            "regeneration_id": None,
        },
        idempotency_key="changed-plan",
        resources=["asset-generation"],
    )
    payload = json.loads(plan.read_text(encoding="utf-8"))
    payload["video"]["fps"] = 24
    plan.write_text(json.dumps(payload), encoding="utf-8")
    worker = SupervisorWorker(
        store,
        project_root=Path(__file__).resolve().parents[1],
        event_db=tmp_path / "events.db",
        worker_id="asset-worker",
        lease_seconds=30,
        heartbeat_seconds=5,
        command_runner=lambda *args: pytest.fail("changed plan must not execute"),
    )
    try:
        result = worker.run_once()
        snapshot = store.get_task(task["task_id"])
        assert result.status == "failed"
        assert snapshot["state"] == "FAILED"
        assert "enqueue task baru" in snapshot["last_error"]
    finally:
        store.close()


def test_supervisor_asset_enqueue_cli_is_idempotent(tmp_path):
    _, _, plan = prepared_plan(tmp_path)
    project_root = Path(__file__).resolve().parents[1]
    database = tmp_path / "supervisor.db"
    command = [
        sys.executable,
        str(project_root / "scripts" / "supervisor.py"),
        "--db", str(database),
        "enqueue", "assets",
        "--plan", str(plan),
        "--shot", "shot-01",
        "--asset", "reference",
    ]
    first = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
    second = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
    assert first["created"] is True
    assert second["created"] is False
    assert first["task"]["task_id"] == second["task"]["task_id"]


def test_asset_cli_records_real_agent_identity_and_asset_progress(tmp_path):
    _, _, plan = prepared_plan(tmp_path)
    project_root = Path(__file__).resolve().parents[1]
    event_db = tmp_path / "events.db"
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "animation_studio.py"),
            "generate",
            "--plan", str(plan),
            "--event-db", str(event_db),
            "--task-id", "task-assets-observed",
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
    assert [event["agent_id"] for event in working] == [
        "reference-artist", "animator", "voice-director", "sound-designer",
    ]
    assert all(event["unit"] == "assets" for event in working)
    assert events[-1]["state"] == "COMPLETED"
