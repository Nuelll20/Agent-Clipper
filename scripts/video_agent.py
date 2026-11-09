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
from core.visual_profiles import (
    load_visual_profile_registry,
    resolve_delivery_profile,
    resolve_visual_style,
)
from scripts.providers import comfyui_animation as animation_provider
from scripts.providers import comfyui_reference as reference_provider
from scripts.providers import comfyui_resources as resource_provider
from scripts.providers import video_finalize as finalization_provider


# Preset yang sudah berhasil pada RTX 4060 Laptop 8 GB.
REFERENCE_WIDTH = 768
REFERENCE_HEIGHT = 1344
WAN_WIDTH = 416
WAN_HEIGHT = 736
OUTPUT_WIDTH = 720
OUTPUT_HEIGHT = 1280
SOURCE_FPS = 24.0
DURATION_SECONDS = 2.0
MAX_FRAMES = 49

DEFAULT_VISUAL_PROFILE_CONFIG = (
    Path(__file__).resolve().parent.parent / "config" / "visual-style-profiles.json"
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

    base = Path(configured).expanduser() if configured else PROJECT_ROOT / "productions"

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
        )
        + "\n",
        encoding="utf-8",
    )


def new_production_id(requested: str | None) -> str:
    if requested:
        return slug(requested)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    return f"quick-video-{timestamp}-" f"{secrets.token_hex(3)}"


def build_reference_request(
    *,
    story_id: str,
    production_id: str,
    idea: str,
    seed: int,
    width: int = REFERENCE_WIDTH,
    height: int = REFERENCE_HEIGHT,
) -> dict[str, Any]:
    request = {
        "shot_id": "shot-01",
        "width": width,
        "height": height,
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
        "fps": SOURCE_FPS,
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
                "Preserve the environment shown " "in the reference image."
            ),
        },
        "prompt": motion,
        "camera": ("stable cinematic camera, " "no sudden movement, no scene cut"),
        "continuity_notes": [
            ("preserve the main subject " "identity and proportions"),
            "keep the environment stable",
            ("avoid duplicated subjects " "and anatomy deformation"),
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
        "dependency_artifacts": ["sha256:" + reference_hash],
        "request": request,
    }


