"""业务库的完整性检查：只读数据库，返回违规描述列表，空列表表示通过。"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

from . import config as C


def _t(text: str) -> datetime:
    return datetime.strptime(text, C.TIME_FMT)


def integrity(conn: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    accounts = {
        r[0]: (r[1], _t(r[2]))
        for r in conn.execute("SELECT account_id, platform, created_at FROM accounts")
    }
    events = {
        r[0]: (r[1], r[2], _t(r[3]))
        for r in conn.execute("SELECT event_id, level, status, first_seen_at FROM events")
    }
    posts = {
        r[0]: {"event": r[1], "account": r[2], "platform": r[3], "time": _t(r[4]), "parent": r[5]}
        for r in conn.execute(
            "SELECT post_id, event_id, account_id, platform, created_at, parent_post_id FROM posts"
        )
    }
    window = timedelta(hours=C.WINDOW_HOURS)

    by_event: dict[str, list[dict]] = defaultdict(list)
    for post_id, p in sorted(posts.items()):
        by_event[p["event"]].append(p)
        acc_platform, acc_created = accounts[p["account"]]
        if acc_platform != p["platform"]:
            problems.append(f"{post_id}：帖子平台 {p['platform']} 与账号平台 {acc_platform} 不一致")
        if acc_created >= p["time"]:
            problems.append(f"{post_id}：账号 {p['account']} 注册时间不早于发帖时间")
        first_seen = events[p["event"]][2]
        if not first_seen <= p["time"] < first_seen + window:
            problems.append(f"{post_id}：发帖时间不在事件的 72 小时窗口内")
        if p["parent"] is not None:
            parent = posts.get(p["parent"])
            if parent is None:
                problems.append(f"{post_id}：被转发帖 {p['parent']} 不存在")
            elif parent["event"] != p["event"] or parent["platform"] != p["platform"]:
                problems.append(f"{post_id}：被转发帖 {p['parent']} 不在同一事件或同一平台")
            elif parent["time"] >= p["time"]:
                problems.append(f"{post_id}：转发时间不晚于被转发帖 {p['parent']}")

    metrics: dict[str, dict[int, tuple[int, int, int]]] = defaultdict(dict)
    for event_id, hour, post_count, repost_count, unique_accounts in conn.execute(
        "SELECT event_id, hour, post_count, repost_count, unique_accounts FROM metrics_hourly"
    ):
        metrics[event_id][hour] = (post_count, repost_count, unique_accounts)

    for event_id, (level, status, first_seen) in sorted(events.items()):
        rows = by_event.get(event_id, [])
        if not rows:
            problems.append(f"{event_id}：没有帖子")
            continue
        if min(p["time"] for p in rows) != first_seen:
            problems.append(f"{event_id}：first_seen_at 不等于首帖时间")
        expected = [[0, 0, set()] for _ in range(C.WINDOW_HOURS)]
        for p in rows:
            bucket = expected[int((p["time"] - first_seen).total_seconds() // 3600)]
            bucket[0] += 1
            bucket[1] += p["parent"] is not None
            bucket[2].add(p["account"])
        expected_rows = {h: (b[0], b[1], len(b[2])) for h, b in enumerate(expected)}
        if metrics.get(event_id) != expected_rows:
            problems.append(f"{event_id}：metrics_hourly 与帖子聚合结果不一致")
        counts = [expected_rows[h][0] for h in range(C.WINDOW_HOURS)]
        if level != C.level_for(sum(counts)):
            problems.append(f"{event_id}：level {level} 与总帖数 {sum(counts)} 的派生规则不符")
        if status != C.status_for(counts):
            problems.append(f"{event_id}：status {status} 与派生规则不符")
    return problems
