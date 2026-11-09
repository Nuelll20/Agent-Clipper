import json
from pathlib import Path

import pytest

from core.event_store import EventStore
from core.visual_profiles import load_visual_profile_registry
from scripts import video_agent
from scripts.providers import video_finalize


PNG_FIXTURE = b"\x89PNG\r\n\x1a\nfixture-reference"
SOURCE_MP4_FIXTURE = b"\x00\x00\x00\x18ftypmp42source-video"
FINAL_MP4_FIXTURE = b"\x00\x00\x00\x18ftypmp42final-video"

PROFILE_CONFIG = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "visual-style-profiles.json"
)


class FakeReferenceClient:
    def healthcheck(self, settings, *, require_ipadapter):
        assert require_ipadapter is False
        return {
            "ready": True,
            "checkpoint": settings.checkpoint,
        }

    def queue_prompt(self, workflow, *, client_id):
        assert workflow["4"]["inputs"] == {
            "width": 768,
            "height": 1344,
            "batch_size": 1,
        }
        assert client_id.startswith("hermes-")
        return "reference-prompt"

    def wait_for_image(
        self,
        prompt_id,
        *,
        output_node,
        timeout,
        poll_interval,
    ):
        assert prompt_id == "reference-prompt"
        assert output_node == "7"
        return {
            "filename": "reference.png",
            "subfolder": "HermesAI",
            "type": "output",
        }

    def download_image(self, image):
        return PNG_FIXTURE


class FakeAnimationClient:
    def healthcheck(self, settings):
        assert settings.max_frames == 49
        assert settings.generation_width == 416
        assert settings.generation_height == 736
        return {
            "ready": True,
            "diffusion_model": settings.diffusion_model,
        }

    def upload_image(self, source: Path, *, content_hash):
        assert source.read_bytes() == PNG_FIXTURE
        assert len(content_hash) == 64
        return "HermesAI/provider-inputs/reference.png"

    def queue_prompt(self, workflow, *, client_id):
        assert workflow["8"]["inputs"]["length"] == 49
        assert workflow["11"]["inputs"]["width"] == 720
        assert workflow["11"]["inputs"]["height"] == 1280
        assert client_id.startswith("hermes-")
        return "animation-prompt"

    def wait_for_video(
        self,
        prompt_id,
        *,
        output_node,
        timeout,
        poll_interval,
    ):
        assert prompt_id == "animation-prompt"
        assert output_node == "13"
        return {
            "filename": "video.mp4",
            "subfolder": "HermesAI",
            "type": "output",
        }

    def download_output(self, info):
        return SOURCE_MP4_FIXTURE

class FakeResourceManager:
    def __init__(self):
        self.release_calls = 0

    def release(self):
        self.release_calls += 1

        return {
            "released": True,
            "server": "http://fake-comfyui",
            "unload_models": True,
            "free_memory": True,
            "response": None,
        }

class FailingResourceManager(
    FakeResourceManager
):
    def release(self):
        self.release_calls += 1

        raise RuntimeError(
            "synthetic resource cleanup failure"
        )

class ForbiddenResourceManager:
    def release(self):
        raise AssertionError(
            "Resource cleanup must not run."
        )

class FakeFinalizer:
    def __init__(self):
        self.healthcheck_calls = 0
        self.finalize_calls = 0

    def healthcheck(self):
        self.healthcheck_calls += 1
        return {
            "ready": True,
            "interpolation_method": "ffmpeg-minterpolate",
            "video_encoder": "libx264",
        }

    def finalize(self, input_path, output_path, *, settings):
        self.finalize_calls += 1
        source = Path(input_path)
        output = Path(output_path)
        assert source.name == "video-source.mp4"
        assert source.read_bytes() == SOURCE_MP4_FIXTURE
        assert output.name == "video.mp4"
        assert settings.width == 720
        assert settings.height == 1280
        assert settings.target_fps == 60
        assert settings.interpolation_method == "ffmpeg-minterpolate"
        output.write_bytes(FINAL_MP4_FIXTURE)
        return {
            "input": str(source),
            "output": str(output),
            "interpolation_method": settings.interpolation_method,
            "source_probe": {
                "width": 720,
                "height": 1280,
                "codec_name": "h264",
                "frame_rate": 24.0,
                "duration": 2.04,
            },
            "final_probe": {
                "width": 720,
                "height": 1280,
                "codec_name": "h264",
                "frame_rate": 60.0,
                "duration": 2.0,
            },
            "duration_delta": 0.04,
            "size_bytes": len(FINAL_MP4_FIXTURE),
        }


