import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.artifacts import sha256_file
from core.event_store import EventStore
from core.supervisor import (
    LeaseLostError,
    SupervisorError,
    SupervisorStore,
    SupervisorWorker,
)


def enqueue(
    store,
    *,
    job="job-01",
    key="key-01",
    resources=None,
    max_attempts=2,
    plan_path="plan.json",
):
    payload = {"plan_path": plan_path, "clip_ids": []}
    if Path(plan_path).is_file():
        payload["plan_fingerprint"] = sha256_file(plan_path)
    return store.enqueue(
        job_id=job,
        kind="CLIP_VIDEO",
        payload=payload,
        idempotency_key=key,
        resources=resources or [],
        max_attempts=max_attempts,
    )


def test_enqueue_is_idempotent_across_reopen(tmp_path):
    path = tmp_path / "supervisor.db"
    store = SupervisorStore(path)
    first, created = enqueue(store)
    store.close()

    reopened = SupervisorStore(path)
    try:
        duplicate, duplicate_created = enqueue(reopened)
        assert created is True
        assert duplicate_created is False
        assert duplicate["task_id"] == first["task_id"]
        assert len(reopened.list_tasks()) == 1
        assert [event["state"] for event in reopened.read_events()] == ["QUEUED"]
    finally:
        reopened.close()


def test_job_and_resource_leases_allow_only_one_writer(tmp_path):
    path = tmp_path / "supervisor.db"
    one = SupervisorStore(path)
    two = SupervisorStore(path)
    try:
        first, _ = enqueue(one, key="first", resources=["clipper-render"])
        second, _ = enqueue(one, key="second", resources=["clipper-render"])
        claimed, recovered = one.claim_next("worker-a", lease_seconds=10, now=100)
        blocked, _ = two.claim_next("worker-b", lease_seconds=10, now=101)
        assert recovered == []
        assert claimed["task_id"] == first["task_id"]
        assert blocked is None

        one.complete_task(
            first["task_id"],
            "worker-a",
            claimed["lease_token"],
            now=102,
        )
        next_task, _ = two.claim_next("worker-b", lease_seconds=10, now=103)
        assert next_task["task_id"] == second["task_id"]
    finally:
        one.close()
        two.close()


def test_shared_resource_blocks_different_jobs(tmp_path):
    store = SupervisorStore(tmp_path / "supervisor.db")
    try:
        enqueue(store, job="job-a", key="a", resources=["gpu:0"])
        enqueue(store, job="job-b", key="b", resources=["gpu:0"])
        first, _ = store.claim_next("worker-a", lease_seconds=10, now=100)
        second, _ = store.claim_next("worker-b", lease_seconds=10, now=101)
        assert first is not None
        assert second is None
    finally:
        store.close()


def test_heartbeat_renews_lease_and_fences_wrong_owner(tmp_path):
    store = SupervisorStore(tmp_path / "supervisor.db")
    try:
        task, _ = enqueue(store)
        claimed, _ = store.claim_next("worker-a", lease_seconds=10, now=100)
        renewed = store.heartbeat(
            task["task_id"],
            "worker-a",
            claimed["lease_token"],
            lease_seconds=10,
            now=105,
        )
        assert renewed["lease_expires_at"] == 115
        assert store.recover_expired(now=111) == []
        with pytest.raises(LeaseLostError):
            store.heartbeat(
                task["task_id"],
                "worker-b",
                claimed["lease_token"],
                lease_seconds=10,
                now=112,
            )
    finally:
        store.close()


def test_expired_worker_requires_operator_recovery_instead_of_auto_retry(tmp_path):
    store = SupervisorStore(tmp_path / "supervisor.db")
    try:
        task, _ = enqueue(store)
        claimed, _ = store.claim_next("dead-worker", lease_seconds=10, now=100)
        recovered = store.recover_expired(now=111)
        assert recovered == [task["task_id"]]
        snapshot = store.get_task(task["task_id"])
        assert snapshot["state"] == "RECOVERY_REQUIRED"
        assert snapshot["attempt_count"] == 1
        assert snapshot["lease_owner"] is None
        assert store.claim_next("new-worker", lease_seconds=10, now=112)[0] is None
        with pytest.raises(LeaseLostError):
            store.complete_task(
                task["task_id"],
                "dead-worker",
                claimed["lease_token"],
                now=112,
            )

        queued = store.retry_task(task["task_id"], now=113)
        assert queued["state"] == "QUEUED"
        retried, _ = store.claim_next("new-worker", lease_seconds=10, now=114)
        assert retried["task_id"] == task["task_id"]
        assert retried["attempt_count"] == 2
    finally:
        store.close()


