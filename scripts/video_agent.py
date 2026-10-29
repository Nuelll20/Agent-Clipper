#!/usr/bin/env python3
"""Generate one short video from one idea through local ComfyUI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.observation import RunObserver
from scripts.providers import comfyui_animation as animation_provider
from scripts.providers import comfyui_reference as reference_provider


# Preset yang sudah berhasil pada RTX 4060 Laptop 8 GB.
REFERENCE_WIDTH = 416
REFERENCE_HEIGHT = 736
OUTPUT_WIDTH = 720
OUTPUT_HEIGHT = 1280
FPS = 24.0
DURATION_SECONDS = 2.0
MAX_FRAMES = 49

REFERENCE_SUFFIX = (
    "photorealistic cinematic still, natural colors, realistic anatomy, "
    "detailed environment, clean vertical composition, "
    "no illustration, no cartoon"
)

ANIMATION_SUFFIX = (
    "photorealistic cinematic footage, stable realistic anatomy, "
    "natural subtle motion, preserve the reference appearance, "
    "no scene cut, no text, no watermark"
)

DEFAULT_MOTION = (
    "The main subject makes one small natural movement, "
    "then settles into a stable pose. "
    "The movement is subtle, realistic, and anatomically correct."
)


class VideoAgentError(RuntimeError):
    """The automatic idea-to-video run cannot continue safely."""


def default_output_root() -> Path:
    configured = os.environ.get("HERMES_PRODUCTION_ROOT", "").strip()

    base = (
        Path(configured).expanduser()
        if configured
        else PROJECT_ROOT / "productions"
    )

    return base / "quick-video"


def default_event_db() -> Path:
    configured = os.environ.get("HERMES_EVENT_DB", "").strip()

    if configured:
        return Path(configured).expanduser()

    return PROJECT_ROOT / "state" / "production-events.db"


def slug(value: str, fallback: str = "quick-video") -> str:
    cleaned = re.sub(
        r"[^a-z0-9]+",
        "-",
        value.lower(),
    ).strip("-")

    return (cleaned or fallback)[:64]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def new_production_id(requested: str | None) -> str:
    if requested:
        return slug(requested)

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")

    return (
        f"quick-video-{timestamp}-"
        f"{secrets.token_hex(3)}"
    )


def build_reference_request(
    *,
    story_id: str,
    production_id: str,
    idea: str,
    seed: int,
) -> dict[str, Any]:
    request = {
        "shot_id": "shot-01",
        "width": REFERENCE_WIDTH,
        "height": REFERENCE_HEIGHT,
        "seed": seed,
        "prompt": idea,
    }

    identity = {
        "story_id": story_id,
        "production_id": production_id,
        "kind": "reference",
        "request": request,
    }

    return {
        "schema_version": "1.0",
        "story_id": story_id,
        "revision_id": "quick-rev-01",
        "production_id": production_id,
        "shot_id": "shot-01",
        "kind": "reference",
        "input_fingerprint": fingerprint(identity),
        "dependency_artifacts": [],
        "request": request,
    }


def build_animation_request(
    *,
    story_id: str,
    production_id: str,
    idea: str,
    motion: str,
    reference_hash: str,
    seed: int,
) -> dict[str, Any]:
    request = {
        "shot_id": "shot-01",
        "scene_id": "scene-01",
        "duration_seconds": DURATION_SECONDS,
        "width": OUTPUT_WIDTH,
        "height": OUTPUT_HEIGHT,
        "fps": FPS,
        "characters": [
            {
                "id": "main-subject",
                "name": "Main subject",
                "visual_identity": idea,
            }
        ],
        "location": {
            "id": "main-location",
            "name": "Main location",
            "description": (
                "Preserve the environment shown "
                "in the reference image."
            ),
        },
        "prompt": motion,
        "camera": (
            "stable cinematic camera, "
            "no sudden movement, no scene cut"
        ),
        "continuity_notes": [
            (
                "preserve the main subject "
                "identity and proportions"
            ),
            "keep the environment stable",
            (
                "avoid duplicated subjects "
                "and anatomy deformation"
            ),
        ],
        "reference_dependency": "reference",
        "seed": seed,
    }

    identity = {
        "story_id": story_id,
        "production_id": production_id,
        "kind": "animation",
        "dependency": reference_hash,
        "request": request,
    }

    return {
        "schema_version": "1.0",
        "story_id": story_id,
        "revision_id": "quick-rev-01",
        "production_id": production_id,
        "shot_id": "shot-01",
        "kind": "animation",
        "input_fingerprint": fingerprint(identity),
        "dependency_artifacts": [
            "sha256:" + reference_hash
        ],
        "request": request,
    }


def run_pipeline(
    args: argparse.Namespace,
    *,
    reference_client: (
        reference_provider.ComfyUIClient | None
    ) = None,
    animation_client: (
        animation_provider.ComfyUIClient | None
    ) = None,
) -> dict[str, Any]:
    idea = str(args.idea or "").strip()
    motion = str(args.motion or "").strip()

    if not idea:
        raise VideoAgentError(
            "--idea tidak boleh kosong."
        )

    if not motion:
        raise VideoAgentError(
            "--motion tidak boleh kosong."
        )

    if not 0 <= args.seed <= 0xFFFFFFFFFFFFFFFF:
        raise VideoAgentError(
            "--seed harus integer unsigned 64-bit."
        )

    story_id = slug(
        args.story_id or idea
    )

    production_id = new_production_id(
        args.production_id
    )

    run_root = (
        Path(args.output_root)
        .expanduser()
        .resolve()
        / production_id
    )

    if (
        run_root.exists()
        and any(run_root.iterdir())
    ):
        raise VideoAgentError(
            "Folder production sudah berisi file: "
            f"{run_root}. "
            "Gunakan --production-id baru."
        )

    run_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    reference_request_path = (
        run_root / "reference-request.json"
    )

    reference_output_path = (
        run_root / "reference.png"
    )

    animation_request_path = (
        run_root / "animation-request.json"
    )

    video_output_path = (
        run_root / "video.mp4"
    )

    manifest_path = (
        run_root / "run-manifest.json"
    )

    reference_settings = (
        reference_provider.GenerationSettings(
            checkpoint=(
                "sd_xl_base_1.0.safetensors"
            ),
            steps=20,
            cfg=6.0,
            sampler="dpmpp_2m",
            scheduler="karras",
            negative_prompt=(
                reference_provider
                .DEFAULT_NEGATIVE_PROMPT
            ),
            prompt_suffix=REFERENCE_SUFFIX,
        )
    )

    animation_settings = (
        animation_provider.GenerationSettings(
            diffusion_model=(
                "wan2.2_ti2v_5B_fp16.safetensors"
            ),
            text_encoder=(
                "umt5_xxl_fp8_e4m3fn_scaled"
                ".safetensors"
            ),
            vae="wan2.2_vae.safetensors",
            steps=20,
            cfg=5.0,
            sampler="uni_pc",
            scheduler="simple",
            shift=8.0,
            weight_dtype="default",
            negative_prompt=(
                animation_provider
                .DEFAULT_NEGATIVE_PROMPT
            ),
            prompt_suffix=ANIMATION_SUFFIX,
            generation_width=REFERENCE_WIDTH,
            generation_height=REFERENCE_HEIGHT,
            max_frames=MAX_FRAMES,
        )
    )

    reference_client = (
        reference_client
        or reference_provider.ComfyUIClient(
            args.server
        )
    )

    animation_client = (
        animation_client
        or animation_provider.ComfyUIClient(
            args.server
        )
    )

    observer = RunObserver(
        Path(args.event_db)
        .expanduser()
        .resolve(),
        story_id,
        "QUICK_VIDEO_GENERATION",
        agent_id="video-producer",
    )

    observer.current = 0
    observer.total = 2

    try:
        observer.emit(
            "health_check",
            (
                "Checking local reference "
                "and animation providers"
            ),
            agent_id="video-producer",
            unit="assets",
        )

        health = {
            "reference": (
                reference_client.healthcheck(
                    reference_settings,
                    require_ipadapter=False,
                )
            ),
            "animation": (
                animation_client.healthcheck(
                    animation_settings
                )
            ),
        }

        reference_payload = (
            build_reference_request(
                story_id=story_id,
                production_id=production_id,
                idea=idea,
                seed=args.seed,
            )
        )

        write_json(
            reference_request_path,
            reference_payload,
        )

        observer.emit(
            "reference:shot-01",
            "Generating reference image",
            agent_id="reference-artist",
            unit="assets",
        )

        reference_result = (
            reference_provider.generate_reference(
                reference_request_path,
                reference_output_path,
                client=reference_client,
                settings=reference_settings,
                execution_timeout=900,
                poll_interval=5,
            )
        )

        reference_hash = sha256_file(
            reference_output_path
        )

        observer.current = 1

        observer.emit(
            "reference_completed:shot-01",
            (
                "Reference image generated "
                "and fingerprinted"
            ),
            agent_id="reference-artist",
            unit="assets",
        )

        animation_payload = (
            build_animation_request(
                story_id=story_id,
                production_id=production_id,
                idea=idea,
                motion=motion,
                reference_hash=reference_hash,
                seed=args.seed,
            )
        )

        write_json(
            animation_request_path,
            animation_payload,
        )

        observer.emit(
            "animation:shot-01",
            (
                "Generating Wan animation "
                "from the reference image"
            ),
            agent_id="animator",
            unit="assets",
        )

        animation_result = (
            animation_provider
            .generate_animation(
                animation_request_path,
                video_output_path,
                client=animation_client,
                settings=animation_settings,
                execution_timeout=3600,
                poll_interval=5,
                reference_image=(
                    reference_output_path
                ),
                reference_sha256=(
                    reference_hash
                ),
            )
        )

        observer.current = 2

        manifest = {
            "schema_version": "1.0",
            "story_id": story_id,
            "production_id": production_id,
            "state": "COMPLETED",
            "seed": args.seed,
            "idea": idea,
            "motion": motion,
            "health": health,
            "reference": {
                **reference_result,
                "request": str(
                    reference_request_path
                ),
                "sha256": reference_hash,
            },
            "animation": {
                **animation_result,
                "request": str(
                    animation_request_path
                ),
                "sha256": sha256_file(
                    video_output_path
                ),
            },
        }

        write_json(
            manifest_path,
            manifest,
        )

        observer.emit(
            "completed",
            (
                "Reference and animation "
                "assets completed"
            ),
            state="COMPLETED",
            agent_id="video-producer",
            unit="assets",
        )

        return {
            **manifest,
            "manifest": str(manifest_path),
        }

    except Exception as exc:
        observer.emit(
            "failed",
            (
                "Quick video generation failed: "
                f"{type(exc).__name__}"
            ),
            state="FAILED",
            agent_id="video-producer",
            unit="assets",
        )

        raise

    finally:
        observer.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate one short vertical video "
            "from one idea through local ComfyUI."
        )
    )

    parser.add_argument(
        "--idea",
        required=True,
    )

    parser.add_argument(
        "--motion",
        default=DEFAULT_MOTION,
    )

    parser.add_argument(
        "--story-id",
    )

    parser.add_argument(
        "--production-id",
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=20261030,
    )

    parser.add_argument(
        "--output-root",
        default=str(
            default_output_root()
        ),
    )

    parser.add_argument(
        "--event-db",
        default=str(
            default_event_db()
        ),
    )

    parser.add_argument(
        "--server",
        default=os.environ.get(
            "HERMES_COMFYUI_URL",
            "http://127.0.0.1:8188",
        ),
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        result = run_pipeline(args)

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )

        return 0

    except (
        VideoAgentError,
        reference_provider.ComfyUIProviderError,
        animation_provider.ComfyUIAnimationError,
        OSError,
        ValueError,
    ) as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        return 1

    except KeyboardInterrupt:
        print(
            "\nVideo Agent dihentikan oleh operator.",
            file=sys.stderr,
        )

        return 130


if __name__ == "__main__":
    raise SystemExit(main())