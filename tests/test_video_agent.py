import json
from pathlib import Path

import pytest

from core.event_store import EventStore
from scripts import video_agent


PNG_FIXTURE = (
    b"\x89PNG\r\n\x1a\nfixture-reference"
)

MP4_FIXTURE = (
    b"\x00\x00\x00\x18ftypmp42fixture-video"
)


class FakeReferenceClient:
    def healthcheck(
        self,
        settings,
        *,
        require_ipadapter,
    ):
        assert (
            settings.checkpoint
            == "sd_xl_base_1.0.safetensors"
        )

        assert require_ipadapter is False

        return {
            "ready": True,
            "checkpoint": settings.checkpoint,
        }

    def queue_prompt(
        self,
        workflow,
        *,
        client_id,
    ):
        assert workflow["4"]["inputs"] == {
            "width": 416,
            "height": 736,
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
    def healthcheck(
        self,
        settings,
    ):
        assert settings.max_frames == 49

        return {
            "ready": True,
            "diffusion_model": (
                settings.diffusion_model
            ),
        }

    def upload_image(
        self,
        source: Path,
        *,
        content_hash,
    ):
        assert (
            source.read_bytes()
            == PNG_FIXTURE
        )

        assert len(content_hash) == 64

        return (
            "HermesAI/provider-inputs/"
            "reference.png"
        )

    def queue_prompt(
        self,
        workflow,
        *,
        client_id,
    ):
        assert (
            workflow["8"]["inputs"]["length"]
            == 49
        )

        assert (
            workflow["11"]["inputs"]["width"]
            == 720
        )

        assert (
            workflow["11"]["inputs"]["height"]
            == 1280
        )

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
        return MP4_FIXTURE


def test_one_command_builds_requests_video_manifest_and_events(
    tmp_path,
):
    args = (
        video_agent
        .build_parser()
        .parse_args([
            "--idea",
            (
                "A realistic wood frog "
                "resting on moss"
            ),
            "--motion",
            (
                "The frog makes one "
                "short natural hop."
            ),
            "--story-id",
            "frog-test",
            "--production-id",
            "frog-take-01",
            "--output-root",
            str(
                tmp_path / "productions"
            ),
            "--event-db",
            str(
                tmp_path / "events.db"
            ),
        ])
    )

    result = video_agent.run_pipeline(
        args,
        reference_client=(
            FakeReferenceClient()
        ),
        animation_client=(
            FakeAnimationClient()
        ),
    )

    run_root = (
        tmp_path
        / "productions"
        / "frog-take-01"
    )

    assert (
        run_root / "reference.png"
    ).read_bytes() == PNG_FIXTURE

    assert (
        run_root / "video.mp4"
    ).read_bytes() == MP4_FIXTURE

    reference_request = json.loads(
        (
            run_root
            / "reference-request.json"
        ).read_text()
    )

    animation_request = json.loads(
        (
            run_root
            / "animation-request.json"
        ).read_text()
    )

    assert (
        reference_request["request"]["prompt"]
        .startswith("A realistic")
    )

    assert (
        animation_request["request"]["prompt"]
        .endswith("natural hop.")
    )

    assert (
        animation_request[
            "dependency_artifacts"
        ]
        == [
            (
                "sha256:"
                + result["reference"]["sha256"]
            )
        ]
    )

    assert result["state"] == "COMPLETED"

    assert Path(
        result["manifest"]
    ).is_file()

    store = EventStore(
        tmp_path / "events.db"
    )

    try:
        events = store.read_events()
    finally:
        store.close()

    assert (
        events[-1]["state"]
        == "COMPLETED"
    )

    assert {
        event["agent_id"]
        for event in events
    } >= {
        "video-producer",
        "reference-artist",
        "animator",
    }


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "--idea",
            "",
        ],
        [
            "--idea",
            "frog",
            "--motion",
            "",
        ],
    ],
)
def test_rejects_empty_idea_or_motion(
    tmp_path,
    arguments,
):
    args = (
        video_agent
        .build_parser()
        .parse_args(
            arguments
            + [
                "--output-root",
                str(
                    tmp_path
                    / "productions"
                ),
            ]
        )
    )

    with pytest.raises(
        video_agent.VideoAgentError,
        match="tidak boleh kosong",
    ):
        video_agent.run_pipeline(
            args,
            reference_client=(
                FakeReferenceClient()
            ),
            animation_client=(
                FakeAnimationClient()
            ),
        )