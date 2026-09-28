from pathlib import Path

from scripts.motioncraft_bridge import (
    OPENING_RESERVED_UNTIL,
    assign_semantic_placements,
    normalize_visual_events,
    semantic_events,
)


def test_semantic_overlay_avoids_active_caption_zone():
    events = [{
        "event_id": "semantic-01",
        "start": 5.8,
        "end": 7.0,
        "anchor": 6.0,
    }]
    tracks = [{
        "time": 0.0,
        "face": {
            "x": 0.32,
            "y": 0.16,
            "width": 0.36,
            "height": 0.38,
        },
        "free_zone": "bottom",
        "detected": True,
    }]
    subtitle_analysis = {
        "captions": [{
            "start": 5.5,
            "end": 6.5,
            "position": "bawah",
        }]
    }

    placed = assign_semantic_placements(
        events,
        tracks,
        subtitle_analysis,
    )

    assert placed[0]["avoids_caption_zone"] == "bottom"
    assert placed[0]["placement_zone"] != "bottom"


def test_semantic_events_respect_opening_reservation():
    transcript = {
        "segments": [{
            "id": 1,
            "start": 0.0,
            "end": 8.0,
            "text": "Ini bahaya besar untuk bisnis",
            "words": [{
                "word": "bahaya",
                "start": 1.0,
                "end": 1.4,
            }],
        }]
    }

    events = semantic_events(
        transcript,
        {"events": []},
        {"events": []},
        8.0,
    )

    assert events
    assert all(
        event["start"] >= OPENING_RESERVED_UNTIL
        for event in events
    )


def test_visual_events_respect_opening_reservation():
    events = normalize_visual_events(
        {
            "events": [{
                "event_id": "visual-01",
                "start": 0.0,
                "end": 8.0,
                "asset_path": "example.png",
            }]
        },
        8.0,
    )

    assert events
    assert events[0]["start"] >= OPENING_RESERVED_UNTIL


def test_renderer_has_defensive_opening_gate():
    source = Path(
        "motioncraft-renderer/src/hermes/"
        "HermesSemanticMotion.tsx"
    ).read_text(encoding="utf-8")

    assert "const openingReserved =" in source
    assert "!openingReserved && !activeVisual" in source
    assert "event.placement_zone ||" in source
