"""从事件日志重放一次运行：python -m yuqing.agent.replay [run_id]（默认最近一次）"""

from __future__ import annotations

import argparse
import sys

from ..core.events import EventLog
from .__main__ import EVENTS_PATH
from .trace import format_event


def replay(log: EventLog, run_id: str) -> list[str]:
    lines = []
    for event in log.read(run_id):
        line = format_event(event.type, event.payload)
        if line:
            lines.append(f"{event.seq:>5}  {line}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuqing.agent.replay")
    parser.add_argument("run_id", nargs="?")
    args = parser.parse_args(argv)
    if not EVENTS_PATH.exists():
        print("[FAIL] 还没有事件日志", file=sys.stderr)
        return 1
    log = EventLog(EVENTS_PATH)
    run_id = args.run_id or log.last_run_id()
    lines = replay(log, run_id) if run_id else []
    if not lines:
        print(f"[FAIL] 找不到运行 {run_id}", file=sys.stderr)
        return 1
    print(f"run_id {run_id}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
