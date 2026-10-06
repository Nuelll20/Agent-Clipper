#!/usr/bin/env python3
"""Local durable supervisor for Agent-Clipper tasks."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.artifacts import sha256_file, sha256_json
from core.supervisor import (
    SupervisorError,
    SupervisorStore,
    SupervisorWorker,
    TASK_STATES,
)
from core.story import safe_story_id, validate_story_brief


def default_supervisor_db() -> Path:
    configured = os.environ.get("HERMES_SUPERVISOR_DB", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else PROJECT_ROOT / "state" / "supervisor.db"
    )


def default_event_db() -> Path:
    configured = os.environ.get("HERMES_EVENT_DB", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else PROJECT_ROOT / "state" / "production-events.db"
    )


def safe_job_id(value: Any) -> str:
    job_id = str(value or "").strip()
    if not job_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", job_id):
        raise SupervisorError("job_id pada clip plan tidak valid.")
    return job_id


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise SupervisorError(f"{label} tidak ditemukan: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SupervisorError(f"{label} bukan JSON valid: {exc}") from None
    if not isinstance(value, dict):
        raise SupervisorError(f"{label} harus berupa object JSON.")
    return value


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def command_enqueue_clip_video(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).expanduser().resolve()
    plan = load_json_object(plan_path, "Clip plan")
    job_id = safe_job_id(plan.get("job_id"))
    clips = plan.get("clips")
    if not isinstance(clips, list) or not clips:
        raise SupervisorError("Clip plan belum memiliki clips untuk diantrikan.")
    clip_ids = [
        str(item.get("id") or "").strip() if isinstance(item, dict) else ""
        for item in clips
    ]
    if any(not clip_id for clip_id in clip_ids):
        raise SupervisorError("Setiap clip pada rencana harus memiliki ID.")
    available = set(clip_ids)
    if len(available) != len(clip_ids):
        raise SupervisorError("Clip plan memiliki ID clip duplikat.")
    requested = list(dict.fromkeys(args.clip or []))
    unknown = sorted(set(requested) - available)
    if unknown:
        raise SupervisorError("Clip ID tidak ada dalam rencana: " + ", ".join(unknown))
    plan_fingerprint = sha256_file(plan_path)
    payload = {
        "plan_path": str(plan_path),
        "plan_fingerprint": plan_fingerprint,
        "clip_ids": requested,
        "delivery": "not_requested",
    }
    idempotency_key = args.idempotency_key or (
        "clip-video:"
        + sha256_json({"job_id": job_id, **payload}).split(":", 1)[1]
    )
    store = SupervisorStore(args.db)
    try:
        task, created = store.enqueue(
            job_id=job_id,
            kind="CLIP_VIDEO",
            payload=payload,
            idempotency_key=idempotency_key,
            resources=["clipper-render"],
            priority=args.priority,
            max_attempts=args.max_attempts,
        )
    finally:
        store.close()
    print_json({"created": created, "task": task})
    return 0


def command_enqueue_story(args: argparse.Namespace) -> int:
    brief_path = Path(args.brief).expanduser().resolve()
    brief = validate_story_brief(load_json_object(brief_path, "Story brief"))
    story_id = safe_story_id(brief["story_id"])
    if args.provider == "openai-compatible" and not str(args.model or "").strip():
        raise SupervisorError(
            "--model wajib untuk provider openai-compatible; secret tetap dibaca worker dari env."
        )
    payload = {
        "story_id": story_id,
        "brief_path": str(brief_path),
        "brief_fingerprint": sha256_file(brief_path),
        "provider": args.provider,
        "output_root": str(Path(args.output_root).expanduser().resolve()),
    }
    if args.provider == "openai-compatible":
        payload.update({
            "base_url": args.base_url,
            "model": args.model.strip(),
            "api_key_env": args.api_key_env,
            "timeout_seconds": args.timeout_seconds,
        })
    idempotency_key = args.idempotency_key or (
        "develop-story:"
        + sha256_json(payload).split(":", 1)[1]
    )
    resources = [f"story:{story_id}"]
    if args.provider == "openai-compatible":
        resources.append("story-llm")
    store = SupervisorStore(args.db)
    try:
        task, created = store.enqueue(
            job_id=story_id,
            kind="DEVELOP_STORY",
            payload=payload,
            idempotency_key=idempotency_key,
            resources=resources,
            priority=args.priority,
            max_attempts=args.max_attempts,
        )
    finally:
        store.close()
    print_json({"created": created, "task": task})
    return 0


def command_list(args: argparse.Namespace) -> int:
    store = SupervisorStore(args.db)
    try:
        tasks = store.list_tasks(job_id=args.job, state=args.state, limit=args.limit)
    finally:
        store.close()
    print_json(tasks)
    return 0


def command_show(args: argparse.Namespace) -> int:
    store = SupervisorStore(args.db)
    try:
        task = store.get_task(args.task_id)
    finally:
        store.close()
    if task is None:
        raise SupervisorError(f"Task tidak ditemukan: {args.task_id}")
    print_json(task)
    return 0


def command_events(args: argparse.Namespace) -> int:
    store = SupervisorStore(args.db)
    try:
        events = store.read_events(
            task_id=args.task,
            after=args.after,
            limit=args.limit,
        )
    finally:
        store.close()
    print_json(events)
    return 0


def command_retry(args: argparse.Namespace) -> int:
    store = SupervisorStore(args.db)
    try:
        task = store.retry_task(args.task_id)
    finally:
        store.close()
    print_json(task)
    return 0


def command_recover(args: argparse.Namespace) -> int:
    store = SupervisorStore(args.db)
    try:
        recovered = store.recover_expired()
    finally:
        store.close()
    print_json({"recovered_task_ids": recovered})
    return 0


def command_worker(args: argparse.Namespace) -> int:
    store = SupervisorStore(args.db)
    worker = SupervisorWorker(
        store,
        project_root=PROJECT_ROOT,
        event_db=args.event_db,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        retry_delay_seconds=args.retry_delay_seconds,
    )
    try:
        while True:
            result = worker.run_once()
            print_json({
                "worker_id": worker.worker_id,
                "status": result.status,
                "task_id": result.task_id,
                "recovered_task_ids": list(result.recovered_task_ids),
            })
            if args.once:
                return 0 if result.status in {"idle", "completed"} else 1
            if result.status == "idle":
                time.sleep(args.poll_seconds)
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Durable local supervisor untuk pipeline Agent-Clipper.",
    )
    parser.add_argument("--db", default=str(default_supervisor_db()))
    parser.add_argument("--event-db", default=str(default_event_db()))
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue", help="Tambahkan task idempotent")
    enqueue_actions = enqueue.add_subparsers(dest="task_kind", required=True)
    clip_video = enqueue_actions.add_parser(
        "clip-video",
        help="Antrekan render clip lokal tanpa delivery",
    )
    clip_video.add_argument("--plan", required=True)
    clip_video.add_argument("--clip", action="append")
    clip_video.add_argument("--priority", type=int, default=0)
    clip_video.add_argument("--max-attempts", type=int, default=2)
    clip_video.add_argument("--idempotency-key")
    clip_video.set_defaults(handler=command_enqueue_clip_video)

    story = enqueue_actions.add_parser(
        "story",
        help="Antrekan enam agent story dengan artifact review-gated",
    )
    story.add_argument("--brief", required=True)
    story.add_argument(
        "--provider",
        choices=["openai-compatible", "fixture"],
        default="openai-compatible",
    )
    story.add_argument(
        "--base-url",
        default=os.environ.get("HERMES_LLM_BASE_URL", "http://127.0.0.1:20128/v1"),
    )
    story.add_argument("--model", default=os.environ.get("HERMES_LLM_MODEL", ""))
    story.add_argument("--api-key-env", default="HERMES_LLM_API_KEY")
    story.add_argument("--timeout-seconds", type=float, default=180)
    story.add_argument(
        "--output-root",
        default=os.environ.get("HERMES_STORY_ROOT", str(PROJECT_ROOT / "stories")),
    )
    story.add_argument("--priority", type=int, default=0)
    story.add_argument("--max-attempts", type=int, default=2)
    story.add_argument("--idempotency-key")
    story.set_defaults(handler=command_enqueue_story)

    worker = subparsers.add_parser("worker", help="Jalankan worker queue")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--worker-id")
    worker.add_argument("--lease-seconds", type=float, default=60)
    worker.add_argument("--heartbeat-seconds", type=float, default=10)
    worker.add_argument("--retry-delay-seconds", type=float, default=5)
    worker.add_argument("--poll-seconds", type=float, default=2)
    worker.set_defaults(handler=command_worker)

    task_list = subparsers.add_parser("list", help="Daftar task")
    task_list.add_argument("--job")
    task_list.add_argument("--state", choices=sorted(TASK_STATES))
    task_list.add_argument("--limit", type=int, default=20)
    task_list.set_defaults(handler=command_list)

    show = subparsers.add_parser("show", help="Snapshot satu task")
    show.add_argument("task_id")
    show.set_defaults(handler=command_show)

    events = subparsers.add_parser("events", help="Riwayat transisi task")
    events.add_argument("--task")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    events.set_defaults(handler=command_events)

    retry = subparsers.add_parser("retry", help="Retry manual dalam attempt budget")
    retry.add_argument("task_id")
    retry.set_defaults(handler=command_retry)

    recover = subparsers.add_parser(
        "recover",
        help="Tandai task dengan lease kedaluwarsa untuk pemeriksaan operator",
    )
    recover.set_defaults(handler=command_recover)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args) or 0)
    except (SupervisorError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nWorker dihentikan oleh operator.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
