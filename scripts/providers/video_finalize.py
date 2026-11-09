#!/usr/bin/env python3
"""Finalize a generated video for the Hermes Shorts delivery contract."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Sequence


class VideoFinalizeError(RuntimeError):
    """A source video cannot be finalized safely."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class VideoProbe:
    width: int
    height: int
    codec_name: str
    frame_rate: float
    duration: float
    nb_frames: int | None
    size_bytes: int
    has_audio: bool
    audio_codec: str | None
    audio_sample_rate: int | None
    audio_channels: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FinalizationSettings:
    width: int = 720
    height: int = 1280
    target_fps: int = 60
    interpolation_method: str = "ffmpeg-minterpolate"
    pixel_format: str = "yuv420p"
    crf: int = 18
    preset: str = "slow"
    audio_bitrate: str = "192k"
    audio_sample_rate: int = 48000
    duration_tolerance: float = 0.10

    def __post_init__(self) -> None:
        if self.width != 720 or self.height != 1280:
            raise VideoFinalizeError(
                "Final delivery resolution must be 720x1280."
            )
        if self.target_fps != 60:
            raise VideoFinalizeError("Final delivery frame rate must be 60 FPS.")
        if self.interpolation_method != "ffmpeg-minterpolate":
            raise VideoFinalizeError(
                "Only ffmpeg-minterpolate is supported by this finalizer."
            )
        if self.pixel_format != "yuv420p":
            raise VideoFinalizeError("Final delivery pixel format must be yuv420p.")
        if not 0 <= self.crf <= 51:
            raise VideoFinalizeError("CRF must be between 0 and 51.")
        if not self.preset.strip() or not self.audio_bitrate.strip():
            raise VideoFinalizeError("Preset and audio bitrate cannot be empty.")
        if self.audio_sample_rate <= 0 or self.duration_tolerance < 0:
            raise VideoFinalizeError(
                "Audio sample rate must be positive and duration tolerance non-negative."
            )


def _runner(runner: Runner | None) -> Runner:
    return runner or subprocess.run


