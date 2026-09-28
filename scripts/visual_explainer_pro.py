#!/usr/bin/env python3
"""Plan and draw selective explainer cutaways for visually quiet footage."""

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

import numpy as np
from PIL import Image, ImageDraw, ImageFont


SCHEMA_VERSION = 1
WIDTH = 720
HEIGHT = 1280
DEFAULT_ACCENT = "47D7FF"
SUPPORTED_STYLES = {"off", "subtle", "balanced", "immersive"}
SUPPORTED_TYPES = {"concept", "process", "comparison", "metric"}
STOPWORDS = {
    "ada", "adalah", "akan", "atau", "bahwa", "bisa", "dalam", "dan", "dari",
    "dengan", "di", "dia", "ini", "itu", "jadi", "juga", "kalau", "karena",
    "bagi", "justru", "ke", "kita", "lebih", "maka", "masalahnya", "mereka",
    "oleh", "pada", "saat", "saya", "sebagai", "sebuah", "sudah", "tapi",
    "tidak", "untuk", "yang",
}
MEANING_CUES = {
    "artinya", "akibat", "alasan", "beda", "caranya", "dampak", "hasil", "inti",
    "kunci", "masalah", "penting", "proses", "risiko", "solusi", "ternyata",
}


class VisualExplainerError(RuntimeError):
    """Expected visual planning or asset-generation failure."""


