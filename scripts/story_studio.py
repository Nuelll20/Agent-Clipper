#!/usr/bin/env python3
"""Local multi-agent story development with review-gated artifacts."""

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

from core.artifacts import CONTRACT_SCHEMA_VERSION
from core.observation import RunObserver
from core.story import (
    FixtureStoryProvider,
    OpenAICompatibleStoryProvider,
    StoryError,
    StoryGateError,
    StoryPipeline,
    assert_story_approved,
    review_story,
    safe_story_id,
    validate_story_brief,
    validate_story_revision,
    write_story_brief,
)
from validators.artifact_validator import ArtifactValidationError, load_json_object


def default_output_root() -> Path:
    configured = os.environ.get("HERMES_STORY_ROOT", "").strip()
    return Path(configured).expanduser() if configured else PROJECT_ROOT / "stories"


def default_event_db() -> Path:
    configured = os.environ.get("HERMES_EVENT_DB", "").strip()
    return Path(configured).expanduser() if configured else PROJECT_ROOT / "state" / "production-events.db"


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def command_init(args: argparse.Namespace) -> int:
    story_id = safe_story_id(args.story)
    payload = {
        "artifact_type": "story-brief",
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "story_id": story_id,
        "title": args.title.strip(),
        "premise": args.premise.strip(),
        "language": args.language.strip(),
        "audience": args.audience.strip(),
        "format": args.format.strip(),
        "target_duration_seconds": args.duration,
        "constraints": list(dict.fromkeys(item.strip() for item in args.constraint if item.strip())),
    }
    destination = (
        Path(args.output).expanduser().resolve()
        if args.output
        else Path(args.output_root).expanduser().resolve() / story_id / "brief.json"
    )
    write_story_brief(destination, payload)
    print_json({"story_id": story_id, "brief_path": str(destination), "brief": payload})
    return 0


def build_provider(args: argparse.Namespace):
    if args.provider == "fixture":
        return FixtureStoryProvider()
    model = str(args.model or os.environ.get("HERMES_LLM_MODEL", "")).strip()
    if not model:
        raise StoryError(
            "Model wajib untuk provider openai-compatible; gunakan --model atau HERMES_LLM_MODEL."
        )
    return OpenAICompatibleStoryProvider(
        base_url=args.base_url,
        model=model,
        api_key=os.environ.get(args.api_key_env, ""),
        timeout_seconds=args.timeout_seconds,
    )


def command_develop(args: argparse.Namespace) -> int:
    brief = validate_story_brief(load_json_object(args.brief, "Story brief"))
    requested_story_id = safe_story_id(args.story_id)
    if requested_story_id != brief["story_id"]:
        raise StoryError("--story-id harus sama dengan story_id di dalam brief.")
    provider = build_provider(args)
    pipeline = StoryPipeline(args.output_root, provider)
    observer = RunObserver(
        Path(args.event_db).expanduser().resolve(),
        requested_story_id,
        "STORY_DEVELOPMENT",
        task_id=args.task_id,
        agent_id="story-supervisor",
    )
    observer.current = 0
    observer.total = 6

    def on_stage(stage: str, agent_id: str, current: int, total: int, reused: bool) -> None:
        observer.current = current
        observer.total = total
        action = "Reused validated" if reused else "Generated and validated"
        observer.emit(
            stage,
            f"{action} {stage} artifact",
            state="WORKING",
            agent_id=agent_id,
            unit="stages",
        )

    try:
        result = pipeline.run(
            args.brief,
            revision_id=args.revision_id or args.task_id,
            on_stage=on_stage,
        )
        observer.emit(
            "review_required",
            "Story artifacts complete; human review required before production",
            state="COMPLETED",
            agent_id="story-supervisor",
            unit="stages",
        )
    except StoryGateError as exc:
        observer.emit(
            "review_required",
            str(exc),
            state="REVIEWING",
            agent_id="story-supervisor",
            unit="stages",
        )
        raise
    except Exception as exc:
        observer.emit(
            "failed",
            f"Story development failed: {type(exc).__name__}",
            state="FAILED",
            agent_id="story-supervisor",
            unit="stages",
        )
        raise
    finally:
        observer.close()
    print_json({
        "story_id": result.story_id,
        "revision_id": result.revision_id,
        "status": result.manifest["status"],
        "manifest_path": str(result.manifest_path),
    })
    return 0


