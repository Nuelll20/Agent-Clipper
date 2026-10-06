"""Durable local task queue with leases and bounded retry semantics."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


TASK_STATES = frozenset({
    "QUEUED",
    "RUNNING",
    "RETRY_WAIT",
    "RECOVERY_REQUIRED",
    "FAILED",
    "COMPLETED",
})
TERMINAL_TASK_STATES = frozenset({"FAILED", "COMPLETED"})
RETRYABLE_TASK_STATES = frozenset({"FAILED", "RECOVERY_REQUIRED"})


class SupervisorError(RuntimeError):
    """Expected queue, lease, or adapter failure."""


class LeaseLostError(SupervisorError):
    """A worker no longer owns the task lease and must stop work."""


class TaskConfigurationError(SupervisorError):
    """A queued payload cannot be executed safely."""


def utc_now(timestamp: float | None = None) -> str:
    instant = dt.datetime.fromtimestamp(
        time.time() if timestamp is None else timestamp,
        dt.timezone.utc,
    )
    return instant.isoformat(timespec="milliseconds")


def default_worker_id() -> str:
    host = socket.gethostname().strip() or "local"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _clean_error(value: Any) -> str:
    return " ".join(str(value).replace("\x00", "").split())[:1000]


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise TaskConfigurationError(
            f"Artifact task tidak dapat dibaca: {path} ({exc})"
        ) from None
    return f"sha256:{digest.hexdigest()}"


class SupervisorStore:
    """SQLite source of truth for task state, attempts, and resource leases."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                resources TEXT NOT NULL,
                state TEXT NOT NULL,
                priority INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                next_attempt_at REAL NOT NULL,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at REAL,
                heartbeat_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                last_error TEXT,
                result TEXT
            );
            CREATE INDEX IF NOT EXISTS tasks_ready
                ON tasks(state, next_attempt_at, priority, created_at);
            CREATE INDEX IF NOT EXISTS tasks_job
                ON tasks(job_id, created_at);
            CREATE TABLE IF NOT EXISTS task_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                state TEXT NOT NULL,
                stage TEXT NOT NULL,
                message TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS task_events_task
                ON task_events(task_id, sequence);
            CREATE TABLE IF NOT EXISTS resource_leases (
                resource_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                worker_id TEXT NOT NULL,
                lease_token TEXT NOT NULL,
                expires_at REAL NOT NULL,
                heartbeat_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS resource_leases_task
                ON resource_leases(task_id);
        """)

    def close(self) -> None:
        self.connection.close()

    def _begin(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.connection.commit()

    def _rollback(self) -> None:
        self.connection.rollback()

    def _event(
        self,
        task_id: str,
        state: str,
        stage: str,
        message: str,
        *,
        timestamp: float | None = None,
    ) -> None:
        if state not in TASK_STATES:
            raise ValueError(f"Unknown task state: {state}")
        self.connection.execute(
            """
            INSERT INTO task_events
                (event_id, task_id, state, stage, message, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "task_evt_" + uuid.uuid4().hex,
                task_id,
                state,
                stage,
                message,
                utc_now(timestamp),
            ),
        )

    @staticmethod
    def _validate_resources(resources: list[str]) -> list[str]:
        normalized = sorted({str(item).strip() for item in resources})
        if any(not item or len(item) > 120 for item in normalized):
            raise ValueError("resource keys must contain 1-120 characters")
        return normalized

    @staticmethod
    def _task(row: sqlite3.Row, *, include_lease_token: bool = False) -> dict[str, Any]:
        task = dict(row)
        task["payload"] = json.loads(task["payload"])
        task["resources"] = json.loads(task["resources"])
        task["result"] = json.loads(task["result"]) if task["result"] else None
        if not include_lease_token:
            task.pop("lease_token", None)
        return task

    def enqueue(
        self,
        *,
        job_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        resources: list[str] | None = None,
        priority: int = 0,
        max_attempts: int = 2,
    ) -> tuple[dict[str, Any], bool]:
        job_id = str(job_id).strip()
        kind = str(kind).strip().upper()
        idempotency_key = str(idempotency_key).strip()
        if not job_id or not kind or not idempotency_key:
            raise ValueError("job_id, kind, and idempotency_key are required")
        if len(idempotency_key) > 256:
            raise ValueError("idempotency_key is too long")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        if type(priority) is not int:
            raise ValueError("priority must be an integer")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        normalized_resources = self._validate_resources(resources or [])
        timestamp = utc_now()
        try:
            self._begin()
            existing = self.connection.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                self._commit()
                return self._task(existing), False
            task_id = "task_" + uuid.uuid4().hex
            self.connection.execute(
                """
                INSERT INTO tasks (
                    task_id, idempotency_key, job_id, kind, payload, resources,
                    state, priority, attempt_count, max_attempts, next_attempt_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?, 0, ?, 0, ?, ?)
                """,
                (
                    task_id,
                    idempotency_key,
                    job_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(normalized_resources, ensure_ascii=False),
                    priority,
                    max_attempts,
                    timestamp,
                    timestamp,
                ),
            )
            self._event(task_id, "QUEUED", "queued", "Task queued")
            row = self.connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,),
            ).fetchone()
            self._commit()
            return self._task(row), True
        except Exception:
            self._rollback()
            raise

    def get_task(
        self,
        task_id: str,
        *,
        include_lease_token: bool = False,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,),
        ).fetchone()
        return self._task(row, include_lease_token=include_lease_token) if row else None

    def list_tasks(
        self,
        *,
        job_id: str | None = None,
        state: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if state is not None and state not in TASK_STATES:
            raise ValueError(f"Unknown task state: {state}")
        clauses: list[str] = []
        parameters: list[Any] = []
        if job_id is not None:
            clauses.append("job_id = ?")
            parameters.append(job_id)
        if state is not None:
            clauses.append("state = ?")
            parameters.append(state)
        query = "SELECT * FROM tasks"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY rowid DESC LIMIT ?"
        parameters.append(limit)
        return [self._task(row) for row in self.connection.execute(query, parameters)]

    def read_events(
        self,
        *,
        task_id: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("after must be >= 0; limit must be between 1 and 1000")
        query = "SELECT * FROM task_events WHERE sequence > ?"
        parameters: list[Any] = [after]
        if task_id is not None:
            query += " AND task_id = ?"
            parameters.append(task_id)
        query += " ORDER BY sequence LIMIT ?"
        parameters.append(limit)
        return [dict(row) for row in self.connection.execute(query, parameters)]

    def _recover_expired_locked(self, now: float) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT task_id FROM tasks
            WHERE state = 'RUNNING' AND lease_expires_at <= ?
            ORDER BY rowid
            """,
            (now,),
        ).fetchall()
        recovered: list[str] = []
        for row in rows:
            task_id = row["task_id"]
            message = "Worker heartbeat expired; operator recovery required"
            self.connection.execute(
                """
                UPDATE tasks
                SET state = 'RECOVERY_REQUIRED', lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL, updated_at = ?, last_error = ?
                WHERE task_id = ? AND state = 'RUNNING'
                """,
                (utc_now(now), message, task_id),
            )
            self.connection.execute(
                "DELETE FROM resource_leases WHERE task_id = ?", (task_id,),
            )
            self._event(
                task_id,
                "RECOVERY_REQUIRED",
                "lease_expired",
                message,
                timestamp=now,
            )
            recovered.append(task_id)
        return recovered

    def recover_expired(self, *, now: float | None = None) -> list[str]:
        current = time.time() if now is None else float(now)
        try:
            self._begin()
            recovered = self._recover_expired_locked(current)
            self._commit()
            return recovered
        except Exception:
            self._rollback()
            raise

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 60,
        now: float | None = None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        worker_id = str(worker_id).strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        current = time.time() if now is None else float(now)
        try:
            self._begin()
            recovered = self._recover_expired_locked(current)
            candidates = self.connection.execute(
                """
                SELECT * FROM tasks
                WHERE (state = 'QUEUED')
                   OR (state = 'RETRY_WAIT' AND next_attempt_at <= ?)
                ORDER BY priority DESC, rowid ASC
                """,
                (current,),
            ).fetchall()
            claimed: sqlite3.Row | None = None
            for candidate in candidates:
                resources = sorted(set(
                    [f"job:{candidate['job_id']}"]
                    + json.loads(candidate["resources"])
                ))
                placeholders = ",".join("?" for _ in resources)
                conflict = self.connection.execute(
                    f"SELECT 1 FROM resource_leases WHERE resource_key IN ({placeholders}) LIMIT 1",
                    resources,
                ).fetchone()
                if conflict is not None:
                    continue
                lease_token = "lease_" + uuid.uuid4().hex
                expires = current + lease_seconds
                attempt = int(candidate["attempt_count"]) + 1
                timestamp = utc_now(current)
                self.connection.execute(
                    """
                    UPDATE tasks
                    SET state = 'RUNNING', attempt_count = ?, next_attempt_at = 0,
                        lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                        heartbeat_at = ?, started_at = COALESCE(started_at, ?),
                        updated_at = ?, finished_at = NULL, last_error = NULL
                    WHERE task_id = ?
                    """,
                    (
                        attempt,
                        worker_id,
                        lease_token,
                        expires,
                        timestamp,
                        timestamp,
                        timestamp,
                        candidate["task_id"],
                    ),
                )
                for resource in resources:
                    self.connection.execute(
                        """
                        INSERT INTO resource_leases
                            (resource_key, task_id, worker_id, lease_token, expires_at, heartbeat_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            resource,
                            candidate["task_id"],
                            worker_id,
                            lease_token,
                            expires,
                            timestamp,
                        ),
                    )
                self._event(
                    candidate["task_id"],
                    "RUNNING",
                    "leased",
                    f"Attempt {attempt} leased to worker",
                    timestamp=current,
                )
                claimed = self.connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?",
                    (candidate["task_id"],),
                ).fetchone()
                break
            self._commit()
            return (
                self._task(claimed, include_lease_token=True) if claimed else None,
                recovered,
            )
        except Exception:
            self._rollback()
            raise

    def _owned_task(
        self,
        task_id: str,
        worker_id: str,
        lease_token: str,
        now: float,
    ) -> sqlite3.Row:
        row = self.connection.execute(
            """
            SELECT * FROM tasks
            WHERE task_id = ? AND state = 'RUNNING'
              AND lease_owner = ? AND lease_token = ?
              AND lease_expires_at > ?
            """,
            (task_id, worker_id, lease_token, now),
        ).fetchone()
        if row is None:
            raise LeaseLostError(f"Lease task tidak lagi dimiliki worker: {task_id}")
        return row

    def heartbeat(
        self,
        task_id: str,
        worker_id: str,
        lease_token: str,
        *,
        lease_seconds: float = 60,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        current = time.time() if now is None else float(now)
        timestamp = utc_now(current)
        try:
            self._begin()
            self._owned_task(task_id, worker_id, lease_token, current)
            expires = current + lease_seconds
            self.connection.execute(
                """
                UPDATE tasks SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (expires, timestamp, timestamp, task_id),
            )
            self.connection.execute(
                """
                UPDATE resource_leases
                SET expires_at = ?, heartbeat_at = ?
                WHERE task_id = ? AND worker_id = ? AND lease_token = ?
                """,
                (expires, timestamp, task_id, worker_id, lease_token),
            )
            row = self.connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,),
            ).fetchone()
            self._commit()
            return self._task(row)
        except Exception:
            self._rollback()
            raise

    def complete_task(
        self,
        task_id: str,
        worker_id: str,
        lease_token: str,
        *,
        result: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        timestamp = utc_now(current)
        try:
            self._begin()
            self._owned_task(task_id, worker_id, lease_token, current)
            self.connection.execute(
                """
                UPDATE tasks
                SET state = 'COMPLETED', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, heartbeat_at = NULL,
                    updated_at = ?, finished_at = ?, result = ?, last_error = NULL
                WHERE task_id = ?
                """,
                (
                    timestamp,
                    timestamp,
                    json.dumps(result or {}, ensure_ascii=False, sort_keys=True),
                    task_id,
                ),
            )
            self.connection.execute(
                "DELETE FROM resource_leases WHERE task_id = ?", (task_id,),
            )
            self._event(task_id, "COMPLETED", "completed", "Task completed", timestamp=current)
            row = self.connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,),
            ).fetchone()
            self._commit()
            return self._task(row)
        except Exception:
            self._rollback()
            raise

    def fail_task(
        self,
        task_id: str,
        worker_id: str,
        lease_token: str,
        error: Any,
        *,
        retryable: bool,
        retry_delay_seconds: float = 5,
        now: float | None = None,
    ) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        timestamp = utc_now(current)
        try:
            self._begin()
            task = self._owned_task(task_id, worker_id, lease_token, current)
            can_retry = retryable and task["attempt_count"] < task["max_attempts"]
            state = "RETRY_WAIT" if can_retry else "FAILED"
            next_attempt = current + max(0.0, retry_delay_seconds) if can_retry else 0
            cleaned = _clean_error(error) or "Task failed"
            self.connection.execute(
                """
                UPDATE tasks
                SET state = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    updated_at = ?, finished_at = ?, last_error = ?
                WHERE task_id = ?
                """,
                (
                    state,
                    next_attempt,
                    timestamp,
                    None if can_retry else timestamp,
                    cleaned,
                    task_id,
                ),
            )
            self.connection.execute(
                "DELETE FROM resource_leases WHERE task_id = ?", (task_id,),
            )
            self._event(
                task_id,
                state,
                "retry_wait" if can_retry else "failed",
                "Task scheduled for bounded retry" if can_retry else "Task failed",
                timestamp=current,
            )
            row = self.connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,),
            ).fetchone()
            self._commit()
            return self._task(row)
        except Exception:
            self._rollback()
            raise

    def retry_task(self, task_id: str, *, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        timestamp = utc_now(current)
        try:
            self._begin()
            task = self.connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,),
            ).fetchone()
            if task is None:
                raise SupervisorError(f"Task tidak ditemukan: {task_id}")
            if task["state"] not in RETRYABLE_TASK_STATES:
                raise SupervisorError(
                    f"Task {task_id} tidak dapat di-retry dari state {task['state']}."
                )
            if task["attempt_count"] >= task["max_attempts"]:
                raise SupervisorError(
                    f"Task {task_id} sudah menghabiskan batas {task['max_attempts']} attempt."
                )
            self.connection.execute(
                """
                UPDATE tasks
                SET state = 'QUEUED', next_attempt_at = 0, updated_at = ?,
                    finished_at = NULL, last_error = NULL
                WHERE task_id = ?
                """,
                (timestamp, task_id),
            )
            self._event(task_id, "QUEUED", "manual_retry", "Task queued by operator")
            row = self.connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,),
            ).fetchone()
            self._commit()
            return self._task(row)
        except Exception:
            self._rollback()
            raise


def build_clip_video_command(
    task: dict[str, Any],
    *,
    project_root: str | Path,
    event_db: str | Path,
    python_executable: str | Path = sys.executable,
) -> list[str]:
    if task.get("kind") != "CLIP_VIDEO":
        raise TaskConfigurationError(f"Adapter tidak tersedia untuk {task.get('kind')!r}.")
    payload = task.get("payload")
    if not isinstance(payload, dict):
        raise TaskConfigurationError("Payload CLIP_VIDEO harus berupa object.")
    plan = Path(str(payload.get("plan_path") or "")).expanduser()
    if not plan.is_absolute():
        plan = Path(project_root) / plan
    plan = plan.resolve()
    if not plan.is_file():
        raise TaskConfigurationError(f"Clip plan tidak ditemukan: {plan}")
    expected_fingerprint = str(payload.get("plan_fingerprint") or "").strip()
    if not expected_fingerprint:
        raise TaskConfigurationError("Payload CLIP_VIDEO tidak memiliki plan_fingerprint.")
    actual_fingerprint = _file_fingerprint(plan)
    if actual_fingerprint != expected_fingerprint:
        raise TaskConfigurationError(
            "Clip plan berubah setelah task diantrikan; enqueue task baru diperlukan."
        )
    clips = payload.get("clip_ids") or []
    if not isinstance(clips, list) or any(not str(item).strip() for item in clips):
        raise TaskConfigurationError("clip_ids harus berupa daftar ID klip.")
    command = [
        str(python_executable),
        str(Path(project_root) / "scripts" / "podcast_clipper.py"),
        "render",
        "--plan",
        str(plan),
        "--no-send",
        "--event-db",
        str(Path(event_db).expanduser().resolve()),
        "--task-id",
        str(task["task_id"]),
    ]
    for clip_id in clips:
        command.extend(("--clip", str(clip_id).strip()))
    return command


CommandRunner = Callable[[list[str], Path, Callable[[], None], float], int]


def run_command_with_heartbeat(
    command: list[str],
    cwd: Path,
    heartbeat: Callable[[], None],
    heartbeat_seconds: float,
) -> int:
    process = subprocess.Popen(command, cwd=str(cwd))
    try:
        while True:
            try:
                return process.wait(timeout=heartbeat_seconds)
            except subprocess.TimeoutExpired:
                heartbeat()
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        raise


@dataclass(frozen=True)
class WorkerResult:
    status: str
    task_id: str | None
    recovered_task_ids: tuple[str, ...] = ()


class SupervisorWorker:
    """Claims one task at a time and fences execution with a renewable lease."""

    def __init__(
        self,
        store: SupervisorStore,
        *,
        project_root: str | Path,
        event_db: str | Path,
        worker_id: str | None = None,
        lease_seconds: float = 60,
        heartbeat_seconds: float = 10,
        retry_delay_seconds: float = 5,
        command_runner: CommandRunner = run_command_with_heartbeat,
        python_executable: str | Path = sys.executable,
    ):
        if heartbeat_seconds <= 0 or lease_seconds < heartbeat_seconds * 3:
            raise ValueError("lease_seconds must be at least 3x heartbeat_seconds")
        self.store = store
        self.project_root = Path(project_root).resolve()
        self.event_db = Path(event_db).expanduser().resolve()
        self.worker_id = worker_id or default_worker_id()
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.command_runner = command_runner
        self.python_executable = python_executable

    def run_once(self) -> WorkerResult:
        task, recovered = self.store.claim_next(
            self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if task is None:
            return WorkerResult("idle", None, tuple(recovered))
        task_id = task["task_id"]
        lease_token = task["lease_token"]

        def heartbeat() -> None:
            self.store.heartbeat(
                task_id,
                self.worker_id,
                lease_token,
                lease_seconds=self.lease_seconds,
            )

        try:
            command = build_clip_video_command(
                task,
                project_root=self.project_root,
                event_db=self.event_db,
                python_executable=self.python_executable,
            )
            returncode = self.command_runner(
                command,
                self.project_root,
                heartbeat,
                self.heartbeat_seconds,
            )
            if returncode == 0:
                self.store.complete_task(
                    task_id,
                    self.worker_id,
                    lease_token,
                    result={"exit_code": 0},
                )
                return WorkerResult("completed", task_id, tuple(recovered))
            delay = min(
                3600.0,
                self.retry_delay_seconds * (2 ** max(0, task["attempt_count"] - 1)),
            )
            failed = self.store.fail_task(
                task_id,
                self.worker_id,
                lease_token,
                f"CLIP_VIDEO process exited with code {returncode}",
                retryable=True,
                retry_delay_seconds=delay,
            )
            return WorkerResult(failed["state"].casefold(), task_id, tuple(recovered))
        except TaskConfigurationError as exc:
            failed = self.store.fail_task(
                task_id,
                self.worker_id,
                lease_token,
                exc,
                retryable=False,
            )
            return WorkerResult(failed["state"].casefold(), task_id, tuple(recovered))
        except LeaseLostError:
            return WorkerResult("lease_lost", task_id, tuple(recovered))
        except KeyboardInterrupt:
            try:
                self.store.fail_task(
                    task_id,
                    self.worker_id,
                    lease_token,
                    "Worker interrupted by operator",
                    retryable=False,
                )
            except LeaseLostError:
                pass
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            failed = self.store.fail_task(
                task_id,
                self.worker_id,
                lease_token,
                f"Worker could not start CLIP_VIDEO process ({type(exc).__name__})",
                retryable=False,
            )
            return WorkerResult(failed["state"].casefold(), task_id, tuple(recovered))
