#!/usr/bin/env python3
"""Campaign briefing, asset composition, and compliance gate for podcast clips."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
OUTPUT_WIDTH = 720
OUTPUT_HEIGHT = 1280
COMPONENT_TYPES = {
    "image",
    "image_overlay",
    "video",
    "video_overlay",
    "audio",
    "music",
    "intro",
    "outro",
}
APPLY_MODES = {"every_clip", "clip_ids", "at_least_once", "at_least_n_clips"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
FONT_EXTENSIONS = {".ttf", ".otf", ".ttc"}


class CampaignError(RuntimeError):
    """Expected campaign configuration or compliance failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    if not path.is_file():
        raise CampaignError(f"File tidak ditemukan: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise CampaignError(f"JSON tidak valid pada {path.name}: {exc}") from None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_program(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise CampaignError(f"Program tidak ditemukan di PATH: {name}")
    return resolved


def run_process(
    command: list[str],
    *,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def ffprobe_payload(path: Path) -> dict[str, Any]:
    ffprobe = require_program("ffprobe")
    result = run_process(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,width,height:format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise CampaignError(
            f"Tidak dapat membaca media {path.name}: "
            + (result.stderr or result.stdout or "unknown error")[-1000:]
        )
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        raise CampaignError(f"Output ffprobe tidak valid untuk {path.name}.") from None


def media_duration(path: Path) -> float:
    payload = ffprobe_payload(path)
    return float((payload.get("format") or {}).get("duration") or 0.0)


def has_stream(path: Path, stream_type: str) -> bool:
    return any(
        str(stream.get("codec_type")) == stream_type
        for stream in ffprobe_payload(path).get("streams") or []
    )


def normalize_text(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^0-9a-zà-ÿ]+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def transcript_text(payload: dict[str, Any]) -> str:
    values = []
    for segment in payload.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        values.append(
            str(
                segment.get("text_corrected")
                or segment.get("text")
                or segment.get("text_original")
                or ""
            )
        )
    return " ".join(values).strip()


def sliced_text(
    full_transcript: dict[str, Any],
    start: float,
    end: float,
) -> str:
    values = []
    for segment in full_transcript.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        segment_start = float(segment.get("start", 0.0))
        segment_end = float(segment.get("end", segment_start))
        if segment_end <= start or segment_start >= end:
            continue
        values.append(
            str(
                segment.get("text_corrected")
                or segment.get("text")
                or segment.get("text_original")
                or ""
            )
        )
    return " ".join(values).strip()


def keyword_match(text: str, keywords: Iterable[Any], match_mode: str = "any") -> bool:
    haystack = normalize_text(text)
    needles = [normalize_text(item) for item in keywords if normalize_text(item)]
    if not needles:
        return False
    matches = [needle in haystack for needle in needles]
    return all(matches) if match_mode == "all" else any(matches)


def safe_identifier(value: Any, label: str) -> str:
    identifier = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-.")
    if not identifier:
        raise CampaignError(f"{label} tidak valid.")
    return identifier[:80]


def string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise CampaignError(f"{label} harus berupa daftar.")
    return [str(item).strip() for item in value if str(item).strip()]


def resolve_asset(campaign_path: Path, raw_path: Any) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        raise CampaignError("Komponen campaign belum memiliki file.")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = campaign_path.parent / candidate
    return candidate.resolve()


def normalize_component(
    raw: Any,
    campaign_path: Path,
    index: int,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CampaignError(f"required_components #{index} harus berupa object.")
    component_id = safe_identifier(raw.get("id") or f"component-{index:02d}", "Component ID")
    component_type = str(raw.get("type") or "image_overlay").casefold()
    aliases = {"image": "image_overlay", "video": "video_overlay", "music": "audio"}
    canonical_type = aliases.get(component_type, component_type)
    if component_type not in COMPONENT_TYPES:
        raise CampaignError(f"Tipe komponen {component_id} tidak didukung: {component_type}")
    apply_mode = str(raw.get("apply") or "every_clip").casefold()
    if apply_mode not in APPLY_MODES:
        raise CampaignError(f"Mode apply {component_id} tidak valid: {apply_mode}")
    asset = resolve_asset(campaign_path, raw.get("file"))
    if not asset.is_file():
        raise CampaignError(f"Aset {component_id} tidak ditemukan: {asset}")
    suffix = asset.suffix.casefold()
    expected_extensions = (
        IMAGE_EXTENSIONS
        if canonical_type == "image_overlay"
        else AUDIO_EXTENSIONS
        if canonical_type == "audio"
        else VIDEO_EXTENSIONS
    )
    if suffix not in expected_extensions:
        raise CampaignError(
            f"Format aset {component_id} tidak sesuai tipe {component_type}: {suffix or '(tanpa ekstensi)'}"
        )
    clip_ids = string_list(raw.get("clip_ids"), f"{component_id}.clip_ids")
    minimum_clips = int(raw.get("minimum_clips") or (1 if apply_mode == "at_least_once" else 0))
    if apply_mode == "at_least_n_clips" and minimum_clips < 1:
        raise CampaignError(f"{component_id}.minimum_clips minimal 1.")
    return {
        **raw,
        "id": component_id,
        "type": canonical_type,
        "source_type": component_type,
        "file": str(asset),
        "apply": apply_mode,
        "clip_ids": clip_ids,
        "minimum_clips": minimum_clips,
        "required": bool(raw.get("required", True)),
        "asset_size": asset.stat().st_size,
        "asset_mtime_ns": asset.stat().st_mtime_ns,
    }


def normalize_content_requirement(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CampaignError(f"content_requirements #{index} harus berupa object.")
    point_id = safe_identifier(raw.get("id") or f"point-{index:02d}", "Brief point ID")
    keywords = string_list(raw.get("keywords"), f"{point_id}.keywords")
    if not keywords:
        raise CampaignError(f"{point_id} belum memiliki keywords untuk verifikasi.")
    apply_mode = str(raw.get("apply") or "at_least_once").casefold()
    if apply_mode not in APPLY_MODES:
        raise CampaignError(f"Mode apply brief point {point_id} tidak valid.")
    match_mode = str(raw.get("match") or "any").casefold()
    if match_mode not in {"any", "all"}:
        raise CampaignError(f"{point_id}.match harus any atau all.")
    minimum_clips = int(raw.get("minimum_clips") or (1 if apply_mode == "at_least_once" else 0))
    return {
        **raw,
        "id": point_id,
        "description": str(raw.get("description") or point_id).strip(),
        "keywords": keywords,
        "match": match_mode,
        "apply": apply_mode,
        "clip_ids": string_list(raw.get("clip_ids"), f"{point_id}.clip_ids"),
        "minimum_clips": minimum_clips,
        "required": bool(raw.get("required", True)),
    }


def normalize_campaign(campaign_path: Path) -> dict[str, Any]:
    raw = read_json(campaign_path)
    if not isinstance(raw, dict):
        raise CampaignError("Root campaign.json harus berupa object.")
    campaign_id = safe_identifier(raw.get("campaign_id") or campaign_path.parent.name, "Campaign ID")
    components = [
        normalize_component(item, campaign_path, index)
        for index, item in enumerate(raw.get("required_components") or [], start=1)
    ]
    component_ids = [item["id"] for item in components]
    if len(component_ids) != len(set(component_ids)):
        raise CampaignError("Component ID pada campaign harus unik.")
    points = [
        normalize_content_requirement(item, index)
        for index, item in enumerate(raw.get("content_requirements") or [], start=1)
    ]
    point_ids = [item["id"] for item in points]
    if len(point_ids) != len(set(point_ids)):
        raise CampaignError("Brief point ID pada campaign harus unik.")
    caption_rules = raw.get("caption_rules") or {}
    if not isinstance(caption_rules, dict):
        raise CampaignError("caption_rules harus berupa object.")
    brief_value = str(raw.get("brief_file") or "brief.md").strip()
    brief_path = Path(brief_value).expanduser()
    if not brief_path.is_absolute():
        brief_path = campaign_path.parent / brief_path
    brief_path = brief_path.resolve()
    if not brief_path.is_file():
        raise CampaignError(f"Brief campaign tidak ditemukan: {brief_path}")
    visual_identity = raw.get("visual_identity") or {}
    if not isinstance(visual_identity, dict):
        raise CampaignError("visual_identity harus berupa object.")
    font_file_value = str(visual_identity.get("font_file") or "").strip()
    if font_file_value:
        font_file = Path(font_file_value).expanduser()
        if not font_file.is_absolute():
            font_file = campaign_path.parent / font_file
        font_file = font_file.resolve()
        if not font_file.is_file() or font_file.suffix.casefold() not in FONT_EXTENSIONS:
            raise CampaignError(f"File font campaign tidak valid: {font_file}")
        visual_identity = {
            **visual_identity,
            "font_file": str(font_file),
            "fonts_dir": str(font_file.parent),
        }
        if not str(visual_identity.get("font_name") or "").strip():
            raise CampaignError("visual_identity.font_name wajib diisi ketika font_file digunakan.")
    hex_to_ass(visual_identity.get("primary_color"), "visual_identity.primary_color")
    hex_to_ass(visual_identity.get("outline_color"), "visual_identity.outline_color")
    return {
        **raw,
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "campaign_name": str(raw.get("campaign_name") or campaign_id).strip(),
        "brief_file": str(brief_path),
        "brief_sha256": file_sha256(brief_path),
        "visual_identity": visual_identity,
        "required_components": components,
        "content_requirements": points,
        "caption_rules": {
            **caption_rules,
            "required_text": string_list(caption_rules.get("required_text"), "caption_rules.required_text"),
            "required_hashtags": string_list(
                caption_rules.get("required_hashtags"),
                "caption_rules.required_hashtags",
            ),
        },
        "forbidden_phrases": string_list(raw.get("forbidden_phrases"), "forbidden_phrases"),
    }


def validate_plan_shape(plan: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(plan, dict):
        raise CampaignError("Root clip-plan.json harus berupa object.")
    clips = plan.get("clips")
    if not isinstance(clips, list) or not clips:
        raise CampaignError("clip-plan.json belum memiliki clips.")
    normalized = []
    ids = []
    for index, raw in enumerate(clips, start=1):
        if not isinstance(raw, dict):
            raise CampaignError(f"Clip #{index} harus berupa object.")
        clip_id = safe_identifier(raw.get("id") or f"clip-{index:02d}", "Clip ID")
        if clip_id in ids:
            raise CampaignError(f"Clip ID duplikat: {clip_id}")
        ids.append(clip_id)
        normalized.append({**raw, "id": clip_id})
    return normalized, ids


def explicit_or_default_targets(
    rule: dict[str, Any],
    clip_ids: list[str],
) -> list[str]:
    explicit = list(rule.get("clip_ids") or [])
    unknown = sorted(set(explicit) - set(clip_ids))
    if unknown:
        raise CampaignError(
            f"{rule['id']} merujuk clip yang tidak ada: {', '.join(unknown)}"
        )
    mode = rule["apply"]
    if mode == "every_clip":
        return list(clip_ids)
    if mode == "clip_ids":
        if not explicit:
            raise CampaignError(f"{rule['id']} menggunakan clip_ids tetapi daftarnya kosong.")
        return explicit
    candidates = explicit or list(clip_ids)
    count = 1 if mode == "at_least_once" else int(rule.get("minimum_clips") or 1)
    if count > len(candidates):
        raise CampaignError(
            f"{rule['id']} memerlukan {count} klip tetapi kandidat hanya {len(candidates)}."
        )
    return candidates[:count]


def point_targets(
    point: dict[str, Any],
    clip_ids: list[str],
    clip_texts: dict[str, str],
) -> list[str]:
    explicit = list(point.get("clip_ids") or [])
    unknown = sorted(set(explicit) - set(clip_ids))
    if unknown:
        raise CampaignError(
            f"{point['id']} merujuk clip yang tidak ada: {', '.join(unknown)}"
        )
    candidates = explicit or list(clip_ids)
    matched = [
        clip_id
        for clip_id in candidates
        if keyword_match(clip_texts.get(clip_id, ""), point["keywords"], point["match"])
    ]
    if point["apply"] == "every_clip":
        return list(clip_ids) if len(matched) == len(clip_ids) else matched
    if point["apply"] == "clip_ids":
        return matched
    count = 1 if point["apply"] == "at_least_once" else int(point.get("minimum_clips") or 1)
    return matched[:count]


def check_result(
    check_id: str,
    passed: bool,
    message: str,
    *,
    required: bool = True,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "passed": bool(passed),
        "required": bool(required),
        "message": message,
    }


def preflight(
    campaign_path: Path,
    plan_path: Path,
    assignment_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    campaign = normalize_campaign(campaign_path)
    plan = read_json(plan_path)
    clips, clip_ids = validate_plan_shape(plan)
    transcript_path = Path(str(plan.get("transcript_path") or "")).expanduser()
    if not transcript_path.is_absolute():
        transcript_path = plan_path.parent / transcript_path
    full_transcript = read_json(transcript_path.resolve())
    if not isinstance(full_transcript, dict):
        raise CampaignError("Transkrip sumber harus berupa object.")

    clip_texts = {
        clip["id"]: sliced_text(
            full_transcript,
            float(clip.get("start", 0.0)),
            float(clip.get("end", 0.0)),
        )
        for clip in clips
    }
    assignments = {
        clip_id: {"components": [], "brief_points": []}
        for clip_id in clip_ids
    }
    checks: list[dict[str, Any]] = []

    for component in campaign["required_components"]:
        targets = explicit_or_default_targets(component, clip_ids)
        for clip_id in targets:
            assignments[clip_id]["components"].append(component)
        checks.append(
            check_result(
                f"component:{component['id']}",
                bool(targets),
                f"{component['id']} dijadwalkan pada {len(targets)} klip.",
                required=component["required"],
            )
        )

    point_map = {point["id"]: point for point in campaign["content_requirements"]}
    for point in campaign["content_requirements"]:
        targets = point_targets(point, clip_ids, clip_texts)
        expected_count = (
            len(clip_ids)
            if point["apply"] == "every_clip"
            else len(point.get("clip_ids") or [])
            if point["apply"] == "clip_ids"
            else 1
            if point["apply"] == "at_least_once"
            else int(point.get("minimum_clips") or 1)
        )
        passed = len(targets) >= expected_count
        for clip_id in targets:
            assignments[clip_id]["brief_points"].append(point)
        checks.append(
            check_result(
                f"brief-point:{point['id']}",
                passed,
                (
                    f"{point['description']}: ditemukan pada "
                    f"{len(targets)}/{expected_count} klip yang diwajibkan."
                ),
                required=point["required"],
            )
        )

    for clip in clips:
        requested_points = string_list(
            clip.get("brief_points"),
            f"{clip['id']}.brief_points",
        )
        for point_id in requested_points:
            point = point_map.get(point_id)
            if point is None:
                checks.append(
                    check_result(
                        f"clip-point:{clip['id']}:{point_id}",
                        False,
                        f"{clip['id']} meminta brief point yang tidak terdaftar: {point_id}",
                    )
                )
                continue
            passed = keyword_match(
                clip_texts[clip["id"]],
                point["keywords"],
                point["match"],
            )
            if passed and not any(
                assigned["id"] == point_id
                for assigned in assignments[clip["id"]]["brief_points"]
            ):
                assignments[clip["id"]]["brief_points"].append(point)
            checks.append(
                check_result(
                    f"clip-point:{clip['id']}:{point_id}",
                    passed,
                    f"{point_id} {'terverifikasi' if passed else 'tidak ditemukan'} dalam ucapan {clip['id']}.",
                )
            )

    for clip_id, text in clip_texts.items():
        normalized = normalize_text(text)
        for phrase in campaign["forbidden_phrases"]:
            needle = normalize_text(phrase)
            found = bool(needle and needle in normalized)
            checks.append(
                check_result(
                    f"forbidden:{clip_id}:{phrase}",
                    not found,
                    f"Forbidden phrase '{phrase}' {'ditemukan' if found else 'tidak ditemukan'} pada {clip_id}.",
                )
            )

    passed = all(item["passed"] or not item["required"] for item in checks)
    assignment_payload = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign["campaign_id"],
        "campaign_name": campaign["campaign_name"],
        "campaign_path": str(campaign_path),
        "campaign_sha256": file_sha256(campaign_path),
        "brief_file": campaign["brief_file"],
        "brief_sha256": campaign["brief_sha256"],
        "plan_path": str(plan_path),
        "generated_at": utc_now(),
        "clips": assignments,
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "stage": "preflight",
        "campaign_id": campaign["campaign_id"],
        "status": "passed" if passed else "failed",
        "passed": passed,
        "checks_passed": sum(1 for item in checks if item["passed"]),
        "checks_total": len(checks),
        "checks": checks,
        "assignment_path": str(assignment_path),
        "generated_at": utc_now(),
    }
    write_json(assignment_path, assignment_payload)
    write_json(report_path, report)
    return report


def timing(component: dict[str, Any], duration: float) -> tuple[float, float]:
    raw_duration = component.get("duration")
    shown_for = max(0.15, float(raw_duration)) if raw_duration not in {None, ""} else 0.0
    show_at = str(component.get("show_at") or "").casefold()
    if show_at in {"ending", "end"}:
        end = duration
        start = max(0.0, end - (shown_for or 2.5))
        return start, end
    start = max(0.0, float(component.get("start") or 0.0))
    raw_end = component.get("end", "full")
    if isinstance(raw_end, str) and raw_end.casefold() == "full":
        end = duration
    elif raw_end is not None:
        end = min(duration, float(raw_end))
    elif shown_for:
        end = min(duration, start + shown_for)
    else:
        end = duration
    if show_at in {"opening", "start"}:
        start = 0.0
        end = min(duration, shown_for or end)
    if end <= start:
        end = min(duration, start + max(shown_for, 0.15))
    return start, end


def hex_to_ass(value: Any, label: str) -> str | None:
    text = str(value or "").strip().lstrip("#")
    if not text:
        return None
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", text):
        raise CampaignError(f"{label} harus berupa warna hex RRGGBB.")
    red, green, blue = text[0:2], text[2:4], text[4:6]
    return f"&H00{blue.upper()}{green.upper()}{red.upper()}"


def style_subtitle(
    campaign_path: Path,
    subtitle_path: Path,
    edit_plan_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    campaign = normalize_campaign(campaign_path)
    visual = campaign.get("visual_identity") or {}
    font_name = str(visual.get("font_name") or "").strip()
    primary = hex_to_ass(visual.get("primary_color"), "visual_identity.primary_color")
    outline = hex_to_ass(visual.get("outline_color"), "visual_identity.outline_color")
    subtitle_text = subtitle_path.read_text(encoding="utf-8-sig")
    output_lines = []
    styles_changed = 0
    for line in subtitle_text.splitlines():
        if line.startswith("Style:"):
            fields = line.split(",")
            if len(fields) >= 6:
                if font_name:
                    fields[1] = font_name
                if primary:
                    fields[3] = primary
                    fields[4] = primary
                if outline:
                    fields[5] = outline
                line = ",".join(fields)
                styles_changed += 1
        output_lines.append(line)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8-sig")

    edit_plan = read_json(edit_plan_path)
    if not isinstance(edit_plan, dict):
        raise CampaignError("Subtitle edit plan harus berupa object.")
    if visual.get("fonts_dir"):
        edit_plan["fonts_dir"] = str(visual["fonts_dir"])
    edit_plan["campaign_visual_identity"] = {
        "font_name": font_name or None,
        "primary_color": primary,
        "outline_color": outline,
    }
    write_json(edit_plan_path, edit_plan)
    return {
        "campaign_id": campaign["campaign_id"],
        "styles_changed": styles_changed,
        "font_name": font_name or None,
        "primary_color": primary,
        "outline_color": outline,
        "fonts_dir": visual.get("fonts_dir"),
        "output": str(output_path),
    }


def overlay_position(component: dict[str, Any]) -> tuple[str, str]:
    position = str(component.get("position") or "top-right").casefold()
    margin = max(0, min(180, int(component.get("margin") or 36)))
    left = str(margin)
    center_x = "(W-w)/2"
    right = f"W-w-{margin}"
    top = str(margin)
    center_y = "(H-h)/2"
    bottom = f"H-h-{margin}"
    mapping = {
        "top-left": (left, top),
        "top-center": (center_x, top),
        "top-right": (right, top),
        "center-left": (left, center_y),
        "center": (center_x, center_y),
        "center-right": (right, center_y),
        "bottom-left": (left, bottom),
        "bottom-center": (center_x, bottom),
        "bottom-right": (right, bottom),
    }
    if position not in mapping:
        raise CampaignError(f"Posisi overlay tidak didukung: {position}")
    return mapping[position]


def audio_stream_exists(path: Path) -> bool:
    return has_stream(path, "audio")


def video_codec_arguments(use_nvenc: bool) -> list[str]:
    if use_nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "19"]


def supports_nvenc(ffmpeg: str) -> bool:
    result = run_process([ffmpeg, "-hide_banner", "-encoders"])
    return result.returncode == 0 and "h264_nvenc" in ((result.stdout or "") + (result.stderr or ""))


def execute_ffmpeg(command_factory: Any) -> None:
    ffmpeg = require_program("ffmpeg")
    use_nvenc = supports_nvenc(ffmpeg)
    command = command_factory(ffmpeg, use_nvenc)
    result = run_process(command)
    if result.returncode != 0 and use_nvenc:
        command = command_factory(ffmpeg, False)
        result = run_process(command)
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout or "unknown error")[-5000:]
        raise CampaignError("FFmpeg campaign gagal:\n" + diagnostic)


def render_core(
    input_video: Path,
    components: list[dict[str, Any]],
    output_video: Path,
) -> list[dict[str, Any]]:
    duration = media_duration(input_video)
    overlays = [item for item in components if item["type"] in {"image_overlay", "video_overlay"}]
    audio_components = [item for item in components if item["type"] == "audio"]
    if not overlays and not audio_components:
        shutil.copy2(input_video, output_video)
        return []

    def command_factory(ffmpeg: str, use_nvenc: bool) -> list[str]:
        command = [ffmpeg, "-y", "-hide_banner", "-i", str(input_video)]
        input_indexes: dict[str, int] = {}
        next_index = 1
        for component in overlays:
            if component["type"] == "image_overlay":
                command.extend(["-loop", "1", "-i", component["file"]])
            else:
                command.extend(["-stream_loop", "-1", "-i", component["file"]])
            input_indexes[component["id"]] = next_index
            next_index += 1
        for component in audio_components:
            command.extend(["-stream_loop", "-1", "-i", component["file"]])
            input_indexes[component["id"]] = next_index
            next_index += 1

        filters = ["[0:v]setpts=PTS-STARTPTS[video0]"]
        current_video = "video0"
        for index, component in enumerate(overlays, start=1):
            asset_index = input_indexes[component["id"]]
            start, end = timing(component, duration)
            width_percent = max(4.0, min(100.0, float(component.get("width_percent") or 18.0)))
            width = max(32, round(OUTPUT_WIDTH * width_percent / 100.0))
            opacity = max(0.05, min(1.0, float(component.get("opacity", 1.0))))
            x, y = overlay_position(component)
            filters.append(
                f"[{asset_index}:v]scale={width}:-1,format=rgba,"
                f"colorchannelmixer=aa={opacity:.3f},setpts=PTS-STARTPTS[asset{index}]"
            )
            next_video = f"video{index}"
            filters.append(
                f"[{current_video}][asset{index}]overlay=x='{x}':y='{y}':"
                f"enable='between(t,{start:.3f},{end:.3f})':eof_action=pass:shortest=0[{next_video}]"
            )
            current_video = next_video

        has_base_audio = audio_stream_exists(input_video)
        if has_base_audio:
            filters.append("[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[voice]")
        else:
            filters.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={duration:.3f}[voice]"
            )
        audio_labels = ["[voice]"]
        for index, component in enumerate(audio_components, start=1):
            asset_index = input_indexes[component["id"]]
            start, end = timing(component, duration)
            gain = max(-40.0, min(-8.0, float(component.get("volume_db", -24.0))))
            delay_ms = round(start * 1000)
            span = max(0.15, end - start)
            label = f"campaignaudio{index}"
            filters.append(
                f"[{asset_index}:a]atrim=duration={span:.3f},asetpts=PTS-STARTPTS,"
                f"volume={gain:.1f}dB,adelay={delay_ms}:all=1[{label}]"
            )
            audio_labels.append(f"[{label}]")
        filters.append(
            "".join(audio_labels)
            + f"amix=inputs={len(audio_labels)}:duration=first:dropout_transition=0,"
            + "loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
        )
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                f"[{current_video}]",
                "-map",
                "[aout]",
            ]
        )
        command.extend(video_codec_arguments(use_nvenc))
        command.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-t",
                f"{duration:.3f}",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output_video),
            ]
        )
        return command

    execute_ffmpeg(command_factory)
    return [
        {
            "id": item["id"],
            "type": item["type"],
            "file": item["file"],
            "start": round(timing(item, duration)[0], 3),
            "end": round(timing(item, duration)[1], 3),
        }
        for item in [*overlays, *audio_components]
    ]


