import json
from pathlib import Path

import pytest

from scripts.make_cinematic_ass_pro import validate_ass_content
from validators.subtitle_validator import validate_segments


def dialogue(text: str) -> str:
    return (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,0:00:01.00,Cinematic,,0,0,0,,{text}\n"
    )


def test_valid_ass_passes():
    validate_ass_content(dialogue("Halo dunia"), "valid.ass")


def test_raw_ass_leak_is_blocked():
    with pytest.raises(ValueError, match="RAW ASS TAG LEAKED"):
        validate_ass_content(
            dialogue("fscx120 Halo dunia"),
            "leaked.ass",
        )


def test_unbalanced_ass_tag_is_blocked():
    with pytest.raises(ValueError, match="Unbalanced braces"):
        validate_ass_content(
            dialogue(r"{\1c&H00FFFFFF& Halo dunia"),
            "broken.ass",
        )


def test_subtitle_overlap_fixture_is_blocked():
    fixture = Path("tests/fixtures/subtitle_overlap.json")
    payload = json.loads(fixture.read_text(encoding="utf-8-sig"))

    result = validate_segments(payload["segments"])

    assert result["valid"] is False
    assert "TIMING_OVERLAP" in result["issues"]


def test_ascii_control_character_is_blocked():
    with pytest.raises(ValueError, match="ASCII control character"):
        validate_ass_content(
            dialogue("Halo " + chr(1) + "c&H00FFFFFF& dunia"),
            "control-character.ass",
        )
