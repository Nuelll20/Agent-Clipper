#!/usr/bin/env python3
"""Analyze editing density and add selective editorial motion text.

The analyzer favors transcript punchlines that occur in visually quiet spans.
It deliberately limits event frequency so the result supports the speaker
instead of turning every subtitle into an effect.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np


SCHEMA_VERSION = 1
DEFAULT_ACCENT = "&H0047D7FF"
SEMANTIC_CUES = {
    "akhirnya",
    "artinya",
    "bahaya",
    "bukan",
    "gagal",
    "jadi",
    "jangan",
    "kebanyakan",
    "masalah",
    "penting",
    "ternyata",
    "tapi",
}
IMPACT_CUES = {"bahaya", "gagal", "jangan", "kebanyakan", "masalah", "bukan"}
REVEAL_CUES = {"akhirnya", "artinya", "jadi", "ternyata"}
FILLERS = {"anu", "eh", "hmm", "kayak", "mungkin", "nah", "oke", "terus"}


class EditorialError(RuntimeError):
    """Expected analysis or ASS augmentation failure."""


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path) -> Any:
    if not path.is_file():
        raise EditorialError(f"File tidak ditemukan: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise EditorialError(f"JSON tidak valid pada {path.name}: {exc}") from None


def require_program(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise EditorialError(f"Program tidak ditemukan: {name}")
    return resolved


def probe_duration(video: Path) -> float:
    result = subprocess.run(
        [
            require_program("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise EditorialError("Durasi video tidak dapat dibaca.")
    return float((result.stdout or "0").strip())


def analyze_frames(video: Path, duration: float, sample_fps: float = 3.0) -> list[dict[str, float]]:
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise EditorialError(f"Video tidak dapat dibuka: {video}")

    timeline: list[dict[str, float]] = []
    previous_gray: np.ndarray | None = None
    previous_hist: np.ndarray | None = None
    sample_count = max(1, int(math.ceil(duration * sample_fps)))
    try:
        for index in range(sample_count):
            timestamp = min(duration, index / sample_fps)
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            height, width = frame.shape[:2]
            scale = min(1.0, 480.0 / max(width, height))
            if scale < 1.0:
                frame = cv2.resize(frame, None, fx=scale, fy=scale)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            hist = cv2.calcHist([gray], [0], None, [32], [0, 256])
            cv2.normalize(hist, hist)

            motion = 0.0
            scene_change = 0.0
            if previous_gray is not None and previous_gray.shape == gray.shape:
                motion = float(np.mean(cv2.absdiff(gray, previous_gray)) / 255.0)
            if previous_hist is not None:
                correlation = float(cv2.compareHist(previous_hist, hist, cv2.HISTCMP_CORREL))
                scene_change = max(0.0, min(1.0, 1.0 - correlation))

            edges = cv2.Canny(gray, 80, 170)
            top = edges[: max(1, edges.shape[0] // 3), :]
            bottom = edges[2 * edges.shape[0] // 3 :, :]
            edge_density = float((np.mean(top > 0) + np.mean(bottom > 0)) / 2.0)
            contrast = min(1.0, float(np.std(gray)) / 96.0)
            overlay_likelihood = min(1.0, edge_density * 7.0 * (0.5 + contrast * 0.5))

            timeline.append(
                {
                    "time": round(timestamp, 3),
                    "motion": round(motion, 5),
                    "scene_change": round(scene_change, 5),
                    "overlay_likelihood": round(overlay_likelihood, 5),
                }
            )
            previous_gray = gray
            previous_hist = hist
    finally:
        capture.release()
    return timeline


def analyze_audio(video: Path, duration: float, frame_seconds: float = 0.1) -> list[dict[str, float]]:
    sample_rate = 16000
    result = subprocess.run(
        [
            require_program("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "pipe:1",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0 or not result.stdout:
        return []
    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    frame_size = max(1, int(sample_rate * frame_seconds))
    output: list[dict[str, float]] = []
    for index in range(0, len(samples), frame_size):
        frame = samples[index : index + frame_size]
        if frame.size == 0:
            continue
        rms = float(np.sqrt(np.mean(np.square(frame)) + 1e-12))
        output.append({"time": round(index / sample_rate, 3), "rms": rms})
    if not output:
        return []
    values = np.array([item["rms"] for item in output], dtype=np.float32)
    low = float(np.percentile(values, 15))
    high = float(np.percentile(values, 95))
    span = max(1e-6, high - low)
    for item in output:
        item["energy"] = round(max(0.0, min(1.0, (item["rms"] - low) / span)), 5)
        del item["rms"]
    return output


def values_in_range(
    timeline: list[dict[str, float]],
    key: str,
    start: float,
    end: float,
) -> list[float]:
    return [
        float(item[key])
        for item in timeline
        if start <= float(item["time"]) <= end and key in item
    ]


def mean_or(values: list[float], default: float = 0.0) -> float:
    return float(np.mean(values)) if values else default


def max_or(values: list[float], default: float = 0.0) -> float:
    return max(values) if values else default


def spoken_text(segment: dict[str, Any]) -> str:
    return " ".join(
        str(
            segment.get("text_corrected")
            or segment.get("text_original")
            or segment.get("text")
            or ""
        ).split()
    )


def normalized_tokens(text: str) -> list[str]:
    return [token.casefold() for token in re.findall(r"[0-9A-Za-zÀ-ÿ]+", text)]


def punchline_phrase(text: str) -> str:
    clauses = [
        item.strip(" ,.;:!?-")
        for item in re.split(r"[.!?;]|\b(?:tapi|jadi|ternyata)\b", text, flags=re.IGNORECASE)
        if item.strip(" ,.;:!?-")
    ]
    candidate = max(
        clauses or [text],
        key=lambda item: (
            sum(2 for token in normalized_tokens(item) if token in SEMANTIC_CUES),
            min(len(normalized_tokens(item)), 7),
        ),
    )
    words = [word for word in candidate.split() if word.casefold().strip(".,!?") not in FILLERS]
    if len(words) > 7:
        cue_indices = [
            index for index, word in enumerate(words)
            if word.casefold().strip(".,!?") in SEMANTIC_CUES
        ]
        center = cue_indices[0] if cue_indices else len(words) // 2
        start = max(0, min(len(words) - 6, center - 2))
        words = words[start : start + 6]
    return " ".join(words).strip(" ,.;:!?").upper()


def event_character(tokens: list[str], text: str) -> tuple[str, str]:
    token_set = set(tokens)
    if token_set & IMPACT_CUES:
        return "impact", "impact"
    if "?" in text:
        return "slide", "whoosh"
    if token_set & REVEAL_CUES:
        return "pop", "pop"
    if any(token.isdigit() for token in tokens):
        return "pop", "click"
    return "soft_pop", "pop"


def recommend_events(
    transcript: dict[str, Any],
    visual: list[dict[str, float]],
    audio: list[dict[str, float]],
    duration: float,
    style: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    style_factor = {"subtle": 0.84, "balanced": 1.0, "energetic": 1.14}.get(style, 1.0)
    for index, segment in enumerate(transcript.get("segments", []), start=1):
        start = max(0.0, float(segment.get("start", 0.0)))
        end = min(duration, float(segment.get("end", start)))
        text = spoken_text(segment)
        tokens = normalized_tokens(text)
        if end - start < 0.65 or not tokens or start < 3.25:
            continue

        motion = mean_or(values_in_range(visual, "motion", start, end))
        overlay = mean_or(values_in_range(visual, "overlay_likelihood", start, end))
        scene_peak = max_or(values_in_range(visual, "scene_change", start, end))
        energy = mean_or(values_in_range(audio, "energy", start, end), 0.45)
        cue_count = sum(1 for token in tokens if token in SEMANTIC_CUES)
        semantic = min(1.0, cue_count * 0.24 + (0.24 if "?" in text else 0.0))
        semantic += 0.15 if any(token.isdigit() for token in tokens) else 0.0
        semantic = min(1.0, semantic)

        existing_edit = min(1.0, overlay * 0.42 + min(1.0, scene_peak * 2.3) * 0.38 + min(1.0, motion * 7.0) * 0.20)
        rawness = max(0.0, 1.0 - existing_edit)
        score = (semantic * 0.50 + energy * 0.26 + rawness * 0.24) * style_factor
        phrase = punchline_phrase(text)
        if len(phrase.split()) < 2 or len(phrase) > 52:
            continue
        animation, sfx = event_character(tokens, text)
        center = start + (end - start) * 0.48
        event_duration = min(1.55, max(0.85, 0.72 + len(phrase.split()) * 0.12))
        event_start = max(start, center - event_duration * 0.38)
        event_end = min(end, event_start + event_duration)
        candidates.append(
            {
                "segment_id": segment.get("id", index),
                "start": round(event_start, 3),
                "end": round(event_end, 3),
                "text": phrase,
                "animation": animation,
                "position": "auto",
                "sfx": sfx,
                "sfx_gain_db": -20 if sfx == "whoosh" else -18,
                "score": round(score, 4),
                "reason": {
                    "semantic": round(semantic, 4),
                    "voice_energy": round(energy, 4),
                    "rawness": round(rawness, 4),
                    "existing_edit": round(existing_edit, 4),
                },
            }
        )

    max_events = 1 if style == "subtle" else (3 if duration >= 42 else 2)
    if style == "energetic" and duration >= 25:
        max_events += 1
    threshold = {"subtle": 0.57, "balanced": 0.48, "energetic": 0.42}.get(style, 0.48)
    selected: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: float(item["score"]), reverse=True):
        if float(candidate["score"]) < threshold:
            continue
        center = (float(candidate["start"]) + float(candidate["end"])) / 2.0
        if any(
            abs(center - (float(item["start"]) + float(item["end"])) / 2.0) < 4.2
            for item in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= max_events:
            break
    selected.sort(key=lambda item: float(item["start"]))
    for event in selected:
        event.pop("score", None)
    return selected, candidates


def editing_density(
    visual: list[dict[str, float]],
    duration: float,
) -> dict[str, Any]:
    if not visual:
        return {"score": 0.0, "classification": "unknown", "scene_cuts": 0}
    cuts = sum(1 for item in visual if float(item["scene_change"]) >= 0.34)
    cut_rate = cuts / max(1.0, duration)
    overlay = mean_or([float(item["overlay_likelihood"]) for item in visual])
    motion = mean_or([float(item["motion"]) for item in visual])
    score = min(1.0, min(1.0, cut_rate / 0.22) * 0.45 + overlay * 0.35 + min(1.0, motion * 7.0) * 0.20)
    classification = "raw" if score < 0.34 else ("lightly_edited" if score < 0.64 else "already_edited")
    return {
        "score": round(score, 4),
        "classification": classification,
        "scene_cuts": cuts,
        "cuts_per_second": round(cut_rate, 4),
        "overlay_activity": round(overlay, 4),
        "motion_activity": round(motion, 4),
    }


def command_analyze(args: argparse.Namespace) -> int:
    video = Path(args.video).resolve()
    transcript = read_json(Path(args.transcript).resolve())
    if not isinstance(transcript, dict):
        raise EditorialError("Transkrip harus berupa object JSON.")
    duration = probe_duration(video)
    visual = analyze_frames(video, duration, sample_fps=args.sample_fps)
    audio = analyze_audio(video, duration)
    events, candidates = recommend_events(transcript, visual, audio, duration, args.style)
    density = editing_density(visual, duration)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "style": args.style,
        "duration": round(duration, 3),
        "editing_density": density,
        "policy": {
            "max_events": len(events),
            "minimum_spacing_seconds": 4.2,
            "opening_hook_reserved_until": 3.25,
            "avoid_already_edited_spans": True,
        },
        "events": events,
        "candidate_count": len(candidates),
    }
    write_json(Path(args.output).resolve(), payload)
    print("Analisis editorial selesai.")
    print(f"Kepadatan edit : {density['classification']} ({density['score']})")
    print(f"Motion text    : {len(events)} event")
    print(f"Rencana        : {Path(args.output).resolve()}")
    return 0


def ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remaining = seconds % 60
    return f"{hours}:{minutes:02d}:{remaining:05.2f}"


def sanitize_transcript_text(text: str) -> str:
    if not text:
        return ""
    cleaned = "".join(ch for ch in str(text) if ord(ch) >= 32 and ord(ch) != 127)
    cleaned = cleaned.replace("\\", "").replace("{", "").replace("}", "")
    cleaned = re.sub(r"&[hH][0-9a-fA-F]+&?", "", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()


def ass_escape(text: str) -> str:
    return sanitize_transcript_text(text)


def nearby_caption(
    captions: list[dict[str, Any]],
    start: float,
    end: float,
) -> dict[str, Any] | None:
    overlaps = [
        item
        for item in captions
        if float(item.get("end", 0.0)) > start and float(item.get("start", 0.0)) < end
    ]
    return overlaps[0] if overlaps else None


def choose_y(position: str, caption_position: str | None) -> int:
    mapping = {"top": 260, "atas": 260, "center": 570, "tengah": 570, "bottom": 860, "bawah": 860}
    if position in mapping:
        return mapping[position]
    if caption_position == "atas":
        return 760
    if caption_position == "tengah":
        return 270
    return 500


def accent_keyword(text: str, accent: str) -> str:
    cleaned = sanitize_transcript_text(text)
    words = cleaned.split()
    if not words:
        return ""
    index = max(range(len(words)), key=lambda item: len(re.sub(r"\W", "", words[item])))
    acc_safe = accent.strip()
    if not acc_safe.startswith("&H") and not acc_safe.startswith("&h"): acc_safe = f"&H{acc_safe}"
    if not acc_safe.endswith("&"): acc_safe = f"{acc_safe}&"
    rendered = []
    for word_index, word in enumerate(words):
        if word_index == index:
            rendered.append(f"{{\1c{acc_safe}}}" + word + r"{\1c&H00F8F8F8&}")
        else:
            rendered.append(word)
    return " ".join(rendered)
def event_override(event: dict[str, Any], x: int, y: int, accent: str) -> str:
    animation = str(event.get("animation") or "soft_pop")
    base = rf"{{\an5\pos({x},{y})\1c&H00F8F8F8&\3c&H00101010&\bord3\shad0\blur0.35"
    if animation == "impact":
        return base + r"\fscx135\fscy135\fad(45,170)\t(0,135,\fscx98\fscy98)\t(135,210,\fscx100\fscy100)}"
    if animation == "slide":
        return rf"{{\an5\move({x - 55},{y},{x},{y},0,210)\1c&H00F8F8F8&\3c&H00101010&\bord3\shad0\blur0.35\fad(80,180)}}"
    if animation == "pop":
        return base + r"\fscx72\fscy72\fad(70,170)\t(0,150,\fscx108\fscy108)\t(150,245,\fscx100\fscy100)}"
    return base + r"\fscx88\fscy88\fad(100,190)\t(0,190,\fscx100\fscy100)}"


def command_augment(args: argparse.Namespace) -> int:
    source_ass = Path(args.subtitle).resolve()
    subtitle_analysis = read_json(Path(args.subtitle_analysis).resolve())
    editorial_plan = read_json(Path(args.editorial_plan).resolve())
    output = Path(args.output).resolve()
    if not isinstance(subtitle_analysis, dict) or not isinstance(editorial_plan, dict):
        raise EditorialError("Analisis subtitle dan rencana editorial harus berupa object.")

    ass_text = source_ass.read_text(encoding="utf-8-sig")
    if "Style: EditorialSpotlight," not in ass_text:
        style_line = (
            "Style: EditorialSpotlight,Segoe UI Semibold,54,&H00F8F8F8,&H00F8F8F8,"
            "&H00101010,&H70000000,-1,0,0,0,100,100,0.5,0,1,3,0,5,42,42,60,1\n"
        )
        ass_text = ass_text.replace("[Events]", style_line + "\n[Events]", 1)

    captions = subtitle_analysis.get("captions") or []
    events = editorial_plan.get("events") or []
    dialogue_lines: list[str] = []
    for event in events:
        start = float(event.get("start", 0.0))
        end = float(event.get("end", start + 1.0))
        if end <= start:
            continue
        caption = nearby_caption(captions, start, end)
        caption_position = str(caption.get("position")) if caption else None
        accent = str(caption.get("accent_color") or DEFAULT_ACCENT) if caption else DEFAULT_ACCENT
        y = choose_y(str(event.get("position") or "auto").casefold(), caption_position)
        override = event_override(event, 360, y, accent)
        rendered_text = accent_keyword(str(event.get("text") or ""), accent)
        dialogue_lines.append(
            "Dialogue: 5,"
            f"{ass_time(start)},{ass_time(end)},"
            "EditorialSpotlight,,0,0,0,,"
            f"{override}{rendered_text}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        ass_text.rstrip() + ("\n" + "\n".join(dialogue_lines) if dialogue_lines else "") + "\n",
        encoding="utf-8-sig",
    )
    print("Motion text editorial berhasil ditambahkan.")
    print(f"Jumlah event: {len(dialogue_lines)}")
    print(f"ASS output  : {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analisis kepadatan edit dan motion text profesional untuk klip podcast."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze")
    analyze.add_argument("video")
    analyze.add_argument("transcript")
    analyze.add_argument("output")
    analyze.add_argument("--style", choices=("subtle", "balanced", "energetic"), default="balanced")
    analyze.add_argument("--sample-fps", type=float, default=3.0)
    analyze.set_defaults(handler=command_analyze)

    augment = subparsers.add_parser("augment")
    augment.add_argument("subtitle")
    augment.add_argument("subtitle_analysis")
    augment.add_argument("editorial_plan")
    augment.add_argument("output")
    augment.set_defaults(handler=command_augment)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args) or 0)
    except EditorialError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