class ForbiddenAnimationClient:
    def healthcheck(self, settings):
        raise AssertionError("Animation must not run in reference-only mode.")


class ForbiddenFinalizer:
    def healthcheck(self):
        raise AssertionError("Finalizer must not run in this mode.")

    def finalize(self, input_path, output_path, *, settings):
        raise AssertionError("Finalizer must not run in this mode.")


class FailingFinalizer(FakeFinalizer):
    def finalize(self, input_path, output_path, *, settings):
        raise video_finalize.VideoFinalizeError("synthetic finalizer failure")


def _make_args(
    tmp_path,
    *,
    idea="A carefully composed test scene",
    motion=None,
    story_id="test-story",
    production_id="test-take-01",
    visual_style=None,
    reference_only=False,
    skip_finalization=False,
    extra_args=None,
):
    cli = [
        "--idea",
        idea,
        "--story-id",
        story_id,
        "--production-id",
        production_id,
        "--output-root",
        str(tmp_path / "productions"),
        "--event-db",
        str(tmp_path / "events.db"),
    ]
    if motion is not None:
        cli += ["--motion", motion]
    if visual_style is not None:
        cli += ["--visual-style", visual_style]
    if reference_only:
        cli += ["--reference-only"]
    if skip_finalization:
        cli += ["--skip-finalization"]
    if extra_args:
        cli += extra_args

    return video_agent.build_parser().parse_args(cli)


def _registry():
    return load_visual_profile_registry(PROFILE_CONFIG)


def _run_full(tmp_path, resource_manager=None, **argument_overrides):
    args = _make_args(
        tmp_path,
        motion="The subject makes one controlled natural movement.",
        **argument_overrides,
    )
    if resource_manager is None:
        resource_manager = FakeResourceManager()
    finalizer = FakeFinalizer()
    result = video_agent.run_pipeline(
        args,
        reference_client=FakeReferenceClient(),
        animation_client=FakeAnimationClient(),
        finalizer=finalizer,
        resource_manager=resource_manager,
        profile_registry=_registry(),
    )
    return result, finalizer


def test_full_pipeline_finalizes_selected_visual_profile(tmp_path):
    result, finalizer = _run_full(
        tmp_path,
        idea="A modern electric train passing through a city",
        visual_style="photoreal",
    )
    run_root = tmp_path / "productions" / "test-take-01"

    assert result["state"] == "AWAITING_FINAL_QC"
    assert result["visual_style"] == "photoreal"
    assert result["visual_profile_status"] == "development"
    assert (run_root / "video-source.mp4").read_bytes() == SOURCE_MP4_FIXTURE
    assert (run_root / "video.mp4").read_bytes() == FINAL_MP4_FIXTURE
    assert finalizer.healthcheck_calls == 1
    assert finalizer.finalize_calls == 1

    reference_request = json.loads(
        (run_root / "reference-request.json").read_text(encoding="utf-8")
    )
    animation_request = json.loads(
        (run_root / "animation-request.json").read_text(encoding="utf-8")
    )
    assert reference_request["request"]["prompt"].startswith("A modern")
    assert animation_request["dependency_artifacts"] == [
        "sha256:" + result["reference"]["sha256"]
    ]

def test_full_pipeline_releases_reference_resources(
    tmp_path,
):
    resource_manager = FakeResourceManager()

    result, _ = _run_full(
        tmp_path,
        production_id="resource-cleanup-test",
        resource_manager=resource_manager,
    )

    assert resource_manager.release_calls == 1

    handoffs = result["resource_handoffs"]

    assert len(handoffs) == 1
    assert handoffs[0]["from_stage"] == "reference"
    assert handoffs[0]["to_stage"] == "animation"
    assert handoffs[0]["released"] is True
    assert handoffs[0]["unload_models"] is True
    assert handoffs[0]["free_memory"] is True

