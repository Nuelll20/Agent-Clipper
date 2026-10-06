"""Fail-closed JSON artifact validation helpers and CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator, FormatChecker


class ArtifactValidationError(ValueError):
    """An artifact cannot safely enter the next pipeline stage."""


def load_json_object(path: str | Path, label: str = "Artifact") -> dict[str, Any]:
    artifact_path = Path(path)
    if not artifact_path.is_file():
        raise ArtifactValidationError(f"{label} tidak ditemukan: {artifact_path}")
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(
            f"{label} bukan JSON yang dapat dibaca: {artifact_path} ({exc})"
        ) from None
    if not isinstance(payload, dict):
        raise ArtifactValidationError(f"{label} harus berupa object JSON: {artifact_path}")
    return payload


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def validate_payload(
    payload: Any,
    schema: dict[str, Any],
    *,
    label: str = "Artifact",
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ArtifactValidationError(f"{label} harus berupa object JSON.")
    errors = sorted(
        Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"{_json_path(error.absolute_path)}: {error.message}"
            for error in errors[:8]
        )
        if len(errors) > 8:
            details += f"; dan {len(errors) - 8} masalah lain"
        raise ArtifactValidationError(f"{label} tidak sesuai kontrak: {details}")
    return payload


def validate_artifact(
    data_path: str | Path,
    schema_path: str | Path,
) -> dict[str, Any]:
    data = load_json_object(data_path)
    schema = load_json_object(schema_path, "Schema")
    return validate_payload(data, schema, label=str(data_path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validasi artifact JSON secara fail-closed.")
    parser.add_argument("artifact")
    parser.add_argument("schema")
    args = parser.parse_args()
    try:
        validate_artifact(args.artifact, args.schema)
    except ArtifactValidationError as exc:
        print(f"[BLOCK] {args.artifact}")
        print(exc)
        return 1
    print(f"[PASS] {args.artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