def test_retry_is_delayed_and_bounded(tmp_path):
    store = SupervisorStore(tmp_path / "supervisor.db")
    try:
        task, _ = enqueue(store, max_attempts=2)
        first, _ = store.claim_next("worker", lease_seconds=10, now=100)
        waiting = store.fail_task(
            task["task_id"],
            "worker",
            first["lease_token"],
            "renderer failed",
            retryable=True,
            retry_delay_seconds=5,
            now=101,
        )
        assert waiting["state"] == "RETRY_WAIT"
        assert store.claim_next("worker", lease_seconds=10, now=105)[0] is None
        second, _ = store.claim_next("worker", lease_seconds=10, now=107)
        failed = store.fail_task(
            task["task_id"],
            "worker",
            second["lease_token"],
            "renderer failed again",
            retryable=True,
            retry_delay_seconds=5,
            now=108,
        )
        assert failed["state"] == "FAILED"
        assert failed["attempt_count"] == 2
        with pytest.raises(SupervisorError, match="menghabiskan batas"):
            store.retry_task(task["task_id"], now=109)
        assert [event["state"] for event in store.read_events(task_id=task["task_id"])] == [
            "QUEUED",
            "RUNNING",
            "RETRY_WAIT",
            "RUNNING",
            "FAILED",
        ]
    finally:
        store.close()


def test_clip_video_worker_uses_existing_cli_without_delivery(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    plan = tmp_path / "clip-plan.json"
    plan.write_text(json.dumps({"job_id": "job", "clips": []}), encoding="utf-8")
    store = SupervisorStore(tmp_path / "supervisor.db")
    task, _ = enqueue(
        store,
        plan_path=str(plan),
        resources=["clipper-render"],
    )
    calls = []

    def runner(command, cwd, heartbeat, heartbeat_seconds):
        calls.append((command, cwd, heartbeat_seconds))
        heartbeat()
        return 0

    worker = SupervisorWorker(
        store,
        project_root=project_root,
        event_db=tmp_path / "events.db",
        worker_id="test-worker",
        lease_seconds=30,
        heartbeat_seconds=5,
        command_runner=runner,
        python_executable="python-fixture",
    )
    try:
        result = worker.run_once()
        assert result.status == "completed"
        assert result.task_id == task["task_id"]
        command, cwd, heartbeat_seconds = calls[0]
        assert command[0] == "python-fixture"
        assert command[2] == "render"
        assert "--no-send" in command
        assert command[command.index("--task-id") + 1] == task["task_id"]
        assert cwd == project_root
        assert heartbeat_seconds == 5
        snapshot = store.get_task(task["task_id"])
        assert snapshot["state"] == "COMPLETED"
        assert snapshot["result"] == {"exit_code": 0}
    finally:
        store.close()


def test_clip_video_worker_blocks_plan_changed_after_enqueue(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    plan = tmp_path / "clip-plan.json"
    plan.write_text(json.dumps({"job_id": "job", "clips": []}), encoding="utf-8")
    store = SupervisorStore(tmp_path / "supervisor.db")
    task, _ = enqueue(store, plan_path=str(plan))
    plan.write_text(json.dumps({"job_id": "job", "clips": [{"id": "new"}]}), encoding="utf-8")
    worker = SupervisorWorker(
        store,
        project_root=project_root,
        event_db=tmp_path / "events.db",
        worker_id="test-worker",
        lease_seconds=30,
        heartbeat_seconds=5,
        command_runner=lambda *args: pytest.fail("changed plan must not execute"),
    )
    try:
        result = worker.run_once()
        assert result.status == "failed"
        snapshot = store.get_task(task["task_id"])
        assert snapshot["state"] == "FAILED"
        assert "enqueue task baru" in snapshot["last_error"]
    finally:
        store.close()


def test_event_store_preserves_supervisor_task_identity(tmp_path):
    store = EventStore(tmp_path / "events.db")
    try:
        run_id = store.start_run(
            "job",
            "CLIP_RENDER",
            task_id="task-supervised",
            agent_id="clipper",
        )
        event = store.read_events(run_id)[0]
        assert event["task_id"] == "task-supervised"
        assert event["run_id"] != event["task_id"]
        assert store.list_runs(task_id="task-supervised")[0] == event
    finally:
        store.close()


def test_supervisor_cli_enqueue_is_idempotent(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    script = project_root / "scripts" / "supervisor.py"
    database = tmp_path / "supervisor.db"
    plan = tmp_path / "clip-plan.json"
    plan.write_text(
        json.dumps({
            "job_id": "cli-job",
            "clips": [{"id": "clip-01"}],
        }),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(script),
        "--db",
        str(database),
        "enqueue",
        "clip-video",
        "--plan",
        str(plan),
        "--clip",
        "clip-01",
    ]
    first = subprocess.run(command, capture_output=True, text=True, check=True)
    second = subprocess.run(command, capture_output=True, text=True, check=True)
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["created"] is True
    assert second_payload["created"] is False
    assert first_payload["task"]["task_id"] == second_payload["task"]["task_id"]
