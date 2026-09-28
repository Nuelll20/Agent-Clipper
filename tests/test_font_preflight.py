import json
import os
from pathlib import Path

import pytest

from validators.font_validator import validate_font_manifest


def write_manifest(path: Path, filename: str) -> None:
    payload = {
        "schema_version": "1.0",
        "fonts": [
            {
                "family": "Test Font",
                "required": True,
                "candidate_files": [filename],
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_existing_font_passes(tmp_path):
    font_file = tmp_path / "example.ttf"
    font_file.write_bytes(b"test-font")

    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, font_file.name)

    result = validate_font_manifest(manifest, [tmp_path])

    assert result["valid"] is True
    assert result["resolved"]


def test_missing_required_font_is_blocked(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, "missing-font.ttf")

    result = validate_font_manifest(manifest, [tmp_path])

    assert result["valid"] is False
    assert "REQUIRED_FONT_MISSING:Test Font" in result["issues"]


@pytest.mark.skipif(
    os.name != "nt",
    reason="Hermes production runtime menggunakan Windows",
)
def test_hermes_required_font_is_installed():
    result = validate_font_manifest(
        Path("fonts/font-manifest.json")
    )

    assert result["valid"] is True, result["issues"]
