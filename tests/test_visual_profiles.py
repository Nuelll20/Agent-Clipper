import json
from pathlib import Path

import pytest

from core.visual_profiles import (
    VisualProfileError,
    load_visual_profile_registry,
    resolve_delivery_profile,
    resolve_visual_style,
)


CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "visual-style-profiles.json"
)


def _delivery(**overrides):
    value = {
        "name": "shorts-720p60",
        "aspect_ratio": "9:16",
        "width": 720,
        "height": 1280,
        "target_fps": 60,
        "orientation": "portrait",
        "container": "mp4",
        "video_codec": "h264",
        "audio_codec": "aac",
        "interpolation_method": "ffmpeg-minterpolate",
        "pixel_format": "yuv420p",
        "crf": 18,
        "preset": "slow",
        "audio_bitrate": "192k",
        "audio_sample_rate": 48000,
    }
    value.update(overrides)
    return value


def _visual(**overrides):
    value = {
        "name": "neutral",
        "description": "A test visual style.",
        "status": "development",
        "checkpoint": "test.safetensors",
        "reference_steps": 20,
        "reference_cfg": 6.0,
        "reference_sampler": "dpmpp_2m",
        "reference_scheduler": "karras",
        "reference_prompt_suffix": "clean composition",
        "reference_negative_prompt_suffix": "text, watermark",
        "animation_prompt_suffix": "stable motion",
    }
    value.update(overrides)
    return value


def _config(*, delivery_profiles=None, visual_profiles=None, **overrides):
    value = {
        "schema_version": "1.0",
        "default_delivery_profile": "shorts-720p60",
        "default_visual_style": "neutral",
        "delivery_profiles": (
            [_delivery()] if delivery_profiles is None else delivery_profiles
        ),
        "visual_profiles": (
            [_visual()] if visual_profiles is None else visual_profiles
        ),
    }
    value.update(overrides)
    return value


def _write_config(tmp_path, value):
    path = tmp_path / "visual-profiles.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_config_loads_complete_shorts_delivery_contract():
    registry = load_visual_profile_registry(CONFIG_PATH)
    delivery = resolve_delivery_profile(
        registry,
        registry.default_delivery_profile,
    )

    assert registry.schema_version == "1.0"
    assert registry.default_delivery_profile == "shorts-720p60"
    assert registry.default_visual_style == "neutral"
    assert (delivery.width, delivery.height) == (720, 1280)
    assert delivery.target_fps == 60
    assert delivery.aspect_ratio == "9:16"
    assert delivery.interpolation_method == "ffmpeg-minterpolate"
    assert delivery.pixel_format == "yuv420p"
    assert delivery.crf == 18
    assert delivery.preset == "slow"
    assert delivery.audio_bitrate == "192k"
    assert delivery.audio_sample_rate == 48000


def test_all_visual_profiles_available():
    registry = load_visual_profile_registry(CONFIG_PATH)

    assert {profile.name for profile in registry.visual_profiles} == {
        "neutral",
        "photoreal",
        "corporate-3d",
        "children-cartoon",
    }


@pytest.mark.parametrize(
    "style_name",
    ["neutral", "photoreal", "corporate-3d", "children-cartoon"],
)
def test_visual_profiles_can_be_resolved(style_name):
    registry = load_visual_profile_registry(CONFIG_PATH)

    assert resolve_visual_style(registry, style_name).name == style_name


def test_unknown_profiles_rejected():
    registry = load_visual_profile_registry(CONFIG_PATH)

    with pytest.raises(VisualProfileError, match="Visual style.*not found"):
        resolve_visual_style(registry, "missing-style")

    with pytest.raises(VisualProfileError, match="Delivery profile.*not found"):
        resolve_delivery_profile(registry, "missing-delivery")


def test_photoreal_negative_does_not_leak_to_other_styles():
    registry = load_visual_profile_registry(CONFIG_PATH)
    photoreal = resolve_visual_style(registry, "photoreal")

    for style_name in ("corporate-3d", "children-cartoon"):
        other = resolve_visual_style(registry, style_name)
        for term in ("cgi", "plastic", "waxy surface", "illustration", "cartoon"):
            assert term in photoreal.reference_negative_prompt_suffix
            assert term not in other.reference_negative_prompt_suffix


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (_config(delivery_profiles=[]), "delivery_profiles must not be empty"),
        (_config(visual_profiles=[]), "visual_profiles must not be empty"),
        (
            _config(delivery_profiles=[_delivery(), _delivery()]),
            "Duplicate delivery profile name",
        ),
        (
            _config(visual_profiles=[_visual(), _visual()]),
            "Duplicate visual profile name",
        ),
        (
            _config(default_delivery_profile="missing"),
            "Default delivery profile.*not found",
        ),
        (
            _config(default_visual_style="missing"),
            "Default visual style.*not found",
        ),
        (
            _config(delivery_profiles=[_delivery(aspect_ratio="16:9")]),
            "does not match",
        ),
        (
            _config(
                delivery_profiles=[
                    _delivery(
                        name="shorts-1080p60",
                        width=1080,
                        height=1920,
                    )
                ],
                default_delivery_profile="shorts-1080p60",
            ),
            "must be 720x1280",
        ),
        (
            _config(delivery_profiles=[_delivery(orientation="landscape")]),
            "orientation must be 'portrait'",
        ),
        (
            _config(visual_profiles=[_visual(description="   ")]),
            "must be a non-empty string",
        ),
        (
            _config(
                delivery_profiles=[
                    _delivery(interpolation_method="frame-duplication")
                ]
            ),
            "interpolation_method",
        ),
        (
            _config(delivery_profiles=[_delivery(pixel_format="yuv444p")]),
            "pixel_format",
        ),
        (
            _config(delivery_profiles=[_delivery(crf=52)]),
            "crf must be an integer",
        ),
        (
            _config(delivery_profiles=[_delivery(audio_sample_rate=0)]),
            "audio_sample_rate must be a positive integer",
        ),
    ],
)
def test_invalid_configuration_rejected(tmp_path, config, message):
    with pytest.raises(VisualProfileError, match=message):
        load_visual_profile_registry(_write_config(tmp_path, config))