def concat_bumpers(
    core_video: Path,
    intros: list[dict[str, Any]],
    outros: list[dict[str, Any]],
    output_video: Path,
) -> list[dict[str, Any]]:
    ordered = [*intros, {"id": "__core__", "file": str(core_video), "type": "core"}, *outros]
    if len(ordered) == 1:
        shutil.copy2(core_video, output_video)
        return []
    durations = []
    for item in ordered:
        source_duration = media_duration(Path(item["file"]))
        requested = float(item.get("duration") or source_duration)
        durations.append(max(0.1, min(source_duration, requested)))

    def command_factory(ffmpeg: str, use_nvenc: bool) -> list[str]:
        command = [ffmpeg, "-y", "-hide_banner"]
        for item in ordered:
            command.extend(["-i", str(item["file"])])
        filters: list[str] = []
        concat_labels: list[str] = []
        for index, (item, duration) in enumerate(zip(ordered, durations)):
            filters.append(
                f"[{index}:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},fps=30,setsar=1,format=yuv420p,"
                f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[v{index}]"
            )
            if audio_stream_exists(Path(item["file"])):
                filters.append(
                    f"[{index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[a{index}]"
                )
            else:
                filters.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=duration={duration:.3f}[a{index}]"
                )
            concat_labels.extend([f"[v{index}]", f"[a{index}]"])
        filters.append(
            "".join(concat_labels)
            + f"concat=n={len(ordered)}:v=1:a=1[vout][aout]"
        )
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
            ]
        )
        command.extend(video_codec_arguments(use_nvenc))
        command.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output_video),
            ]
        )
        return command

    execute_ffmpeg(command_factory)
    return [
        {
            "id": item["id"],
            "type": item["type"],
            "file": item["file"],
            "duration": round(duration, 3),
        }
        for item, duration in zip(ordered, durations)
        if item["id"] != "__core__"
    ]