def test_reference_only_skips_animation_and_finalizer(tmp_path):
    args = _make_args(
        tmp_path,
        idea="A colorful geometric composition",
        motion="",
        reference_only=True,
    )
    result = video_agent.run_pipeline(
        args,
        reference_client=FakeReferenceClient(),
        animation_client=ForbiddenAnimationClient(),
        finalizer=ForbiddenFinalizer(),
        resource_manager=ForbiddenResourceManager(),
        profile_registry=_registry(),
    )
    run_root = tmp_path / "productions" / "test-take-01"

    assert result["state"] == "AWAITING_REFERENCE_QC"
    assert (run_root / "reference.png").is_file()
    assert not (run_root / "animation-request.json").exists()
    assert not (run_root / "video-source.mp4").exists()
    assert not (run_root / "video.mp4").exists()
    assert "source_video" not in result
    assert "final_video" not in result
    assert set(result["health"]) == {"reference"}


def test_skip_finalization_keeps_only_source_video(tmp_path):
    args = _make_args(
        tmp_path,
        motion="The camera moves forward slowly.",
        skip_finalization=True,
    )
    result = video_agent.run_pipeline(
        args,
        reference_client=FakeReferenceClient(),
        animation_client=FakeAnimationClient(),
        finalizer=ForbiddenFinalizer(),
        resource_manager=FakeResourceManager(),
        profile_registry=_registry(),
    )
    run_root = tmp_path / "productions" / "test-take-01"

    assert result["state"] == "AWAITING_FINALIZATION"
    assert (run_root / "video-source.mp4").is_file()
    assert not (run_root / "video.mp4").exists()
    assert "source_video" in result
    assert "final_video" not in result
    assert "finalization" not in result["health"]


def test_manifest_records_source_and_final_delivery(tmp_path):
    result, _ = _run_full(tmp_path)
    manifest = json.loads(
        Path(result["manifest"]).read_text(encoding="utf-8")
    )

    assert manifest["target_delivery"] == {
        "width": 720,
        "height": 1280,
        "fps": 60,
        "aspect_ratio": "9:16",
    }
    assert manifest["source_video"]["output"].endswith("video-source.mp4")
    assert manifest["source_video"]["fps"] == 24.0
    assert manifest["final_video"]["output"].endswith("video.mp4")
    assert manifest["source_video"]["probe"]["frame_rate"] == 24.0
    assert manifest["final_video"]["probe"]["frame_rate"] == 60.0
    assert len(manifest["source_video"]["sha256"]) == 64
    assert len(manifest["final_video"]["sha256"]) == 64


def test_default_style_and_cli_override(tmp_path):
    default_result, _ = _run_full(tmp_path, production_id="default-style")
    override_result, _ = _run_full(
        tmp_path,
        production_id="override-style",
        visual_style="corporate-3d",
    )

    assert default_result["visual_style"] == _registry().default_visual_style
    assert override_result["visual_style"] == "corporate-3d"


def test_reference_cli_overrides_profile(tmp_path):
    checkpoint = "custom-model.safetensors"
    negative_prompt = "custom negative prompt"
    prompt_suffix = "custom prompt suffix"

    class CheckingReferenceClient(FakeReferenceClient):
        def healthcheck(self, settings, *, require_ipadapter):
            assert settings.checkpoint == checkpoint
            assert settings.negative_prompt == negative_prompt
            assert settings.prompt_suffix == prompt_suffix
            return super().healthcheck(
                settings,
                require_ipadapter=require_ipadapter,
            )

    args = _make_args(
        tmp_path,
        motion="The object rotates slightly.",
        extra_args=[
            "--checkpoint",
            checkpoint,
            "--reference-negative-prompt",
            negative_prompt,
            "--reference-prompt-suffix",
            prompt_suffix,
        ],
    )
    result = video_agent.run_pipeline(
        args,
        reference_client=CheckingReferenceClient(),
        animation_client=FakeAnimationClient(),
        finalizer=FakeFinalizer(),
        resource_manager=FakeResourceManager(),
        profile_registry=_registry(),
    )

    assert result["state"] == "AWAITING_FINAL_QC"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--idea", ""],
        ["--idea", "test subject", "--motion", ""],
    ],
)
def test_rejects_empty_idea_or_motion(tmp_path, arguments):
    args = video_agent.build_parser().parse_args(
        arguments
        + [
            "--output-root",
            str(tmp_path / "productions"),
            "--event-db",
            str(tmp_path / "events.db"),
        ]
    )

    with pytest.raises(video_agent.VideoAgentError, match="tidak boleh kosong"):
        video_agent.run_pipeline(
            args,
            reference_client=FakeReferenceClient(),
            animation_client=FakeAnimationClient(),
            finalizer=FakeFinalizer(),
        )


