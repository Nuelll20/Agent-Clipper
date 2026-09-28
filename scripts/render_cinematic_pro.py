from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


OUTPUT_WIDTH = 720
OUTPUT_HEIGHT = 1280


def run_process(
    command: list[str],
    cwd: Path | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def require_program(name: str) -> str:
    resolved = shutil.which(name)

    if not resolved:
        raise RuntimeError(
            f"{name} tidak ditemukan di PATH. "
            "Pastikan FFmpeg portable sudah aktif di sesi PowerShell."
        )

    return resolved


def probe_duration(ffprobe: str, video_path: Path) -> float:
    result = run_process([
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ])

    if result.returncode != 0:
        raise RuntimeError(
            "Gagal membaca durasi video:\n"
            + (result.stderr or result.stdout or "unknown error")
        )

    return float((result.stdout or "0").strip())


def has_audio_stream(ffprobe: str, video_path: Path) -> bool:
    result = run_process([
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(video_path),
    ])
    return result.returncode == 0 and bool((result.stdout or "").strip())


def supports_nvenc(ffmpeg: str) -> bool:
    result = run_process([ffmpeg, "-hide_banner", "-encoders"])
    text = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0 and "h264_nvenc" in text


def motion_expression(motions: list[dict[str, Any]]) -> str:
    terms = []

    for motion in motions:
        if motion.get("type") != "soft_punch":
            continue

        start = max(0.0, float(motion.get("start", 0.0)))
        end = max(start + 0.25, float(motion.get("end", start + 1.0)))
        amount = max(0.0, min(0.08, float(motion.get("amount", 0.03))))
        span = end - start

        # A smooth pulse avoids a visible jump when the punch-in ends.
        terms.append(
            f"{amount:.5f}*between(t,{start:.3f},{end:.3f})"
            f"*pow(sin(PI*(t-{start:.3f})/{span:.3f}),2)"
        )

    if not terms:
        return "1.0"

    return "1.0+" + "+".join(terms)


def filter_filename(name: str) -> str:
    return name.replace("\\", r"\\").replace("'", r"\'").replace(":", r"\:")


def build_video_filter(
    subtitle_name: str,
    edit_plan: dict[str, Any],
    duration: float,
) -> str:
    zoom = motion_expression(edit_plan.get("video_motion") or [])
    transition = edit_plan.get("transition") or {}
    fade_in = max(0.0, float(transition.get("fade_in_seconds", 0.12)))
    fade_out = max(0.0, float(transition.get("fade_out_seconds", 0.18)))
    fade_out_start = max(0.0, duration - fade_out)
    subtitle_name = filter_filename(subtitle_name)
    subtitle_filter = f"subtitles=filename='{subtitle_name}'"
    fonts_dir = str(edit_plan.get("fonts_dir") or "").strip()
    if fonts_dir:
        subtitle_filter += f":fontsdir='{filter_filename(fonts_dir)}'"

    cf = edit_plan.get("cinematic_focus") or {}
    cf_mode = str(cf.get("mode") or "off")
    edge_w = int(cf.get("edge_width") or 0)
    edge_op = max(0.0, min(0.72, float(cf.get("edge_opacity") or 0.0)))
    vignette = bool(cf.get("vignette", False))
    contrast = float(cf.get("contrast") or 1.0)
    saturation = float(cf.get("saturation") or 1.0)
    brightness = float(cf.get("brightness") or 0.0)
    gradient_bands = max(8, min(16, int(cf.get("gradient_bands") or 12)))

    filters = [
        (
            f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
            "force_original_aspect_ratio=increase"
        ),
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}",
        (
            f"scale=w='trunc({OUTPUT_WIDTH}*({zoom})/2)*2':"
            f"h='trunc({OUTPUT_HEIGHT}*({zoom})/2)*2':eval=frame"
        ),
        (
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
            "x='(iw-ow)/2':y='(ih-oh)/2'"
        ),
    ]

    # Cinematic color grade: contrast, saturation, brightness lift
    if cf_mode != "off":
        eq_parts = []
        if contrast != 1.0:
            eq_parts.append(f"contrast={contrast:.4f}")
        if saturation != 1.0:
            eq_parts.append(f"saturation={saturation:.4f}")
        if brightness != 0.0:
            eq_parts.append(f"brightness={brightness:.4f}")
        if eq_parts:
            filters.append("eq=" + ":".join(eq_parts))

    if fade_in > 0:
        filters.append(f"fade=t=in:st=0:d={fade_in:.3f}")

    if fade_out > 0 and duration > fade_out:
        filters.append(
            f"fade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}"
        )

    # Vignette/matte: build gradient bars on left/right edges
    # Applied BEFORE subtitles so subtitle text stays readable
    if vignette and edge_w > 0:
        # Build a luminance gradient mask via geq for both edges
        w = OUTPUT_WIDTH
        h = OUTPUT_HEIGHT
        step = max(1, edge_w // gradient_bands)
        # Use drawbox overlays to approximate gradient bars
        # Left bar
        for band in range(gradient_bands):
            bw = max(1, int(edge_w * (band + 1) / gradient_bands) - int(edge_w * band / gradient_bands))
            bx = int(edge_w * band / gradient_bands)
            alpha = edge_op * (1.0 - (band / max(1, gradient_bands - 1)))
            alpha_hex = int(round(alpha * 255))
            filters.append(
                f"drawbox=x={bx}:y=0:w={bw}:h={h}:color=black@{alpha:.4f}:t=fill"
            )
            # Right bar (mirrored)
            rbx = w - int(edge_w * (band + 1) / gradient_bands)
            filters.append(
                f"drawbox=x={rbx}:y=0:w={bw}:h={h}:color=black@{alpha:.4f}:t=fill"
            )

    filters.extend([
        subtitle_filter,
        "format=yuv420p",
    ])
    return ",".join(filters)


def video_codec_arguments(use_nvenc: bool) -> list[str]:
    if use_nvenc:
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-rc",
            "vbr",
            "-cq",
            "20",
            "-b:v",
            "0",
        ]

    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "19",
    ]


