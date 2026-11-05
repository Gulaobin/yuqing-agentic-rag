"""运行服务：提交问题后立即返回 run_id，Agent 在后台线程执行；进度和结果都从事件日志读取。

状态不放在内存里：一次运行是否结束，看它的最后一条事件是不是 run.finished / run.failed，
结果就是那条事件的 payload。服务重启后，已经结束的运行照样能查到。
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from ..agent.config import PRESETS, RunConfig
from ..core.events import EventLog

TERMINAL = {"run.finished", "run.failed"}
MAX_QUESTION_CHARS = 500


class AgentLike(Protocol):
    events: EventLog

    def run(self, question: str, config: RunConfig, run_id: str | None = None) -> Any: ...


class RunService:
    def __init__(self, agent: AgentLike, workers: int = 2, configs: dict[str, RunConfig] | None = None):
        self.agent = agent
        self.events = agent.events
        self.configs = configs or PRESETS
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent-run")
        self._pending: set[str] = set()  # 已提交、还没写出 run.started 的运行
        self._lock = threading.Lock()

    def submit(self, question: str, config_name: str = "v0.1") -> str:
        question = question.strip()
        if not question:
            raise ValueError("问题不能为空")
        if len(question) > MAX_QUESTION_CHARS:
            raise ValueError(f"问题不能超过 {MAX_QUESTION_CHARS} 字")
        if config_name not in self.configs:
            raise ValueError(f"未知配置 {config_name}，可用：{', '.join(self.configs)}")
        run_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._pending.add(run_id)
        self._executor.submit(self._run, run_id, question, self.configs[config_name])
        return run_id

    def _run(self, run_id: str, question: str, config: RunConfig) -> None:
        try:
            self.agent.run(question, config, run_id)
        except Exception as exc:  # Agent 内部未处理的异常也要落一条终止事件，前端才不会一直等
            self.events.append(run_id, "run.failed", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            with self._lock:
                self._pending.discard(run_id)

    def status(self, run_id: str, after: int = 0) -> dict[str, Any] | None:
        """返回 seq > after 的事件和运行状态；运行不存在时返回 None。"""
        last = self.events.last_event(run_id)
        with self._lock:
            pending = run_id in self._pending
        if last is None and not pending:
            return None
        events = self.events.read(run_id, after)
        done = last is not None and last.type in TERMINAL
        return {
            "run_id": run_id,
            "done": done,
            "last_seq": events[-1].seq if events else after,
            "result": {"type": last.type, **last.payload} if done else None,
            "events": [
                {"seq": e.seq, "ts": e.ts, "type": e.type, "call_id": e.call_id,
                 "parent_call_id": e.parent_call_id, "payload": e.payload}
                for e in events
            ],
        }

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