def test_events_record_finalization_and_awaiting_qc(tmp_path):
    _run_full(tmp_path, production_id="events-test")
    store = EventStore(tmp_path / "events.db")
    try:
        events = store.read_events()
    finally:
        store.close()

    assert events[-1]["state"] == "AWAITING_FINAL_QC"
    assert {event["agent_id"] for event in events} >= {
        "video-producer",
        "reference-artist",
        "animator",
        "video-finalizer",
    }

def test_resource_cleanup_failure_writes_manifest(
    tmp_path,
):
    args = _make_args(
        tmp_path,
        motion="The camera moves forward slowly.",
        production_id="cleanup-failure-test",
    )

    manager = FailingResourceManager()

    with pytest.raises(
        RuntimeError,
        match="synthetic resource cleanup failure",
    ):
        video_agent.run_pipeline(
            args,
            reference_client=FakeReferenceClient(),
            animation_client=FakeAnimationClient(),
            finalizer=FakeFinalizer(),
            resource_manager=manager,
            profile_registry=_registry(),
        )

    manifest_path = (
        tmp_path
        / "productions"
        / "cleanup-failure-test"
        / "run-manifest.json"
    )

    assert manifest_path.is_file()

    manifest = json.loads(
        manifest_path.read_text(
            encoding="utf-8"
        )
    )

    assert manager.release_calls == 1
    assert manifest["state"] == "FAILED"
    assert (
        manifest["failure"]["stage"]
        == "resource_cleanup"
    )
    assert (
        manifest["failure"]["error_type"]
        == "RuntimeError"
    )
    assert "reference" in manifest
    assert "animation" not in manifest
    assert "source_video" not in manifest

def test_finalizer_failure_records_failed_event(tmp_path):
    args = _make_args(
        tmp_path,
        motion="The object moves slightly.",
        production_id="failure-test",
    )

    with pytest.raises(
        video_finalize.VideoFinalizeError,
        match="synthetic finalizer failure",
    ):
        video_agent.run_pipeline(
            args,
            reference_client=FakeReferenceClient(),
            animation_client=FakeAnimationClient(),
            finalizer=FailingFinalizer(),
            resource_manager=FakeResourceManager(),
            profile_registry=_registry(),
        )

    run_root = (
    tmp_path
    / "productions"
    / "failure-test"
    )

    manifest_path = (
        run_root / "run-manifest.json"
    )

    assert manifest_path.is_file()

    manifest = json.loads(
        manifest_path.read_text(
            encoding="utf-8"
        )
    )

    assert manifest["state"] == "FAILED"
    assert manifest["failure"]["stage"] == "finalization"
    assert (
        manifest["failure"]["error_type"]
        == "VideoFinalizeError"
    )
    assert (
        "synthetic finalizer failure"
        in manifest["failure"]["message"]
    )

    assert "reference" in manifest
    assert "source_video" in manifest
    assert "animation" in manifest
    assert len(manifest["resource_handoffs"]) == 1

    store = EventStore(tmp_path / "events.db")
    try:
        events = store.read_events()
    finally:
        store.close()

    assert events[-1]["state"] == "FAILED"
    assert events[-1]["stage"] == "failed"