def render_campaign(
    campaign_path: Path,
    assignment_path: Path,
    clip_id: str,
    input_video: Path,
    output_video: Path,
    applied_path: Path,
) -> dict[str, Any]:
    campaign = normalize_campaign(campaign_path)
    assignment = read_json(assignment_path)
    if assignment.get("campaign_sha256") != file_sha256(campaign_path):
        raise CampaignError(
            "campaign.json berubah setelah preflight. Jalankan validate atau campaign check kembali."
        )
    if assignment.get("brief_sha256") != campaign.get("brief_sha256"):
        raise CampaignError(
            "Brief campaign berubah setelah preflight. Perbarui campaign.json lalu jalankan campaign check kembali."
        )
    clips = assignment.get("clips") or {}
    if clip_id not in clips:
        raise CampaignError(f"Assignment campaign tidak memiliki {clip_id}.")
    components = clips[clip_id].get("components") or []
    current_components = {
        item["id"]: item for item in campaign["required_components"]
    }
    for component in components:
        current = current_components.get(str(component.get("id")))
        if current is None:
            raise CampaignError(
                f"Komponen {component.get('id')} tidak lagi tersedia di campaign.json."
            )
        if (
            int(current.get("asset_size") or -1) != int(component.get("asset_size") or -2)
            or int(current.get("asset_mtime_ns") or -1)
            != int(component.get("asset_mtime_ns") or -2)
        ):
            raise CampaignError(
                f"Aset {component.get('id')} berubah setelah preflight. Jalankan campaign check kembali."
            )
    intros = [item for item in components if item.get("type") == "intro"]
    outros = [item for item in components if item.get("type") == "outro"]
    core_components = [item for item in components if item.get("type") not in {"intro", "outro"}]
    output_video.parent.mkdir(parents=True, exist_ok=True)
    core_path = output_video.with_name(output_video.stem + ".campaign-core.tmp.mp4")
    applied = []
    try:
        applied.extend(render_core(input_video, core_components, core_path))
        applied.extend(concat_bumpers(core_path, intros, outros, output_video))
    finally:
        core_path.unlink(missing_ok=True)
    if not output_video.is_file() or output_video.stat().st_size == 0:
        raise CampaignError("Render campaign selesai tanpa video output yang valid.")
    expected_ids = [str(item.get("id")) for item in components]
    applied_ids = [str(item.get("id")) for item in applied]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign["campaign_id"],
        "clip_id": clip_id,
        "expected_component_ids": expected_ids,
        "applied_component_ids": applied_ids,
        "components": applied,
        "output_video": str(output_video),
        "rendered_at": utc_now(),
    }
    write_json(applied_path, payload)
    return payload


