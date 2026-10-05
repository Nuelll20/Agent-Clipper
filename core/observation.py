"""Best-effort telemetry: an unavailable event store must not break rendering."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from core.event_store import EventStore


class RunObserver:
    def __init__(self, path: Path, job_id: str, kind: str):
        self.store: EventStore | None = None
        self.run_id: str | None = None
        self.current: int | None = None
        self.total: int | None = None
        try:
            self.store = EventStore(path)
            self.run_id = self.store.start_run(job_id, kind)
            print(f"Production run: {self.run_id}")
        except (OSError, sqlite3.Error) as exc:
            self._disable(exc)

    def _disable(self, exc: Exception) -> None:
        print(
            f"WARNING: production events unavailable ({type(exc).__name__}); "
            "pipeline continues, stored progress may be incomplete.",
            file=sys.stderr,
        )
        self.close()

    def emit(
        self, stage: str, message: str, *, state: str = "WORKING",
        clip_id: str | None = None,
    ) -> None:
        if self.store is None:
            return
        try:
            self.store.record(
                self.run_id, state, stage, message,
                current=self.current, total=self.total, clip_id=clip_id,
            )
        except (OSError, sqlite3.Error) as exc:
            self._disable(exc)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None