def build_command(
    ffmpeg: str,
    video_path: Path,
    output_path: Path,
    video_filter: str,
    audio_exists: bool,
    edit_plan: dict[str, Any],
    use_nvenc: bool,
) -> list[str]:
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        video_filter,
    ]
    command.extend(video_codec_arguments(use_nvenc))

    if audio_exists:
        audio = edit_plan.get("audio") or {}
        target_lufs = float(audio.get("target_lufs", -16))
        true_peak = float(audio.get("true_peak_db", -1.5))
        command.extend([
            "-af",
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ])

    command.extend([
        "-movflags",
        "+faststart",
        str(output_path),
    ])
    return command


def render_video(
    video_path: Path,
    subtitle_path: Path,
    edit_plan_path: Path,
    output_path: Path,
) -> None:
    ffmpeg = require_program("ffmpeg")
    ffprobe = require_program("ffprobe")
    edit_plan = json.loads(
        edit_plan_path.read_text(encoding="utf-8-sig")
    )
    duration = probe_duration(ffprobe, video_path)
    audio_exists = has_audio_stream(ffprobe, video_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    video_filter = build_video_filter(
        subtitle_path.name,
        edit_plan,
        duration,
    )
    use_nvenc = supports_nvenc(ffmpeg)
    command = build_command(
        ffmpeg,
        video_path,
        output_path,
        video_filter,
        audio_exists,
        edit_plan,
        use_nvenc,
    )

    print("Merender video cinematic...")
    print(f"Encoder          : {'NVIDIA NVENC' if use_nvenc else 'CPU libx264'}")
    print(f"Normalisasi audio: {'aktif' if audio_exists else 'tidak ada audio'}")
    result = run_process(command, cwd=subtitle_path.parent)

    if result.returncode != 0 and use_nvenc:
        print("NVENC gagal digunakan. Mengulang dengan encoder CPU...")
        command = build_command(
            ffmpeg,
            video_path,
            output_path,
            video_filter,
            audio_exists,
            edit_plan,
            False,
        )
        result = run_process(command, cwd=subtitle_path.parent)

    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout or "unknown error")[-5000:]
        raise RuntimeError("FFmpeg gagal merender video:\n" + diagnostic)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg selesai tanpa menghasilkan video yang valid.")

    print("Video cinematic berhasil dibuat.")
    print(f"Output: {output_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Menerapkan motion lembut, transisi, normalisasi audio, "
            "dan subtitle ASS ke video vertikal."
        )
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("subtitle", type=Path)
    parser.add_argument("edit_plan", type=Path)
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    render_video(
        args.video.resolve(),
        args.subtitle.resolve(),
        args.edit_plan.resolve(),
        args.output.resolve(),
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
