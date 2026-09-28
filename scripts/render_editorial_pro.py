#!/usr/bin/env python3
"""Render cinematic captions with selective, low-volume editorial SFX."""

from __future__ import annotations

import argparse
import json
import math
import wave
from pathlib import Path
from typing import Any

import numpy as np

import render_cinematic_pro as base


SAMPLE_RATE = 48000
SFX_NAMES = {"pop", "impact", "whoosh", "click"}


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_sfx_root() -> Path:
    return project_root() / "assets" / "sfx"


def normalize_audio(samples: np.ndarray, peak: float = 0.76) -> np.ndarray:
    maximum = float(np.max(np.abs(samples))) if samples.size else 0.0
    if maximum > 0:
        samples = samples * (peak / maximum)
    return np.clip(samples, -1.0, 1.0)


def synthesize_sfx(name: str) -> np.ndarray:
    rng = np.random.default_rng(20260921 + sum(ord(char) for char in name))
    if name == "pop":
        duration = 0.19
        time = np.arange(int(SAMPLE_RATE * duration)) / SAMPLE_RATE
        phase = 2 * np.pi * (210 * time + 0.5 * (640 / duration) * np.square(time))
        samples = np.sin(phase) * np.exp(-16 * time)
        samples += 0.16 * rng.standard_normal(time.size) * np.exp(-30 * time)
        return normalize_audio(samples.astype(np.float32), 0.68)
    if name == "impact":
        duration = 0.42
        time = np.arange(int(SAMPLE_RATE * duration)) / SAMPLE_RATE
        low = np.sin(2 * np.pi * (82 - 32 * time / duration) * time) * np.exp(-8.5 * time)
        noise = rng.standard_normal(time.size)
        kernel = np.ones(55, dtype=np.float32) / 55.0
        low_noise = np.convolve(noise, kernel, mode="same") * np.exp(-11 * time)
        return normalize_audio((low + 0.65 * low_noise).astype(np.float32), 0.74)
    if name == "whoosh":
        duration = 0.62
        time = np.arange(int(SAMPLE_RATE * duration)) / SAMPLE_RATE
        noise = rng.standard_normal(time.size).astype(np.float32)
        smoothed = np.convolve(noise, np.ones(18, dtype=np.float32) / 18.0, mode="same")
        airy = noise - smoothed * 0.58
        envelope = np.power(np.sin(np.pi * np.clip(time / duration, 0, 1)), 1.7)
        tremolo = 0.82 + 0.18 * np.sin(2 * np.pi * 7 * time)
        return normalize_audio(airy * envelope * tremolo, 0.56)
    duration = 0.085
    time = np.arange(int(SAMPLE_RATE * duration)) / SAMPLE_RATE
    samples = (
        0.74 * np.sin(2 * np.pi * 1350 * time)
        + 0.26 * rng.standard_normal(time.size)
    ) * np.exp(-48 * time)
    return normalize_audio(samples.astype(np.float32), 0.62)


def write_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (normalize_audio(samples) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())


def ensure_sfx_pack(root: Path) -> dict[str, Path]:
    assets: dict[str, Path] = {}
    for name in sorted(SFX_NAMES):
        path = root / f"{name}.wav"
        if not path.is_file() or path.stat().st_size == 0:
            write_wav(path, synthesize_sfx(name))
        assets[name] = path
    return assets