def audit_clip(
    campaign_path: Path,
    assignment_path: Path,
    clip_id: str,
    transcript_path: Path,
    caption_path: Path,
    video_path: Path,
    applied_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    campaign = normalize_campaign(campaign_path)
    assignment = read_json(assignment_path)
    if assignment.get("campaign_sha256") != file_sha256(campaign_path):
        raise CampaignError(
            "campaign.json berubah setelah preflight. Jalankan validate atau campaign check kembali."
        )
    if assignment.get("brief_sha256") != campaign.get("brief_sha256"):
        raise CampaignError(
            "Brief campaign berubah setelah preflight. Jalankan campaign check kembali."
        )
    clip_assignment = (assignment.get("clips") or {}).get(clip_id)
    if not isinstance(clip_assignment, dict):
        raise CampaignError(f"Assignment campaign tidak memiliki {clip_id}.")
    applied = read_json(applied_path)
    transcript = read_json(transcript_path)
    if not isinstance(transcript, dict):
        raise CampaignError("Transkrip klip harus berupa object.")
    caption = caption_path.read_text(encoding="utf-8-sig") if caption_path.is_file() else ""
    spoken = transcript_text(transcript)
    checks: list[dict[str, Any]] = []

    expected_components = {
        str(item.get("id")): item
        for item in clip_assignment.get("components") or []
        if isinstance(item, dict)
    }
    applied_ids = set(str(item) for item in applied.get("applied_component_ids") or [])
    for component_id, component in expected_components.items():
        present = component_id in applied_ids
        checks.append(
            check_result(
                f"component:{component_id}",
                present,
                f"Komponen {component_id} {'terpasang' if present else 'tidak terpasang'} pada video.",
                required=bool(component.get("required", True)),
            )
        )

    for point in clip_assignment.get("brief_points") or []:
        if not isinstance(point, dict):
            continue
        matched = keyword_match(spoken, point.get("keywords") or [], str(point.get("match") or "any"))
        checks.append(
            check_result(
                f"brief-point:{point.get('id')}",
                matched,
                f"Poin {point.get('id')} {'terverifikasi' if matched else 'tidak ditemukan'} dalam ucapan.",
                required=bool(point.get("required", True)),
            )
        )

    normalized_caption = normalize_text(caption)
    for index, text in enumerate(campaign["caption_rules"]["required_text"], start=1):
        present = normalize_text(text) in normalized_caption
        checks.append(
            check_result(
                f"caption-text:{index}",
                present,
                f"Teks caption wajib {'tersedia' if present else 'belum tersedia'}: {text}",
            )
        )
    caption_casefold = caption.casefold()
    for hashtag in campaign["caption_rules"]["required_hashtags"]:
        value = hashtag if hashtag.startswith("#") else "#" + hashtag
        present = value.casefold() in caption_casefold
        checks.append(
            check_result(
                f"hashtag:{value}",
                present,
                f"Hashtag {value} {'tersedia' if present else 'belum tersedia'}.",
            )
        )

    video_valid = video_path.is_file() and video_path.stat().st_size > 0
    duration = media_duration(video_path) if video_valid else 0.0
    checks.append(
        check_result(
            "output-video",
            video_valid and duration > 0,
            f"Video output {'valid' if video_valid and duration > 0 else 'tidak valid'} ({duration:.2f} detik).",
        )
    )
    passed = all(item["passed"] or not item["required"] for item in checks)
    report = {
        "schema_version": SCHEMA_VERSION,
        "stage": "clip-audit",
        "campaign_id": campaign["campaign_id"],
        "campaign_name": campaign["campaign_name"],
        "clip_id": clip_id,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "checks_passed": sum(1 for item in checks if item["passed"]),
        "checks_total": len(checks),
        "checks": checks,
        "audited_at": utc_now(),
    }
    write_json(report_path, report)
    return report


def template_payload(name: str) -> dict[str, Any]:
    campaign_id = safe_identifier(name, "Campaign name").casefold()
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "campaign_name": name,
        "brief_file": "brief.md",
        "visual_identity": {
            "font_name": "Segoe UI Semibold",
            "font_file": "",
            "primary_color": "F6F6F6",
            "outline_color": "141414"
        },
        "required_components": [
            {
                "id": "brand-logo",
                "type": "image_overlay",
                "file": "assets/logo.png",
                "apply": "every_clip",
                "required": True,
                "position": "top-right",
                "width_percent": 16,
                "opacity": 0.9,
                "margin": 36,
                "start": 0,
                "end": "full",
            }
        ],
        "content_requirements": [
            {
                "id": "main-message",
                "description": "Pesan utama campaign",
                "keywords": ["GANTI DENGAN KATA KUNCI DARI BRIEF"],
                "match": "any",
                "apply": "at_least_once",
                "required": True,
            }
        ],
        "caption_rules": {
            "required_text": [],
            "required_hashtags": [],
        },
        "forbidden_phrases": [],
    }