def _run_command(
    command: Sequence[str],
    *,
    runner: Runner | None = None,
    purpose: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = _runner(runner)(
            list(command),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise VideoFinalizeError(f"Cannot run {purpose}: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise VideoFinalizeError(f"{purpose} failed: {detail}")

    return result


def _parse_rate(value: Any) -> float:
    if value in (None, "", "0/0", "N/A"):
        return 0.0

    try:
        rate = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError) as exc:
        raise VideoFinalizeError(f"Invalid frame rate from ffprobe: {value!r}.") from exc

    return rate


def _parse_duration(format_data: dict[str, Any], video: dict[str, Any]) -> float:
    raw = format_data.get("duration", video.get("duration"))
    try:
        duration = float(raw)
    except (TypeError, ValueError) as exc:
        raise VideoFinalizeError(f"Invalid video duration from ffprobe: {raw!r}.") from exc

    if duration <= 0:
        raise VideoFinalizeError(f"Video duration must be positive, got {duration}.")

    return duration


def probe_video(
    path: str | Path,
    *,
    ffprobe_bin: str = "ffprobe",
    runner: Runner | None = None,
) -> VideoProbe:
    """Read technical video metadata through ffprobe."""

    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise VideoFinalizeError(f"Video input does not exist or is empty: {path}.")

    result = _run_command(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,size:"
                "stream=codec_name,codec_type,width,height,"
                "r_frame_rate,avg_frame_rate,nb_frames,duration,"
                "sample_rate,channels"
            ),
            "-of",
            "json",
            str(path),
        ],
        runner=runner,
        purpose="ffprobe",
    )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VideoFinalizeError("ffprobe returned invalid JSON.") from exc

    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise VideoFinalizeError("ffprobe response does not contain a streams list.")

    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise VideoFinalizeError("Video input has no video stream.")

    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )
    frame_rate = _parse_rate(
        video.get("avg_frame_rate") or video.get("r_frame_rate")
    )
    if frame_rate <= 0:
        frame_rate = _parse_rate(video.get("r_frame_rate"))
    if frame_rate <= 0:
        raise VideoFinalizeError("Video frame rate must be positive.")

    try:
        width = int(video["width"])
        height = int(video["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VideoFinalizeError("ffprobe returned invalid video dimensions.") from exc

    raw_frames = video.get("nb_frames")
    try:
        nb_frames = None if raw_frames in (None, "", "N/A") else int(raw_frames)
    except (TypeError, ValueError) as exc:
        raise VideoFinalizeError(f"Invalid frame count: {raw_frames!r}.") from exc

    sample_rate: int | None = None
    channels: int | None = None
    if audio is not None:
        try:
            if audio.get("sample_rate") not in (None, "", "N/A"):
                sample_rate = int(audio["sample_rate"])
            if audio.get("channels") not in (None, "", "N/A"):
                channels = int(audio["channels"])
        except (TypeError, ValueError) as exc:
            raise VideoFinalizeError("ffprobe returned invalid audio metadata.") from exc

    return VideoProbe(
        width=width,
        height=height,
        codec_name=str(video.get("codec_name") or ""),
        frame_rate=frame_rate,
        duration=_parse_duration(payload.get("format") or {}, video),
        nb_frames=nb_frames,
        size_bytes=path.stat().st_size,
        has_audio=audio is not None,
        audio_codec=(str(audio.get("codec_name") or "") if audio else None),
        audio_sample_rate=sample_rate,
        audio_channels=channels,
    )


def healthcheck(
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Verify the required local FFmpeg capabilities."""

    filters = _run_command(
        [ffmpeg_bin, "-hide_banner", "-filters"],
        runner=runner,
        purpose="FFmpeg filter discovery",
    )
    filter_text = (filters.stdout or "") + (filters.stderr or "")
    if "minterpolate" not in filter_text:
        raise VideoFinalizeError("FFmpeg filter minterpolate is unavailable.")

    encoders = _run_command(
        [ffmpeg_bin, "-hide_banner", "-encoders"],
        runner=runner,
        purpose="FFmpeg encoder discovery",
    )
    encoder_text = (encoders.stdout or "") + (encoders.stderr or "")
    if "libx264" not in encoder_text:
        raise VideoFinalizeError("FFmpeg encoder libx264 is unavailable.")

    _run_command(
        [ffprobe_bin, "-version"],
        runner=runner,
        purpose="ffprobe discovery",
    )

    return {
        "ready": True,
        "ffmpeg": ffmpeg_bin,
        "ffprobe": ffprobe_bin,
        "interpolation_method": "ffmpeg-minterpolate",
        "video_encoder": "libx264",
    }


def _validate_final_probe(
    source: VideoProbe,
    final: VideoProbe,
    settings: FinalizationSettings,
) -> float:
    if final.width != settings.width or final.height != settings.height:
        raise VideoFinalizeError(
            "Final video resolution is invalid: "
            f"{final.width}x{final.height}; expected "
            f"{settings.width}x{settings.height}."
        )
    if abs(final.frame_rate - settings.target_fps) > 0.01:
        raise VideoFinalizeError(
            f"Final video frame rate is {final.frame_rate}; "
            f"expected {settings.target_fps}."
        )
    if final.codec_name != "h264":
        raise VideoFinalizeError(
            f"Final video codec is {final.codec_name!r}; expected 'h264'."
        )

    duration_delta = abs(final.duration - source.duration)
    if duration_delta > settings.duration_tolerance:
        raise VideoFinalizeError(
            "Final video duration changed by "
            f"{duration_delta:.6f}s; allowed "
            f"{settings.duration_tolerance:.6f}s."
        )
    if source.has_audio and not final.has_audio:
        raise VideoFinalizeError("Final video lost the source audio stream.")

    return duration_delta


def finalize_video(
    input_path: str | Path,
    output_path: str | Path,
    *,
    settings: FinalizationSettings | None = None,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Interpolate, encode, validate, and atomically publish one video."""

    settings = settings or FinalizationSettings()
    source_path = Path(input_path).expanduser().resolve()
    final_path = Path(output_path).expanduser().resolve()

    if source_path == final_path:
        raise VideoFinalizeError("Input and output video paths must be different.")
    if not source_path.is_file() or source_path.stat().st_size <= 0:
        raise VideoFinalizeError(
            f"Video input does not exist or is empty: {source_path}."
        )
    if final_path.exists():
        raise VideoFinalizeError(f"Final video output already exists: {final_path}.")

    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = final_path.parent / (
        f".{final_path.stem}.{uuid.uuid4().hex}.tmp{final_path.suffix}"
    )
    source_probe = probe_video(
        source_path,
        ffprobe_bin=ffprobe_bin,
        runner=runner,
    )
    video_filter = (
        f"scale={settings.width}:{settings.height}:"
        "force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={settings.width}:{settings.height},"
        "tpad=stop_mode=clone:stop_duration=0.1,"
        f"minterpolate=fps={settings.target_fps}:"
        "mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,"
        f"trim=duration={source_probe.duration:.9f},"
        "setpts=PTS-STARTPTS"
    )

    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        video_filter,
        "-c:v",
        "libx264",
        "-preset",
        settings.preset,
        "-crf",
        str(settings.crf),
        "-pix_fmt",
        settings.pixel_format,
        "-fps_mode",
        "cfr",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        settings.audio_bitrate,
        "-ar",
        str(settings.audio_sample_rate),
        str(temporary_path),
    ]

    try:
        _run_command(
            command,
            runner=runner,
            purpose="FFmpeg video finalization",
        )
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise VideoFinalizeError("FFmpeg did not create a valid temporary output.")

        final_probe = probe_video(
            temporary_path,
            ffprobe_bin=ffprobe_bin,
            runner=runner,
        )
        duration_delta = _validate_final_probe(
            source_probe,
            final_probe,
            settings,
        )
        size_bytes = temporary_path.stat().st_size
        os.replace(temporary_path, final_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return {
        "input": str(source_path),
        "output": str(final_path),
        "interpolation_method": settings.interpolation_method,
        "source_probe": source_probe.to_dict(),
        "final_probe": final_probe.to_dict(),
        "duration_delta": duration_delta,
        "size_bytes": size_bytes,
    }


class VideoFinalizer:
    """Injectable finalizer used by the automatic video agent."""

    def __init__(
        self,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        *,
        runner: Runner | None = None,
    ) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin
        self.runner = runner

    def healthcheck(self) -> dict[str, Any]:
        return healthcheck(
            ffmpeg_bin=self.ffmpeg_bin,
            ffprobe_bin=self.ffprobe_bin,
            runner=self.runner,
        )

    def finalize(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        settings: FinalizationSettings,
    ) -> dict[str, Any]:
        return finalize_video(
            input_path,
            output_path,
            settings=settings,
            ffmpeg_bin=self.ffmpeg_bin,
            ffprobe_bin=self.ffprobe_bin,
            runner=self.runner,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Finalize a video as a 720x1280 60 FPS Shorts MP4."
    )
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    parser.add_argument("--health-check", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        if args.health_check:
            result = healthcheck(
                ffmpeg_bin=args.ffmpeg_bin,
                ffprobe_bin=args.ffprobe_bin,
            )
        else:
            if not args.input or not args.output:
                raise VideoFinalizeError(
                    "--input and --output are required unless --health-check is used."
                )
            result = finalize_video(
                args.input,
                args.output,
                ffmpeg_bin=args.ffmpeg_bin,
                ffprobe_bin=args.ffprobe_bin,
            )

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except VideoFinalizeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nVideo finalization stopped by operator.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