def valid_sfx_events(plan: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    for item in plan.get("events") or []:
        name = str(item.get("sfx") or "").casefold()
        if name not in SFX_NAMES:
            continue
        start = max(0.0, float(item.get("start", 0.0)))
        gain = max(-32.0, min(-10.0, float(item.get("sfx_gain_db", -18.0))))
        events.append({"name": name, "start": start, "gain_db": gain})
        if len(events) >= 6:
            break
    return events


def merge_sfx_events(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep semantic and visual accents without stacking sounds on one beat."""
    merged: list[dict[str, Any]] = []
    for event in sorted(
        (item for group in groups for item in group),
        key=lambda item: float(item["start"]),
    ):
        if any(abs(float(event["start"]) - float(item["start"])) < 0.18 for item in merged):
            continue
        merged.append(event)
        if len(merged) >= 8:
            break
    return merged


def build_filter_complex(
    video_filter: str,
    edit_plan: dict[str, Any],
    events: list[dict[str, Any]],
) -> str:
    filters = [f"[0:v]{video_filter}[vout]", "[0:a]volume=1.0[voice]"]
    mix_labels = ["[voice]"]
    for index, event in enumerate(events, start=1):
        delay_ms = round(float(event["start"]) * 1000)
        gain = float(event["gain_db"])
        label = f"sfx{index}"
        filters.append(
            f"[{index}:a]asetpts=PTS-STARTPTS,volume={gain:.1f}dB,"
            f"adelay={delay_ms}:all=1[{label}]"
        )
        mix_labels.append(f"[{label}]")

    audio = edit_plan.get("audio") or {}
    target_lufs = float(audio.get("target_lufs", -16))
    true_peak = float(audio.get("true_peak_db", -1.5))
    filters.append(
        "".join(mix_labels)
        + f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0,"
        + f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11[aout]"
    )
    return ";".join(filters)


def build_command(
    ffmpeg: str,
    video: Path,
    subtitle: Path,
    output: Path,
    edit_plan: dict[str, Any],
    events: list[dict[str, Any]],
    assets: dict[str, Path],
    duration: float,
    use_nvenc: bool,
) -> list[str]:
    video_filter = base.build_video_filter(subtitle.name, edit_plan, duration)
    command = [ffmpeg, "-y", "-hide_banner", "-i", str(video)]
    for event in events:
        command.extend(["-i", str(assets[event["name"]])])
    command.extend(
        [
            "-filter_complex",
            build_filter_complex(video_filter, edit_plan, events),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
        ]
    )
    command.extend(base.video_codec_arguments(use_nvenc))
    command.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return command


def render(
    video: Path,
    subtitle: Path,
    edit_plan_path: Path,
    editorial_plan_path: Path,
    output: Path,
    sfx_root: Path,
    visual_plan_path: Path | None = None,
) -> None:
    edit_plan = json.loads(edit_plan_path.read_text(encoding="utf-8-sig"))
    editorial_plan = json.loads(editorial_plan_path.read_text(encoding="utf-8-sig"))
    editorial_events = valid_sfx_events(editorial_plan)
    visual_events: list[dict[str, Any]] = []
    if visual_plan_path is not None and visual_plan_path.is_file():
        visual_plan = json.loads(visual_plan_path.read_text(encoding="utf-8-sig"))
        visual_events = valid_sfx_events(visual_plan)
    events = merge_sfx_events(editorial_events, visual_events)
    ffmpeg = base.require_program("ffmpeg")
    ffprobe = base.require_program("ffprobe")
    duration = base.probe_duration(ffprobe, video)
    audio_exists = base.has_audio_stream(ffprobe, video)
    output.parent.mkdir(parents=True, exist_ok=True)

    if not events or not audio_exists:
        base.render_video(video, subtitle, edit_plan_path, output)
        return

    assets = ensure_sfx_pack(sfx_root)
    use_nvenc = base.supports_nvenc(ffmpeg)
    command = build_command(
        ffmpeg,
        video,
        subtitle,
        output,
        edit_plan,
        events,
        assets,
        duration,
        use_nvenc,
    )
    print("Merender editorial motion dan SFX...")
    print(f"Motion/SFX event : {len(events)}")
    print(f"Encoder          : {'NVIDIA NVENC' if use_nvenc else 'CPU libx264'}")
    result = base.run_process(command, cwd=subtitle.parent)
    if result.returncode != 0 and use_nvenc:
        print("NVENC gagal digunakan. Mengulang dengan encoder CPU...")
        command = build_command(
            ffmpeg,
            video,
            subtitle,
            output,
            edit_plan,
            events,
            assets,
            duration,
            False,
        )
        result = base.run_process(command, cwd=subtitle.parent)
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout or "unknown error")[-5000:]
        raise RuntimeError("FFmpeg gagal merender editorial video:\n" + diagnostic)
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("Render selesai tanpa video output yang valid.")
    print("Video editorial profesional berhasil dibuat.")
    print(f"Output: {output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render motion text dan SFX editorial secara selektif."
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("subtitle", type=Path)
    parser.add_argument("edit_plan", type=Path)
    parser.add_argument("editorial_plan", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sfx-root", type=Path, default=default_sfx_root())
    parser.add_argument("--visual-plan", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    render(
        args.video.resolve(),
        args.subtitle.resolve(),
        args.edit_plan.resolve(),
        args.editorial_plan.resolve(),
        args.output.resolve(),
        args.sfx_root.resolve(),
        args.visual_plan.resolve() if args.visual_plan else None,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
