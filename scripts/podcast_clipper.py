#!/usr/bin/env python3
"""Orchestrate link ingestion, highlight rendering, and Telegram review.

Designed for the Windows Hermes video-agent project. The script delegates
transcription, cinematic subtitles, final rendering, and Telegram approval to
the already-tested project scripts instead of duplicating their logic.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.event_store import EventStore
from core.observation import RunObserver


SCHEMA_VERSION = 1
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}


class WorkflowError(RuntimeError):
    """Expected workflow or configuration failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def scripts_root() -> Path:
    return Path(__file__).resolve().parent


def default_event_db() -> Path:
    configured = os.environ.get("HERMES_EVENT_DB", "").strip()
    return Path(configured).expanduser() if configured else project_root() / "state" / "production-events.db"


def observe(args: argparse.Namespace, stage: str, message: str) -> None:
    observer = getattr(args, "observer", None)
    if observer is not None:
        observer.emit(stage, message)


def default_jobs_root() -> Path:
    configured = os.environ.get("HERMES_VIDEO_JOBS", "").strip()
    return Path(configured).expanduser() if configured else project_root() / "jobs"


def default_motioncraft_renderer() -> Path:
    configured = os.environ.get("HERMES_MOTIONCRAFT_RENDERER", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else project_root() / "motioncraft-renderer"
    )


def listener_state_path() -> Path:
    return project_root() / "state" / "telegram-listener.json"


def listener_log_path() -> Path:
    return project_root() / "logs" / "telegram-listener.log"


def safe_job_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.")
    if not cleaned or cleaned in {".", ".."}:
        raise WorkflowError("Job ID tidak valid.")
    return cleaned[:80]


def generated_job_id() -> str:
    return dt.datetime.now().strftime("job-%Y%m%d-%H%M%S")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path) -> Any:
    if not path.is_file():
        raise WorkflowError(f"File tidak ditemukan: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"JSON tidak valid pada {path.name}: {exc}") from None


def command_text(command: Iterable[str]) -> str:
    return " ".join(str(item) for item in command)


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if capture:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    else:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            check=False,
        )
    if check and result.returncode != 0:
        detail = ""
        if capture and result.stdout:
            detail = "\n" + result.stdout[-3000:]
        raise WorkflowError(
            f"Perintah gagal dengan exit code {result.returncode}: "
            f"{command_text(command)}{detail}"
        )
    return result


def require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise WorkflowError(f"{label} tidak ditemukan: {path}")
    return path


def resolve_plan_file(value: Any, plan_path: Path, label: str) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        path = plan_path.parent / path
    return require_file(path.resolve(), label)


def require_program(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise WorkflowError(f"Program tidak ditemukan di PATH: {name}")
    return resolved


def ffprobe(path: Path) -> dict[str, Any]:
    executable = require_program("ffprobe")
    result = run_command(
        [
            executable,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture=True,
    )
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise WorkflowError(f"Tidak ada video stream pada {path.name}")
    stream = streams[0]
    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "duration": duration,
    }


def find_downloaded_source(job_dir: Path) -> Path:
    candidates = [
        path
        for path in job_dir.glob("source.*")
        if path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS
    ]
    if not candidates:
        raise WorkflowError("Video hasil unduhan tidak ditemukan.")
    candidates.sort(key=lambda item: item.stat().st_size, reverse=True)
    return candidates[0]


def load_source_metadata(job_dir: Path) -> dict[str, Any]:
    info_files = list(job_dir.glob("source*.info.json"))
    if not info_files:
        return {}
    payload = read_json(info_files[0])
    return payload if isinstance(payload, dict) else {}


def create_plan_template(
    job_id: str,
    url: str,
    source: Path,
    transcript: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "source_url": url,
        "source_path": str(source.resolve()),
        "transcript_path": str(transcript.resolve()),
        "source_title": metadata.get("title") or source.stem,
        "clips": [],
        "planning_notes": (
            "Isi 3-5 clip. Pilih batas kalimat yang utuh, durasi 20-60 detik, "
            "hook akurat 3-8 kata, headline open-loop 3-5 detik dengan nama tokoh "
            "yang terverifikasi, cinematic focus, caption natural, dan 3-5 hashtag relevan."
        ),
    }


def command_ingest(args: argparse.Namespace) -> int:
    job_id = safe_job_id(args.job or generated_job_id())
    job_dir = Path(args.jobs_root).expanduser().resolve() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    python = sys.executable
    transcript_script = require_file(scripts_root() / "transcribe_pro.py", "Transcriber")
    transcript_path = job_dir / "transcript-full.json"
    output_template = job_dir / "source.%(ext)s"

    existing: Path | None = None
    try:
        existing = find_downloaded_source(job_dir)
    except WorkflowError:
        pass

    if existing and not args.force_download:
        observe(args, "source_reused", "Menggunakan video sumber yang tersedia")
        source = existing
        print(f"Menggunakan source yang sudah ada: {source.name}")
    else:
        print("Mengunduh video sumber...")
        observe(args, "downloading", "Mengunduh video sumber")
        download_command = [
            python,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--newline",
            "--write-info-json",
            "--merge-output-format",
            "mp4",
            "--output",
            str(output_template),
            "--format",
            "bv*+ba/b",
        ]
        if args.force_download:
            download_command.append("--force-overwrites")
        download_command.append(args.url)
        run_command(download_command, cwd=job_dir)
        source = find_downloaded_source(job_dir)

    observe(args, "probing", "Memeriksa video sumber")
    media = ffprobe(source)
    if media["duration"] <= 0:
        raise WorkflowError("Durasi video sumber tidak dapat dibaca.")
    if media["duration"] > args.max_duration_hours * 3600:
        raise WorkflowError(
            f"Durasi sumber {media['duration'] / 3600:.2f} jam melebihi batas "
            f"{args.max_duration_hours:.2f} jam."
        )

    if transcript_path.is_file() and not args.force_transcribe:
        observe(args, "transcript_reused", "Menggunakan transkrip yang tersedia")
        print("Menggunakan transkrip yang sudah ada.")
    else:
        print("Mentranskripsikan sumber dengan word timestamps...")
        observe(args, "transcribing", "Mentranskripsikan video sumber")
        run_command(
            [
                python,
                str(transcript_script),
                str(source),
                str(transcript_path),
                "--model",
                args.model,
                "--language",
                args.language,
            ],
            cwd=project_root(),
        )

    metadata = load_source_metadata(job_dir)
    observe(args, "saving_artifacts", "Menyimpan metadata dan rencana klip")
    job_payload = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "created_at": utc_now(),
        "source_url": args.url,
        "source_path": str(source.resolve()),
        "transcript_path": str(transcript_path.resolve()),
        "media": media,
        "title": metadata.get("title") or source.stem,
        "uploader": metadata.get("uploader") or metadata.get("channel"),
    }
    write_json(job_dir / "job.json", job_payload)

    plan_path = job_dir / "clip-plan.json"
    if not plan_path.exists() or args.force_plan:
        write_json(
            plan_path,
            create_plan_template(
                job_id,
                args.url,
                source,
                transcript_path,
                metadata,
            ),
        )

    print("\nINGEST SELESAI")
    print(f"Job ID          : {job_id}")
    print(f"Source          : {source}")
    print(f"Transcript      : {transcript_path}")
    print(f"Template rencana: {plan_path}")
    return 0