def read_json(path: Path) -> Any:
    if not path.is_file():
        raise VisualExplainerError(f"File tidak ditemukan: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise VisualExplainerError(f"JSON tidak valid pada {path.name}: {exc}") from None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def require_program(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise VisualExplainerError(f"Program tidak ditemukan: {name}")
    return resolved


def probe_duration(video: Path) -> float:
    result = subprocess.run(
        [
            require_program("ffprobe"), "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
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
        raise VisualExplainerError("Durasi video tidak dapat dibaca.")
    return float((result.stdout or "0").strip())


def analyze_visual_timeline(video: Path, sample_fps: float = 2.0) -> list[dict[str, float]]:
    frame_width, frame_height = 180, 320
    filter_chain = (
        f"fps={sample_fps:.3f},"
        f"scale={frame_width}:{frame_height}:force_original_aspect_ratio=decrease,"
        f"pad={frame_width}:{frame_height}:(ow-iw)/2:(oh-ih)/2,format=gray"
    )
    result = subprocess.run(
        [
            require_program("ffmpeg"), "-v", "error", "-i", str(video),
            "-an", "-vf", filter_chain, "-f", "rawvideo", "-pix_fmt", "gray",
            "pipe:1",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        diagnostic = (result.stderr or b"").decode("utf-8", errors="replace")[-2000:]
        raise VisualExplainerError("Analisis frame gagal:\n" + diagnostic)
    frame_size = frame_width * frame_height
    frame_count = len(result.stdout) // frame_size
    if frame_count == 0:
        return []
    raw = np.frombuffer(result.stdout[: frame_count * frame_size], dtype=np.uint8)
    frames = raw.reshape(frame_count, frame_height, frame_width)
    timeline: list[dict[str, float]] = []
    previous: np.ndarray | None = None
    previous_hist: np.ndarray | None = None
    for index, frame in enumerate(frames):
        working = frame.astype(np.float32)
        horizontal = np.abs(np.diff(working, axis=1))
        vertical = np.abs(np.diff(working, axis=0))
        edge_density = float(
            (np.mean(horizontal > 32.0) + np.mean(vertical > 32.0)) / 2.0
        )
        motion = 0.0
        scene_change = 0.0
        histogram, _ = np.histogram(frame, bins=32, range=(0, 256))
        histogram = histogram.astype(np.float32)
        histogram /= max(1.0, float(histogram.sum()))
        if previous is not None:
            motion = float(np.mean(np.abs(working - previous)) / 255.0)
        if previous_hist is not None:
            scene_change = float(np.sum(np.abs(histogram - previous_hist)) / 2.0)
        overlay_likelihood = min(1.0, edge_density * 5.2)
        timeline.append(
            {
                "time": round(index / sample_fps, 3),
                "motion": round(motion, 5),
                "scene_change": round(scene_change, 5),
                "overlay_likelihood": round(overlay_likelihood, 5),
            }
        )
        previous = working
        previous_hist = histogram
    return timeline


def spoken_text(segment: dict[str, Any]) -> str:
    return " ".join(
        str(
            segment.get("text_corrected")
            or segment.get("text_original")
            or segment.get("text")
            or ""
        ).split()
    )


def tokens(text: str) -> list[str]:
    return [item.casefold() for item in re.findall(r"[0-9A-Za-zÀ-ÿ%]+", text)]


def content_words(text: str) -> list[str]:
    output: list[str] = []
    for token in tokens(text):
        if token in STOPWORDS or len(token) < 3:
            continue
        if token not in output:
            output.append(token)
    return output


def values_in_range(
    timeline: list[dict[str, float]], key: str, start: float, end: float
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


def segment_plainness(
    timeline: list[dict[str, float]], start: float, end: float
) -> tuple[float, dict[str, float]]:
    motion = mean_or(values_in_range(timeline, "motion", start, end))
    overlay = mean_or(values_in_range(timeline, "overlay_likelihood", start, end))
    scene_peak = max_or(values_in_range(timeline, "scene_change", start, end))
    editing = min(
        1.0,
        min(1.0, motion * 5.8) * 0.25
        + overlay * 0.38
        + min(1.0, scene_peak * 2.1) * 0.37,
    )
    return max(0.0, 1.0 - editing), {
        "motion": round(motion, 4),
        "overlay": round(overlay, 4),
        "scene_peak": round(scene_peak, 4),
        "existing_edit": round(editing, 4),
    }


def choose_visual_type(text: str) -> str:
    lowered = text.casefold()
    if re.search(r"(?:\b\d+[.,]?\d*\s*%|\brp\s*\d|\b\d+\s*(?:kali|tahun|hari|jam))", lowered):
        return "metric"
    if re.search(r"\b(?:dibandingkan|sedangkan|sementara|versus|vs)\b", lowered) or (
        "bukan" in lowered and "tetapi" in lowered
    ):
        return "comparison"
    if re.search(r"\b(?:langkah|proses|pertama|kedua|ketiga|kemudian|selanjutnya|akhirnya)\b", lowered):
        return "process"
    return "concept"


def short_phrase(value: str, limit: int = 5) -> str:
    words = [item for item in re.findall(r"[0-9A-Za-zÀ-ÿ%]+", value) if item]
    return " ".join(words[:limit]).strip().upper()


def event_content(visual_type: str, text: str) -> dict[str, Any]:
    words = content_words(text)
    if visual_type == "metric":
        number_match = re.search(
            r"(?:Rp\s*)?\d+(?:[.,]\d+)?\s*(?:%|juta|ribu|kali|tahun|hari|jam)?",
            text,
            flags=re.IGNORECASE,
        )
        number = short_phrase(number_match.group(0), 3) if number_match else "ANGKA KUNCI"
        label_words = [word for word in words if word not in tokens(number)]
        return {
            "title": "FAKTA UTAMA",
            "number": number,
            "items": [" ".join(label_words[:5]).upper() or "POIN PENTING"],
        }
    if visual_type == "comparison":
        parts = re.split(
            r"\b(?:dibandingkan|sedangkan|sementara|versus|vs|tetapi)\b",
            text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )
        if len(parts) == 2:
            items = [short_phrase(parts[0], 5), short_phrase(parts[1], 5)]
        else:
            items = [" ".join(words[:3]).upper(), " ".join(words[3:6]).upper()]
        return {
            "title": "BEDAKAN DUA HAL INI",
            "items": [item or label for item, label in zip(items, ("OPS I", "OPS II"))],
        }
    if visual_type == "process":
        chunks = re.split(
            r"\b(?:lalu|kemudian|selanjutnya|akhirnya|pertama|kedua|ketiga)\b",
            text,
            flags=re.IGNORECASE,
        )
        items = [short_phrase(item, 4) for item in chunks if short_phrase(item, 4)]
        if len(items) < 2:
            items = [word.upper() for word in words[:3]]
        return {"title": "ALUR PENJELASAN", "items": items[:3] or ["PROSES"]}
    title = " ".join(words[:3]).upper() or "INTI PENJELASAN"
    remaining = [word.upper() for word in words[3:6]]
    return {"title": title, "items": remaining or ["PAHAMI KONSEPNYA"]}


def semantic_score(text: str) -> float:
    token_list = tokens(text)
    cue_count = sum(1 for token in token_list if token in MEANING_CUES)
    number_bonus = 0.22 if any(char.isdigit() for char in text) else 0.0
    structure_bonus = 0.18 if re.search(
        r"\b(?:karena|sehingga|artinya|tapi|pertama|kedua|solusi|masalah)\b",
        text,
        flags=re.IGNORECASE,
    ) else 0.0
    length_score = min(0.28, len(token_list) / 55.0)
    return min(1.0, cue_count * 0.16 + number_bonus + structure_bonus + length_score)


def plan_events(
    transcript: dict[str, Any],
    timeline: list[dict[str, float]],
    editorial_plan: dict[str, Any],
    duration: float,
    style: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if style == "off":
        return [], []
    candidates: list[dict[str, Any]] = []
    minimum_plainness = {"subtle": 0.72, "balanced": 0.58, "immersive": 0.46}[style]
    for index, segment in enumerate(transcript.get("segments") or [], start=1):
        start = max(0.0, float(segment.get("start", 0.0)))
        end = min(duration, float(segment.get("end", start)))
        text = spoken_text(segment)
        if start < 3.4 or end - start < 1.15 or len(tokens(text)) < 5:
            continue
        plainness, evidence = segment_plainness(timeline, start, end)
        meaning = semantic_score(text)
        if plainness < minimum_plainness:
            continue
        visual_type = choose_visual_type(text)
        content = event_content(visual_type, text)
        score = meaning * 0.60 + plainness * 0.40
        if visual_type in {"process", "comparison", "metric"}:
            score += 0.10
        event_span = {"subtle": 2.7, "balanced": 3.2, "immersive": 3.8}[style]
        center = start + (end - start) * 0.52
        event_start = max(3.4, center - event_span * 0.42)
        event_end = min(duration - 0.35, event_start + event_span)
        if event_end - event_start < 2.2:
            continue
        candidates.append(
            {
                "segment_id": segment.get("id", index),
                "start": round(event_start, 3),
                "end": round(event_end, 3),
                "type": visual_type,
                **content,
                "treatment": (
                    "cutaway" if style == "immersive" and plainness >= 0.68 else "overlay_card"
                ),
                "animation": "push" if visual_type in {"metric", "comparison"} else "slide",
                "sfx": "whoosh" if visual_type != "metric" else "impact",
                "sfx_gain_db": -22.0 if visual_type != "metric" else -23.0,
                "score": round(score, 4),
                "reason": {
                    "semantic": round(meaning, 4),
                    "plainness": round(plainness, 4),
                    **evidence,
                },
            }
        )

    density = str((editorial_plan.get("editing_density") or {}).get("classification") or "unknown")
    max_events = {"subtle": 2, "balanced": 4, "immersive": 5}[style]
    if duration < 26:
        max_events = min(max_events, 2)
    if density == "already_edited":
        max_events = min(max_events, 2)
    threshold = {"subtle": 0.60, "balanced": 0.50, "immersive": 0.42}[style]
    selected: list[dict[str, Any]] = []
    minimum_spacing = 6.0 if style != "immersive" else 5.0
    for candidate in sorted(candidates, key=lambda item: float(item["score"]), reverse=True):
        if float(candidate["score"]) < threshold:
            continue
        center = (float(candidate["start"]) + float(candidate["end"])) / 2.0
        if any(
            abs(center - (float(item["start"]) + float(item["end"])) / 2.0)
            < minimum_spacing
            for item in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= max_events:
            break
    selected.sort(key=lambda item: float(item["start"]))
    for event_index, event in enumerate(selected, start=1):
        event["event_id"] = f"visual-{event_index:02d}"
        event.pop("score", None)
    return selected, candidates


def normalize_hex(value: Any, fallback: str = DEFAULT_ACCENT) -> str:
    text = str(value or fallback).strip().lstrip("#")
    return text.upper() if re.fullmatch(r"[0-9A-Fa-f]{6}", text) else fallback


def campaign_accent(campaign_path: Path | None) -> str:
    if campaign_path is None:
        return DEFAULT_ACCENT
    payload = read_json(campaign_path)
    visual = payload.get("visual_identity") or {}
    return normalize_hex(visual.get("primary_color"), DEFAULT_ACCENT)


def rgb(value: str) -> tuple[int, int, int]:
    value = normalize_hex(value)
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def font_candidates(bold: bool) -> list[Path]:
    names = (
        ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"]
        if bold else
        ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"]
    )
    names.extend(
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold else
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf"
            if bold else
            "/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf",
        ]
    )
    return [Path(item) for item in names]


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in font_candidates(bold):
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def fit_text(draw: ImageDraw.ImageDraw, text: str, maximum_width: int, size: int, bold: bool = True):
    font = load_font(size, bold)
    while size > 24 and draw.textbbox((0, 0), text, font=font)[2] > maximum_width:
        size -= 2
        font = load_font(size, bold)
    return font


def centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    left, top, right, bottom = box
    bounds = draw.multiline_textbbox((0, 0), text, font=font, align="center", spacing=8)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.multiline_text(
        (left + (right - left - width) / 2, top + (bottom - top - height) / 2),
        text,
        font=font,
        fill=fill,
        align="center",
        spacing=8,
    )


def draw_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill: tuple[int, int, int, int],
    outline: tuple[int, int, int, int],
    radius: int = 30,
    width: int = 3,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def render_visual_asset(event: dict[str, Any], path: Path, accent_hex: str) -> None:
    accent = rgb(accent_hex)
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    treatment = str(event.get("treatment") or "overlay_card")
    if treatment == "cutaway":
        draw.rectangle((0, 0, WIDTH, HEIGHT), fill=(7, 11, 19, 255))
        for offset in range(-600, 900, 110):
            draw.line((offset, 0, offset + 600, HEIGHT), fill=(*accent, 18), width=2)
        draw.ellipse((500, -70, 850, 280), fill=(*accent, 28))
        draw.ellipse((-180, 930, 220, 1330), fill=(*accent, 22))
        panel = (42, 250, 678, 1010)
    else:
        draw_panel(draw, (32, 262, 688, 998), (8, 12, 20, 220), (*accent, 150), 38, 3)
        panel = (55, 285, 665, 975)

    title = str(event.get("title") or "INTI PENJELASAN").upper()
    title_font = fit_text(draw, title, panel[2] - panel[0] - 46, 52, True)
    centered_text(draw, (panel[0] + 20, panel[1] + 22, panel[2] - 20, panel[1] + 118), title, title_font, (245, 248, 252, 255))
    draw.rounded_rectangle(
        (panel[0] + 110, panel[1] + 126, panel[2] - 110, panel[1] + 134),
        radius=4,
        fill=(*accent, 220),
    )

    visual_type = str(event.get("type") or "concept")
    items = [str(item).upper() for item in (event.get("items") or []) if str(item).strip()]
    if visual_type == "metric":
        number = str(event.get("number") or "ANGKA KUNCI").upper()
        number_font = fit_text(draw, number, 540, 112, True)
        centered_text(draw, (panel[0] + 20, panel[1] + 180, panel[2] - 20, panel[1] + 430), number, number_font, (*accent, 255))
        label = items[0] if items else "POIN PENTING"
        label_font = fit_text(draw, label, 520, 44, True)
        centered_text(draw, (panel[0] + 45, panel[1] + 455, panel[2] - 45, panel[1] + 575), label, label_font, (241, 244, 249, 255))
        draw.arc((250, panel[1] + 615, 470, panel[1] + 835), 205, 335, fill=(*accent, 220), width=14)
        draw.polygon([(458, panel[1] + 681), (491, panel[1] + 693), (468, panel[1] + 716)], fill=(*accent, 230))
    elif visual_type == "comparison":
        comparison_items = (items + ["OPSI A", "OPSI B"])[:2]
        y_positions = (panel[1] + 190, panel[1] + 430)
        for item_index, (item, y) in enumerate(zip(comparison_items, y_positions), start=1):
            draw_panel(draw, (panel[0] + 40, y, panel[2] - 40, y + 175), (18, 24, 35, 242), (*accent, 105), 28, 2)
            badge = "A" if item_index == 1 else "B"
            draw.ellipse((panel[0] + 62, y + 53, panel[0] + 126, y + 117), fill=(*accent, 235))
            badge_font = load_font(32, True)
            centered_text(draw, (panel[0] + 62, y + 53, panel[0] + 126, y + 117), badge, badge_font, (7, 11, 19, 255))
            item_font = fit_text(draw, item, 430, 40, True)
            centered_text(draw, (panel[0] + 145, y + 18, panel[2] - 60, y + 157), item, item_font, (245, 248, 252, 255))
        versus_font = load_font(28, True)
        centered_text(draw, (300, panel[1] + 370, 420, panel[1] + 430), "VS", versus_font, (*accent, 255))
    elif visual_type == "process":
        process_items = (items + ["LANGKAH BERIKUTNYA"] * 3)[:3]
        for item_index, item in enumerate(process_items, start=1):
            y = panel[1] + 175 + (item_index - 1) * 180
            draw_panel(draw, (panel[0] + 40, y, panel[2] - 40, y + 122), (18, 24, 35, 242), (*accent, 115), 25, 2)
            draw.ellipse((panel[0] + 58, y + 29, panel[0] + 122, y + 93), fill=(*accent, 235))
            number_font = load_font(29, True)
            centered_text(draw, (panel[0] + 58, y + 29, panel[0] + 122, y + 93), str(item_index), number_font, (7, 11, 19, 255))
            item_font = fit_text(draw, item, 430, 36, True)
            centered_text(draw, (panel[0] + 145, y + 10, panel[2] - 60, y + 112), item, item_font, (245, 248, 252, 255))
            if item_index < 3:
                x = WIDTH // 2
                draw.line((x, y + 125, x, y + 167), fill=(*accent, 190), width=7)
                draw.polygon([(x - 13, y + 155), (x + 13, y + 155), (x, y + 176)], fill=(*accent, 220))
    else:
        draw.ellipse((250, panel[1] + 140, 470, panel[1] + 360), fill=(*accent, 35), outline=(*accent, 210), width=5)
        draw.ellipse((285, panel[1] + 175, 435, panel[1] + 325), fill=(17, 24, 36, 246), outline=(*accent, 130), width=3)
        badge = "".join(word[0] for word in title.split()[:2]) or "!"
        icon_font = load_font(66, True)
        centered_text(draw, (285, panel[1] + 175, 435, panel[1] + 325), badge, icon_font, (*accent, 255))
        for item_index, item in enumerate((items or ["PAHAMI KONSEPNYA"])[:3]):
            y = panel[1] + 520 + item_index * 92
            draw_panel(draw, (panel[0] + 70, y, panel[2] - 70, y + 70), (18, 24, 35, 232), (*accent, 90), 24, 2)
            item_font = fit_text(draw, item, 450, 31, True)
            centered_text(draw, (panel[0] + 90, y + 4, panel[2] - 90, y + 66), item, item_font, (245, 248, 252, 255))

    if treatment == "cutaway":
        opaque_base = Image.new("RGBA", (WIDTH, HEIGHT), (7, 11, 19, 255))
        image = Image.alpha_composite(opaque_base, image)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")


def validate_event(raw: dict[str, Any], index: int, duration: float) -> dict[str, Any]:
    start = max(0.0, float(raw.get("start", 0.0)))
    end = min(duration, float(raw.get("end", start)))
    if end - start < 1.0:
        raise VisualExplainerError(f"Visual event #{index} terlalu pendek atau tidak valid.")
    visual_type = str(raw.get("type") or "concept").casefold()
    if visual_type not in SUPPORTED_TYPES:
        raise VisualExplainerError(f"Visual type tidak didukung: {visual_type}")
    items = raw.get("items") or []
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        raise VisualExplainerError(f"Visual event #{index}.items harus berupa daftar.")
    return {
        **raw,
        "event_id": str(raw.get("event_id") or f"visual-{index:02d}"),
        "start": round(start, 3),
        "end": round(end, 3),
        "type": visual_type,
        "title": short_phrase(str(raw.get("title") or "INTI PENJELASAN"), 7),
        "items": [short_phrase(str(item), 7) for item in items if str(item).strip()][:3],
        "treatment": str(raw.get("treatment") or "overlay_card"),
        "animation": str(raw.get("animation") or "slide"),
        "sfx": str(raw.get("sfx") or "whoosh"),
        "sfx_gain_db": max(-32.0, min(-14.0, float(raw.get("sfx_gain_db", -22.0)))),
    }


def build_assets(plan_path: Path, asset_dir: Path, campaign_path: Path | None) -> dict[str, Any]:
    payload = read_json(plan_path)
    if not isinstance(payload, dict):
        raise VisualExplainerError("Root visual-plan.json harus berupa object.")
    duration = float(payload.get("duration") or 0.0)
    accent = campaign_accent(campaign_path) if campaign_path else normalize_hex(payload.get("accent_color"))
    events = []
    for index, raw in enumerate(payload.get("events") or [], start=1):
        if not isinstance(raw, dict):
            raise VisualExplainerError(f"Visual event #{index} harus berupa object.")
        event = validate_event(raw, index, duration)
        supplied_asset = str(event.get("asset_path") or "").strip()
        if str(event.get("asset_source") or "") == "generated_motion_graphic":
            supplied_asset = ""
        if supplied_asset:
            asset_path = Path(supplied_asset).expanduser()
            if not asset_path.is_absolute():
                asset_path = plan_path.parent / asset_path
            if not asset_path.is_file():
                raise VisualExplainerError(f"Aset visual tidak ditemukan: {asset_path}")
            event["asset_path"] = str(asset_path.resolve())
            event["asset_source"] = "provided"
        else:
            asset_path = asset_dir / f"{event['event_id']}.png"
            render_visual_asset(event, asset_path, accent)
            event["asset_path"] = str(asset_path.resolve())
            event["asset_source"] = "generated_motion_graphic"
        events.append(event)
    payload["events"] = events
    payload["accent_color"] = accent
    payload["asset_directory"] = str(asset_dir.resolve())
    write_json(plan_path, payload)
    return payload


def command_analyze(args: argparse.Namespace) -> int:
    style = str(args.style).casefold()
    if style not in SUPPORTED_STYLES:
        raise VisualExplainerError(f"visual_style tidak valid: {style}")
    video = Path(args.video).resolve()
    transcript = read_json(Path(args.transcript).resolve())
    editorial_plan = read_json(Path(args.editorial_plan).resolve())
    duration = probe_duration(video)
    timeline = analyze_visual_timeline(video, sample_fps=args.sample_fps) if style != "off" else []
    events, candidates = plan_events(transcript, timeline, editorial_plan, duration, style)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "style": style,
        "duration": round(duration, 3),
        "reference_profile": "selective_explainer_cutaway",
        "source_editing_density": editorial_plan.get("editing_density"),
        "policy": {
            "plain_footage_only": True,
            "minimum_spacing_seconds": 6.5 if style == "immersive" else 8.0,
            "subtitle_safe_layer": True,
            "maximum_event_count": len(events),
            "do_not_invent_claims": True,
        },
        "accent_color": campaign_accent(Path(args.campaign).resolve()) if args.campaign else DEFAULT_ACCENT,
        "events": events,
        "candidate_count": len(candidates),
    }
    output = Path(args.output).resolve()
    write_json(output, payload)
    build_assets(
        output,
        Path(args.assets).resolve(),
        Path(args.campaign).resolve() if args.campaign else None,
    )
    print("Analisis visual explainer selesai.")
    print(f"Mode            : {style}")
    print(f"Visual cutaway  : {len(events)} event")
    print(f"Rencana         : {output}")
    return 0


def command_build(args: argparse.Namespace) -> int:
    payload = build_assets(
        Path(args.plan).resolve(),
        Path(args.assets).resolve(),
        Path(args.campaign).resolve() if args.campaign else None,
    )
    print("Aset visual explainer berhasil dibuat.")
    print(f"Jumlah event: {len(payload.get('events') or [])}")
    print(f"Folder aset : {payload.get('asset_directory')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mendeteksi footage plain dan membuat motion graphic penjelas."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="Buat visual-plan dan aset otomatis")
    analyze.add_argument("video")
    analyze.add_argument("transcript")
    analyze.add_argument("editorial_plan")
    analyze.add_argument("output")
    analyze.add_argument("--assets", required=True)
    analyze.add_argument("--style", choices=sorted(SUPPORTED_STYLES), default="balanced")
    analyze.add_argument("--sample-fps", type=float, default=2.0)
    analyze.add_argument("--campaign")
    analyze.set_defaults(handler=command_analyze)

    build = subparsers.add_parser("build", help="Bangun ulang aset dari visual-plan")
    build.add_argument("plan")
    build.add_argument("--assets", required=True)
    build.add_argument("--campaign")
    build.set_defaults(handler=command_build)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args) or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VisualExplainerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
