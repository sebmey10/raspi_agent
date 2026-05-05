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

CREATE TABLE IF NOT EXISTS long_runs (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    topic TEXT NOT NULL,
    status TEXT NOT NULL,
    cycles INTEGER NOT NULL DEFAULT 0,
    max_cycles INTEGER,
    interval_s REAL NOT NULL DEFAULT 0,
    max_child_lanes INTEGER NOT NULL DEFAULT 3,
    state_json TEXT NOT NULL DEFAULT '{}',
    session_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_long_runs_status ON long_runs(status);
CREATE INDEX IF NOT EXISTS idx_long_runs_updated ON long_runs(updated_at);

CREATE TABLE IF NOT EXISTS long_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    data_json TEXT,
    FOREIGN KEY (run_id) REFERENCES long_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_long_run_events_run ON long_run_events(run_id, id);
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

    def record_tool_result(self, name: str, success: bool) -> None:
        key = f"tool_success:{name}"
        raw = self.get_kv(key, '{"ok":0,"fail":0}')
        try:
            stats = json.loads(raw or "{}")
        except json.JSONDecodeError:
            stats = {}
        stats["ok" if success else "fail"] = int(stats.get("ok" if success else "fail", 0)) + 1
        self.set_kv(key, json.dumps(stats, sort_keys=True))

    def tool_success_stats(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT k, v FROM kv WHERE k LIKE 'tool_success:%' ORDER BY k"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            name = r["k"].split(":", 1)[1]
            try:
                stats = json.loads(r["v"])
            except json.JSONDecodeError:
                stats = {}
            ok = int(stats.get("ok", 0))
            fail = int(stats.get("fail", 0))
            total = ok + fail
            out.append({
                "name": name,
                "ok": ok,
                "fail": fail,
                "total": total,
                "rate": (ok / total) if total else 0.0,
            })
        return out

    # ---- long-running research / project loops ----

    def create_long_run(
        self,
        topic: str,
        *,
        max_cycles: int | None = None,
        interval_s: float = 0.0,
        max_child_lanes: int = 3,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        now = time.time()
        clean_topic = topic.strip()
        with self._conn() as c:
            c.execute(
                "INSERT INTO long_runs("
                "id, created_at, updated_at, topic, status, cycles, max_cycles, "
                "interval_s, max_child_lanes, state_json, session_id"
                ") VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    now,
                    now,
                    clean_topic,
                    "active",
                    max_cycles,
                    float(interval_s),
                    int(max_child_lanes),
                    json.dumps(state or {}, sort_keys=True),
                    session_id,
                ),
            )
        self.append_long_run_event(
            run_id,
            "created",
            clean_topic,
            {
                "max_cycles": max_cycles,
                "interval_s": interval_s,
                "max_child_lanes": max_child_lanes,
            },
        )
        return run_id

    def get_long_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, created_at, updated_at, topic, status, cycles, max_cycles, "
                "interval_s, max_child_lanes, state_json, session_id "
                "FROM long_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return self._decode_long_run(row) if row else None

    def list_long_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, created_at, updated_at, topic, status, cycles, max_cycles, "
                "interval_s, max_child_lanes, state_json, session_id "
                "FROM long_runs ORDER BY updated_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._decode_long_run(r) for r in rows]

    def update_long_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "topic",
            "status",
            "cycles",
            "max_cycles",
            "interval_s",
            "max_child_lanes",
            "state",
            "session_id",
        }
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"unknown long-run field(s): {', '.join(unknown)}")
        if not fields:
            return
        columns: list[str] = ["updated_at = ?"]
        values: list[Any] = [time.time()]
        for name, value in fields.items():
            column = "state_json" if name == "state" else name
            columns.append(f"{column} = ?")
            if name == "state":
                value = json.dumps(value or {}, sort_keys=True)
            values.append(value)
        values.append(run_id)
        sql = f"UPDATE long_runs SET {', '.join(columns)} WHERE id = ?"
        with self._conn() as c:
            c.execute(sql, values)

    def append_long_run_event(
        self,
        run_id: str,
        kind: str,
        content: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO long_run_events(run_id, ts, kind, content, data_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    time.time(),
                    kind,
                    content,
                    json.dumps(data, sort_keys=True, default=str) if data is not None else None,
                ),
            )

    def long_run_events(self, run_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, run_id, ts, kind, content, data_json "
                "FROM long_run_events WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, max(1, int(limit))),
            ).fetchall()
        return [self._decode_long_run_event(r) for r in reversed(rows)]

    def _decode_long_run(self, row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        raw = out.pop("state_json", "{}") or "{}"
        try:
            out["state"] = json.loads(raw)
        except json.JSONDecodeError:
            out["state"] = {}
        return out

    def _decode_long_run_event(self, row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        raw = out.pop("data_json", None)
        if raw:
            try:
                out["data"] = json.loads(raw)
            except json.JSONDecodeError:
                out["data"] = {}
        else:
            out["data"] = {}
        return out

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
