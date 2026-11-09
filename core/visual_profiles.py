"""Load and resolve visual-style profiles for the Hermes video pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class VisualProfileError(Exception):
    """A visual-style profile configuration is invalid or missing."""


_SUPPORTED_SCHEMA_VERSIONS = {"1.0"}


@dataclass(frozen=True)
class DeliveryProfile:
    name: str
    aspect_ratio: str
    width: int
    height: int
    target_fps: int
    orientation: str
    container: str
    video_codec: str
    audio_codec: str
    interpolation_method: str
    pixel_format: str
    crf: int
    preset: str
    audio_bitrate: str
    audio_sample_rate: int


@dataclass(frozen=True)
class VisualStyleProfile:
    name: str
    description: str
    status: str
    checkpoint: str
    reference_steps: int
    reference_cfg: float
    reference_sampler: str
    reference_scheduler: str
    reference_prompt_suffix: str
    reference_negative_prompt_suffix: str
    animation_prompt_suffix: str


@dataclass(frozen=True)
class VisualProfileRegistry:
    schema_version: str
    default_delivery_profile: str
    default_visual_style: str
    delivery_profiles: tuple[DeliveryProfile, ...]
    visual_profiles: tuple[VisualStyleProfile, ...]


def _parse_delivery(raw: dict[str, Any]) -> DeliveryProfile:
    if not isinstance(raw, dict):
        raise VisualProfileError("Delivery profile must be a dict.")

    for field in (
        "name",
        "aspect_ratio",
        "orientation",
        "container",
        "video_codec",
        "audio_codec",
        "interpolation_method",
        "pixel_format",
        "preset",
        "audio_bitrate",
    ):
        val = raw.get(field)
        if not isinstance(val, str) or not val.strip():
            raise VisualProfileError(
                f"Delivery profile field {field!r} must be a non-empty string, got {val!r}."
            )

    orientation = raw["orientation"]
    if orientation != "portrait":
        raise VisualProfileError(
            f"orientation must be 'portrait', got {orientation!r}."
        )

    fps = raw.get("target_fps")
    if isinstance(fps, bool) or not isinstance(fps, int) or fps != 60:
        raise VisualProfileError(
            f"target_fps must be 60, got {fps!r}."
        )

    interpolation_method = raw["interpolation_method"]
    if interpolation_method != "ffmpeg-minterpolate":
        raise VisualProfileError(
            "interpolation_method must be "
            f"'ffmpeg-minterpolate', got {interpolation_method!r}."
        )

    pixel_format = raw["pixel_format"]
    if pixel_format != "yuv420p":
        raise VisualProfileError(
            f"pixel_format must be 'yuv420p', got {pixel_format!r}."
        )

    crf = raw.get("crf")
    if isinstance(crf, bool) or not isinstance(crf, int) or not 0 <= crf <= 51:
        raise VisualProfileError(
            f"crf must be an integer from 0 to 51, got {crf!r}."
        )

    audio_sample_rate = raw.get("audio_sample_rate")
    if (
        isinstance(audio_sample_rate, bool)
        or not isinstance(audio_sample_rate, int)
        or audio_sample_rate <= 0
    ):
        raise VisualProfileError(
            "audio_sample_rate must be a positive integer, "
            f"got {audio_sample_rate!r}."
        )

    width = raw.get("width")
    height = raw.get("height")
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        raise VisualProfileError(
            f"Delivery width must be a positive integer, got {width!r}."
        )
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        raise VisualProfileError(
            f"Delivery height must be a positive integer, got {height!r}."
        )

    ar = raw["aspect_ratio"]
    parts = ar.split(":")

    if len(parts) != 2:
        raise VisualProfileError(
            f"Invalid aspect_ratio format: {ar!r}."
        )

    try:
        ar_w, ar_h = int(parts[0]), int(parts[1])
    except ValueError:
        raise VisualProfileError(
            f"Non-integer aspect_ratio components: {ar!r}."
        )

    if ar_w <= 0 or ar_h <= 0:
        raise VisualProfileError(
            f"Aspect ratio components must be positive: {ar!r}."
        )

    if width * ar_h != height * ar_w:
        raise VisualProfileError(
            f"Delivery {width}x{height} does not match "
            f"aspect_ratio {ar}."
        )

    return DeliveryProfile(
        name=raw["name"],
        aspect_ratio=ar,
        width=width,
        height=height,
        target_fps=fps,
        orientation=orientation,
        container=raw["container"],
        video_codec=raw["video_codec"],
        audio_codec=raw["audio_codec"],
        interpolation_method=interpolation_method,
        pixel_format=pixel_format,
        crf=crf,
        preset=raw["preset"],
        audio_bitrate=raw["audio_bitrate"],
        audio_sample_rate=audio_sample_rate,
    )


def _parse_visual(raw: dict[str, Any]) -> VisualStyleProfile:
    if not isinstance(raw, dict):
        raise VisualProfileError("Visual profile must be a dict.")

    for field in (
        "name",
        "description",
        "status",
        "checkpoint",
        "reference_sampler",
        "reference_scheduler",
        "reference_prompt_suffix",
        "reference_negative_prompt_suffix",
        "animation_prompt_suffix",
    ):
        val = raw.get(field)
        if not isinstance(val, str) or not val.strip():
            raise VisualProfileError(
                f"Visual profile field {field!r} must be a non-empty string, got {val!r}."
            )

    steps = raw.get("reference_steps")
    if isinstance(steps, bool) or not isinstance(steps, int) or steps <= 0:
        raise VisualProfileError(
            f"reference_steps must be a positive integer, got {steps!r}."
        )

    cfg = raw.get("reference_cfg")
    if isinstance(cfg, bool) or not isinstance(cfg, (int, float)) or cfg <= 0:
        raise VisualProfileError(
            f"reference_cfg must be a positive number, got {cfg!r}."
        )

    return VisualStyleProfile(
        name=raw["name"],
        description=raw["description"],
        status=raw["status"],
        checkpoint=raw["checkpoint"],
        reference_steps=steps,
        reference_cfg=float(cfg),
        reference_sampler=raw["reference_sampler"],
        reference_scheduler=raw["reference_scheduler"],
        reference_prompt_suffix=raw["reference_prompt_suffix"],
        reference_negative_prompt_suffix=raw[
            "reference_negative_prompt_suffix"
        ],
        animation_prompt_suffix=raw["animation_prompt_suffix"],
    )


def load_visual_profile_registry(
    path: str | Path,
) -> VisualProfileRegistry:
    """Load and validate a visual-style profiles JSON file."""

    path = Path(path)

    try:
        data = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise VisualProfileError(
            f"Cannot load visual profile config "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise VisualProfileError("Configuration root must be a JSON object.")

    try:
        version = data.get("schema_version")

        if version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise VisualProfileError(
                f"Unsupported schema_version {version!r}. "
                f"Supported: {_SUPPORTED_SCHEMA_VERSIONS}."
            )

        delivery_raws = data.get("delivery_profiles")
        if not delivery_raws or not isinstance(delivery_raws, list):
            raise VisualProfileError(
                "delivery_profiles must not be empty."
            )

        delivery_profiles = tuple(
            _parse_delivery(d)
            for d in delivery_raws
        )

        delivery_names = set()
        for d in delivery_profiles:
            if d.name in delivery_names:
                raise VisualProfileError(
                    f"Duplicate delivery profile name: {d.name!r}."
                )
            delivery_names.add(d.name)

        visual_raws = data.get("visual_profiles")
        if not visual_raws or not isinstance(visual_raws, list):
            raise VisualProfileError(
                "visual_profiles must not be empty."
            )

        visual_profiles = tuple(
            _parse_visual(v) for v in visual_raws
        )

        visual_names = set()
        for v in visual_profiles:
            if v.name in visual_names:
                raise VisualProfileError(
                    f"Duplicate visual profile name: {v.name!r}."
                )
            visual_names.add(v.name)

        default_delivery = data.get("default_delivery_profile")
        if not isinstance(default_delivery, str) or not default_delivery.strip():
            raise VisualProfileError(
                "default_delivery_profile must be a non-empty string."
            )
        if default_delivery not in delivery_names:
            raise VisualProfileError(
                f"Default delivery profile {default_delivery!r} not found in delivery_profiles."
            )

        default_visual = data.get("default_visual_style")
        if not isinstance(default_visual, str) or not default_visual.strip():
            raise VisualProfileError(
                "default_visual_style must be a non-empty string."
            )
        if default_visual not in visual_names:
            raise VisualProfileError(
                f"Default visual style {default_visual!r} not found in visual_profiles."
            )

        registry = VisualProfileRegistry(
            schema_version=version,
            default_delivery_profile=default_delivery,
            default_visual_style=default_visual,
            delivery_profiles=delivery_profiles,
            visual_profiles=visual_profiles,
        )

        active_delivery = resolve_delivery_profile(
            registry, default_delivery
        )
        if active_delivery.width != 720 or active_delivery.height != 1280:
            raise VisualProfileError(
                f"Active delivery profile must be 720x1280, got "
                f"{active_delivery.width}x{active_delivery.height}."
            )

        return registry

    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, VisualProfileError):
            raise
        raise VisualProfileError(
            f"Invalid visual profile configuration: {exc}"
        ) from exc


def resolve_delivery_profile(
    registry: VisualProfileRegistry,
    profile_name: str,
) -> DeliveryProfile:
    """Find a delivery profile by name or raise VisualProfileError."""

    for profile in registry.delivery_profiles:
        if profile.name == profile_name:
            return profile

    available = [
        p.name for p in registry.delivery_profiles
    ]

    raise VisualProfileError(
        f"Delivery profile {profile_name!r} not found. "
        f"Available: {available}."
    )


def resolve_visual_style(
    registry: VisualProfileRegistry,
    style_name: str,
) -> VisualStyleProfile:
    """Find a visual profile by name or raise VisualProfileError."""

    for profile in registry.visual_profiles:
        if profile.name == style_name:
            return profile

    available = [
        p.name for p in registry.visual_profiles
    ]

    raise VisualProfileError(
        f"Visual style {style_name!r} not found. "
        f"Available: {available}."
    )
