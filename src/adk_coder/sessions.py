"""SQLite-backed named, resumable, and forkable coding sessions per workspace."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, state_dir: Path):
        self.db_path = state_dir / "sessions.db"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(mode=0o700, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    branch TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    history_json TEXT NOT NULL DEFAULT '[]',
                    pinned_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def create(
        self,
        name: str = "",
        *,
        model: str = "",
        branch: str = "",
        summary: str = "",
        history: list[dict[str, str]] | None = None,
        pinned: list[str] | None = None,
    ) -> dict:
        sid = uuid.uuid4().hex[:8]
        label = (name or f"session-{sid}").strip()
        stamp = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                (id, name, model, branch, summary, history_json, pinned_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    label,
                    model,
                    branch,
                    summary,
                    json.dumps(history or []),
                    json.dumps(pinned or []),
                    stamp,
                    stamp,
                ),
            )
            conn.commit()
        return self.get(sid)  # type: ignore[return-value]

    def get(self, key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ? OR name = ? ORDER BY updated_at DESC LIMIT 1",
                (key, key),
            ).fetchone()
        return self._decode(row) if row else None

    def latest(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return self._decode(row) if row else None

    def list_all(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [self._decode(r) for r in rows]

    def save(
        self,
        sid: str,
        *,
        model: str,
        branch: str,
        summary: str,
        history: list[dict[str, str]],
        pinned: list[str],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET model = ?, branch = ?, summary = ?, history_json = ?, pinned_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    model,
                    branch,
                    summary,
                    json.dumps(history[-40:]),
                    json.dumps(pinned),
                    _now(),
                    sid,
                ),
            )
            conn.commit()

    def rename(self, key: str, new_name: str) -> dict:
        target = self.get(key)
        if not target:
            raise ValueError(f"Session not found: {key}")
        clean = " ".join((new_name or "").split()).strip()[:80]
        if not clean:
            raise ValueError("Session name cannot be empty.")
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET name = ?, updated_at = ? WHERE id = ?",
                (clean, _now(), target["id"]),
            )
            conn.commit()
        return self.get(target["id"])  # type: ignore[return-value]

    def search(self, query: str) -> list[dict]:
        q = (query or "").strip().lower()
        items = self.list_all()
        if not q:
            return items
        matched: list[dict] = []
        for s in items:
            if q in (s.get("name") or "").lower() or q in (s.get("summary") or "").lower():
                matched.append(s)
                continue
            if any(q in str(t.get("text") or "").lower() for t in (s.get("history") or [])):
                matched.append(s)
        return matched

    def fork(self, source_id: str, new_name: str = "") -> dict:
        source = self.get(source_id)
        if not source:
            raise ValueError(f"Session not found: {source_id}")
        label = (new_name or f"{source['name']}-fork").strip()
        return self.create(
            label,
            model=source["model"],
            branch=source["branch"],
            summary=source["summary"],
            history=list(source["history"]),
            pinned=list(source["pinned"]),
        )

    def delete(self, key: str) -> str:
        target = self.get(key)
        if not target:
            raise ValueError(f"Session not found: {key}")
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (target["id"],))
            conn.commit()
        return target["id"]

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "model": row["model"],
            "branch": row["branch"],
            "summary": row["summary"],
            "history": json.loads(row["history_json"] or "[]"),
            "pinned": json.loads(row["pinned_json"] or "[]"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