def command_init(args: argparse.Namespace) -> int:
    root = Path(args.directory).expanduser().resolve()
    assets = root / "assets"
    root.mkdir(parents=True, exist_ok=True)
    assets.mkdir(parents=True, exist_ok=True)
    campaign_path = root / "campaign.json"
    brief_path = root / "brief.md"
    if (campaign_path.exists() or brief_path.exists()) and not args.force:
        raise CampaignError("Folder campaign sudah berisi campaign.json atau brief.md. Gunakan --force untuk mengganti.")
    write_json(campaign_path, template_payload(args.name))
    brief_path.write_text(
        "# Brief Campaign\n\n"
        "Tempel briefing asli di sini. Jelaskan tujuan, audiens, poin wajib, larangan, "
        "gaya editing, CTA, caption, hashtag, dan aturan penggunaan setiap aset.\n",
        encoding="utf-8",
    )
    print("Template campaign berhasil dibuat.")
    print(f"Brief   : {brief_path}")
    print(f"Config  : {campaign_path}")
    print(f"Assets  : {assets}")
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    report = preflight(
        Path(args.campaign).expanduser().resolve(),
        Path(args.plan).expanduser().resolve(),
        Path(args.assignment).expanduser().resolve(),
        Path(args.report).expanduser().resolve(),
    )
    print(f"Campaign preflight: {report['status'].upper()}")
    print(f"Checks: {report['checks_passed']}/{report['checks_total']}")
    print(f"Report: {Path(args.report).expanduser().resolve()}")
    if not report["passed"]:
        for check in report["checks"]:
            if check["required"] and not check["passed"]:
                print(f"- GAGAL: {check['message']}")
        return 1
    return 0


