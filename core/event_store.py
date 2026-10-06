"""Durable observation of CLI runs; not a scheduler or worker supervisor."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any


STATES = frozenset({
    "IDLE", "QUEUED", "STARTING", "WORKING", "WAITING", "REVIEWING",
    "RETRYING", "FAILED", "COMPLETED", "PAUSED",
})
TERMINAL = {"FAILED", "COMPLETED"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class EventStore:
    """Append an event and its latest run snapshot in one transaction."""

    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT,
                job_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                agent_id TEXT,
                created_at TEXT NOT NULL,
                state TEXT NOT NULL,
                snapshot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, sequence);
            CREATE INDEX IF NOT EXISTS runs_job ON runs(job_id, created_at);
        """)
        self._migrate_runs()

    def _migrate_runs(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(runs)")
        }
        with self.connection:
            if "task_id" not in columns:
                self.connection.execute("ALTER TABLE runs ADD COLUMN task_id TEXT")
            if "agent_id" not in columns:
                self.connection.execute("ALTER TABLE runs ADD COLUMN agent_id TEXT")
            self.connection.execute(
                "UPDATE runs SET task_id = run_id WHERE task_id IS NULL OR task_id = ''"
            )
            self.connection.execute(
                "UPDATE runs SET agent_id = 'clipper' WHERE agent_id IS NULL OR agent_id = ''"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id, created_at)"
            )

    def close(self) -> None:
        self.connection.close()

    def start_run(
        self,
        job_id: str,
        kind: str,
        *,
        task_id: str | None = None,
        agent_id: str = "clipper",
    ) -> str:
        if not job_id or not kind or not agent_id:
            raise ValueError("job_id, kind, and agent_id are required")
        run_id = "run_" + uuid.uuid4().hex
        task_id = str(task_id or run_id).strip()
        if not task_id:
            raise ValueError("task_id cannot be empty")
        timestamp = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO runs
                    (run_id, task_id, job_id, kind, agent_id, created_at, state, snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, task_id, job_id, kind, agent_id, timestamp, "STARTING", "{}"),
            )
            self._append(run_id, "STARTING", "starting", "Starting production run")
        return run_id

    def record(
        self, run_id: str, state: str, stage: str, message: str,
        *, current: int | None = None, total: int | None = None,
        clip_id: str | None = None, unit: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        if state not in STATES:
            raise ValueError(f"Unknown state: {state}")
        if not stage or not message:
            raise ValueError("stage and message are required")
        if (current is None) != (total is None):
            raise ValueError("current and total must both be known or both unknown")
        if current is not None and (
            type(current) is not int or type(total) is not int
            or current < 0 or total < 0 or current > total
        ):
            raise ValueError("progress requires integers: 0 <= current <= total")
        if unit is not None and not str(unit).strip():
            raise ValueError("unit cannot be empty")
        if agent_id is not None and not str(agent_id).strip():
            raise ValueError("agent_id cannot be empty")
        with self.connection:
            # Serialize the terminal-state check with the following write.
            self.connection.execute("BEGIN IMMEDIATE")
            return self._append(
                run_id, state, stage, message, current, total, clip_id, unit, agent_id,
            )

    def _append(
        self, run_id: str, state: str, stage: str, message: str,
        current: int | None = None, total: int | None = None,
        clip_id: str | None = None, unit: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown run: {run_id}")
        if row["state"] in TERMINAL:
            raise ValueError("A finished run cannot be changed; create a new attempt")
        event = {
            "schema_version": 1,
            "event_id": "evt_" + uuid.uuid4().hex,
            "run_id": run_id,
            "task_id": row["task_id"] or run_id,
            "job_id": row["job_id"],
            "kind": row["kind"],
            "agent_id": str(agent_id or row["agent_id"] or "clipper").strip(),
            "state": state,
            "stage": stage,
            "message": message,
            "current": current,
            "total": total,
            "unit": (str(unit).strip() if unit is not None else "clips")
            if total is not None else None,
            "clip_id": clip_id,
            "timestamp": utc_now(),
        }
        cursor = self.connection.execute(
            "INSERT INTO events (event_id, run_id, payload) VALUES (?, ?, ?)",
            (event["event_id"], run_id, json.dumps(event, ensure_ascii=False)),
        )
        event["sequence"] = cursor.lastrowid
        self.connection.execute(
            "UPDATE runs SET state = ?, snapshot = ? WHERE run_id = ?",
            (state, json.dumps(event, ensure_ascii=False), run_id),
        )
        return event

    def list_runs(
        self,
        job_id: str | None = None,
        limit: int = 20,
        *,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        query = "SELECT snapshot FROM runs"
        clauses: list[str] = []
        parameters: list[Any] = []
        if job_id is not None:
            clauses.append("job_id = ?")
            parameters.append(job_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY rowid DESC LIMIT ?"
        parameters.append(limit)
        return [json.loads(row[0]) for row in self.connection.execute(query, parameters)]

    def read_events(
        self, run_id: str | None = None, after: int = 0, limit: int = 100,
    ) -> list[dict[str, Any]]:
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("after must be >= 0; limit must be between 1 and 1000")
        query = "SELECT sequence, payload FROM events WHERE sequence > ?"
        parameters: list[Any] = [after]
        if run_id is not None:
            query += " AND run_id = ?"
            parameters.append(run_id)
        query += " ORDER BY sequence LIMIT ?"
        parameters.append(limit)
        return [
            {**json.loads(row["payload"]), "sequence": row["sequence"]}
            for row in self.connection.execute(query, parameters)
        ]