def command_validate(args: argparse.Namespace) -> int:
    manifest = validate_story_revision(args.manifest)
    print_json({
        "valid": True,
        "story_id": manifest["story_id"],
        "revision_id": manifest["revision_id"],
        "status": manifest["status"],
    })
    return 0


def command_review(args: argparse.Namespace) -> int:
    decision = "APPROVED" if args.decision == "approve" else "CHANGES_REQUESTED"
    review, review_path = review_story(
        args.manifest,
        decision=decision,
        reviewer=args.reviewer,
        note=args.note,
    )
    observer = RunObserver(
        Path(args.event_db).expanduser().resolve(),
        review["story_id"],
        "STORY_REVIEW",
        task_id=args.task_id,
        agent_id="human-review",
    )
    observer.emit(
        "approved" if review["decision"] == "APPROVED" else "changes_requested",
        f"Story revision {review['decision'].lower()} by {review['reviewer']}",
        state="COMPLETED",
        agent_id="human-review",
    )
    observer.close()
    print_json({"review_path": str(review_path), "review": review})
    return 0


def command_approval_check(args: argparse.Namespace) -> int:
    review = assert_story_approved(args.manifest)
    print_json({
        "approved": True,
        "story_id": review["story_id"],
        "revision_id": review["revision_id"],
        "reviewer": review["reviewer"],
        "created_at": review["created_at"],
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local multi-agent story pipeline untuk Hermes Animation Studio.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="Buat story brief tervalidasi")
    initialize.add_argument("--story", required=True)
    initialize.add_argument("--title", required=True)
    initialize.add_argument("--premise", required=True)
    initialize.add_argument("--language", default="id")
    initialize.add_argument("--audience", default="general")
    initialize.add_argument("--format", default="short-animation")
    initialize.add_argument("--duration", type=float, default=60)
    initialize.add_argument("--constraint", action="append", default=[])
    initialize.add_argument("--output")
    initialize.add_argument("--output-root", default=str(default_output_root()))
    initialize.set_defaults(handler=command_init)

    develop = subparsers.add_parser("develop", help="Jalankan enam agent story")
    develop.add_argument("--brief", required=True)
    develop.add_argument("--story-id", required=True, help="Identitas observasi; harus sama dengan brief")
    develop.add_argument("--revision-id")
    develop.add_argument(
        "--provider",
        choices=["openai-compatible", "fixture"],
        default="openai-compatible",
    )
    develop.add_argument(
        "--base-url",
        default=os.environ.get("HERMES_LLM_BASE_URL", "http://127.0.0.1:20128/v1"),
    )
    develop.add_argument("--model")
    develop.add_argument("--api-key-env", default="HERMES_LLM_API_KEY")
    develop.add_argument("--timeout-seconds", type=float, default=180)
    develop.add_argument("--output-root", default=str(default_output_root()))
    develop.add_argument("--event-db", default=str(default_event_db()))
    develop.add_argument("--task-id")
    develop.set_defaults(handler=command_develop)

    validate = subparsers.add_parser("validate", help="Validasi seluruh lineage revisi")
    validate.add_argument("--manifest", required=True)
    validate.set_defaults(handler=command_validate)

    review = subparsers.add_parser("review", help="Catat approval atau permintaan perubahan")
    review.add_argument("--manifest", required=True)
    review.add_argument("--decision", choices=["approve", "request-changes"], required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--note", default="")
    review.add_argument("--event-db", default=str(default_event_db()))
    review.add_argument("--task-id")
    review.set_defaults(handler=command_review)

    approval = subparsers.add_parser(
        "approval-check",
        help="Fail-closed bila revisi belum disetujui untuk produksi",
    )
    approval.add_argument("--manifest", required=True)
    approval.set_defaults(handler=command_approval_check)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args) or 0)
    except StoryGateError as exc:
        print(f"REVIEW_REQUIRED: {exc}", file=sys.stderr)
        return 3
    except (StoryError, ArtifactValidationError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStory pipeline dihentikan oleh operator.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
