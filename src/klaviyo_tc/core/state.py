"""SQLite-backed sync state: runs (job/scope attempts), generations (per-translation
pushes within a run), written values, pull stats.
"""
from __future__ import annotations

import json
import functools
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    scope_description TEXT NOT NULL,
    provider_state TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_scope_key ON runs(scope_key);

CREATE TABLE IF NOT EXISTS generations (
    id TEXT PRIMARY KEY,
    translation_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    sent_snapshot TEXT NOT NULL,
    locales TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_state TEXT,
    run_id TEXT,
    baseline TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_generations_translation ON generations(translation_id);

CREATE TABLE IF NOT EXISTS written (
    translation_id TEXT NOT NULL,
    value_id TEXT NOT NULL,
    locale TEXT NOT NULL,
    value TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    written_at REAL NOT NULL,
    PRIMARY KEY (translation_id, value_id, locale)
);

CREATE TABLE IF NOT EXISTS pull_status (
    generation_id TEXT NOT NULL,
    locale TEXT NOT NULL,
    downloaded INTEGER NOT NULL,
    written INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (generation_id, locale)
);
"""


def _row_to_generation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "translation_id": row["translation_id"],
        "file_name": row["file_name"],
        "sent_snapshot": json.loads(row["sent_snapshot"]),
        "locales": json.loads(row["locales"]),
        "status": row["status"],
        "provider_state": json.loads(row["provider_state"]) if row["provider_state"] else None,
        "run_id": row["run_id"],
        "baseline": json.loads(row["baseline"]) if row["baseline"] else {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "job_name": row["job_name"],
        "scope_key": row["scope_key"],
        "scope_description": row["scope_description"],
        "provider_state": json.loads(row["provider_state"]) if row["provider_state"] else None,
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class State:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection shared across pull worker threads; every public method holds this lock.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        # Older generations tables lack these columns.
        for column in ("run_id", "baseline"):
            try:
                self.conn.execute(f"ALTER TABLE generations ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass
        # Created here, not in SCHEMA, so it runs after the column exists on upgraded databases.
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_generations_run ON generations(run_id)")

    # -- Runs ---------------------------------------------------------------

    def create_run(self, run_id: str, job_name: str, scope_key: str, scope_description: str) -> str:
        now = time.time()
        self.conn.execute(
            "INSERT INTO runs (id, job_name, scope_key, scope_description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (run_id, job_name, scope_key, scope_description, now, now),
        )
        self.conn.commit()
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None

    def get_pending_run(self, scope_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE scope_key = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (scope_key,),
        ).fetchone()
        return _row_to_run(row) if row else None

    def set_run_provider_state(self, run_id: str, provider_state: dict) -> None:
        self.conn.execute(
            "UPDATE runs SET provider_state = ?, updated_at = ? WHERE id = ?",
            (json.dumps(provider_state), time.time(), run_id),
        )
        self.conn.commit()

    def finalize_run(self, run_id: str, generation_provider_states: dict[str, dict]) -> None:
        """Mark a run and its generations submitted in one transaction."""
        now = time.time()
        with self.conn:
            for generation_id, provider_state in generation_provider_states.items():
                self.conn.execute(
                    "UPDATE generations SET provider_state = ?, status = 'submitted', updated_at = ? WHERE id = ?",
                    (json.dumps(provider_state), now, generation_id),
                )
            self.conn.execute("UPDATE runs SET status = 'submitted', updated_at = ? WHERE id = ?", (now, run_id))

    def set_run_status(self, run_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?", (status, time.time(), run_id)
        )
        self.conn.commit()

    def list_generations_for_run(self, run_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM generations WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [_row_to_generation(row) for row in rows]

    # -- Generations ----------------------------------------------------------

    def create_generation(
        self,
        translation_id: str,
        file_name: str,
        sent_snapshot: dict,
        locales: dict,
        run_id: str | None = None,
        baseline: dict | None = None,
    ) -> str:
        gen_id = str(uuid.uuid4())
        now = time.time()
        self.conn.execute(
            "INSERT INTO generations "
            "(id, translation_id, file_name, sent_snapshot, locales, status, run_id, baseline, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (
                gen_id, translation_id, file_name, json.dumps(sent_snapshot), json.dumps(locales), run_id,
                json.dumps(baseline or {}), now, now,
            ),
        )
        self.conn.commit()
        return gen_id

    def get_generation(self, generation_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM generations WHERE id = ?", (generation_id,)).fetchone()
        return _row_to_generation(row) if row else None

    def get_pending_generation(self, translation_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM generations WHERE translation_id = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (translation_id,),
        ).fetchone()
        return _row_to_generation(row) if row else None

    def get_active_generation(self, translation_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM generations WHERE translation_id = ? AND status = 'submitted' "
            "ORDER BY created_at DESC LIMIT 1",
            (translation_id,),
        ).fetchone()
        return _row_to_generation(row) if row else None

    def update_generation_snapshot(
        self, generation_id: str, sent_snapshot: dict, locales: dict, baseline: dict | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE generations SET sent_snapshot = ?, locales = ?, baseline = ?, updated_at = ? WHERE id = ?",
            (json.dumps(sent_snapshot), json.dumps(locales), json.dumps(baseline or {}), time.time(), generation_id),
        )
        self.conn.commit()

    def set_provider_state(self, generation_id: str, provider_state: dict) -> None:
        self.conn.execute(
            "UPDATE generations SET provider_state = ?, updated_at = ? WHERE id = ?",
            (json.dumps(provider_state), time.time(), generation_id),
        )
        self.conn.commit()

    def set_status(self, generation_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE generations SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), generation_id),
        )
        self.conn.commit()

    def list_translation_ids(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT translation_id FROM generations ORDER BY translation_id"
        ).fetchall()
        return [row["translation_id"] for row in rows]

    def record_written(
        self, translation_id: str, value_id: str, locale: str, value: str, generation_id: str
    ) -> None:
        self.conn.execute(
            "INSERT INTO written (translation_id, value_id, locale, value, generation_id, written_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(translation_id, value_id, locale) DO UPDATE SET "
            "value = excluded.value, generation_id = excluded.generation_id, written_at = excluded.written_at",
            (translation_id, value_id, locale, value, generation_id, time.time()),
        )
        self.conn.commit()

    def get_written(self, translation_id: str, value_id: str, locale: str) -> str | None:
        record = self.get_written_record(translation_id, value_id, locale)
        return record["value"] if record else None

    def get_written_record(self, translation_id: str, value_id: str, locale: str) -> dict | None:
        row = self.conn.execute(
            "SELECT value, generation_id FROM written WHERE translation_id = ? AND value_id = ? AND locale = ?",
            (translation_id, value_id, locale),
        ).fetchone()
        return {"value": row["value"], "generation_id": row["generation_id"]} if row else None

    def record_pull_status(self, generation_id: str, locale: str, downloaded: int, written: int) -> None:
        self.conn.execute(
            "INSERT INTO pull_status (generation_id, locale, downloaded, written, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(generation_id, locale) DO UPDATE SET "
            "downloaded = excluded.downloaded, written = excluded.written, updated_at = excluded.updated_at",
            (generation_id, locale, downloaded, written, time.time()),
        )
        self.conn.commit()

    def get_pull_status(self, generation_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT locale, downloaded, written FROM pull_status WHERE generation_id = ? ORDER BY locale",
            (generation_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _locked(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


for _name, _method in list(vars(State).items()):
    if callable(_method) and not _name.startswith("_"):
        setattr(State, _name, _locked(_method))
