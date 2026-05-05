from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL,
    title TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_name TEXT,
    tool_args TEXT,
    tool_result TEXT,
    tier TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);

CREATE TABLE IF NOT EXISTS memory_meta (
    name TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    description TEXT,
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 1.0,
    source_session TEXT
);

CREATE TABLE IF NOT EXISTS dreams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at REAL NOT NULL,
    summary TEXT NOT NULL,
    turns_consumed INTEGER NOT NULL,
    memories_written INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: sqlite3.Connection | None = self._open()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._db is None:
            self._db = self._open()
        yield self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def start_session(self, title: str | None = None) -> str:
        sid = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO sessions(id, started_at, title) VALUES (?, ?, ?)",
                (sid, time.time(), title),
            )
        return sid

    def end_session(self, session_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (time.time(), session_id))

    def append_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_name: str | None = None,
        tool_args: dict | None = None,
        tool_result: str | None = None,
        tier: str | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO turns(session_id, ts, role, content, tool_name, tool_args, tool_result, tier) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    time.time(),
                    role,
                    content,
                    tool_name,
                    json.dumps(tool_args, default=str) if tool_args is not None else None,
                    tool_result,
                    tier,
                ),
            )

    def recent_turns(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT role, content, tool_name, tool_args, tool_result, tier, ts "
                "FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def turns_since(self, since_ts: float, limit: int | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT session_id, ts, role, content, tool_name, tool_args, tool_result "
            "FROM turns WHERE ts >= ? ORDER BY ts ASC"
        )
        params: tuple[Any, ...]
        if limit is not None:
            sql += " LIMIT ?"
            params = (since_ts, max(1, int(limit)))
        else:
            params = (since_ts,)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                      (key, value))

    def upsert_memory_meta(self, name: str, mem_type: str, description: str,
                           source_session: str | None = None) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO memory_meta(name, type, description, created_at, last_accessed, "
                "access_count, score, source_session) VALUES (?, ?, ?, ?, ?, 0, 1.0, ?) "
                "ON CONFLICT(name) DO UPDATE SET type = excluded.type, "
                "description = excluded.description, last_accessed = ?",
                (name, mem_type, description, now, now, source_session, now),
            )

    def touch_memory(self, name: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE memory_meta SET access_count = access_count + 1, "
                "last_accessed = ?, score = MIN(1.0, score + 0.05) WHERE name = ?",
                (time.time(), name),
            )

    def all_memory_meta(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT name, type, description, score, access_count, last_accessed, created_at "
                "FROM memory_meta ORDER BY type, name"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_memory_meta(self, name: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM memory_meta WHERE name = ?", (name,))

    def decay_scores(self, factor: float) -> None:
        with self._conn() as c:
            c.execute("UPDATE memory_meta SET score = score * ?", (factor,))

    def low_score_memories(self, threshold: float) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT name, type, score FROM memory_meta WHERE score < ?", (threshold,)
            ).fetchall()
        return [dict(r) for r in rows]

    def log_dream(self, summary: str, turns_consumed: int, memories_written: int) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO dreams(ran_at, summary, turns_consumed, memories_written) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), summary, turns_consumed, memories_written),
            )

    def last_dream(self) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT ran_at, summary, turns_consumed, memories_written FROM dreams "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
