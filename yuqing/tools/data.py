"""业务数据工具：query_events、query_posts。

- 只读连接（SQLite mode=ro），每次调用新建连接；
- 模型只能填参数值：表名、列名、过滤条件都写死在代码里，参数一律绑定，不拼接 SQL。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..core.tools import Tool

TOPICS = ["食品安全", "消费维权", "公共交通", "教育", "医疗", "环境", "企业经营", "网络谣言", "文旅", "城市管理"]
PLATFORMS = ["weibo", "douyin", "xiaohongshu", "news"]
TEXT_LIMIT = 80  # query_posts 返回的正文最多字符数，避免一次塞进几十条全文
HOUR_SQL = "(CAST(strftime('%s', p.created_at) AS INTEGER) - CAST(strftime('%s', e.first_seen_at) AS INTEGER)) / 3600"


class BusinessDB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"业务库 {self.path} 不存在：先运行 python -m datagen")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """只读连接，用完即关。注意 sqlite3 自带的 with 只提交事务、不关闭连接，Windows 上会一直占着文件。"""
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def query_events(self, args: dict[str, Any]) -> dict[str, Any]:
        where, params = [], []
        for column, key in (("event_id", "event_id"), ("topic", "topic"), ("status", "status"), ("level", "level")):
            if key in args:
                where.append(f"{column} = ?")
                params.append(args[key])
        sql = "SELECT event_id, title, topic, level, status, first_seen_at FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
            rows = conn.execute(sql + " ORDER BY event_id LIMIT ?", [*params, args["limit"]]).fetchall()
        return {"total": total, "returned": len(rows), "events": [dict(r) for r in rows]}

    def query_posts(self, args: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            event = conn.execute(
                "SELECT event_id, first_seen_at FROM events WHERE event_id = ?", (args["event_id"],)
            ).fetchone()
            if event is None:
                raise ValueError(f"事件 {args['event_id']} 不存在")
            where, params = ["p.event_id = ?"], [args["event_id"]]
            simple = {"account_id": "p.account_id", "sentiment": "p.sentiment", "platform": "p.platform",
                      "reposts_of": "p.parent_post_id"}
            for key, column in simple.items():
                if key in args:
                    where.append(f"{column} = ?")
                    params.append(args[key])
            if "hour_from" in args:
                where.append(f"{HOUR_SQL} >= ?")
                params.append(args["hour_from"])
            if "hour_to" in args:
                where.append(f"{HOUR_SQL} <= ?")
                params.append(args["hour_to"])
            if "is_repost" in args:
                where.append("p.parent_post_id IS NOT NULL" if args["is_repost"] else "p.parent_post_id IS NULL")
            if "keyword" in args:
                escaped = args["keyword"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where.append("p.text LIKE ? ESCAPE '\\'")
                params.append(f"%{escaped}%")
            base = f"FROM posts p JOIN events e ON e.event_id = p.event_id WHERE {' AND '.join(where)}"
            order = "ASC" if args["order"] == "time_asc" else "DESC"
            total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT p.post_id, p.account_id, p.platform, p.created_at, {HOUR_SQL} AS hour, "
                f"p.sentiment, p.parent_post_id, p.text {base} "
                f"ORDER BY p.created_at {order}, p.post_id {order} LIMIT ?",
                [*params, args["limit"]],
            ).fetchall()
        posts = []
        for row in rows:
            post = dict(row)
            if len(post["text"]) > TEXT_LIMIT:
                post["text"] = post["text"][:TEXT_LIMIT] + "…"
            posts.append(post)
        return {"event_id": event["event_id"], "first_seen_at": event["first_seen_at"], "total": total,
                "returned": len(rows), "posts": posts}

    def get_metrics(self, args: dict[str, Any]) -> dict[str, Any]:
        hour_from, hour_to = args["hour_from"], args["hour_to"]
        if hour_from > hour_to:
            raise ValueError(f"hour_from（{hour_from}）不能大于 hour_to（{hour_to}）")
        with self.connect() as conn:
            event = conn.execute(
                "SELECT event_id, level, status, first_seen_at FROM events WHERE event_id = ?", (args["event_id"],)
            ).fetchone()
            if event is None:
                raise ValueError(f"事件 {args['event_id']} 不存在")
            rows = conn.execute(
                "SELECT hour, post_count, repost_count, unique_accounts FROM metrics_hourly "
                "WHERE event_id = ? ORDER BY hour",
                (args["event_id"],),
            ).fetchall()
        counts = [r["post_count"] for r in rows]
        total = sum(counts)
        peak = max(range(len(counts)), key=lambda h: (counts[h], -h))  # 并列取最早，与 MTH-001 一致
        last_12h = sum(counts[60:72])
        return {
            "event_id": event["event_id"],
            "first_seen_at": event["first_seen_at"],
            "level": event["level"],
            "status": event["status"],
            "summary": {  # 始终按完整 72 小时计算
                "total_posts_72h": total,
                "peak_hour": peak,
                "peak_posts": counts[peak],
                "posts_hour_60_71": last_12h,
                "share_hour_60_71": round(last_12h / total, 4) if total else 0.0,
            },
            "columns": ["hour", "post_count", "repost_count", "unique_accounts"],
            "hourly": [[r["hour"], r["post_count"], r["repost_count"], r["unique_accounts"]]
                       for r in rows if hour_from <= r["hour"] <= hour_to],
        }


def data_tools(db: BusinessDB) -> list[Tool]:
    event_id = {"type": "string", "pattern": "^EVT-\\d{4}$", "description": "事件编号，如 EVT-0007"}
    return [
        Tool(
            name="query_events",
            description="查询舆情事件列表：编号、标题、主题、等级（1 最高）、状态（rising 发酵中 / declining 回落中 / "
                        "subsided 已平息）、首帖时间。可按编号、主题、状态、等级过滤。",
            parameters={
                "type": "object",
                "properties": {
                    "event_id": event_id,
                    "topic": {"type": "string", "enum": TOPICS},
                    "status": {"type": "string", "enum": ["rising", "declining", "subsided"]},
                    "level": {"type": "integer", "minimum": 1, "maximum": 4},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 30},
                },
                "additionalProperties": False,
            },
            handler=db.query_events,
        ),
        Tool(
            name="query_posts",
            description="查询某个事件的帖子：编号、发帖账号、平台、发布时间、距首帖的小时数（hour）、情感、被转发帖编号、正文。"
                        "返回 total（符合条件的总数）和按时间排序的前 limit 条。",
            parameters={
                "type": "object",
                "properties": {
                    "event_id": event_id,
                    "account_id": {"type": "string", "pattern": "^A\\d{5}$"},
                    "hour_from": {"type": "integer", "minimum": 0, "maximum": 71, "description": "距首帖小时数下限（含）"},
                    "hour_to": {"type": "integer", "minimum": 0, "maximum": 71, "description": "距首帖小时数上限（含）"},
                    "sentiment": {"type": "string", "enum": ["negative", "neutral", "positive"]},
                    "platform": {"type": "string", "enum": PLATFORMS},
                    "is_repost": {"type": "boolean", "description": "true 只看转发，false 只看原帖"},
                    "reposts_of": {"type": "string", "pattern": "^P\\d{4}-\\d{4}$", "description": "只看直接转发这条帖子的帖子"},
                    "keyword": {"type": "string", "minLength": 1, "maxLength": 20, "description": "正文包含的关键词"},
                    "order": {"type": "string", "enum": ["time_asc", "time_desc"], "default": "time_asc"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
            handler=db.query_posts,
        ),
        Tool(
            name="get_metrics",
            description="查询某个事件的逐小时指标：每小时帖子数（含转发）、转发数、去重账号数，以及 72 小时汇总"
                        "（总帖数、峰值小时及帖子数、第 60–71 小时帖子数与占比）、等级和状态。"
                        "统计数量、看热度变化时用它，不要用 query_posts 逐段计数。",
            parameters={
                "type": "object",
                "properties": {
                    "event_id": event_id,
                    "hour_from": {"type": "integer", "minimum": 0, "maximum": 71, "default": 0},
                    "hour_to": {"type": "integer", "minimum": 0, "maximum": 71, "default": 71},
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
            handler=db.get_metrics,
        ),
    ]
