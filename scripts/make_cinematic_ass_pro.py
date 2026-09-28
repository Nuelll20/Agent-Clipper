from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from validators.font_validator import assert_required_fonts

import cv2
import numpy as np


PLAY_RES_X = 720
PLAY_RES_Y = 1280
MIN_WORDS = 2
MAX_WORDS = 5
PAUSE_BREAK_SECONDS = 0.42

# Koreksi dibuat konservatif. Hermes dapat mengisi text_corrected pada
# transcript untuk koreksi berbasis konteks yang lebih lengkap.
DEFAULT_WORD_CORRECTIONS = {
    "macah": "matcha",
}

HOOK_TERMS = {
    "bahaya",
    "berhenti",
    "gagal",
    "jangan",
    "kebanyakan",
    "kenapa",
    "ternyata",
}


def ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


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


def safe_ass_color(color_str: str) -> str:
    c = str(color_str).strip()
    if not c.startswith("&H") and not c.startswith("&h"):
        c = f"&H{c}"
    if not c.endswith("&"):
        c = f"{c}&"
    return c


def validate_ass_content(ass_content: str, source_name: str = "subtitle.ass") -> None:
    lines = ass_content.splitlines()
    dialogue_idx = 0
    forbidden_outside_regex = re.compile(
        r"(?:fsc[xy]\d*|pos\s*\(|move\s*\(|fad\s*\(|\bt\s*\(|H00[0-9A-Fa-f]{6}|&H[0-9A-Fa-f]{6}&?|\\1c|\\2c|\\3c|\\4c|\\bord|\\shad|\\blur)",
        re.IGNORECASE
    )
    errors = []
    for line_no, line in enumerate(lines, 1):
        for char_idx, ch in enumerate(line):
            code = ord(ch)
            if code < 32 and ch not in ('\r', '\n'):
                errors.append(f"Line {line_no}: ASCII control character 0x{code:02X} found at pos {char_idx}")

        if not line.startswith("Dialogue:"):
            continue
        dialogue_idx += 1
        open_count = line.count("{")
        close_count = line.count("}")
        if open_count != close_count:
            errors.append(f"Dialogue {dialogue_idx} (Line {line_no}): Unbalanced braces! {open_count} '{{' vs {close_count} '}}")
        in_brace = False
        for char_idx, ch in enumerate(line):
            if ch == '{':
                if in_brace:
                    errors.append(f"Dialogue {dialogue_idx} (Line {line_no}): Nested '{{' at pos {char_idx}")
                in_brace = True
            elif ch == '}':
                if not in_brace:
                    errors.append(f"Dialogue {dialogue_idx} (Line {line_no}): Unexpected '}}' without '{{' at pos {char_idx}")
                in_brace = False
        parts = line.split(",", 9)
        if len(parts) >= 10:
            text_body = parts[9]
            text_clean = re.sub(r'\{[^}]*\}', '', text_body)
            text_clean_no_breaks = text_clean.replace(r'\N', ' ').replace(r'\n', ' ')
            leaks = forbidden_outside_regex.findall(text_clean_no_breaks)
            if leaks:
                errors.append(
                    f"Dialogue {dialogue_idx} (Line {line_no}): RAW ASS TAG LEAKED in visible text: {leaks}\n"
                    f"  Full text field: {text_body[:120]}\n"
                    f"  Cleaned text:    {text_clean_no_breaks[:80]}"
                )
            stray_bs = [m.start() for m in re.finditer(r'\\', text_clean_no_breaks)]
            if stray_bs:
                errors.append(
                    f"Dialogue {dialogue_idx} (Line {line_no}): Stray backslash outside tags: {text_clean_no_breaks[:80]}"
                )
    if errors:
        error_msg = f"ASS VALIDATION FAILED for {source_name}:\n" + "\n".join(errors)
        raise ValueError(error_msg)

def normalized_word(text: str) -> str:
    return re.sub(r"[^0-9a-zA-ZÀ-ÿ]+", "", str(text)).casefold()


def apply_case_pattern(source: str, replacement: str) -> str:
    letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", source)

    if letters.isupper():
        return replacement.upper()

    if letters[:1].isupper():
        return replacement[:1].upper() + replacement[1:]

    return replacement


def replace_word_preserving_punctuation(
    original: str,
    replacement: str,
) -> str:
    match = re.match(
        r"^(?P<prefix>[^0-9A-Za-zÀ-ÿ]*)(?P<core>.*?)(?P<suffix>[^0-9A-Za-zÀ-ÿ]*)$",
        original,
    )

    if not match:
        return replacement

    replacement = apply_case_pattern(match.group("core"), replacement)
    return f"{match.group('prefix')}{replacement}{match.group('suffix')}"