def command_style(args: argparse.Namespace) -> int:
    result = style_subtitle(
        Path(args.campaign).expanduser().resolve(),
        Path(args.subtitle).expanduser().resolve(),
        Path(args.edit_plan).expanduser().resolve(),
        Path(args.output).expanduser().resolve(),
    )
    print("Visual identity campaign diterapkan pada subtitle.")
    print(f"Style diubah: {result['styles_changed']}")
    print(f"Output: {result['output']}")
    return 0


def command_render(args: argparse.Namespace) -> int:
    payload = render_campaign(
        Path(args.campaign).expanduser().resolve(),
        Path(args.assignment).expanduser().resolve(),
        safe_identifier(args.clip, "Clip ID"),
        Path(args.input).expanduser().resolve(),
        Path(args.output).expanduser().resolve(),
        Path(args.applied).expanduser().resolve(),
    )
    print(f"Campaign render selesai: {len(payload['applied_component_ids'])} komponen.")
    print(f"Output: {payload['output_video']}")
    return 0


def command_audit(args: argparse.Namespace) -> int:
    report = audit_clip(
        Path(args.campaign).expanduser().resolve(),
        Path(args.assignment).expanduser().resolve(),
        safe_identifier(args.clip, "Clip ID"),
        Path(args.transcript).expanduser().resolve(),
        Path(args.caption).expanduser().resolve(),
        Path(args.video).expanduser().resolve(),
        Path(args.applied).expanduser().resolve(),
        Path(args.report).expanduser().resolve(),
    )
    print(f"Campaign audit {args.clip}: {report['status'].upper()}")
    print(f"Checks: {report['checks_passed']}/{report['checks_total']}")
    if not report["passed"]:
        for check in report["checks"]:
            if check["required"] and not check["passed"]:
                print(f"- GAGAL: {check['message']}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Campaign briefing, asset compositor, dan compliance gate untuk klip podcast."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Buat template folder campaign")
    init.add_argument("--directory", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=command_init)

    preflight_parser = subparsers.add_parser("preflight", help="Validasi brief, aset, dan coverage")
    preflight_parser.add_argument("--campaign", required=True)
    preflight_parser.add_argument("--plan", required=True)
    preflight_parser.add_argument("--assignment", required=True)
    preflight_parser.add_argument("--report", required=True)
    preflight_parser.set_defaults(handler=command_preflight)

    style = subparsers.add_parser("style", help="Terapkan font dan warna campaign pada subtitle")
    style.add_argument("--campaign", required=True)
    style.add_argument("--subtitle", required=True)
    style.add_argument("--edit-plan", required=True)
    style.add_argument("--output", required=True)
    style.set_defaults(handler=command_style)

    render_parser = subparsers.add_parser("render", help="Terapkan komponen campaign ke satu klip")
    render_parser.add_argument("--campaign", required=True)
    render_parser.add_argument("--assignment", required=True)
    render_parser.add_argument("--clip", required=True)
    render_parser.add_argument("--input", required=True)
    render_parser.add_argument("--output", required=True)
    render_parser.add_argument("--applied", required=True)
    render_parser.set_defaults(handler=command_render)

    audit = subparsers.add_parser("audit", help="Audit hasil akhir sebelum Telegram")
    audit.add_argument("--campaign", required=True)
    audit.add_argument("--assignment", required=True)
    audit.add_argument("--clip", required=True)
    audit.add_argument("--transcript", required=True)
    audit.add_argument("--caption", required=True)
    audit.add_argument("--video", required=True)
    audit.add_argument("--applied", required=True)
    audit.add_argument("--report", required=True)
    audit.set_defaults(handler=command_audit)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args) or 0)
    except CampaignError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nProses dihentikan oleh pengguna.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