def parse_time(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise WorkflowError(f"{label} harus berupa angka detik.") from None
    if result < 0:
        raise WorkflowError(f"{label} tidak boleh negatif.")
    return result


def normalize_headline(raw: Any, hook: str, clip_id: str) -> dict[str, Any]:
    if raw is None:
        return {
            "kicker": "SIMAK SAMPAI AKHIR...",
            "text": hook,
            "highlight": "",
            "subject": "",
            "duration": 3.8,
            "open_loop": True,
            "source": "hook_fallback",
        }
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        raise WorkflowError(f"{clip_id}.headline harus berupa object atau teks.")

    kicker = " ".join(str(raw.get("kicker") or "SIMAK SAMPAI AKHIR...").split()).upper()
    text = " ".join(str(raw.get("text") or hook).split()).upper()
    subject = " ".join(str(raw.get("subject") or "").split()).upper()
    highlight = " ".join(str(raw.get("highlight") or subject).split()).upper()
    if not kicker or len(kicker) > 32 or len(kicker.split()) > 5:
        raise WorkflowError(
            f"{clip_id}.headline.kicker maksimal 5 kata atau 32 karakter."
        )
    if not text or len(text) > 96 or len(text.split()) > 16:
        raise WorkflowError(
            f"{clip_id}.headline.text maksimal 16 kata atau 96 karakter."
        )
    if subject and subject.casefold() not in text.casefold():
        raise WorkflowError(
            f"{clip_id}.headline.subject harus muncul persis di headline.text."
        )
    if highlight and highlight.casefold() not in text.casefold():
        raise WorkflowError(
            f"{clip_id}.headline.highlight harus muncul persis di headline.text."
        )
    try:
        duration = float(raw.get("duration", 3.8))
    except (TypeError, ValueError):
        raise WorkflowError(
            f"{clip_id}.headline.duration harus berupa angka detik."
        ) from None
    if not 3.0 <= duration <= 5.0:
        raise WorkflowError(f"{clip_id}.headline.duration harus 3,0-5,0 detik.")
    return {
        **raw,
        "kicker": kicker,
        "text": text,
        "highlight": highlight,
        "subject": subject,
        "duration": round(duration, 3),
        "open_loop": bool(raw.get("open_loop", True)),
        "source": str(raw.get("source") or "agent_plan"),
    }


def normalize_motioncraft(raw: Any, clip_id: str) -> dict[str, Any]:
    if raw is None:
        raw = {"mode": "auto"}
    elif isinstance(raw, bool):
        raw = {"mode": "on" if raw else "off"}
    elif isinstance(raw, str):
        raw = {"mode": raw}
    if not isinstance(raw, dict):
        raise WorkflowError(
            f"{clip_id}.motioncraft harus berupa auto/on/off atau object."
        )
    mode = str(raw.get("mode") or "auto").casefold()
    if mode not in {"auto", "on", "off"}:
        raise WorkflowError(
            f"{clip_id}.motioncraft.mode harus auto, on, atau off."
        )
    renderer_value = str(raw.get("renderer") or "").strip()
    return {
        **raw,
        "mode": mode,
        "renderer": renderer_value,
        "strict_transcript": bool(raw.get("strict_transcript", False)),
    }


def validate_plan(plan: Any, source_duration: float | None = None) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise WorkflowError("Root clip-plan.json harus berupa object.")
    job_id = safe_job_id(str(plan.get("job_id") or ""))
    clips = plan.get("clips")
    if not isinstance(clips, list) or not clips:
        raise WorkflowError("Rencana belum memiliki clips.")
    if len(clips) > 10:
        raise WorkflowError("Maksimal 10 klip dalam satu job.")

    seen: set[str] = set()
    normalized_clips: list[dict[str, Any]] = []
    for index, raw in enumerate(clips, start=1):
        if not isinstance(raw, dict):
            raise WorkflowError(f"Clip #{index} harus berupa object.")
        clip_id = safe_job_id(str(raw.get("id") or f"clip-{index:02d}"))
        if clip_id in seen:
            raise WorkflowError(f"ID klip duplikat: {clip_id}")
        seen.add(clip_id)
        start = parse_time(raw.get("start"), f"{clip_id}.start")
        end = parse_time(raw.get("end"), f"{clip_id}.end")
        duration = end - start
        if duration < 8:
            raise WorkflowError(f"{clip_id} terlalu pendek ({duration:.1f} detik).")
        if duration > 90:
            raise WorkflowError(f"{clip_id} terlalu panjang ({duration:.1f} detik).")
        if source_duration is not None and end > source_duration + 0.1:
            raise WorkflowError(f"{clip_id}.end melewati durasi sumber.")

        hook = " ".join(str(raw.get("hook") or "").split())
        if not hook:
            raise WorkflowError(f"{clip_id} belum memiliki hook.")
        if len(hook.split()) > 12:
            raise WorkflowError(f"Hook {clip_id} terlalu panjang; maksimal 12 kata.")

        headline = normalize_headline(raw.get("headline"), hook, clip_id)
        motioncraft = normalize_motioncraft(raw.get("motioncraft"), clip_id)
        cinematic_focus = str(raw.get("cinematic_focus") or "dramatic").casefold()
        if cinematic_focus not in {"off", "subtle", "dramatic"}:
            raise WorkflowError(
                f"{clip_id}.cinematic_focus harus off, subtle, atau dramatic."
            )
        cinematic_side = str(raw.get("cinematic_side") or "both").casefold()
        if cinematic_side not in {"auto", "both", "left", "right"}:
            raise WorkflowError(
                f"{clip_id}.cinematic_side harus auto, both, left, atau right."
            )

        title = " ".join(str(raw.get("title") or clip_id).split())
        caption = str(raw.get("caption") or "").strip()
        hashtags = raw.get("hashtags") or []
        if isinstance(hashtags, str):
            hashtags = hashtags.split()
        if not isinstance(hashtags, list):
            raise WorkflowError(f"{clip_id}.hashtags harus berupa daftar.")
        focus = raw.get("focus", "auto")
        normalized_clips.append(
            {
                **raw,
                "id": clip_id,
                "start": round(start, 3),
                "end": round(end, 3),
                "hook": hook,
                "title": title,
                "caption": caption,
                "hashtags": [str(item).strip() for item in hashtags if str(item).strip()],
                "focus": focus,
                "headline": headline,
                "motioncraft": motioncraft,
                "cinematic_focus": cinematic_focus,
                "cinematic_side": cinematic_side,
            }
        )
    return {**plan, "job_id": job_id, "clips": normalized_clips}


def clean_spoken_text(words: list[dict[str, Any]]) -> str:
    text = " ".join(str(word.get("word") or "").strip() for word in words).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text


def slice_transcript(
    full_transcript: dict[str, Any],
    start: float,
    end: float,
    hook: str,
    headline: dict[str, Any],
) -> dict[str, Any]:
    output_segments: list[dict[str, Any]] = []
    for segment in full_transcript.get("segments", []):
        segment_start = float(segment.get("start", 0.0))
        segment_end = float(segment.get("end", segment_start))
        if segment_end <= start or segment_start >= end:
            continue

        new_segment = dict(segment)
        words = []
        for word in segment.get("words") or []:
            word_start = float(word.get("start", segment_start))
            word_end = float(word.get("end", word_start))
            if word_end <= start or word_start >= end:
                continue
            new_word = dict(word)
            new_word["start"] = round(max(word_start, start) - start, 3)
            new_word["end"] = round(min(word_end, end) - start, 3)
            words.append(new_word)

        new_segment["start"] = round(max(segment_start, start) - start, 3)
        new_segment["end"] = round(min(segment_end, end) - start, 3)
        if words:
            new_segment["words"] = words
            spoken = clean_spoken_text(words)
            if spoken:
                new_segment["text"] = spoken
                new_segment["text_original"] = spoken
                new_segment["text_corrected"] = spoken
        output_segments.append(new_segment)

    if not output_segments:
        raise WorkflowError("Tidak ada transkrip pada rentang klip yang dipilih.")
    return {
        "language": full_transcript.get("language", "id"),
        "duration": round(end - start, 3),
        "hook_text": hook,
        "headline": headline,
        "segments": output_segments,
    }


def numeric_focus(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return min(1.0, max(0.0, float(value)))
    text = str(value).strip().casefold()
    mapping = {
        "left": 0.25,
        "kiri": 0.25,
        "center": 0.5,
        "centre": 0.5,
        "tengah": 0.5,
        "right": 0.75,
        "kanan": 0.75,
    }
    return mapping.get(text)


def detect_face_focus(source: Path, start: float, end: float) -> float:
    try:
        import cv2  # type: ignore
    except Exception:
        return 0.5
    if not hasattr(cv2, "CascadeClassifier") or not hasattr(cv2, "data"):
        return 0.5

    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(str(cascade_path))
    if detector.empty():
        return 0.5
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        return 0.5

    centers: list[float] = []
    sample_count = min(24, max(8, round((end - start) / 1.5)))
    try:
        for index in range(sample_count):
            timestamp = start + (end - start) * ((index + 0.5) / sample_count)
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            height, width = frame.shape[:2]
            scale = min(1.0, 720.0 / max(width, height))
            if scale < 1.0:
                frame = cv2.resize(frame, None, fx=scale, fy=scale)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = detector.detectMultiScale(
                gray,
                scaleFactor=1.12,
                minNeighbors=5,
                minSize=(36, 36),
            )
            if len(faces) == 0:
                continue
            x, _, face_width, _ = max(faces, key=lambda face: int(face[2]) * int(face[3]))
            scaled_width = frame.shape[1]
            centers.append((float(x) + float(face_width) / 2.0) / float(scaled_width))
    finally:
        capture.release()
    return float(statistics.median(centers)) if centers else 0.5


def vertical_filter(
    media: dict[str, Any],
    focus: float,
) -> str:
    width = int(media["width"])
    height = int(media["height"])
    target_ratio = 9.0 / 16.0
    if width / height > target_ratio:
        crop_width = max(2, int(round(height * target_ratio)) // 2 * 2)
        center_x = width * focus
        crop_x = int(round(center_x - crop_width / 2))
        crop_x = max(0, min(width - crop_width, crop_x))
        crop_x = crop_x // 2 * 2
        return (
            f"crop={crop_width}:{height}:{crop_x}:0,"
            "scale=720:1280:flags=lanczos,setsar=1"
        )
    return (
        "scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=720:1280,setsar=1"
    )


def ffmpeg_supports_nvenc(ffmpeg_executable: str) -> bool:
    result = run_command(
        [ffmpeg_executable, "-hide_banner", "-encoders"],
        capture=True,
        check=False,
    )
    return result.returncode == 0 and "h264_nvenc" in (result.stdout or "")


def cut_vertical_clip(
    source: Path,
    output: Path,
    start: float,
    end: float,
    focus_value: Any,
) -> dict[str, Any]:
    ffmpeg_executable = require_program("ffmpeg")
    media = ffprobe(source)
    focus = numeric_focus(focus_value)
    focus_source = "plan"
    if focus is None:
        focus = detect_face_focus(source, start, end)
        focus_source = "face_detection" if focus != 0.5 else "center_fallback"
    video_filter = vertical_filter(media, focus)
    duration = end - start
    output.parent.mkdir(parents=True, exist_ok=True)

    base = [
        ffmpeg_executable,
        "-hide_banner",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        video_filter,
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
    ]
    if ffmpeg_supports_nvenc(ffmpeg_executable):
        gpu_command = base + [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-cq",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ]
        gpu_result = run_command(gpu_command, capture=True, check=False)
        if gpu_result.returncode == 0:
            return {"focus": round(focus, 4), "focus_source": focus_source, "encoder": "h264_nvenc"}

    cpu_command = base + [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "19",
        "-pix_fmt",
        "yuv420p",
        str(output),
    ]
    run_command(cpu_command)
    return {"focus": round(focus, 4), "focus_source": focus_source, "encoder": "libx264"}


def cinematic_focus_plan(
    clip: dict[str, Any],
    cut_details: dict[str, Any],
) -> dict[str, Any]:
    mode = str(clip.get("cinematic_focus") or "dramatic").casefold()
    side = str(clip.get("cinematic_side") or "both").casefold()
    subject_x = float(cut_details.get("focus", 0.5))
    if side == "auto":
        if subject_x < 0.42:
            side = "right"
        elif subject_x > 0.58:
            side = "left"
        else:
            side = "both"
    presets = {
        "off": {
            "edge_width": 0,
            "edge_opacity": 0.0,
            "vignette": False,
            "contrast": 1.0,
            "saturation": 1.0,
            "brightness": 0.0,
        },
        "subtle": {
            "edge_width": 100,
            "edge_opacity": 0.50,
            "vignette": True,
            "contrast": 1.035,
            "saturation": 0.98,
            "brightness": 0.004,
        },
        "dramatic": {
            "edge_width": 135,
            "edge_opacity": 0.64,
            "vignette": True,
            "contrast": 1.06,
            "saturation": 0.95,
            "brightness": 0.006,
        },
    }
    return {
        "mode": mode,
        "side": side,
        "subject_x": round(subject_x, 4),
        "protect_center": True,
        "gradient_bands": 12,
        **presets[mode],
    }


def compose_caption(clip: dict[str, Any]) -> str:
    parts = [clip["title"]]
    if clip.get("caption"):
        parts.extend(["", str(clip["caption"]).strip()])
    hashtags = []
    for item in clip.get("hashtags") or []:
        value = re.sub(r"[^0-9A-Za-z_\-]", "", str(item).lstrip("#"))
        if value:
            hashtags.append("#" + value)
    if hashtags:
        parts.extend(["", " ".join(hashtags[:8])])
    return "\n".join(parts).strip()


def resolve_campaign_path(
    plan: dict[str, Any],
    plan_path: Path,
) -> Path | None:
    value = str(plan.get("campaign_path") or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = plan_path.parent / path
    return require_file(path.resolve(), "Campaign config")


def campaign_caption(
    base_caption: str,
    campaign_path: Path | None,
) -> str:
    if campaign_path is None:
        return base_caption
    payload = read_json(campaign_path)
    rules = payload.get("caption_rules") or {}
    if not isinstance(rules, dict):
        raise WorkflowError("campaign.caption_rules harus berupa object.")
    required_text = rules.get("required_text") or []
    required_hashtags = rules.get("required_hashtags") or []
    if isinstance(required_text, str):
        required_text = [required_text]
    if isinstance(required_hashtags, str):
        required_hashtags = [required_hashtags]
    if not isinstance(required_text, list) or not isinstance(required_hashtags, list):
        raise WorkflowError("Aturan caption campaign harus berupa daftar.")

    caption = base_caption.strip()
    normalized = caption.casefold()
    additions = []
    for item in required_text:
        value = str(item).strip()
        if value and value.casefold() not in normalized:
            additions.append(value)
            normalized += " " + value.casefold()

    hashtag_additions = []
    for item in required_hashtags:
        value = str(item).strip()
        if not value:
            continue
        if not value.startswith("#"):
            value = "#" + value.lstrip("#")
        if value.casefold() not in normalized:
            hashtag_additions.append(value)
            normalized += " " + value.casefold()
    blocks = [caption] if caption else []
    if additions:
        blocks.append("\n".join(additions))
    if hashtag_additions:
        blocks.append(" ".join(hashtag_additions))
    return "\n\n".join(blocks).strip()


def prepare_campaign(
    plan: dict[str, Any],
    plan_path: Path,
) -> tuple[Path | None, Path | None, Path | None]:
    campaign_path = resolve_campaign_path(plan, plan_path)
    if campaign_path is None:
        return None, None, None
    campaign_script = require_file(scripts_root() / "campaign_pro.py", "Campaign engine")
    assignment_path = plan_path.parent / "campaign-assignment.json"
    report_path = plan_path.parent / "campaign-preflight.json"
    result = run_command(
        [
            sys.executable,
            str(campaign_script),
            "preflight",
            "--campaign",
            str(campaign_path),
            "--plan",
            str(plan_path),
            "--assignment",
            str(assignment_path),
            "--report",
            str(report_path),
        ],
        capture=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        detail = result.stdout or "Campaign preflight gagal."
        raise WorkflowError(detail[-4000:])
    report = read_json(report_path)
    if not bool(report.get("passed")):
        raise WorkflowError("Campaign preflight tidak lulus.")
    return campaign_path, assignment_path, report_path


def normalize_editorial_overrides(
    raw_events: Any,
    clip_duration: float,
) -> list[dict[str, Any]]:
    if raw_events is None:
        return []
    if not isinstance(raw_events, list):
        raise WorkflowError("editorial_events harus berupa daftar.")
    normalized = []
    for index, raw in enumerate(raw_events, start=1):
        if not isinstance(raw, dict):
            raise WorkflowError(f"editorial_events #{index} harus berupa object.")
        start = parse_time(raw.get("start"), f"editorial_events[{index}].start")
        end = parse_time(raw.get("end"), f"editorial_events[{index}].end")
        if end <= start or end > clip_duration + 0.1:
            raise WorkflowError(f"Rentang editorial event #{index} tidak valid.")
        text = " ".join(str(raw.get("text") or "").split())
        if not text or len(text) > 64:
            raise WorkflowError(f"Teks editorial event #{index} kosong atau terlalu panjang.")
        normalized.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text.upper(),
                "animation": str(raw.get("animation") or "soft_pop"),
                "position": str(raw.get("position") or "auto"),
                "sfx": str(raw.get("sfx") or "pop"),
                "sfx_gain_db": max(-32.0, min(-10.0, float(raw.get("sfx_gain_db", -18)))),
                "reason": {"source": "agent_override"},
            }
        )
    return normalized[:6]


def normalize_visual_overrides(
    raw_events: Any,
    clip_duration: float,
    plan_directory: Path,
) -> list[dict[str, Any]]:
    if raw_events is None:
        return []
    if not isinstance(raw_events, list):
        raise WorkflowError("visual_events harus berupa daftar.")
    supported_types = {"concept", "process", "comparison", "metric"}
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_events, start=1):
        if not isinstance(raw, dict):
            raise WorkflowError(f"visual_events #{index} harus berupa object.")
        start = parse_time(raw.get("start"), f"visual_events[{index}].start")
        end = parse_time(raw.get("end"), f"visual_events[{index}].end")
        if end <= start or end > clip_duration + 0.1:
            raise WorkflowError(f"Rentang visual event #{index} tidak valid.")
        visual_type = str(raw.get("type") or "concept").casefold()
        if visual_type not in supported_types:
            raise WorkflowError(f"visual_events #{index}.type tidak didukung: {visual_type}")
        title = " ".join(str(raw.get("title") or "INTI PENJELASAN").split())
        items = raw.get("items") or []
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            raise WorkflowError(f"visual_events #{index}.items harus berupa daftar.")
        event: dict[str, Any] = {
            "event_id": f"visual-{index:02d}",
            "start": round(start, 3),
            "end": round(end, 3),
            "type": visual_type,
            "title": title[:80],
            "items": [" ".join(str(item).split())[:80] for item in items[:3]],
            "number": " ".join(str(raw.get("number") or "").split())[:40],
            "treatment": str(raw.get("treatment") or "cutaway"),
            "animation": str(raw.get("animation") or "slide"),
            "sfx": str(raw.get("sfx") or "whoosh"),
            "sfx_gain_db": max(-32.0, min(-14.0, float(raw.get("sfx_gain_db", -22)))),
            "reason": {"source": "agent_override"},
        }
        asset_value = str(raw.get("asset_path") or "").strip()
        if asset_value:
            asset_path = Path(asset_value).expanduser()
            if not asset_path.is_absolute():
                asset_path = plan_directory / asset_path
            event["asset_path"] = str(asset_path.resolve())
        normalized.append(event)
    return normalized[:4]


def remove_editorial_visual_conflicts(
    editorial_payload: dict[str, Any],
    visual_payload: dict[str, Any],
) -> int:
    visual_ranges = [
        (float(item.get("start", 0.0)), float(item.get("end", 0.0)))
        for item in visual_payload.get("events") or []
        if str(item.get("treatment") or "") == "cutaway"
    ]
    if not visual_ranges:
        return 0
    original = list(editorial_payload.get("events") or [])
    editorial_payload["events"] = [
        item
        for item in original
        if not any(
            float(item.get("end", 0.0)) > visual_start - 0.35
            and float(item.get("start", 0.0)) < visual_end + 0.35
            for visual_start, visual_end in visual_ranges
        )
    ]
    return len(original) - len(editorial_payload["events"])


def render_one_clip(
    plan: dict[str, Any],
    clip: dict[str, Any],
    source: Path,
    full_transcript: dict[str, Any],
    job_dir: Path,
    send_to_telegram: bool,
    campaign_path: Path | None = None,
    campaign_assignment: Path | None = None,
    observer: RunObserver | None = None,
) -> dict[str, Any]:
    def progress(stage: str, message: str) -> None:
        if observer is not None:
            observer.emit(stage, message, clip_id=clip["id"])

    python = sys.executable
    clip_dir = job_dir / "clips" / clip["id"]
    clip_dir.mkdir(parents=True, exist_ok=True)
    raw_video = clip_dir / "clip-vertical.mp4"
    clip_transcript = clip_dir / "transcript.json"
    subtitle = clip_dir / "subtitle.ass"
    campaign_subtitle = clip_dir / "subtitle-campaign.ass"
    subtitle_analysis = subtitle.with_suffix(".analysis.json")
    edit_plan = subtitle.with_suffix(".edit-plan.json")
    editorial_plan = clip_dir / "editorial-plan.json"
    editorial_subtitle = clip_dir / "subtitle-editorial.ass"
    visual_plan = clip_dir / "visual-plan.json"
    visual_assets = clip_dir / "visual-assets"
    editorial_video = clip_dir / "clip-editorial.mp4"
    output_video = clip_dir / "clip-final.mp4"
    caption_path = clip_dir / "caption.txt"
    campaign_applied = clip_dir / "campaign-applied.json"
    campaign_compliance = clip_dir / "campaign-compliance.json"
    motion_plan = clip_dir / "motion-plan.json"
    motioncraft_video = clip_dir / "clip-motioncraft.mp4"

    print(f"\n[{clip['id']}] Memotong dan membuat framing vertikal...")
    progress("clipping", "Memotong video dan menyiapkan framing vertikal")
    cut_details = cut_vertical_clip(
        source,
        raw_video,
        clip["start"],
        clip["end"],
        clip.get("focus", "auto"),
    )
    sliced = slice_transcript(
        full_transcript,
        clip["start"],
        clip["end"],
        clip["hook"],
        clip["headline"],
    )
    write_json(clip_transcript, sliced)

    print(f"[{clip['id']}] Membuat hook dan subtitle cinematic...")
    progress("subtitling", "Membuat hook dan subtitle")
    run_command(
        [
            python,
            str(require_file(scripts_root() / "make_cinematic_ass_pro.py", "Subtitle generator")),
            str(raw_video),
            str(clip_transcript),
            str(subtitle),
        ]
    )
    edit_payload = read_json(edit_plan)
    edit_payload["cinematic_focus"] = cinematic_focus_plan(clip, cut_details)
    write_json(edit_plan, edit_payload)

    editorial_style = str(clip.get("editorial_style") or "balanced").casefold()
    progress("editorial_planning", "Menyiapkan rencana editorial")
    if editorial_style not in {"off", "subtle", "balanced", "energetic"}:
        raise WorkflowError(
            f"editorial_style {clip['id']} tidak valid: {editorial_style}"
        )
    editorial_script = require_file(scripts_root() / "editorial_pro.py", "Editorial analyzer")
    if editorial_style == "off":
        write_json(
            editorial_plan,
            {
                "schema_version": 1,
                "style": "off",
                "editing_density": {"classification": "not_analyzed"},
                "events": [],
            },
        )
    else:
        print(f"[{clip['id']}] Menganalisis kepadatan edit dan punchline...")
        run_command(
            [
                python,
                str(editorial_script),
                "analyze",
                str(raw_video),
                str(clip_transcript),
                str(editorial_plan),
                "--style",
                editorial_style,
            ]
        )

    overrides = clip.get("editorial_events")
    if overrides is not None:
        editorial_payload = read_json(editorial_plan)
        editorial_payload["events"] = normalize_editorial_overrides(
            overrides,
            clip["end"] - clip["start"],
        )
        editorial_payload["event_source"] = "agent_override"
        write_json(editorial_plan, editorial_payload)

    visual_style = str(clip.get("visual_style") or "balanced").casefold()
    progress("visual_planning", "Menyiapkan visual pendukung")
    if visual_style not in {"off", "subtle", "balanced", "immersive"}:
        raise WorkflowError(f"visual_style {clip['id']} tidak valid: {visual_style}")
    visual_script = require_file(
        scripts_root() / "visual_explainer_pro.py",
        "Visual explainer",
    )
    visual_overrides = clip.get("visual_events")
    if visual_style == "off" and visual_overrides is None:
        write_json(
            visual_plan,
            {
                "schema_version": 1,
                "style": "off",
                "duration": round(clip["end"] - clip["start"], 3),
                "events": [],
            },
        )
    else:
        print(f"[{clip['id']}] Mendeteksi footage plain untuk visual explainer...")
        visual_command = [
            python,
            str(visual_script),
            "analyze",
            str(raw_video),
            str(clip_transcript),
            str(editorial_plan),
            str(visual_plan),
            "--assets",
            str(visual_assets),
            "--style",
            "balanced" if visual_style == "off" else visual_style,
        ]
        if campaign_path is not None:
            visual_command.extend(["--campaign", str(campaign_path)])
        run_command(visual_command)

    if visual_overrides is not None:
        visual_payload = read_json(visual_plan)
        visual_payload["events"] = normalize_visual_overrides(
            visual_overrides,
            clip["end"] - clip["start"],
            job_dir,
        )
        visual_payload["event_source"] = "agent_override"
        write_json(visual_plan, visual_payload)
        build_command = [
            python,
            str(visual_script),
            "build",
            str(visual_plan),
            "--assets",
            str(visual_assets),
        ]
        if campaign_path is not None:
            build_command.extend(["--campaign", str(campaign_path)])
        run_command(build_command)

    visual_payload = read_json(visual_plan)
    editorial_payload = read_json(editorial_plan)
    removed_spotlights = remove_editorial_visual_conflicts(
        editorial_payload,
        visual_payload,
    )
    if removed_spotlights:
        editorial_payload["visual_conflicts_removed"] = removed_spotlights
        write_json(editorial_plan, editorial_payload)
        print(
            f"[{clip['id']}] Menghapus {removed_spotlights} spotlight yang bertabrakan "
            "dengan visual cutaway."
        )

    motioncraft_config = clip.get("motioncraft") or {"mode": "auto"}
    motioncraft_mode = str(motioncraft_config.get("mode") or "auto")
    configured_renderer = str(motioncraft_config.get("renderer") or "").strip()
    motioncraft_renderer = (
        Path(configured_renderer).expanduser().resolve()
        if configured_renderer
        else default_motioncraft_renderer().resolve()
    )
    motioncraft_rendered = False
    motioncraft_error: str | None = None
    motion_input_video = raw_video
    if motioncraft_mode != "off":
        bridge = require_file(
            scripts_root() / "motioncraft_bridge.py",
            "MotionCraft bridge",
        )
        renderer_available = (
            (motioncraft_renderer / "package.json").is_file()
            and (motioncraft_renderer / "node_modules").is_dir()
            and (motioncraft_renderer / "src" / "hermes" / "index.ts").is_file()
        )
        if not renderer_available and motioncraft_mode == "on":
            raise WorkflowError(
                "MotionCraft diwajibkan tetapi renderer belum siap: "
                f"{motioncraft_renderer}"
            )
        if renderer_available:
            print(f"[{clip['id']}] Merender motion semantik dengan MotionCraft...")
            progress("motion_rendering", "Merender motion dengan MotionCraft")
            motioncraft_command = [
                python,
                str(bridge),
                "render",
                "--video",
                str(raw_video),
                "--transcript",
                str(clip_transcript),
                "--editorial-plan",
                str(editorial_plan),
                "--visual-plan",
                str(visual_plan),
                "--subtitle-analysis",
                str(subtitle_analysis),
                "--output",
                str(motion_plan),
                "--renderer",
                str(motioncraft_renderer),
                "--render-output",
                str(motioncraft_video),
            ]
            if bool(motioncraft_config.get("strict_transcript", False)):
                motioncraft_command.append("--strict-transcript")
            try:
                run_command(motioncraft_command)
                motioncraft_rendered = True
                motion_input_video = motioncraft_video
                edit_payload = read_json(edit_plan)
                legacy_motion = list(edit_payload.get("video_motion") or [])
                edit_payload["motioncraft"] = {
                    "enabled": True,
                    "renderer": str(motioncraft_renderer),
                    "motion_plan": str(motion_plan),
                    "legacy_video_motion": legacy_motion,
                }
                edit_payload["video_motion"] = []
                write_json(edit_plan, edit_payload)
                visual_payload = read_json(visual_plan)
                visual_payload["renderer"] = "motioncraft-remotion"
                write_json(visual_plan, visual_payload)
            except WorkflowError as exc:
                motioncraft_error = str(exc)
                if motioncraft_mode == "on":
                    raise
                progress("motion_fallback", "MotionCraft gagal; memakai renderer FFmpeg")
                print(
                    f"[{clip['id']}] MotionCraft auto gagal; memakai renderer "
                    f"FFmpeg lama. Detail: {motioncraft_error}",
                    file=sys.stderr,
                )
        elif motioncraft_mode == "auto":
            motioncraft_error = f"Renderer belum siap: {motioncraft_renderer}"
            print(
                f"[{clip['id']}] MotionCraft auto dilewati; {motioncraft_error}",
                file=sys.stderr,
            )

    print(f"[{clip['id']}] Menambahkan spotlight text...")
    progress("subtitle_assembly", "Menyatukan subtitle dan spotlight")
    run_command(
        [
            python,
            str(editorial_script),
            "augment",
            str(subtitle),
            str(subtitle_analysis),
            str(editorial_plan),
            str(editorial_subtitle),
        ]
    )

    render_subtitle = editorial_subtitle
    if campaign_path is not None:
        campaign_script = require_file(scripts_root() / "campaign_pro.py", "Campaign engine")
        print(f"[{clip['id']}] Menerapkan visual identity campaign...")
        run_command(
            [
                python,
                str(campaign_script),
                "style",
                "--campaign",
                str(campaign_path),
                "--subtitle",
                str(editorial_subtitle),
                "--edit-plan",
                str(edit_plan),
                "--output",
                str(campaign_subtitle),
            ]
        )
        render_subtitle = campaign_subtitle

    print(f"[{clip['id']}] Merender motion, SFX, dan audio editorial...")
    progress("rendering", "Merender video dan audio editorial")
    editorial_output = editorial_video if campaign_path is not None else output_video
    run_command(
        [
            python,
            str(require_file(scripts_root() / "render_editorial_pro.py", "Editorial renderer")),
            str(motion_input_video),
            str(render_subtitle),
            str(edit_plan),
            str(editorial_plan),
            str(editorial_output),
            "--visual-plan",
            str(visual_plan),
        ]
    )

    caption = campaign_caption(compose_caption(clip), campaign_path)
    caption_path.write_text(caption + "\n", encoding="utf-8")
    campaign_report: dict[str, Any] | None = None
    if campaign_path is not None:
        if campaign_assignment is None:
            raise WorkflowError("Campaign assignment belum tersedia.")
        campaign_script = require_file(scripts_root() / "campaign_pro.py", "Campaign engine")
        print(f"[{clip['id']}] Memasang komponen campaign...")
        progress("campaign", "Menerapkan dan memeriksa campaign")
        run_command(
            [
                python,
                str(campaign_script),
                "render",
                "--campaign",
                str(campaign_path),
                "--assignment",
                str(campaign_assignment),
                "--clip",
                clip["id"],
                "--input",
                str(editorial_video),
                "--output",
                str(output_video),
                "--applied",
                str(campaign_applied),
            ]
        )
        print(f"[{clip['id']}] Menjalankan campaign compliance gate...")
        run_command(
            [
                python,
                str(campaign_script),
                "audit",
                "--campaign",
                str(campaign_path),
                "--assignment",
                str(campaign_assignment),
                "--clip",
                clip["id"],
                "--transcript",
                str(clip_transcript),
                "--caption",
                str(caption_path),
                "--video",
                str(output_video),
                "--applied",
                str(campaign_applied),
                "--report",
                str(campaign_compliance),
            ]
        )
        campaign_report = read_json(campaign_compliance)
        if not bool(campaign_report.get("passed")):
            raise WorkflowError(
                f"Campaign compliance {clip['id']} gagal; video tidak dikirim ke Telegram."
            )
        campaign_label = str(campaign_report.get("campaign_name") or "Campaign")
        compliance_line = (
            f"Campaign: {campaign_label} | "
            f"Compliance: {campaign_report.get('checks_passed', 0)}/"
            f"{campaign_report.get('checks_total', 0)} lulus"
        )
        caption_path.write_text(
            caption_path.read_text(encoding="utf-8-sig").rstrip()
            + "\n\n"
            + compliance_line
            + "\n",
            encoding="utf-8",
        )

    telegram_sent = False
    if send_to_telegram:
        print(f"[{clip['id']}] Mengirim hasil ke Telegram...")
        progress("sending_review", "Mengirim video untuk review Telegram")
        run_command(
            [
                python,
                str(require_file(scripts_root() / "telegram_approval.py", "Telegram approval")),
                "send",
                str(output_video),
                "--job",
                plan["job_id"],
                "--clip",
                clip["id"],
                "--caption-file",
                str(caption_path),
            ]
        )
        telegram_sent = True

    editorial_payload = read_json(editorial_plan)
    visual_payload = read_json(visual_plan)
    return {
        "clip_id": clip["id"],
        "start": clip["start"],
        "end": clip["end"],
        "hook": clip["hook"],
        "headline": clip["headline"],
        "cinematic_focus": clip["cinematic_focus"],
        "cinematic_side": clip["cinematic_side"],
        "output_video": str(output_video.resolve()),
        "caption_path": str(caption_path.resolve()),
        "telegram_sent": telegram_sent,
        "editorial_style": editorial_style,
        "editorial_event_count": len(editorial_payload.get("events") or []),
        "editing_density": editorial_payload.get("editing_density"),
        "editorial_plan": str(editorial_plan.resolve()),
        "visual_style": visual_style,
        "visual_event_count": len(visual_payload.get("events") or []),
        "visual_plan": str(visual_plan.resolve()),
        "motioncraft_mode": motioncraft_mode,
        "motioncraft_rendered": motioncraft_rendered,
        "motioncraft_renderer": str(motioncraft_renderer),
        "motion_plan": str(motion_plan.resolve()) if motion_plan.is_file() else None,
        "motioncraft_error": motioncraft_error,
        "campaign_enabled": campaign_path is not None,
        "campaign_id": campaign_report.get("campaign_id") if campaign_report else None,
        "campaign_compliance": str(campaign_compliance.resolve()) if campaign_report else None,
        "campaign_checks_passed": campaign_report.get("checks_passed") if campaign_report else None,
        "campaign_checks_total": campaign_report.get("checks_total") if campaign_report else None,
        "rendered_at": utc_now(),
        **cut_details,
    }


def command_validate(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).expanduser().resolve()
    raw_plan = read_json(plan_path)
    source = resolve_plan_file(raw_plan.get("source_path"), plan_path, "Video sumber")
    duration = ffprobe(source)["duration"]
    plan = validate_plan(raw_plan, duration)
    print("Rencana valid: True")
    print(f"Job ID: {plan['job_id']}")
    print(f"Jumlah klip: {len(plan['clips'])}")
    for clip in plan["clips"]:
        print(
            f"- {clip['id']}: {clip['start']:.3f}-{clip['end']:.3f} "
            f"({clip['end'] - clip['start']:.1f} detik)"
        )
    campaign_path, _, report_path = prepare_campaign(plan, plan_path)
    if campaign_path is not None:
        print("Campaign mode: True")
        print(f"Campaign config: {campaign_path}")
        print(f"Campaign preflight: {report_path}")
    return 0


def command_render(args: argparse.Namespace) -> int:
    observe(args, "validating", "Memvalidasi sumber dan rencana klip")
    plan_path = Path(args.plan).expanduser().resolve()
    raw_plan = read_json(plan_path)
    source = resolve_plan_file(raw_plan.get("source_path"), plan_path, "Video sumber")
    full_transcript_path = resolve_plan_file(
        raw_plan.get("transcript_path"),
        plan_path,
        "Transkrip sumber",
    )
    source_duration = ffprobe(source)["duration"]
    plan = validate_plan(raw_plan, source_duration)
    selected_clips = plan["clips"]
    if args.clip:
        requested = set(args.clip)
        available = {clip["id"] for clip in plan["clips"]}
        unknown = sorted(requested - available)
        if unknown:
            raise WorkflowError("Clip ID tidak ada dalam rencana: " + ", ".join(unknown))
        selected_clips = [clip for clip in plan["clips"] if clip["id"] in requested]
    full_transcript = read_json(full_transcript_path)
    if not isinstance(full_transcript, dict):
        raise WorkflowError("Transkrip sumber harus berupa object JSON.")

    campaign_path, campaign_assignment, campaign_preflight = prepare_campaign(
        plan,
        plan_path,
    )

    job_dir = plan_path.parent
    manifest_path = job_dir / "render-manifest.json"
    existing_clips = []
    if manifest_path.exists():
        try:
            old_manifest = read_json(manifest_path)
            selected_ids = {c["id"] for c in selected_clips}
            existing_clips = [c for c in old_manifest.get("clips", []) if c.get("clip_id") not in selected_ids]
        except Exception:
            existing_clips = []

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": plan["job_id"],
        "source_path": str(source),
        "campaign_path": str(campaign_path) if campaign_path else None,
        "campaign_preflight": str(campaign_preflight) if campaign_preflight else None,
        "updated_at": utc_now(),
        "clips": existing_clips,
    }

    failures = 0
    observer = getattr(args, "observer", None)
    if observer is not None:
        observer.current = 0
        observer.total = len(selected_clips)
        observer.emit("render_batch", "Memulai batch klip")
    for clip in selected_clips:
        try:
            result = render_one_clip(
                plan,
                clip,
                source,
                full_transcript,
                job_dir,
                send_to_telegram=not args.no_send,
                campaign_path=campaign_path,
                campaign_assignment=campaign_assignment,
                observer=observer,
            )
            manifest["clips"].append({"status": "completed", **result})
            if observer is not None:
                observer.current += 1
                observer.emit("clip_completed", "Klip selesai diproses", clip_id=clip["id"])
        except Exception as exc:
            failures += 1
            if observer is not None:
                observer.emit("clip_failed", "Pemrosesan klip gagal; lihat log developer", clip_id=clip["id"])
            manifest["clips"].append(
                {
                    "clip_id": clip["id"],
                    "status": "failed",
                    "error": str(exc),
                    "failed_at": utc_now(),
                }
            )
            write_json(manifest_path, manifest)
            print(f"ERROR [{clip['id']}]: {exc}", file=sys.stderr)
            if not args.continue_on_error:
                raise
        write_json(manifest_path, manifest)

    print("\nRENDER BATCH SELESAI")
    print(f"Berhasil: {len(selected_clips) - failures}")
    print(f"Gagal   : {failures}")
    print(f"Manifest: {manifest_path}")
    if not args.no_send:
        print("Semua hasil yang berhasil telah dikirim untuk persetujuan Telegram.")
    return 1 if failures else 0


def delegate_approval(arguments: list[str]) -> int:
    approval_script = require_file(scripts_root() / "telegram_approval.py", "Telegram approval")
    result = run_command([sys.executable, str(approval_script), *arguments], check=False)
    return int(result.returncode)


def command_watch(args: argparse.Namespace) -> int:
    command = ["watch", "--timeout", str(args.timeout)]
    if args.once:
        command.append("--once")
    return delegate_approval(command)


def command_status(args: argparse.Namespace) -> int:
    command = ["status"]
    if args.job:
        command.extend(["--job", safe_job_id(args.job)])
    else:
        command.extend(["--limit", str(args.limit)])
    return delegate_approval(command)


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = run_command(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture=True,
            check=False,
        )
        output = (result.stdout or "").casefold()
        return result.returncode == 0 and str(pid) in output and "no tasks" not in output
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_listener_state() -> dict[str, Any]:
    path = listener_state_path()
    if not path.is_file():
        return {}
    payload = read_json(path)
    return payload if isinstance(payload, dict) else {}


def command_listener(args: argparse.Namespace) -> int:
    state_path = listener_state_path()
    current = read_listener_state()
    current_pid = int(current.get("pid") or 0)

    if args.action == "status":
        running = process_is_running(current_pid)
        print(f"Listener aktif: {running}")
        if running:
            print(f"PID: {current_pid}")
        print(f"Log: {listener_log_path()}")
        return 0

    if args.action == "stop":
        if not process_is_running(current_pid):
            state_path.unlink(missing_ok=True)
            print("Listener sudah tidak berjalan.")
            return 0
        if os.name == "nt":
            result = run_command(
                ["taskkill", "/PID", str(current_pid), "/T", "/F"],
                capture=True,
                check=False,
            )
            if result.returncode != 0:
                raise WorkflowError("Listener tidak dapat dihentikan.")
        else:
            os.kill(current_pid, signal.SIGTERM)
        state_path.unlink(missing_ok=True)
        print("Listener berhasil dihentikan.")
        return 0

    if process_is_running(current_pid):
        print("Listener sudah aktif.")
        print(f"PID: {current_pid}")
        print(f"Log: {listener_log_path()}")
        return 0

    approval_script = require_file(scripts_root() / "telegram_approval.py", "Telegram approval")
    log_path = listener_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_handle:
        popen_kwargs: dict[str, Any] = {
            "cwd": str(project_root()),
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(
            [sys.executable, str(approval_script), "watch"],
            **popen_kwargs,
        )
    time_limit = dt.datetime.now().timestamp() + 3.0
    while dt.datetime.now().timestamp() < time_limit and not process_is_running(process.pid):
        time.sleep(0.1)
    time.sleep(1.0)
    if not process_is_running(process.pid):
        raise WorkflowError(f"Listener gagal dimulai. Periksa log: {log_path}")
    write_json(
        state_path,
        {"pid": process.pid, "started_at": utc_now(), "log_path": str(log_path)},
    )
    print("Listener aktif: True")
    print(f"PID: {process.pid}")
    print(f"Log: {log_path}")
    return 0


def command_campaign_init(args: argparse.Namespace) -> int:
    campaign_script = require_file(scripts_root() / "campaign_pro.py", "Campaign engine")
    command = [
        sys.executable,
        str(campaign_script),
        "init",
        "--directory",
        str(Path(args.directory).expanduser().resolve()),
        "--name",
        args.name,
    ]
    if args.force:
        command.append("--force")
    result = run_command(command, check=False)
    return int(result.returncode)


def command_campaign_check(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).expanduser().resolve()
    raw_plan = read_json(plan_path)
    source = resolve_plan_file(raw_plan.get("source_path"), plan_path, "Video sumber")
    duration = ffprobe(source)["duration"]
    plan = validate_plan(raw_plan, duration)
    campaign_path, _, report_path = prepare_campaign(plan, plan_path)
    if campaign_path is None:
        raise WorkflowError("clip-plan.json belum memiliki campaign_path.")
    print("Campaign check: PASSED")
    print(f"Campaign config: {campaign_path}")
    print(f"Report: {report_path}")
    return 0


def command_doctor(_: argparse.Namespace) -> int:
    checks = [
        (scripts_root() / "transcribe_pro.py", "Transcriber"),
        (scripts_root() / "make_cinematic_ass_pro.py", "Subtitle generator"),
        (scripts_root() / "render_cinematic_pro.py", "Renderer"),
        (scripts_root() / "telegram_approval.py", "Telegram approval"),
        (scripts_root() / "editorial_pro.py", "Editorial analyzer"),
        (scripts_root() / "render_editorial_pro.py", "Editorial renderer"),
        (scripts_root() / "visual_explainer_pro.py", "Visual explainer"),
        (scripts_root() / "campaign_pro.py", "Campaign engine"),
        (scripts_root() / "motioncraft_bridge.py", "MotionCraft bridge"),
    ]
    for path, label in checks:
        require_file(path, label)
    require_program("ffmpeg")
    require_program("ffprobe")
    version = run_command(
        [sys.executable, "-m", "yt_dlp", "--version"],
        capture=True,
    ).stdout.strip()
    print("Script pipeline lengkap: True")
    print("FFmpeg/FFprobe siap: True")
    print(f"yt-dlp siap: True ({version})")
    renderer = default_motioncraft_renderer().resolve()
    if (renderer / "package.json").is_file():
        result = run_command(
            [
                sys.executable,
                str(scripts_root() / "motioncraft_bridge.py"),
                "doctor",
                "--renderer",
                str(renderer),
            ],
            capture=True,
            check=False,
        )
        if result.stdout:
            print(result.stdout.rstrip())
        if result.returncode != 0:
            raise WorkflowError("MotionCraft renderer terdeteksi tetapi pemeriksaan gagal.")
    else:
        print(f"MotionCraft bridge: fallback aktif (renderer belum ada di {renderer})")
    return delegate_approval(["doctor"])


def command_runs(args: argparse.Namespace) -> int:
    store = EventStore(Path(args.event_db).expanduser())
    try:
        print(json.dumps(store.list_runs(args.job, args.limit), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def command_events(args: argparse.Namespace) -> int:
    store = EventStore(Path(args.event_db).expanduser())
    try:
        print(json.dumps(store.read_events(args.run, args.after, args.limit), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def execute_observed(args: argparse.Namespace) -> int:
    if args.command not in {"ingest", "render"}:
        return int(args.handler(args) or 0)
    if args.command == "ingest":
        args.job = safe_job_id(args.job or generated_job_id())
        job_id = args.job
    else:
        plan = read_json(Path(args.plan).expanduser().resolve())
        if not isinstance(plan, dict):
            raise WorkflowError("Root clip-plan.json harus berupa object.")
        job_id = safe_job_id(str(plan.get("job_id") or ""))
    observer = RunObserver(Path(args.event_db).expanduser(), job_id, "CLIP_" + args.command.upper())
    args.observer = observer
    try:
        result = int(args.handler(args) or 0)
        observer.emit(
            "completed" if result == 0 else "failed",
            "Perintah selesai" if result == 0 else "Perintah selesai dengan kegagalan; lihat log developer",
            state="COMPLETED" if result == 0 else "FAILED",
        )
        return result
    except (Exception, KeyboardInterrupt) as exc:
        observer.emit(
            "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            "Proses terhenti; lihat log developer",
            state="FAILED",
        )
        raise
    finally:
        observer.close()
        args.observer = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pipeline Hermes untuk podcast clip, render, dan review Telegram.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Unduh dan transkripsikan sebuah link")
    ingest.add_argument("url", help="URL YouTube atau URL media yang didukung yt-dlp")
    ingest.add_argument("--job", help="Job ID opsional")
    ingest.add_argument("--jobs-root", default=str(default_jobs_root()))
    ingest.add_argument("--model", default="medium")
    ingest.add_argument("--language", default="id")
    ingest.add_argument("--max-duration-hours", type=float, default=4.0)
    ingest.add_argument("--force-download", action="store_true")
    ingest.add_argument("--force-transcribe", action="store_true")
    ingest.add_argument("--force-plan", action="store_true")
    ingest.add_argument("--event-db", default=str(default_event_db()))
    ingest.set_defaults(handler=command_ingest)

    validate = subparsers.add_parser("validate", help="Validasi clip-plan.json")
    validate.add_argument("--plan", required=True)
    validate.set_defaults(handler=command_validate)

    render = subparsers.add_parser("render", help="Render semua klip dalam rencana")
    render.add_argument("--plan", required=True)
    render.add_argument("--clip", action="append", help="Render hanya ID klip ini; dapat diulang")
    render.add_argument("--no-send", action="store_true")
    render.add_argument("--continue-on-error", action="store_true")
    render.add_argument("--event-db", default=str(default_event_db()))
    render.set_defaults(handler=command_render)

    watch = subparsers.add_parser("watch", help="Pantau keputusan Telegram")
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--timeout", type=int, default=25)
    watch.set_defaults(handler=command_watch)

    status = subparsers.add_parser("status", help="Tampilkan status persetujuan")
    status.add_argument("--job")
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(handler=command_status)

    listener = subparsers.add_parser("listener", help="Kelola listener Telegram background")
    listener.add_argument("action", choices=("start", "stop", "status"))
    listener.set_defaults(handler=command_listener)

    campaign = subparsers.add_parser("campaign", help="Kelola Campaign Mode")
    campaign_actions = campaign.add_subparsers(dest="campaign_action", required=True)

    campaign_init = campaign_actions.add_parser("init", help="Buat template campaign")
    campaign_init.add_argument("--directory", required=True)
    campaign_init.add_argument("--name", required=True)
    campaign_init.add_argument("--force", action="store_true")
    campaign_init.set_defaults(handler=command_campaign_init)

    campaign_check = campaign_actions.add_parser("check", help="Periksa brief, aset, dan coverage")
    campaign_check.add_argument("--plan", required=True)
    campaign_check.set_defaults(handler=command_campaign_check)

    doctor = subparsers.add_parser("doctor", help="Periksa seluruh pipeline")
    doctor.set_defaults(handler=command_doctor)
    runs = subparsers.add_parser("runs", help="Baca snapshot run produksi")
    runs.add_argument("--job")
    runs.add_argument("--limit", type=int, default=20)
    runs.add_argument("--event-db", default=str(default_event_db()))
    runs.set_defaults(handler=command_runs)
    events = subparsers.add_parser("events", help="Baca event produksi berurutan")
    events.add_argument("--run")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--event-db", default=str(default_event_db()))
    events.set_defaults(handler=command_events)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return execute_observed(args)
    except WorkflowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nProses dihentikan oleh pengguna.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
