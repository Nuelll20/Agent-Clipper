"""Versioned artifact adapters and pipeline validation gates."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from validators.artifact_validator import ArtifactValidationError, validate_payload
from validators.sanitizer import detect_ass_leak


CONTRACT_SCHEMA_VERSION = "2.0"
PRODUCER_VERSION = "agent-clipper/2.1"
SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "artifacts" / "schemas"
LEGACY_TRANSCRIPT_VERSIONS = {None, 1, 2, "1", "2", "1.0"}


def sha256_file(path: str | Path) -> str:
    source = Path(path)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ArtifactValidationError(
            f"Sumber artifact tidak dapat dibaca: {source} ({exc})"
        ) from None
    return f"sha256:{digest.hexdigest()}"


def sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def load_schema(name: str) -> dict[str, Any]:
    path = SCHEMA_ROOT / name
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"Schema artifact rusak: {path} ({exc})") from None
    if not isinstance(schema, dict):
        raise ArtifactValidationError(f"Schema artifact harus berupa object: {path}")
    return schema


def _created_at(artifact_path: str | Path | None) -> str:
    if artifact_path is not None:
        try:
            timestamp = Path(artifact_path).stat().st_mtime
            return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat(
                timespec="seconds"
            )
        except OSError:
            pass
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ArtifactValidationError(f"{label} harus berupa angka finite.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ArtifactValidationError(f"{label} harus berupa angka finite.") from None
    if not math.isfinite(number):
        raise ArtifactValidationError(f"{label} harus berupa angka finite.")
    return number


def _segment_text(segment: dict[str, Any]) -> str:
    for key in ("text_corrected", "text_original", "text"):
        value = segment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    words = segment.get("words")
    if isinstance(words, list):
        return " ".join(
            str(word.get("word") or "").strip()
            for word in words
            if isinstance(word, dict) and str(word.get("word") or "").strip()
        ).strip()
    return ""


def adapt_transcript(
    payload: dict[str, Any],
    *,
    source_path: str | Path,
    artifact_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
    upstream_artifacts: list[str] | None = None,
) -> dict[str, Any]:
    """Upgrade supported legacy transcript shapes to the canonical v2 envelope."""
    if not isinstance(payload, dict):
        raise ArtifactValidationError("Transcript harus berupa object JSON.")

    raw_version = payload.get("schema_version")
    if raw_version == CONTRACT_SCHEMA_VERSION:
        return copy.deepcopy(payload)
    if raw_version not in LEGACY_TRANSCRIPT_VERSIONS:
        raise ArtifactValidationError(
            f"Versi transcript tidak didukung: {raw_version!r}. "
            f"Versi yang didukung: legacy 1/2 dan {CONTRACT_SCHEMA_VERSION}."
        )

    adapted = copy.deepcopy(payload)
    segments = adapted.get("segments")
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if not str(segment.get("text_original") or "").strip():
                text = _segment_text(segment)
                if text:
                    segment["text_original"] = text

    valid_segments = [segment for segment in segments or [] if isinstance(segment, dict)]
    ends: list[float] = []
    for index, segment in enumerate(valid_segments):
        try:
            ends.append(_number(segment.get("end"), f"segments[{index}].end"))
        except ArtifactValidationError:
            pass
    derived_duration = max(ends, default=0.0)
    try:
        supplied_duration = _number(adapted.get("duration"), "duration")
    except ArtifactValidationError:
        supplied_duration = 0.0

    full_text = " ".join(filter(None, (_segment_text(item) for item in valid_segments)))
    total_words = sum(
        len(item.get("words") or [])
        for item in valid_segments
        if isinstance(item.get("words"), list)
    )
    adapted.update(
        {
            "artifact_type": "transcript",
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "producer_version": PRODUCER_VERSION,
            "source_fingerprint": sha256_file(source_path),
            "config_fingerprint": sha256_json(config or {}),
            "created_at": str(adapted.get("created_at") or _created_at(artifact_path)),
            "upstream_artifacts": list(upstream_artifacts or []),
            "language": str(adapted.get("language") or (config or {}).get("language") or "unknown"),
            "duration": max(supplied_duration, derived_duration),
            "total_segments": len(valid_segments),
            "total_words": total_words,
            "full_text_original": str(adapted.get("full_text_original") or full_text),
            "contract_adapter": {
                "from_schema_version": raw_version,
                "to_schema_version": CONTRACT_SCHEMA_VERSION,
            },
        }
    )
    return adapted


def _validate_transcript_semantics(payload: dict[str, Any]) -> None:
    segments = payload["segments"]
    previous_start = -1.0
    max_end = 0.0
    for index, segment in enumerate(segments):
        start = _number(segment.get("start"), f"segments[{index}].start")
        end = _number(segment.get("end"), f"segments[{index}].end")
        if start < 0 or end <= start:
            raise ArtifactValidationError(
                f"segments[{index}] memiliki rentang waktu tidak valid: {start}-{end}."
            )
        if start < previous_start:
            raise ArtifactValidationError(
                f"segments[{index}] tidak berurutan berdasarkan waktu mulai."
            )
        previous_start = start
        max_end = max(max_end, end)

        text = _segment_text(segment)
        if not text:
            raise ArtifactValidationError(f"segments[{index}] tidak memiliki teks.")
        if detect_ass_leak(text):
            raise ArtifactValidationError(
                f"segments[{index}] mengandung raw ASS override tag."
            )
        if any(character in text for character in ("\x00", "\x1b")):
            raise ArtifactValidationError(
                f"segments[{index}] mengandung karakter kontrol terlarang."
            )

        words = segment.get("words")
        if words is None:
            continue
        previous_word_start = -1.0
        for word_index, word in enumerate(words):
            label = f"segments[{index}].words[{word_index}]"
            word_start = _number(word.get("start"), f"{label}.start")
            word_end = _number(word.get("end"), f"{label}.end")
            if word_start < 0 or word_end <= word_start:
                raise ArtifactValidationError(f"{label} memiliki rentang waktu tidak valid.")
            if word_start < previous_word_start:
                raise ArtifactValidationError(f"{label} tidak berurutan.")
            previous_word_start = word_start

    duration = _number(payload.get("duration"), "duration")
    if duration <= 0 or duration + 0.001 < max_end:
        raise ArtifactValidationError(
            f"duration {duration} tidak mencakup akhir transcript {max_end}."
        )


def validate_transcript_contract(
    payload: dict[str, Any],
    *,
    source_path: str | Path,
    artifact_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
    upstream_artifacts: list[str] | None = None,
) -> dict[str, Any]:
    adapted = adapt_transcript(
        payload,
        source_path=source_path,
        artifact_path=artifact_path,
        config=config,
        upstream_artifacts=upstream_artifacts,
    )
    validate_payload(adapted, load_schema("transcript.schema.json"), label="Transcript")
    _validate_transcript_semantics(adapted)
    return adapted


def build_job_manifest_contract(
    payload: dict[str, Any],
    *,
    source_path: str | Path,
    transcript_path: str | Path,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = copy.deepcopy(payload)
    manifest.update(
        {
            "artifact_type": "job-manifest",
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "producer_version": PRODUCER_VERSION,
            "source_fingerprint": sha256_file(source_path),
            "config_fingerprint": sha256_json(config or {}),
            "created_at": str(manifest.get("created_at") or _created_at(None)),
            "upstream_artifacts": [sha256_file(transcript_path)],
        }
    )
    validate_payload(manifest, load_schema("job-manifest.schema.json"), label="Job manifest")
    return manifest