def run_pipeline(
    args: argparse.Namespace,
    *,
    reference_client: reference_provider.ComfyUIClient | None = None,
    animation_client: animation_provider.ComfyUIClient | None = None,
    finalizer: finalization_provider.VideoFinalizer | None = None,
    resource_manager: resource_provider.ComfyUIResourceManager | None = None,
    profile_registry: Any | None = None,
) -> dict[str, Any]:
    idea = str(args.idea or "").strip()
    motion = str(args.motion or "").strip()
    reference_only = getattr(args, "reference_only", False)
    skip_finalization = getattr(args, "skip_finalization", False)

    if not idea:
        raise VideoAgentError("--idea tidak boleh kosong.")

    if not reference_only and not motion:
        raise VideoAgentError("--motion tidak boleh kosong.")

    if not 0 <= args.seed <= 0xFFFFFFFFFFFFFFFF:
        raise VideoAgentError("--seed harus integer unsigned 64-bit.")

    # --- Visual profile resolution ---
    profile_config_path = getattr(
        args,
        "visual_profile_config",
        str(DEFAULT_VISUAL_PROFILE_CONFIG),
    ) or str(DEFAULT_VISUAL_PROFILE_CONFIG)

    if profile_registry is None:
        profile_registry = load_visual_profile_registry(profile_config_path)

    cli_visual_style = getattr(args, "visual_style", None)
    visual_style_name = (
        cli_visual_style
        if cli_visual_style is not None
        else profile_registry.default_visual_style
    )

    visual_profile = resolve_visual_style(
        profile_registry,
        visual_style_name,
    )

    delivery = resolve_delivery_profile(
        profile_registry,
        profile_registry.default_delivery_profile,
    )

    # CLI overrides take precedence.
    checkpoint = getattr(args, "checkpoint", None) or visual_profile.checkpoint

    negative_prompt = (
        getattr(
            args,
            "reference_negative_prompt",
            None,
        )
        or visual_profile.reference_negative_prompt_suffix
    )

    prompt_suffix = (
        getattr(
            args,
            "reference_prompt_suffix",
            None,
        )
        or visual_profile.reference_prompt_suffix
    )

    story_id = slug(args.story_id or idea)

    production_id = new_production_id(args.production_id)

    run_root = Path(args.output_root).expanduser().resolve() / production_id

    if run_root.exists() and any(run_root.iterdir()):
        raise VideoAgentError(
            "Folder production sudah berisi file: "
            f"{run_root}. "
            "Gunakan --production-id baru."
        )

    run_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    reference_request_path = run_root / "reference-request.json"

    reference_output_path = run_root / "reference.png"

    animation_request_path = run_root / "animation-request.json"

    source_video_path = run_root / "video-source.mp4"

    final_video_path = run_root / "video.mp4"

    manifest_path = run_root / "run-manifest.json"

    reference_settings = reference_provider.GenerationSettings(
        checkpoint=checkpoint,
        steps=visual_profile.reference_steps,
        cfg=visual_profile.reference_cfg,
        sampler=(visual_profile.reference_sampler),
        scheduler=(visual_profile.reference_scheduler),
        negative_prompt=negative_prompt,
        prompt_suffix=prompt_suffix,
    )

    animation_suffix = visual_profile.animation_prompt_suffix

    animation_settings = animation_provider.GenerationSettings(
        diffusion_model=("wan2.2_ti2v_5B_fp16.safetensors"),
        text_encoder=("umt5_xxl_fp8_e4m3fn_scaled" ".safetensors"),
        vae="wan2.2_vae.safetensors",
        steps=20,
        cfg=5.0,
        sampler="uni_pc",
        scheduler="simple",
        shift=8.0,
        weight_dtype="default",
        negative_prompt=(animation_provider.DEFAULT_NEGATIVE_PROMPT),
        prompt_suffix=animation_suffix,
        generation_width=WAN_WIDTH,
        generation_height=WAN_HEIGHT,
        max_frames=MAX_FRAMES,
    )

    finalization_settings = finalization_provider.FinalizationSettings(
        width=delivery.width,
        height=delivery.height,
        target_fps=delivery.target_fps,
        interpolation_method=(delivery.interpolation_method),
        pixel_format=delivery.pixel_format,
        crf=delivery.crf,
        preset=delivery.preset,
        audio_bitrate=delivery.audio_bitrate,
        audio_sample_rate=(delivery.audio_sample_rate),
    )

    reference_client = reference_client or reference_provider.ComfyUIClient(args.server)

    if not reference_only:
        animation_client = animation_client or animation_provider.ComfyUIClient(
            args.server
        )
        resource_manager = resource_manager or resource_provider.ComfyUIResourceManager(
            args.server
        )

        if not skip_finalization:
            finalizer = finalizer or finalization_provider.VideoFinalizer(
                getattr(args, "ffmpeg_bin", "ffmpeg"),
                getattr(args, "ffprobe_bin", "ffprobe"),
            )

    observer = RunObserver(
        Path(args.event_db).expanduser().resolve(),
        story_id,
        "QUICK_VIDEO_GENERATION",
        agent_id="video-producer",
    )

    observer.current = 0
    observer.total = 1 if reference_only else (2 if skip_finalization else 3)
    resource_handoffs: list[dict[str, Any]] = []

    target_delivery = {
        "width": delivery.width,
        "height": delivery.height,
        "fps": delivery.target_fps,
        "aspect_ratio": delivery.aspect_ratio,
    }

    run_metadata = {
        "schema_version": "1.0",
        "story_id": story_id,
        "production_id": production_id,
        "seed": args.seed,
        "idea": idea,
        "visual_style": visual_style_name,
        "visual_profile_status": visual_profile.status,
        "delivery_profile": delivery.name,
        "target_delivery": target_delivery,
        "resource_handoffs": resource_handoffs,
    }

    partial_manifest: dict[str, Any] = {
        **run_metadata,
    }

    current_stage = "health_check"

    try:
        health_message = (
            "Checking local reference provider"
            if reference_only
            else (
                "Checking local reference, animation, " "and finalization providers"
                if not skip_finalization
                else "Checking local reference and animation providers"
            )
        )
        observer.emit(
            "health_check",
            health_message,
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
        }

        partial_manifest["health"] = health

        if not reference_only:
            health["animation"] = animation_client.healthcheck(animation_settings)

            if not skip_finalization:
                if finalizer is None:
                    raise VideoAgentError("Video finalizer is unavailable.")
                health["finalization"] = finalizer.healthcheck()

        reference_payload = build_reference_request(
            story_id=story_id,
            production_id=production_id,
            idea=idea,
            seed=args.seed,
            width=REFERENCE_WIDTH,
            height=REFERENCE_HEIGHT,
        )

        write_json(
            reference_request_path,
            reference_payload,
        )

        current_stage = "reference_generation"

        observer.emit(
            "reference:shot-01",
            "Generating reference image",
            agent_id="reference-artist",
            unit="assets",
        )

        reference_result = reference_provider.generate_reference(
            reference_request_path,
            reference_output_path,
            client=reference_client,
            settings=reference_settings,
            execution_timeout=900,
            poll_interval=5,
        )

        reference_hash = sha256_file(reference_output_path)

        observer.current = 1

        observer.emit(
            "reference_completed:shot-01",
            ("Reference image generated " "and fingerprinted"),
            agent_id="reference-artist",
            unit="assets",
        )

        # --- Shared manifest fields ---
        manifest_base = {
            **run_metadata,
            "health": health,
            "reference": {
                **reference_result,
                "request": str(reference_request_path),
                "sha256": reference_hash,
            },
        }

        partial_manifest.update(manifest_base)

        if reference_only:
            manifest = {
                **manifest_base,
                "state": "AWAITING_REFERENCE_QC",
            }

            write_json(
                manifest_path,
                manifest,
            )

            observer.emit(
                "reference_only_completed",
                ("Reference image generated; " "awaiting QC"),
                state="AWAITING_REFERENCE_QC",
                agent_id="video-producer",
                unit="assets",
            )

            return {
                **manifest,
                "manifest": str(manifest_path),
            }

        # --- Full generation path ---
        current_stage = "resource_cleanup"

        if resource_manager is None:
            raise VideoAgentError(
                "ComfyUI resource manager is unavailable."
            )

        observer.emit(
            "resource_cleanup:reference-to-animation",
            (
                "Unloading reference model before "
                "starting Wan animation"
            ),
            agent_id="video-producer",
            unit="assets",
        )

        cleanup_result = resource_manager.release()

        resource_handoffs.append(
            {
                "from_stage": "reference",
                "to_stage": "animation",
                **cleanup_result,
            }
        )

        observer.emit(
            "resource_cleanup_completed:reference-to-animation",
            (
                "Reference model released; "
                "animation stage may continue"
            ),
            agent_id="video-producer",
            unit="assets",
        )

        current_stage = "animation_generation"

        animation_payload = build_animation_request(
            story_id=story_id,
            production_id=production_id,
            idea=idea,
            motion=motion,
            reference_hash=reference_hash,
            seed=args.seed,
        )

        write_json(
            animation_request_path,
            animation_payload,
        )

        observer.emit(
            "animation:shot-01",
            ("Generating Wan animation " "from the reference image"),
            agent_id="animator",
            unit="assets",
        )

        animation_result = animation_provider.generate_animation(
            animation_request_path,
            source_video_path,
            client=animation_client,
            settings=animation_settings,
            execution_timeout=3600,
            poll_interval=5,
            reference_image=(reference_output_path),
            reference_sha256=(reference_hash),
        )

        observer.current = 2

        source_video = {
            "output": str(source_video_path),
            "sha256": sha256_file(source_video_path),
            "size_bytes": (source_video_path.stat().st_size),
            "width": OUTPUT_WIDTH,
            "height": OUTPUT_HEIGHT,
            "fps": SOURCE_FPS,
        }

        animation_manifest = {
            **animation_result,
            "request": str(animation_request_path),
            "sha256": source_video["sha256"],
        }

        partial_manifest.update(
            {
                "motion": motion,
                "source_video": source_video,
                "animation": animation_manifest,
            }
        )

        if skip_finalization:
            manifest = {
                **manifest_base,
                "state": "AWAITING_FINALIZATION",
                "motion": motion,
                "source_video": source_video,
                "animation": animation_manifest,
            }

            write_json(
                manifest_path,
                manifest,
            )

            observer.emit(
                "awaiting_finalization",
                (
                    "Reference and animation "
                    "assets generated; "
                    "awaiting finalization"
                ),
                state="AWAITING_FINALIZATION",
                agent_id="video-producer",
                unit="assets",
            )

            return {
                **manifest,
                "manifest": str(manifest_path),
            }

        current_stage = "finalization"
        observer.emit(
            "finalization:shot-01",
            "Finalizing Shorts delivery at 720x1280 60 FPS",
            agent_id="video-finalizer",
            unit="assets",
        )

        finalization_result = finalizer.finalize(
            source_video_path,
            final_video_path,
            settings=finalization_settings,
        )

        observer.current = 3
        source_video["probe"] = finalization_result["source_probe"]

        manifest = {
            **manifest_base,
            "state": "AWAITING_FINAL_QC",
            "motion": motion,
            "source_video": source_video,
            "animation": animation_manifest,
            "final_video": {
                "output": finalization_result["output"],
                "interpolation_method": (finalization_result["interpolation_method"]),
                "probe": finalization_result["final_probe"],
                "duration_delta": (finalization_result["duration_delta"]),
                "size_bytes": finalization_result["size_bytes"],
                "sha256": sha256_file(final_video_path),
            },
        }

        write_json(
            manifest_path,
            manifest,
        )

        observer.emit(
            "awaiting_final_qc",
            ("Shorts delivery finalized; " "awaiting final quality review"),
            state="AWAITING_FINAL_QC",
            agent_id="video-producer",
            unit="assets",
        )

        return {
            **manifest,
            "manifest": str(manifest_path),
        }

    except Exception as exc:
        failure_manifest = {
            **partial_manifest,
            "state": "FAILED",
            "failure": {
                "stage": current_stage,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            },
        }

        manifest_write_error: str | None = None

        try:
            write_json(
                manifest_path,
                failure_manifest,
            )
        except Exception as manifest_exc:
            manifest_write_error = (
                f"{type(manifest_exc).__name__}: "
                f"{manifest_exc}"
            )

        failure_message = (
            "Quick video generation failed during "
            f"{current_stage}: {type(exc).__name__}"
        )

        if manifest_write_error is not None:
            failure_message += (
                "; failure manifest could not be written: "
                f"{manifest_write_error}"
            )

        observer.emit(
            "failed",
            failure_message,
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
            "Generate one short vertical video " "from one idea through local ComfyUI."
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
        default=str(default_output_root()),
    )

    parser.add_argument(
        "--event-db",
        default=str(default_event_db()),
    )

    parser.add_argument(
        "--visual-style",
        default=None,
        help="Visual style profile name.",
    )

    parser.add_argument(
        "--visual-profile-config",
        default=str(DEFAULT_VISUAL_PROFILE_CONFIG),
        help=("Path to visual-style-profiles " "JSON config."),
    )

    parser.add_argument(
        "--reference-only",
        action="store_true",
        default=False,
        help=("Generate reference image only; " "skip animation."),
    )

    parser.add_argument(
        "--skip-finalization",
        action="store_true",
        default=False,
        help=("Keep the 24 FPS source video and " "skip 60 FPS finalization."),
    )

    parser.add_argument(
        "--ffmpeg-bin",
        default="ffmpeg",
    )

    parser.add_argument(
        "--ffprobe-bin",
        default="ffprobe",
    )

    parser.add_argument(
        "--checkpoint",
        default=None,
        help="Override profile checkpoint.",
    )

    parser.add_argument(
        "--reference-negative-prompt",
        default=None,
        help=("Override profile negative prompt."),
    )

    parser.add_argument(
        "--reference-prompt-suffix",
        default=None,
        help=("Override profile prompt suffix."),
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
        finalization_provider.VideoFinalizeError,
        resource_provider.ComfyUIResourceError,
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
