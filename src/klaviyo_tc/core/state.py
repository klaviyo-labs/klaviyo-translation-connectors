"""SQLite-backed sync state: generations (push attempts), written values, pull stats."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (
    id TEXT PRIMARY KEY,
    translation_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    sent_snapshot TEXT NOT NULL,
    locales TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_state TEXT,
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
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class State:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def create_generation(
        self, translation_id: str, file_name: str, sent_snapshot: dict, locales: dict
    ) -> str:
        gen_id = str(uuid.uuid4())
        now = time.time()
        self.conn.execute(
            "INSERT INTO generations "
            "(id, translation_id, file_name, sent_snapshot, locales, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (gen_id, translation_id, file_name, json.dumps(sent_snapshot), json.dumps(locales), now, now),
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

    def update_generation_snapshot(self, generation_id: str, sent_snapshot: dict, locales: dict) -> None:
        self.conn.execute(
            "UPDATE generations SET sent_snapshot = ?, locales = ?, updated_at = ? WHERE id = ?",
            (json.dumps(sent_snapshot), json.dumps(locales), time.time(), generation_id),
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
        row = self.conn.execute(
            "SELECT value FROM written WHERE translation_id = ? AND value_id = ? AND locale = ?",
            (translation_id, value_id, locale),
        ).fetchone()
        return row["value"] if row else None

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
