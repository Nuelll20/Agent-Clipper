#!/usr/bin/env python3
"""Bridge Hermes transcript intelligence to a dedicated Remotion composition.

Hermes remains the source of truth for clipping, transcript timing, editorial
selection, campaigns, and approvals. This module turns those decisions into a
single deterministic motion plan and asks the installed MotionCraft Remotion
project to render it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEFAULT_FPS = 30
OPENING_RESERVED_UNTIL = 5.2
INTENT_CUES = {
    "warning": {"bahaya", "gagal", "jangan", "kebanyakan", "risiko", "masalah"},
    "reveal": {"akhirnya", "artinya", "jadi", "ternyata"},
    "process": {"cara", "caranya", "langkah", "pertama", "kedua", "ketiga", "kemudian", "proses"},
    "contrast": {"tapi", "tetapi", "bukan", "sedangkan", "sementara", "versus"},
    "explanation": {"karena", "sehingga", "simpelnya", "bekerja", "maksudnya", "penyebab"},
}
STOPWORDS = {
    "ada", "aja", "akan", "atau", "bisa", "buat", "dalam", "dan", "dari", "dengan",
    "di", "dia", "ini", "itu", "jadi", "juga", "kalau", "karena", "ke", "kita", "lagi",
    "lebih", "mereka", "oleh", "pada", "saat", "sampai", "saya", "sebagai", "sudah", "tapi",
    "tetap", "tidak", "untuk", "yang",
}


class MotionCraftBridgeError(RuntimeError):
    """Expected integration or rendering failure."""


def read_json(path: Path) -> Any:
    if not path.is_file():
        raise MotionCraftBridgeError(f"File tidak ditemukan: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise MotionCraftBridgeError(f"JSON tidak valid pada {path.name}: {exc}") from None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def spoken_text(segment: dict[str, Any]) -> str:
    return clean_text(
        segment.get("text_corrected")
        or segment.get("text_original")
        or segment.get("text")
    )


def tokens(value: str) -> list[str]:
    return [item.casefold() for item in re.findall(r"[0-9A-Za-zÀ-ÿ%]+", value)]


def content_label(text: str, maximum: int = 4) -> str:
    selected: list[str] = []
    for token in re.findall(r"[0-9A-Za-zÀ-ÿ%]+", text):
        normalized = token.casefold()
        if normalized in STOPWORDS or len(normalized) < 3:
            continue
        if normalized not in {item.casefold() for item in selected}:
            selected.append(token)
        if len(selected) >= maximum:
            break
    return " ".join(selected).upper()


def intent_label(text: str, intent: str) -> str:
    raw_words = re.findall(r"[0-9A-Za-zÀ-ÿ%]+", text)
    cues = INTENT_CUES.get(intent, set())
    for index, word in enumerate(raw_words):
        if word.casefold() not in cues:
            continue
        window_start = max(0, index - 2)
        window_end = min(len(raw_words), index + 3)
        focused = content_label(" ".join(raw_words[window_start:window_end]))
        if focused:
            return focused
    return content_label(text)


def normalize_hex(value: Any) -> str | None:
    text = str(value or "").strip()
    direct = re.fullmatch(r"#?([0-9A-Fa-f]{6})", text)
    if direct:
        return "#" + direct.group(1).upper()
    ass = re.fullmatch(r"&H(?:[0-9A-Fa-f]{2})?([0-9A-Fa-f]{6})&?", text)
    if ass:
        bgr = ass.group(1)
        return "#" + (bgr[4:6] + bgr[2:4] + bgr[0:2]).upper()
    return None


def choose_accent(
    subtitle_analysis: dict[str, Any],
    visual_plan: dict[str, Any],
) -> str:
    visual_accent = normalize_hex(visual_plan.get("accent_color"))
    if visual_accent:
        return visual_accent
    counts: dict[str, int] = {}
    for caption in subtitle_analysis.get("captions") or []:
        accent = normalize_hex(caption.get("accent_color"))
        if accent:
            counts[accent] = counts.get(accent, 0) + 1
    return max(counts, key=counts.get) if counts else "#47D7FF"


def transcript_warnings(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for segment in transcript.get("segments") or []:
        for word in segment.get("words") or []:
            probability = word.get("probability")
            if probability is None or float(probability) >= 0.68:
                continue
            warnings.append(
                {
                    "segment_id": segment.get("id"),
                    "word": clean_text(word.get("word")),
                    "start": round(float(word.get("start", segment.get("start", 0.0))), 3),
                    "probability": round(float(probability), 4),
                    "severity": "review" if float(probability) >= 0.55 else "high",
                }
            )
    return warnings


def intent_for_segment(text: str) -> tuple[str, float, str]:
    token_list = tokens(text)
    token_set = set(token_list)
    if re.search(r"(?:\b\d+[.,]?\d*\s*%|\brp\s*\d|\b\d+\s*(?:kali|tahun|hari|jam))", text, re.I):
        return "metric", 0.92, "number_or_metric"
    for intent in ("warning", "contrast", "process", "reveal", "explanation"):
        matched = token_set & INTENT_CUES[intent]
        if matched:
            strength = {
                "warning": 0.94,
                "contrast": 0.88,
                "process": 0.84,
                "reveal": 0.90,
                "explanation": 0.80,
            }[intent]
            return intent, strength, "cue:" + sorted(matched)[0]
    repeated = [token for token in token_list if token_list.count(token) >= 3 and token not in STOPWORDS]
    if repeated:
        return "sequence", 0.82, "spoken_repetition"
    if "?" in text:
        return "question", 0.86, "spoken_question"
    return "statement", 0.42, "neutral_statement"


def word_anchor(segment: dict[str, Any], intent: str) -> float:
    cues = INTENT_CUES.get(intent, set())
    for word in segment.get("words") or []:
        word_tokens = set(tokens(str(word.get("word") or "")))
        if word_tokens & cues or (intent == "metric" and any(token[0].isdigit() for token in word_tokens if token)):
            return float(word.get("start", segment.get("start", 0.0)))
    return float(segment.get("start", 0.0))


def overlaps(start: float, end: float, ranges: Iterable[tuple[float, float]], margin: float = 0.0) -> bool:
    return any(end > left - margin and start < right + margin for left, right in ranges)


def semantic_events(
    transcript: dict[str, Any],
    editorial_plan: dict[str, Any],
    visual_plan: dict[str, Any],
    duration: float,
) -> list[dict[str, Any]]:
    editorial_ranges = [
        (float(item.get("start", 0.0)), float(item.get("end", 0.0)))
        for item in editorial_plan.get("events") or []
    ]
    visual_ranges = [
        (float(item.get("start", 0.0)), float(item.get("end", 0.0)))
        for item in visual_plan.get("events") or []
    ]
    candidates: list[dict[str, Any]] = []
    for index, segment in enumerate(transcript.get("segments") or [], start=1):
        text = spoken_text(segment)
        if not text:
            continue
        segment_start = max(0.0, float(segment.get("start", 0.0)))
        segment_end = min(duration, float(segment.get("end", segment_start)))
        if segment_end <= OPENING_RESERVED_UNTIL or segment_end - segment_start < 0.6:
            continue
        intent, strength, reason = intent_for_segment(text)
        if intent == "statement":
            continue
        anchor = max(OPENING_RESERVED_UNTIL, word_anchor(segment, intent))
        event_start = max(OPENING_RESERVED_UNTIL, segment_start, anchor - 0.16)
        span = 1.45 if intent in {"warning", "metric", "reveal"} else 1.9
        event_end = min(segment_end, event_start + span)
        if event_end - event_start < 0.55:
            event_start = max(OPENING_RESERVED_UNTIL, event_end - 0.75)
        label = intent_label(text, intent)
        if not label:
            continue
        if overlaps(event_start, event_end, visual_ranges, 0.3):
            strength -= 0.22
        if overlaps(event_start, event_end, editorial_ranges, 0.2):
            reason += "+editorial_emphasis"
            strength += 0.06
        candidates.append(
            {
                "event_id": f"semantic-{index:02d}",
                "segment_id": segment.get("id", index),
                "start": round(event_start, 3),
                "end": round(event_end, 3),
                "anchor": round(anchor, 3),
                "intent": intent,
                "label": label[:52],
                "strength": round(min(1.0, max(0.0, strength)), 3),
                "reason": reason,
                "effect": {
                    "warning": "focus-pulse",
                    "metric": "number-chip",
                    "question": "question-orbit",
                    "contrast": "split-line",
                    "process": "step-dots",
                    "sequence": "step-dots",
                    "reveal": "soft-reveal",
                    "explanation": "concept-link",
                }.get(intent, "soft-reveal"),
            }
        )

    selected: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: float(item["strength"]), reverse=True):
        center = (float(candidate["start"]) + float(candidate["end"])) / 2.0
        if any(abs(center - (float(item["start"]) + float(item["end"])) / 2.0) < 2.4 for item in selected):
            continue
        selected.append(candidate)
        if len(selected) >= max(2, min(7, math.ceil(duration / 8.0))):
            break
    # A cutaway owns the visual layer. Remove semantic cues that overlap it
    # instead of relying on renderer-side hiding; this makes the plan itself
    # collision-safe and auditable.
    selected = [
        item
        for item in selected
        if not overlaps(
            float(item["start"]),
            float(item["end"]),
            visual_ranges,
            0.0,
        )
    ]
    return sorted(selected, key=lambda item: float(item["start"]))


def caption_zone_at(subtitle_analysis: dict[str, Any], timestamp: float) -> str | None:
    for caption in subtitle_analysis.get("captions") or []:
        if float(caption.get("start", 0.0)) <= timestamp <= float(caption.get("end", 0.0)):
            value = str(caption.get("position") or "").casefold()
            return {"atas": "top", "tengah": "center", "bawah": "bottom"}.get(value, value or None)
    return None


def preferred_zone(face: dict[str, float], avoid: str | None) -> str:
    center_x = face["x"] + face["width"] / 2.0
    center_y = face["y"] + face["height"] / 2.0
    if center_x < 0.43:
        zone = "right"
    elif center_x > 0.57:
        zone = "left"
    elif center_y < 0.48:
        zone = "bottom"
    else:
        zone = "top"
    if zone == avoid:
        alternatives = {
            "top": "right" if center_x < 0.5 else "left",
            "bottom": "right" if center_x < 0.5 else "left",
            "left": "top" if center_y > 0.5 else "bottom",
            "right": "top" if center_y > 0.5 else "bottom",
        }
        zone = alternatives[zone]
    return zone


def layout_zone_at(tracks: list[dict[str, Any]], timestamp: float) -> str:
    if not tracks:
        return "bottom"
    selected = tracks[0]
    for track in tracks:
        if float(track.get("time", 0.0)) > timestamp:
            break
        selected = track
    return str(selected.get("free_zone") or "bottom")


def subject_track_at(
    tracks: list[dict[str, Any]],
    timestamp: float,
) -> dict[str, Any]:
    if not tracks:
        return {
            "time": 0.0,
            "face": {
                "x": 0.32,
                "y": 0.16,
                "width": 0.36,
                "height": 0.38,
            },
            "free_zone": "bottom",
            "detected": False,
        }

    selected = tracks[0]

    for track in tracks:
        if float(track.get("time", 0.0)) > timestamp:
            break
        selected = track

    return selected


def assign_semantic_placements(
    events: list[dict[str, Any]],
    tracks: list[dict[str, Any]],
    subtitle_analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []

    for event in events:
        anchor = float(event.get("anchor", event.get("start", 0.0)))
        track = subject_track_at(tracks, anchor)
        caption_zone = caption_zone_at(subtitle_analysis, anchor)

        placed = dict(event)
        placed["placement_zone"] = preferred_zone(
            track["face"],
            caption_zone,
        )
        placed["avoids_caption_zone"] = caption_zone
        output.append(placed)

    return output


def analyze_subject_layout(
    video: Path,
    duration: float,
    subtitle_analysis: dict[str, Any],
    sample_seconds: float = 0.75,
) -> tuple[list[dict[str, Any]], str]:
    fallback_face = {"x": 0.32, "y": 0.16, "width": 0.36, "height": 0.38}
    try:
        import cv2  # type: ignore
    except Exception:
        return [
            {
                "time": 0.0,
                "face": fallback_face,
                "free_zone": preferred_zone(fallback_face, caption_zone_at(subtitle_analysis, 0.0)),
                "detected": False,
            }
        ], "center_fallback"

    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(str(cascade_path))
    capture = cv2.VideoCapture(str(video))
    if detector.empty() or not capture.isOpened():
        if capture.isOpened():
            capture.release()
        return [{"time": 0.0, "face": fallback_face, "free_zone": "bottom", "detected": False}], "center_fallback"

    tracks: list[dict[str, Any]] = []
    previous = fallback_face
    detected_count = 0
    sample_count = max(1, int(math.ceil(duration / sample_seconds)))
    try:
        for index in range(sample_count):
            timestamp = min(duration, index * sample_seconds)
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
            ok, frame = capture.read()
            detected = False
            face = previous
            if ok and frame is not None:
                height, width = frame.shape[:2]
                scale = min(1.0, 640.0 / max(width, height))
                working = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1.0 else frame
                gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)
                faces = detector.detectMultiScale(gray, scaleFactor=1.11, minNeighbors=5, minSize=(34, 34))
                if len(faces):
                    x, y, fw, fh = max(faces, key=lambda rect: int(rect[2]) * int(rect[3]))
                    wh, ww = gray.shape[:2]
                    face = {
                        "x": round(float(x) / ww, 4),
                        "y": round(float(y) / wh, 4),
                        "width": round(float(fw) / ww, 4),
                        "height": round(float(fh) / wh, 4),
                    }
                    previous = face
                    detected = True
                    detected_count += 1
            tracks.append(
                {
                    "time": round(timestamp, 3),
                    "face": face,
                    "free_zone": preferred_zone(face, caption_zone_at(subtitle_analysis, timestamp)),
                    "detected": detected,
                }
            )
    finally:
        capture.release()
    source = "face_tracking" if detected_count else "center_fallback"
    return tracks, source


def normalize_visual_events(visual_plan: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, item in enumerate(visual_plan.get("events") or [], start=1):
        start = max(OPENING_RESERVED_UNTIL, float(item.get("start", 0.0)))
        end = min(duration, float(item.get("end", start)))
        asset_path = str(item.get("asset_path") or "").strip()
        if end - start < 0.5 or not asset_path:
            continue
        output.append(
            {
                "event_id": str(item.get("event_id") or f"visual-{index:02d}"),
                "start": round(start, 3),
                "end": round(end, 3),
                "type": str(item.get("type") or "concept"),
                "title": clean_text(item.get("title"))[:80],
                "treatment": str(item.get("treatment") or "overlay_card"),
                "animation": str(item.get("animation") or "slide"),
                "asset_path": str(Path(asset_path).expanduser().resolve()),
            }
        )
    return output


def build_motion_plan(
    video: Path,
    transcript: dict[str, Any],
    editorial_plan: dict[str, Any],
    visual_plan: dict[str, Any],
    subtitle_analysis: dict[str, Any],
    *,
    fps: int = DEFAULT_FPS,
) -> dict[str, Any]:
    duration = float(transcript.get("duration") or editorial_plan.get("duration") or visual_plan.get("duration") or 0.0)
    if duration <= 0:
        raise MotionCraftBridgeError("Durasi klip tidak valid pada transkrip/rencana.")
    warnings = transcript_warnings(transcript)
    subject_tracks, tracking_source = analyze_subject_layout(
        video,
        duration,
        subtitle_analysis,
    )
    visual_events = normalize_visual_events(visual_plan, duration)
    semantic_plan = assign_semantic_placements(
        semantic_events(
            transcript,
            editorial_plan,
            visual_plan,
            duration,
        ),
        subject_tracks,
        subtitle_analysis,
    )
    for event in visual_events:
        midpoint = (float(event["start"]) + float(event["end"])) / 2.0
        event["placement_zone"] = (
            "center"
            if str(event.get("treatment")) == "cutaway"
            else layout_zone_at(subject_tracks, midpoint)
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "renderer": "motioncraft-remotion",
        "composition_id": "HermesSemanticMotion",
        "width": 720,
        "height": 1280,
        "fps": fps,
        "duration": round(duration, 3),
        "duration_in_frames": max(1, math.ceil(duration * fps)),
        "style": {
            "preset": "campaign-safe-zones-v3",
            "layout_preset": "D:/Hermes/video-agent/config/campaign_layout_preset.json",
            "safe_margin_ratio": 0.08,
            "accent": choose_accent(subtitle_analysis, visual_plan),
            "density": "restrained",
            "placement": "subject-aware",
            "motion_language": "cinematic-minimal",
        },
        "subject_tracking": {
            "source": tracking_source,
            "sample_seconds": 0.75,
            "tracks": subject_tracks,
        },
        "semantic_events": semantic_plan,
        "visual_events": visual_events,
        "qa": {
            "opening_reserved_until": OPENING_RESERVED_UNTIL,
            "transcript_status": "review" if warnings else "passed",
            "transcript_warnings": warnings,
            "high_risk_word_count": sum(1 for item in warnings if item["severity"] == "high"),
            "do_not_invent_claims": True,
            "duplicate_text_overlay": False,
            "layout_preset": "campaign-safe-zones-v3",
            "safe_area_status": "pending_visual_review",
            "collision_status": "semantic_visual_exclusive",
        },
    }


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned[:70] or "clip"


def copy_asset(source: Path, target: Path) -> None:
    if not source.is_file():
        raise MotionCraftBridgeError(f"Aset MotionCraft tidak ditemukan: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def remotion_executable(renderer: Path) -> Path:
    suffix = ".cmd" if sys.platform.startswith("win") else ""
    executable = renderer / "node_modules" / ".bin" / f"remotion{suffix}"
    if not executable.is_file():
        raise MotionCraftBridgeError(
            f"Remotion lokal tidak ditemukan: {executable}. Jalankan npm install di {renderer}."
        )
    return executable


def verify_renderer(renderer: Path) -> None:
    required = [
        renderer / "package.json",
        renderer / "src" / "hermes" / "index.ts",
        renderer / "src" / "hermes" / "HermesSemanticMotion.tsx",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise MotionCraftBridgeError("File renderer belum lengkap: " + ", ".join(missing))
    remotion_executable(renderer)


def stage_props(
    renderer: Path,
    video: Path,
    plan: dict[str, Any],
    plan_output: Path,
) -> Path:
    digest = hashlib.sha256((str(video.resolve()) + str(plan_output.resolve())).encode("utf-8")).hexdigest()[:10]
    run_name = safe_name(plan_output.parent.name) + "-" + digest
    public_root = renderer / "public" / "hermes" / run_name
    public_root.mkdir(parents=True, exist_ok=True)
    video_target = public_root / ("source" + video.suffix.casefold())
    copy_asset(video, video_target)

    props = json.loads(json.dumps(plan))
    props["video_src"] = video_target.relative_to(renderer / "public").as_posix()
    for index, event in enumerate(props.get("visual_events") or [], start=1):
        source = Path(str(event.pop("asset_path"))).expanduser()
        extension = source.suffix.casefold() or ".png"
        target = public_root / "visuals" / f"{safe_name(str(event.get('event_id') or index))}{extension}"
        copy_asset(source, target)
        event["asset_src"] = target.relative_to(renderer / "public").as_posix()
    props_path = plan_output.with_suffix(".remotion-props.json")
    write_json(props_path, props)
    return props_path


def render_plan(
    renderer: Path,
    video: Path,
    plan: dict[str, Any],
    plan_output: Path,
    output: Path,
) -> None:
    verify_renderer(renderer)
    props_path = stage_props(renderer, video, plan, plan_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(remotion_executable(renderer)),
        "render",
        "src/hermes/index.ts",
        "HermesSemanticMotion",
        str(output.resolve()),
        f"--props={props_path.resolve()}",
        "--codec=h264",
        "--audio-codec=aac",
        "--pixel-format=yuv420p",
        "--crf=18",
        "--concurrency=4",
    ]
    result = subprocess.run(
        command,
        cwd=str(renderer),
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        raise MotionCraftBridgeError("Render Remotion gagal:\n" + (result.stdout or "")[-6000:])
    if not output.is_file() or output.stat().st_size == 0:
        raise MotionCraftBridgeError("Remotion selesai tanpa video output yang valid.")


def command_plan(args: argparse.Namespace) -> int:
    video = Path(args.video).expanduser().resolve()
    transcript = read_json(Path(args.transcript).expanduser().resolve())
    editorial_plan = read_json(Path(args.editorial_plan).expanduser().resolve())
    visual_plan = read_json(Path(args.visual_plan).expanduser().resolve())
    subtitle_analysis = read_json(Path(args.subtitle_analysis).expanduser().resolve())
    plan = build_motion_plan(video, transcript, editorial_plan, visual_plan, subtitle_analysis, fps=args.fps)
    if args.strict_transcript and int(plan["qa"]["high_risk_word_count"]) > 0:
        raise MotionCraftBridgeError(
            "Transkrip memiliki kata berisiko tinggi. Tinjau qa.transcript_warnings sebelum render."
        )
    output = Path(args.output).expanduser().resolve()
    write_json(output, plan)
    print("Motion plan selesai.")
    print(f"Semantic events : {len(plan['semantic_events'])}")
    print(f"Visual events   : {len(plan['visual_events'])}")
    print(f"Transcript QA   : {plan['qa']['transcript_status']}")
    print(f"Output          : {output}")
    return 0


def command_render(args: argparse.Namespace) -> int:
    plan_args = argparse.Namespace(**vars(args))
    command_plan(plan_args)
    plan_path = Path(args.output).expanduser().resolve()
    plan = read_json(plan_path)
    render_output = Path(args.render_output).expanduser().resolve()
    render_plan(
        Path(args.renderer).expanduser().resolve(),
        Path(args.video).expanduser().resolve(),
        plan,
        plan_path,
        render_output,
    )
    print("MotionCraft render selesai.")
    print(f"Video           : {render_output}")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    renderer = Path(args.renderer).expanduser().resolve()
    verify_renderer(renderer)
    print("MotionCraft bridge: siap")
    print(f"Renderer          : {renderer}")
    print(f"Remotion          : {remotion_executable(renderer)}")
    return 0


def add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--video", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--editorial-plan", required=True)
    parser.add_argument("--visual-plan", required=True)
    parser.add_argument("--subtitle-analysis", required=True)
    parser.add_argument("--output", required=True, help="motion-plan.json")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--strict-transcript", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jembatan semantik Hermes ke MotionCraft Remotion.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", help="Buat motion-plan.json tanpa render")
    add_plan_arguments(plan)
    plan.set_defaults(handler=command_plan)
    render = subparsers.add_parser("render", help="Buat motion plan dan render Remotion")
    add_plan_arguments(render)
    render.add_argument("--renderer", required=True)
    render.add_argument("--render-output", required=True)
    render.set_defaults(handler=command_render)
    doctor = subparsers.add_parser("doctor", help="Periksa bridge dan renderer")
    doctor.add_argument("--renderer", required=True)
    doctor.set_defaults(handler=command_doctor)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args) or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MotionCraftBridgeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
