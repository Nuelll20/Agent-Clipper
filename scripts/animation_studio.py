#!/usr/bin/env python3
"""Prepare and generate shot-level animation, voice, and SFX assets."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.observation import RunObserver
from core.production import (
    ASSET_ORDER,
    AssetPipeline,
    AssetProviderError,
    ProductionError,
    ProductionGateError,
    create_voice_cast,
    prepare_asset_plan,
    validate_asset_production,
)
from core.story import validate_story_revision
from validators.artifact_validator import ArtifactValidationError, load_json_object


def default_production_root() -> Path:
    configured = os.environ.get("HERMES_PRODUCTION_ROOT", "").strip()
    return Path(configured).expanduser() if configured else PROJECT_ROOT / "productions"


def default_event_db() -> Path:
    configured = os.environ.get("HERMES_EVENT_DB", "").strip()
    return Path(configured).expanduser() if configured else PROJECT_ROOT / "state" / "production-events.db"


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _fixture_assignments(manifest_path: Path) -> dict[str, dict[str, Any]]:
    manifest = validate_story_revision(manifest_path)
    universe_entry = next(item for item in manifest["artifacts"] if item["stage"] == "universe")
    universe = load_json_object(manifest_path.parent / universe_entry["path"], "Universe")
    return {
        character["id"]: {
            "provider": "fixture",
            "provider_voice_id": "fixture:" + character["id"],
            "language": "id",
            "style": "neutral",
        }
        for character in universe["content"]["characters"]
    }


def command_cast(args: argparse.Namespace) -> int:
    manifest_path = Path(args.story_manifest).expanduser().resolve()
    if args.auto_fixture:
        assignments = _fixture_assignments(manifest_path)
    else:
        if not args.assignments:
            raise ProductionGateError("Gunakan --assignments atau --auto-fixture.")
        assignments = load_json_object(args.assignments, "Voice assignments")
    output = create_voice_cast(manifest_path, assignments, args.output)
    print_json({"voice_cast_path": str(output), "characters": sorted(assignments)})
    return 0


def command_prepare(args: argparse.Namespace) -> int:
    if args.provider == "command" and not args.provider_config:
        raise ProductionGateError("Provider command memerlukan --provider-config.")
    plan = prepare_asset_plan(
        args.story_manifest,
        args.voice_cast,
        production_root=args.production_root,
        provider_mode=args.provider,
        provider_config_path=args.provider_config,
        production_id=args.production_id,
        width=args.width,
        height=args.height,
        fps=args.fps,
    )
    print_json({"asset_plan_path": str(plan)})
    return 0


def command_generate(args: argparse.Namespace) -> int:
    pipeline = AssetPipeline(args.plan)
    selected_shots = list(dict.fromkeys(args.shot or [])) or None
    selected_assets = list(dict.fromkeys(args.asset or [])) or None
    observer = RunObserver(
        Path(args.event_db).expanduser().resolve(),
        pipeline.plan["story_id"],
        "ASSET_GENERATION",
        task_id=args.task_id,
        agent_id="asset-supervisor",
    )

    def on_asset(
        shot_id: str,
        kind: str,
        agent_id: str,
        current: int,
        total: int,
        reused: bool,
    ) -> None:
        observer.current = current
        observer.total = total
        observer.emit(
            f"{kind}:{shot_id}",
            ("Reused validated" if reused else "Processed") + f" {kind} for {shot_id}",
            state="WORKING",
            agent_id=agent_id,
            unit="assets",
        )

    try:
        result = pipeline.generate(
            shot_ids=selected_shots,
            kinds=selected_assets,
            force=args.force,
            regeneration_key=args.regeneration_key,
            continue_on_error=args.continue_on_error,
            on_asset=on_asset,
        )
        if result.failures:
            observer.emit(
                "failed",
                f"Asset generation completed with {len(result.failures)} failure(s)",
                state="FAILED",
                agent_id="asset-supervisor",
                unit="assets",
            )
        else:
            observer.emit(
                "completed",
                "Selected assets generated or safely reused",
                state="COMPLETED",
                agent_id="asset-supervisor",
                unit="assets",
            )
    except Exception as exc:
        observer.emit(
            "failed",
            f"Asset generation failed: {type(exc).__name__}",
            state="FAILED",
            agent_id="asset-supervisor",
            unit="assets",
        )
        raise
    finally:
        observer.close()
    print_json({
        "manifest_path": str(result.manifest_path),
        "status": result.manifest["status"],
        "generated": result.generated,
        "reused": result.reused,
        "failures": list(result.failures),
    })
    return 1 if result.failures else 0


def command_validate(args: argparse.Namespace) -> int:
    manifest = validate_asset_production(args.plan, require_complete=args.require_complete)
    print_json({
        "valid": True,
        "status": manifest["status"],
        "story_id": manifest["story_id"],
        "revision_id": manifest["revision_id"],
        "production_id": manifest["production_id"],
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Shot-level assets untuk Hermes Animation Studio.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    cast = subparsers.add_parser("cast", help="Kunci identitas suara karakter")
    cast.add_argument("--story-manifest", required=True)
    cast.add_argument("--assignments")
    cast.add_argument("--auto-fixture", action="store_true")
    cast.add_argument("--output", required=True)
    cast.set_defaults(handler=command_cast)

    prepare = subparsers.add_parser("prepare", help="Buat asset plan per shot")
    prepare.add_argument("--story-manifest", required=True)
    prepare.add_argument("--voice-cast", required=True)
    prepare.add_argument("--production-root", default=str(default_production_root()))
    prepare.add_argument("--production-id")
    prepare.add_argument("--provider", choices=["fixture", "command"], default="command")
    prepare.add_argument("--provider-config")
    prepare.add_argument("--width", type=int, default=720)
    prepare.add_argument("--height", type=int, default=1280)
    prepare.add_argument("--fps", type=float, default=30)
    prepare.set_defaults(handler=command_prepare)

    generate = subparsers.add_parser("generate", help="Generate atau regenerate asset terpilih")
    generate.add_argument("--plan", required=True)
    generate.add_argument("--shot", action="append")
    generate.add_argument("--asset", action="append", choices=list(ASSET_ORDER))
    generate.add_argument("--force", action="store_true")
    generate.add_argument("--regeneration-key")
    generate.add_argument("--continue-on-error", action="store_true")
    generate.add_argument("--event-db", default=str(default_event_db()))
    generate.add_argument("--task-id")
    generate.set_defaults(handler=command_generate)

    validate = subparsers.add_parser("validate", help="Validasi lineage dan output asset")
    validate.add_argument("--plan", required=True)
    validate.add_argument("--require-complete", action="store_true")
    validate.set_defaults(handler=command_validate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args) or 0)
    except (ProductionError, ArtifactValidationError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAsset pipeline dihentikan oleh operator.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
