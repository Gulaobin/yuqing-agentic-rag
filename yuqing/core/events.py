"""事件日志：只追加的 SQLite 表，每次外部调用（模型、工具）前后各记一条。

seq 全局递增；同一次运行用 run_id 关联；工具调用用 call_id 指回发起它的模型调用（parent_call_id）。
页面上看到的运行轨迹、评测里的代价统计，都从这张表读出来。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    ts             TEXT NOT NULL,
    type           TEXT NOT NULL,
    call_id        TEXT,
    parent_call_id TEXT,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events (run_id, seq);
"""


@dataclass(frozen=True)
class Event:
    seq: int
    run_id: str
    ts: str
    type: str
    call_id: str | None
    parent_call_id: str | None
    payload: dict[str, Any]


class EventLog:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def append(
        self,
        run_id: str,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> int:
        ts = datetime.now().isoformat(timespec="milliseconds")
        data = json.dumps(payload or {}, ensure_ascii=False, default=str)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO events (run_id, ts, type, call_id, parent_call_id, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, ts, type, call_id, parent_call_id, data),
            )
            return int(cursor.lastrowid)

    def read(self, run_id: str, after_seq: int = 0) -> list[Event]:
        with self._lock:  # 同一个连接被多个线程共用，读也要串行
            rows = self._conn.execute(
                "SELECT seq, run_id, ts, type, call_id, parent_call_id, payload FROM events "
                "WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [Event(*row[:6], json.loads(row[6])) for row in rows]

    def last_event(self, run_id: str) -> Event | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, run_id, ts, type, call_id, parent_call_id, payload FROM events "
                "WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return Event(*row[:6], json.loads(row[6])) if row else None

    def last_run_id(self) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT run_id FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._conn.close()