def segment_display_words(
    segment: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    timestamped_words = segment.get("words") or []
    words = [dict(item) for item in timestamped_words]
    corrections: list[dict[str, str]] = []

    corrected_text = str(segment.get("text_corrected") or "").strip()
    corrected_tokens = corrected_text.split()

    if corrected_text and len(corrected_tokens) == len(words):
        for item, corrected in zip(words, corrected_tokens):
            original = str(item.get("word", "")).strip()
            item["display"] = corrected

            if normalized_word(original) != normalized_word(corrected):
                corrections.append({
                    "original": original,
                    "corrected": corrected,
                    "source": "text_corrected",
                })

        return words, corrections

    for item in words:
        original = str(item.get("word", "")).strip()
        key = normalized_word(original)
        replacement = DEFAULT_WORD_CORRECTIONS.get(key)

        if replacement:
            corrected = replace_word_preserving_punctuation(
                original,
                replacement,
            )
            item["display"] = corrected
            corrections.append({
                "original": original,
                "corrected": corrected,
                "source": "safe_dictionary",
            })
        else:
            item["display"] = original

    return words, corrections


def split_plain_text(text: str, start: float, end: float) -> list[dict[str, Any]]:
    tokens = text.split()

    if not tokens:
        return []

    duration = max(0.2, end - start)
    result = []

    for index, token in enumerate(tokens):
        token_start = start + duration * index / len(tokens)
        token_end = start + duration * (index + 1) / len(tokens)
        result.append({
            "word": token,
            "display": token,
            "start": token_start,
            "end": token_end,
            "probability": None,
        })

    return result


def should_break_after(
    group: list[dict[str, Any]],
    next_word: dict[str, Any] | None,
) -> bool:
    if len(group) >= MAX_WORDS:
        return True

    current_text = str(group[-1].get("display", ""))
    punctuation_break = (
        len(group) >= MIN_WORDS
        and bool(re.search(r"[,.!?;:]$", current_text))
    )

    if punctuation_break:
        return True

    if next_word is not None and len(group) >= MIN_WORDS:
        current_end = float(group[-1].get("end", 0.0))
        next_start = float(next_word.get("start", current_end))

        if next_start - current_end >= PAUSE_BREAK_SECONDS:
            return True

    return False


def build_caption_chunks(
    transcript: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chunks: list[dict[str, Any]] = []
    all_corrections: list[dict[str, Any]] = []

    for segment_index, segment in enumerate(
        transcript.get("segments", []),
        start=1,
    ):
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        fallback_text = str(
            segment.get("text_corrected")
            or segment.get("text_original")
            or segment.get("text")
            or ""
        ).strip()

        words, corrections = segment_display_words(segment)

        if not words and fallback_text:
            words = split_plain_text(fallback_text, start, end)

        if not words:
            continue

        for correction in corrections:
            all_corrections.append({
                "segment_id": segment.get("id", segment_index),
                **correction,
            })

        groups: list[list[dict[str, Any]]] = []
        group: list[dict[str, Any]] = []

        for word_index, word in enumerate(words):
            group.append(word)
            next_word = (
                words[word_index + 1]
                if word_index + 1 < len(words)
                else None
            )

            if should_break_after(group, next_word):
                groups.append(group)
                group = []

        if group:
            if (
                len(group) == 1
                and groups
                and len(groups[-1]) <= MAX_WORDS
            ):
                groups[-1].extend(group)
            else:
                groups.append(group)

        for words_in_group in groups:
            chunk_start = float(words_in_group[0].get("start", start))
            chunk_end = float(words_in_group[-1].get("end", end))

            if chunk_end <= chunk_start:
                chunk_end = chunk_start + 0.16

            chunk_text = " ".join(
                str(item.get("display") or item.get("word") or "").strip()
                for item in words_in_group
            ).strip()

            chunks.append({
                "segment_id": segment.get("id", segment_index),
                "start": chunk_start,
                "end": chunk_end,
                "text": chunk_text,
                "words": words_in_group,
            })

    return chunks, all_corrections


def choose_line_break(words: list[dict[str, Any]]) -> int | None:
    rendered = [
        str(item.get("display") or item.get("word") or "").strip()
        for item in words
    ]

    if len(rendered) < 4 or len(" ".join(rendered)) <= 24:
        return None

    best_index = None
    best_difference = math.inf

    for index in range(2, len(rendered) - 1):
        left_length = len(" ".join(rendered[:index]))
        right_length = len(" ".join(rendered[index:]))
        difference = abs(left_length - right_length)

        if difference < best_difference:
            best_difference = difference
            best_index = index

    return best_index


def ass_bgr(color: tuple[int, int, int]) -> str:
    blue, green, red = [
        int(max(0, min(255, channel)))
        for channel in color
    ]
    return f"&H00{blue:02X}{green:02X}{red:02X}&"


def color_distance(
    first: tuple[int, int, int],
    second: tuple[int, int, int],
) -> float:
    return math.sqrt(sum(
        (float(a) - float(b)) ** 2
        for a, b in zip(first, second)
    ))


def choose_colors(
    brightness: float,
    sampled_bgr: tuple[int, int, int],
) -> dict[str, Any]:
    if brightness >= 145:
        text_bgr = (28, 28, 28)
        outline_bgr = (244, 244, 244)
    else:
        text_bgr = (246, 246, 246)
        outline_bgr = (20, 20, 20)

    sample = np.uint8([[list(sampled_bgr)]])
    hue, saturation, _ = [
        int(item)
        for item in cv2.cvtColor(sample, cv2.COLOR_BGR2HSV)[0, 0]
    ]

    if saturation < 55:
        hue = 24 if brightness < 145 else 105

    saturation = max(saturation, 175)
    value = 255 if brightness < 145 else 175

    accent_pixel = cv2.cvtColor(
        np.uint8([[[hue, saturation, value]]]),
        cv2.COLOR_HSV2BGR,
    )[0, 0]
    accent_bgr = tuple(int(item) for item in accent_pixel)

    if color_distance(accent_bgr, text_bgr) < 85:
        hue = (hue + 75) % 180
        accent_pixel = cv2.cvtColor(
            np.uint8([[[hue, saturation, value]]]),
            cv2.COLOR_HSV2BGR,
        )[0, 0]
        accent_bgr = tuple(int(item) for item in accent_pixel)

    return {
        "text": ass_bgr(text_bgr),
        "outline": ass_bgr(outline_bgr),
        "accent": ass_bgr(accent_bgr),
        "accent_bgr": list(accent_bgr),
    }


def overlap_ratio(
    rectangle: tuple[int, int, int, int],
    face: tuple[int, int, int, int],
) -> float:
    rx1, ry1, rx2, ry2 = rectangle
    fx, fy, fw, fh = face
    fx2 = fx + fw
    fy2 = fy + fh

    left = max(rx1, fx)
    top = max(ry1, fy)
    right = min(rx2, fx2)
    bottom = min(ry2, fy2)

    if right <= left or bottom <= top:
        return 0.0

    intersection = (right - left) * (bottom - top)
    rectangle_area = max(1, (rx2 - rx1) * (ry2 - ry1))
    return intersection / rectangle_area


def representative_color(region_bgr: np.ndarray) -> tuple[int, int, int]:
    if region_bgr.size == 0:
        return 40, 190, 245

    reduced = cv2.resize(
        region_bgr,
        (max(8, region_bgr.shape[1] // 8), max(8, region_bgr.shape[0] // 8)),
        interpolation=cv2.INTER_AREA,
    )
    hsv = cv2.cvtColor(reduced, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    mask = (saturation >= 60) & (value >= 55) & (value <= 245)

    if int(np.count_nonzero(mask)) >= 20:
        eligible_hues = hsv[:, :, 0][mask]
        histogram, boundaries = np.histogram(
            eligible_hues,
            bins=18,
            range=(0, 180),
        )
        dominant_index = int(np.argmax(histogram))
        low = boundaries[dominant_index]
        high = boundaries[dominant_index + 1]
        hue_mask = mask & (hsv[:, :, 0] >= low) & (hsv[:, :, 0] < high)
        pixels = reduced[hue_mask]
    else:
        pixels = reduced.reshape(-1, 3)

    median = np.median(pixels, axis=0)
    return tuple(int(item) for item in median)


class FrameAnalyzer:
    def __init__(self, video_path: Path):
        self.capture = cv2.VideoCapture(str(video_path))

        if not self.capture.isOpened():
            raise RuntimeError(f"Tidak dapat membuka video: {video_path}")

        self.width = int(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT))

        self.face_detector = None

        if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
            cascade_path = (
                Path(cv2.data.haarcascades)
                / "haarcascade_frontalface_default.xml"
            )
            detector = cv2.CascadeClassifier(str(cascade_path))

            if not detector.empty():
                self.face_detector = detector
        self.candidates = [
            {"name": "atas", "x_ratio": 0.50, "y_ratio": 0.22, "width_ratio": 0.84, "height_ratio": 0.15, "alignment": 5},
            {"name": "tengah", "x_ratio": 0.50, "y_ratio": 0.52, "width_ratio": 0.84, "height_ratio": 0.15, "alignment": 5},
            {"name": "bawah", "x_ratio": 0.50, "y_ratio": 0.78, "width_ratio": 0.84, "height_ratio": 0.15, "alignment": 5},
            {"name": "kiri", "x_ratio": 0.22, "y_ratio": 0.52, "width_ratio": 0.30, "height_ratio": 0.15, "alignment": 4},
            {"name": "kanan", "x_ratio": 0.78, "y_ratio": 0.52, "width_ratio": 0.30, "height_ratio": 0.15, "alignment": 6},
        ]
        self.previous_candidate: str | None = None

    def read_frame(self, timestamp: float) -> np.ndarray | None:
        self.capture.set(
            cv2.CAP_PROP_POS_MSEC,
            max(0.0, timestamp) * 1000,
        )
        success, frame = self.capture.read()
        return frame if success else None

    def candidate_rectangle(
        self,
        candidate: dict[str, Any],
    ) -> tuple[int, int, int, int]:
        center_x = int(self.width * float(candidate.get("x_ratio", 0.5)))
        center_y = int(self.height * float(candidate["y_ratio"]))
        rectangle_width = int(self.width * float(candidate.get("width_ratio", 0.84)))
        rectangle_height = int(self.height * float(candidate.get("height_ratio", 0.15)))
        x1 = max(0, center_x - rectangle_width // 2)
        y1 = max(0, center_y - rectangle_height // 2)
        x2 = min(self.width, center_x + rectangle_width // 2)
        y2 = min(self.height, center_y + rectangle_height // 2)
        return x1, y1, x2, y2

    def analyze(
        self,
        start: float,
        end: float,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        duration = max(0.2, end - start)
        sample_times = np.linspace(
            start + duration * 0.12,
            end - duration * 0.12,
            5,
        )
        measurements = {
            item["name"]: {
                "scores": [],
                "brightness": [],
                "colors": [],
            }
            for item in self.candidates
        }

        for timestamp in sample_times:
            frame = self.read_frame(float(timestamp))

            if frame is None:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = []

            if self.face_detector is not None:
                faces = self.face_detector.detectMultiScale(
                    gray,
                    scaleFactor=1.12,
                    minNeighbors=5,
                    minSize=(45, 45),
                )

            for candidate in self.candidates:
                rectangle = self.candidate_rectangle(candidate)
                x1, y1, x2, y2 = rectangle
                region_gray = gray[y1:y2, x1:x2]
                region_bgr = frame[y1:y2, x1:x2]

                if region_gray.size == 0:
                    continue

                edges = cv2.Canny(region_gray, 80, 160)
                edge_density = float(np.mean(edges > 0))
                visual_variation = min(float(np.std(region_gray)) / 128, 1.0)
                face_penalty = 0.0
                for face in faces:
                    fx, fy, fw, fh = [int(value) for value in face]
                    pad_x = int(self.width * 0.08)
                    pad_y = int(self.height * 0.08)
                    padded_x = max(0, fx - pad_x)
                    padded_y = max(0, fy - pad_y)
                    padded_x2 = min(self.width, fx + fw + pad_x)
                    padded_y2 = min(self.height, fy + fh + pad_y)
                    padded_face = (
                        padded_x,
                        padded_y,
                        padded_x2 - padded_x,
                        padded_y2 - padded_y,
                    )
                    # Strongly reject text zones touching the subject.
                    face_penalty += overlap_ratio(rectangle, padded_face) * 100
                score = (
                    edge_density * 3
                    + visual_variation * 0.35
                    + face_penalty
                )

                data = measurements[candidate["name"]]
                data["scores"].append(score)
                data["brightness"].append(float(np.median(region_gray)))
                data["colors"].append(representative_color(region_bgr))

        result = []

        for candidate in self.candidates:
            data = measurements[candidate["name"]]
            color_samples = data["colors"]
            sampled_bgr = (
                tuple(
                    int(item)
                    for item in np.median(color_samples, axis=0)
                )
                if color_samples
                else (40, 190, 245)
            )
            result.append({
                **candidate,
                "score": (
                    float(np.mean(data["scores"]))
                    if data["scores"]
                    else 999.0
                ),
                "brightness": (
                    float(np.median(data["brightness"]))
                    if data["brightness"]
                    else 100.0
                ),
                "sampled_bgr": sampled_bgr,
            })

        result.sort(key=lambda item: float(item["score"]))
        selected = result[0]

        # Conservative fallback: when face detection is intermittent, prefer a
        # narrow side zone over a broad vertical zone. This keeps subtitles
        # away from a face that another tracker may still detect.
        side_candidates = [item for item in result if item["name"] in {"kiri", "kanan"}]
        if selected["name"] in {"atas", "tengah", "bawah"} and side_candidates:
            side_selected = min(side_candidates, key=lambda item: float(item["score"]))
            # Side zones are narrower and remain safe when the face detector
            # misses an intermittent frame. Prefer them unless the image side
            # is clearly unusable.
            if float(side_selected["score"]) <= 35.0:
                selected = side_selected

        if self.previous_candidate is not None:
            previous = next(
                (
                    item
                    for item in result
                    if item["name"] == self.previous_candidate
                ),
                None,
            )

            if (
                previous is not None
                and not (
                    selected["name"] in {"kiri", "kanan"}
                    and previous["name"] in {"atas", "tengah", "bawah"}
                )
                and float(previous["score"])
                <= float(selected["score"]) * 1.18 + 0.035
            ):
                selected = previous

        self.previous_candidate = str(selected["name"])
        return selected, result

    def close(self) -> None:
        self.capture.release()


def build_active_word_text(
    chunk: dict[str, Any],
    colors: dict[str, Any],
) -> str:
    words = chunk["words"]
    line_break = choose_line_break(words)
    chunk_start = float(chunk["start"])
    chunk_end = float(chunk["end"])
    chunk_duration_ms = max(1, round((chunk_end - chunk_start) * 1000))
    rendered: list[str] = []

    for index, word in enumerate(words):
        if index > 0:
            rendered.append(r"\N" if line_break == index else " ")

        word_start = float(word.get("start", chunk_start))
        word_end = float(word.get("end", word_start))

        if word_end <= word_start:
            if index + 1 < len(words):
                word_end = float(words[index + 1].get("start", word_start + 0.14))
            else:
                word_end = min(chunk_end, word_start + 0.16)

        relative_start = max(0, round((word_start - chunk_start) * 1000))
        relative_end = min(
            chunk_duration_ms,
            max(relative_start + 80, round((word_end - chunk_start) * 1000)),
        )
        fade_in_end = min(relative_end, relative_start + 75)
        fade_out_start = max(fade_in_end, relative_end - 95)
        clean_text = sanitize_transcript_text(
            str(word.get("display") or word.get("word") or "")
        )
        if not clean_text:
            continue
        c_text = safe_ass_color(colors['text'])
        c_accent = safe_ass_color(colors['accent'])
        override_tag = rf"{{\1c{c_text}\t({relative_start},{fade_in_end},\1c{c_accent})\t({fade_out_start},{relative_end},\1c{c_text})}}"
        rendered.append(f"{override_tag}{clean_text}")

    return "".join(rendered)

def choose_hook_text(transcript: dict[str, Any]) -> tuple[str, str]:
    explicit = transcript.get("hook_text")

    if isinstance(transcript.get("hook"), dict):
        explicit = transcript["hook"].get("text") or explicit

    if explicit:
        cleaned = " ".join(str(explicit).split())
        return cleaned.upper(), "transcript_override"

    all_text = " ".join(
        str(
            segment.get("text_corrected")
            or segment.get("text_original")
            or segment.get("text")
            or ""
        )
        for segment in transcript.get("segments", [])
    )
    lower_text = all_text.casefold()

    if "kebanyakan" in lower_text and "kafein" in lower_text:
        return "KEBANYAKAN KAFEIN,\\NAPA YANG TERJADI?", "topic_risk"

    candidates = []

    for segment in transcript.get("segments", []):
        text = " ".join(str(
            segment.get("text_corrected")
            or segment.get("text_original")
            or segment.get("text")
            or ""
        ).split())

        if not text:
            continue

        lower = text.casefold()
        score = 0
        score += 5 if "?" in text else 0
        score += sum(2 for term in HOOK_TERMS if term in lower)
        score += 2 if 4 <= len(text.split()) <= 10 else 0
        score -= 1 if len(text.split()) > 14 else 0
        candidates.append((score, float(segment.get("start", 0.0)), text))

    if not candidates:
        return "TONTON SAMPAI SELESAI", "fallback"

    candidates.sort(key=lambda item: (-item[0], item[1]))
    chosen = re.sub(
        r"^(tapi|oke|nah)[, ]+",
        "",
        candidates[0][2],
        flags=re.IGNORECASE,
    )
    tokens = chosen.split()

    if len(tokens) > 10:
        chosen = " ".join(tokens[:10]).rstrip(".,;:") + "…"

    return chosen.upper(), "best_spoken_line"


def hook_text_with_accent(text: str, colors: dict[str, Any]) -> str:
    parts = re.split(r"(\\N|\s+)", text)
    output = []

    for part in parts:
        key = normalized_word(part)

        if part == r"\N":
            output.append(part)
        elif part.isspace():
            output.append(part)
        elif key in {"kafein", "kebanyakan", "jangan", "bahaya", "kenapa"}:
            output.append(
                "{"
                f"\\1c{colors['accent']}\\b1"
                "}"
                f"{ass_escape(part)}"
                "{"
                f"\\1c{colors['text']}\\b1"
                "}"
            )
        else:
            output.append(ass_escape(part))

    return "".join(output)


def choose_alternate_zone(
    candidates: list[dict[str, Any]],
    reserved_zone: str | None,
) -> dict[str, Any]:
    if not reserved_zone:
        return candidates[0]

    available = [item for item in candidates if item["name"] != reserved_zone]
    if reserved_zone in {"atas", "bawah"}:
        side = [item for item in available if item["name"] in {"kiri", "kanan"}]
        if side:
            return side[0]
    return available[0] if available else candidates[0]


def build_motion_plan(
    transcript: dict[str, Any],
    hook_end: float,
) -> list[dict[str, Any]]:
    motions = [{
        "start": 0.0,
        "end": round(max(1.2, hook_end), 3),
        "type": "soft_punch",
        "amount": 0.058,
        "reason": "opening_headline",
    }]

    # Keywords that warrant punch-in (important statements, numbers, transitions)
    punch_keywords = {"30", "persen", "satu bulan", "sebulan", "breakout",
                       "review", "brand", "market", "passion", "youtube",
                       "skincare", "bisnis", "launching", "prove"}

    for segment in transcript.get("segments", []):
        text = str(
            segment.get("text_corrected")
            or segment.get("text_original")
            or segment.get("text")
            or ""
        )
        lower = text.casefold()

        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start + 1.5))

        if start < hook_end:
            continue

        # Punch on questions
        if "?" in text:
            motions.append({
                "start": round(start, 3),
                "end": round(min(end, start + 2.2), 3),
                "type": "soft_punch",
                "amount": 0.048,
                "reason": "spoken_question",
            })
            continue

        # Punch on keyword sentences
        if any(kw in lower for kw in punch_keywords):
            motions.append({
                "start": round(start, 3),
                "end": round(min(end, start + 2.0), 3),
                "type": "soft_punch",
                "amount": 0.055,
                "reason": "keyword_emphasis",
            })

    # Slow push for calm segments — add a gentle continuous zoom
    # where no punch is scheduled for >6 seconds
    if motions:
        last_end = max(m["end"] for m in motions)
        if last_end < hook_end + 6:
            motions.append({
                "start": round(last_end + 1.0, 3),
                "end": round(last_end + 6.0, 3),
                "type": "soft_punch",
                "amount": 0.025,
                "reason": "slow_push_calm",
            })

    return motions


def create_ass(
    video_path: Path,
    transcript_path: Path,
    output_path: Path,
) -> None:
    assert_required_fonts(
        PROJECT_ROOT / "fonts" / "font-manifest.json"
    )

    transcript = json.loads(
        transcript_path.read_text(encoding="utf-8-sig")
    )
    chunks, corrections = build_caption_chunks(transcript)

    if not chunks:
        raise RuntimeError(
            "Tidak ada teks yang dapat dijadikan subtitle. "
            "Pastikan transcript memiliki segments dan words."
        )

    analyzer = FrameAnalyzer(video_path)
    events: list[str] = []
    analysis_log: list[dict[str, Any]] = []
    duration = float(transcript.get("duration") or chunks[-1]["end"])
    hook_text, hook_source = choose_hook_text(transcript)
    hook_start = 0.08
    hook_end = min(max(2.7, float(chunks[0]["end"])), 3.2, duration)
    hook_zone: str | None = None

    try:
        _, hook_candidates = analyzer.analyze(hook_start, hook_end)
        top_or_bottom = [
            item
            for item in hook_candidates
            if item["name"] in {"atas", "bawah"}
        ]
        hook_selected = min(
            top_or_bottom or hook_candidates,
            key=lambda item: float(item["score"]),
        )
        hook_zone = str(hook_selected["name"])
        hook_colors = choose_colors(
            float(hook_selected["brightness"]),
            tuple(hook_selected["sampled_bgr"]),
        )
        hook_y = round(PLAY_RES_Y * float(hook_selected["y_ratio"]))

        # --- Two-tier headline: kicker + headline text with subject highlight ---
        headline = transcript.get("headline") or {}
        if isinstance(headline, dict) and headline.get("text"):
            kicker_text = str(headline.get("kicker") or "SIMAK SAMPAI AKHIR...").upper()
            headline_text = str(headline.get("text") or hook_text).upper()
            subject = str(headline.get("subject") or "").upper()
            highlight_kw = str(headline.get("highlight") or subject).upper()
            headline_duration = float(headline.get("duration") or 4.0)
            headline_start = 0.08
            headline_end = min(headline_start + max(3.5, headline_duration), 5.0)
            kicker_end = min(headline_start + max(2.5, headline_duration * 0.65), headline_end - 0.5)

            # Safe ASS colors with &H...& format
            accent_col = safe_ass_color("&H00FFD900&")  # vivid cyan highlight
            text_col = safe_ass_color("&H00F6F6F6&")
            outline_col = safe_ass_color("&H00141414&")
            # Main headline event (layer 3); kicker disabled by campaign layout preset.
            clean_main = sanitize_transcript_text(headline_text)
            clean_highlight = sanitize_transcript_text(highlight_kw) if highlight_kw else ""
            main_base_override = rf"{{\an5\move({PLAY_RES_X // 2},{hook_y + 20},{PLAY_RES_X // 2},{hook_y},0,280)\fad(200,300)\fscx92\fscy92\t(0,280,\fscx100\fscy100)\1c{text_col}\3c{outline_col}\bord3.0\shad0\blur0.5}}"
            
            if clean_highlight and clean_highlight in clean_main:
                if clean_main.startswith(clean_highlight):
                    remainder = clean_main[len(clean_highlight):].strip()
                    rem_words = remainder.split()
                    if len(rem_words) > 3:
                        mid = len(rem_words) // 2
                        rem_formatted = " ".join(rem_words[:mid]) + r"\N" + " ".join(rem_words[mid:])
                    else:
                        rem_formatted = remainder
                    main_override = rf"{{\an5\move({PLAY_RES_X // 2},{hook_y + 20},{PLAY_RES_X // 2},{hook_y},0,280)\fad(200,300)\fscx92\fscy92\t(0,280,\fscx100\fscy100)\1c{accent_col}\3c{outline_col}\bord3.0\shad0\blur0.5}}"
                    color_switch = rf"{{\1c{text_col}}}"
                    main_dialogue = f"{main_override}{clean_highlight}\\N{color_switch}{rem_formatted}"
                else:
                    parts = clean_main.split(clean_highlight, 1)
                    before = sanitize_transcript_text(parts[0])
                    after = sanitize_transcript_text(parts[1])
                    main_dialogue = (
                        f"{main_base_override}{before}"
                        rf"{{\1c{accent_col}}}{clean_highlight}"
                        rf"{{\1c{text_col}}}{after}"
                    )
            else:
                words = clean_main.split()
                mid = len(words) // 2
                line1 = " ".join(words[:mid])
                line2 = " ".join(words[mid:])
                main_dialogue = f"{main_base_override}{line1}\\N{line2}"

            events.append(
                "Dialogue: 3,"
                f"{ass_time(headline_start)},"
                f"{ass_time(headline_end)},"
                "HeadlineMain,,0,0,0,,"
                f"{main_dialogue}"
            )

            hook_end = headline_end
        else:
            clean_hook = sanitize_transcript_text(hook_text)
            hook_animation = rf"{{\an5\move({PLAY_RES_X // 2},{hook_y + 14},{PLAY_RES_X // 2},{hook_y},0,280)\fad(120,260)\fscx94\fscy94\t(0,280,\fscx100\fscy100)\1c{safe_ass_color(hook_colors['text'])}\3c{safe_ass_color(hook_colors['outline'])}\bord3.2\shad0\blur0.5}}"
            hook_rendered = hook_text_with_accent(clean_hook, hook_colors)
            events.append(
                "Dialogue: 3,"
                f"{ass_time(hook_start)},"
                f"{ass_time(hook_end)},"
                "Hook,,0,0,0,,"
                f"{hook_animation}{hook_rendered}"
            )
        analyzer.previous_candidate = None

        for chunk in chunks:
            selected, candidates = analyzer.analyze(
                float(chunk["start"]),
                float(chunk["end"]),
            )

            if float(chunk["start"]) < hook_end:
                selected = choose_alternate_zone(candidates, hook_zone)
                analyzer.previous_candidate = str(selected["name"])

            colors = choose_colors(
                float(selected["brightness"]),
                tuple(selected["sampled_bgr"]),
            )
            position_x = round(PLAY_RES_X * float(selected.get("x_ratio", 0.5)))
            position_y = round(
                PLAY_RES_Y * float(selected["y_ratio"])
            )
            start_y = position_y + 9
            alignment = int(selected.get("alignment", 5))
            text = build_active_word_text(chunk, colors)
            animation = (
                rf"{{\an{alignment}"
                f"\\move({position_x},{start_y},"
                f"{position_x},{position_y},0,190)"
                r"\fad(110,170)"
                r"\fscx97\fscy97"
                r"\t(0,190,\fscx100\fscy100)"
                f"\\1c{colors['text']}"
                f"\\3c{colors['outline']}"
                r"\bord2.6\shad0\blur0.4}"
            )
            events.append(
                "Dialogue: 1,"
                f"{ass_time(float(chunk['start']))},"
                f"{ass_time(float(chunk['end']))},"
                "Cinematic,,0,0,0,,"
                f"{animation}{text}"
            )

            low_confidence = [
                {
                    "word": str(item.get("word", "")),
                    "probability": round(float(item["probability"]), 4),
                }
                for item in chunk["words"]
                if item.get("probability") is not None
                and float(item["probability"]) < 0.68
            ]
            analysis_log.append({
                "segment_id": chunk["segment_id"],
                "start": round(float(chunk["start"]), 3),
                "end": round(float(chunk["end"]), 3),
                "text": chunk["text"],
                "position": selected["name"],
                "x_ratio": round(float(selected.get("x_ratio", 0.5)), 4),
                "y_ratio": round(float(selected.get("y_ratio", 0.5)), 4),
                "alignment": int(selected.get("alignment", 5)),
                "brightness": round(float(selected["brightness"]), 2),
                "text_color": colors["text"],
                "accent_color": colors["accent"],
                "accent_bgr": colors["accent_bgr"],
                "low_confidence_words": low_confidence,
                "candidate_scores": {
                    str(item["name"]): round(float(item["score"]), 4)
                    for item in candidates
                },
            })
    finally:
        analyzer.close()

    ass_header = f"""[Script Info]
Title: Hermes Professional Dynamic Captions
ScriptType: v4.00+
PlayResX: {PLAY_RES_X}
PlayResY: {PLAY_RES_Y}
ScaledBorderAndShadow: yes
WrapStyle: 2
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cinematic,Segoe UI Semibold,38,&H00F6F6F6,&H00F6F6F6,&H00141414,&H00000000,-1,0,0,0,100,100,0.2,0,1,2.6,0,2,58,58,102,1
Style: Hook,Segoe UI Semibold,50,&H00F6F6F6,&H00F6F6F6,&H00141414,&H00000000,-1,0,0,0,100,100,0.4,0,1,3.2,0,8,58,58,102,1
Style: HeadlineMain,Segoe UI Semibold,48,&H00F6F6F6,&H00F6F6F6,&H00141414,&H00000000,-1,0,0,0,100,100,0.3,0,1,3.0,0,8,58,58,102,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    ass_content = ass_header + "\n".join(events) + "\n"
    validate_ass_content(ass_content, str(output_path))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        ass_content,
        encoding="utf-8-sig",
    )

    analysis_path = output_path.with_suffix(".analysis.json")
    analysis_payload = {
        "schema_version": 2,
        "hook": {
            "text": hook_text.replace(r"\N", " "),
            "start": hook_start,
            "end": round(hook_end, 3),
            "position": hook_zone,
            "source": hook_source,
        },
        "corrections_applied": corrections,
        "captions": analysis_log,
    }
    analysis_path.write_text(
        json.dumps(analysis_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    edit_plan_path = output_path.with_suffix(".edit-plan.json")
    edit_plan = {
        "schema_version": 1,
        "layout_preset": "campaign-safe-zones-v3",
        "safe_margin_ratio": 0.08,
        "video_duration": round(duration, 3),
        "hook": analysis_payload["hook"],
        "video_motion": build_motion_plan(transcript, hook_end),
        "transition": {
            "fade_in_seconds": 0.12,
            "fade_out_seconds": 0.18,
        },
        "audio": {
            "target_lufs": -16,
            "true_peak_db": -1.5,
        },
        "review": {
            "low_confidence_threshold": 0.68,
            "low_confidence_words": [
                {
                    "start": item["start"],
                    "text": item["text"],
                    "words": item["low_confidence_words"],
                }
                for item in analysis_log
                if item["low_confidence_words"]
            ],
            "corrections_applied": corrections,
        },
    }
    edit_plan_path.write_text(
        json.dumps(edit_plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    printable_hook = hook_text.replace(r"\N", " / ")
    print("Subtitle profesional berhasil dibuat.")
    print(f"Jumlah tampilan : {len(chunks)}")
    print(f"Hook            : {printable_hook}")
    print(f"Koreksi otomatis: {len(corrections)}")
    print(f"File ASS        : {output_path}")
    print(f"Analisis        : {analysis_path}")
    print(f"Rencana edit    : {edit_plan_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Membuat subtitle ASS profesional dengan hook, warna adaptif, "
            "penempatan dinamis, dan animasi kata aktif."
        )
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    create_ass(
        args.video.resolve(),
        args.transcript.resolve(),
        args.output.resolve(),
    )


if __name__ == "__main__":
    main()
