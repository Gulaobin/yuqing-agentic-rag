"""埋入的规律：评测的标准答案。

每条规律分三部分：
- customize：改事件画像（时间形态、平台、情感、首发账号、固定帖子）；
- inject：在模拟结束后追加帖子和账号（集中转发、分散新号）；
- verify：写库后只用 SQL 读出数据反查，规律不成立就让生成失败；反查得到的事实写进 manifest。
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta
from typing import Callable, Iterator

from . import config as C
from .model import Account, Event, EventProfile, ForcedPost, Post

PLANTS: tuple[dict, ...] = (
    {"plant_id": "P-001", "kind": "coordinated_repost", "event_ids": ["EVT-0007"],
     "description": "40 个注册不足 7 天的账号在 5 分钟内转发同一条帖子，正文完全相同；全库只有这一组满足协同转发判定"},
    {"plant_id": "P-002", "kind": "decay_after", "event_ids": ["EVT-0012"],
     "description": "峰值出现在第 30 小时之前，第 30 小时起每小时帖数不超过峰值的 20%"},
    {"plant_id": "P-003", "kind": "first_poster_amplified", "event_ids": ["EVT-0003"],
     "description": "首帖来自粉丝不足 100 的未认证账号，2 小时内被百万粉认证媒体号转发，该转发引出的转发树占全部转发的一半以上"},
    {"plant_id": "P-004", "kind": "still_rising", "event_ids": ["EVT-0021"],
     "description": "峰值出现在第 60 小时之后，状态为 rising，且是唯一 rising 的事件"},
    {"plant_id": "P-005", "kind": "sentiment_flip", "event_ids": ["EVT-0015"],
     "description": "前 24 小时负面占比 >= 70%，第 36 小时起负面占比 <= 30%"},
    {"plant_id": "P-006", "kind": "platform_migration", "event_ids": ["EVT-0009"],
     "description": "前 12 小时小红书帖子占比 >= 80%，第 24 小时起微博帖子占比 >= 70%"},
    {"plant_id": "P-007", "kind": "dispersed_new_accounts", "event_ids": ["EVT-0018"],
     "description": "额外埋入 12 个注册不足 7 天的账号分散转发，任意 10 分钟内同一帖子的新号转发不超过 2 个；用于检验不误判协同转发"},
    {"plant_id": "P-008", "kind": "serial_first_poster", "event_ids": ["EVT-0005", "EVT-0011", "EVT-0024"],
     "description": "同一个账号是这三个事件的首发账号，且不是其他任何事件的首发账号"},
)

COORDINATED_TEXT = "支持禾沐科技，别听信谣言，正常的业务调整而已。"


def special_accounts() -> dict[str, Account]:
    """规律里扮演固定角色的账号，取值写死，不进入随机发帖池。"""
    return {
        "small_first": Account("special-small_first", "weibo", datetime(2023, 5, 14, 21, 7, 33), 37, False, 0.0),
        "media_amp": Account("special-media_amp", "weibo", datetime(2016, 3, 2, 10, 0, 0), 2_380_000, True, 0.0),
        "serial_first": Account("special-serial_first", "weibo", datetime(2021, 11, 20, 8, 15, 0), 5_200, False, 0.0),
    }


def weibo_heavy(share: float) -> list[tuple[int, dict[str, float]]]:
    rest = 1 - share
    return [(0, {"weibo": share, "douyin": rest * 0.5, "xiaohongshu": rest * 0.35, "news": rest * 0.15})]


def customize(profile: EventProfile) -> None:
    eid = profile.event_id
    if eid == "EVT-0003":
        profile.volume, profile.peak_hour, profile.rise_sigma, profile.decay_tau = 350, 8, 3, 14
        profile.platform_schedule = weibo_heavy(0.85)
        profile.first_account = "small_first"
        profile.forced = [
            ForcedPost(
                offset_seconds=5400,
                account_key="media_amp",
                sentiment="neutral",
                text="#{kw}# 网友反映：{title}。本台记者正在进一步核实。",
                parent="first",
                boost_subtree=0.6,
            )
        ]
    elif eid in ("EVT-0005", "EVT-0011", "EVT-0024"):
        profile.first_account = "serial_first"
    elif eid == "EVT-0007":
        profile.volume, profile.peak_hour, profile.rise_sigma, profile.decay_tau = 300, 12, 4, 12
        profile.platform_schedule = weibo_heavy(0.8)
    elif eid == "EVT-0009":
        profile.volume, profile.peak_hour, profile.rise_sigma, profile.decay_tau = 300, 14, 5, 16
        profile.platform_schedule = [
            (0, {"xiaohongshu": 0.95, "weibo": 0.03, "douyin": 0.02}),  # 样本只有约 50 条，留足余量
            (12, {"xiaohongshu": 0.45, "weibo": 0.45, "douyin": 0.10}),
            (24, {"weibo": 0.85, "xiaohongshu": 0.08, "douyin": 0.05, "news": 0.02}),
        ]
    elif eid == "EVT-0012":
        profile.volume, profile.peak_hour, profile.rise_sigma, profile.decay_tau = 300, 22, 5, 15
        profile.cutoff = (30, 0.03)
    elif eid == "EVT-0015":
        profile.volume, profile.peak_hour, profile.rise_sigma, profile.decay_tau = 400, 10, 3, 20
        profile.sentiment_schedule = [
            (0, {"negative": 0.82, "neutral": 0.14, "positive": 0.04}),
            (24, {"negative": 0.50, "neutral": 0.30, "positive": 0.20}),
            (36, {"negative": 0.12, "neutral": 0.38, "positive": 0.50}),
        ]
    elif eid == "EVT-0018":
        profile.volume, profile.peak_hour, profile.rise_sigma, profile.decay_tau = 200, 8, 3, 14
        profile.platform_schedule = weibo_heavy(0.8)
    elif eid == "EVT-0021":
        profile.volume, profile.peak_hour, profile.rise_sigma, profile.decay_tau = 320, 67, 5, 20


def _new_account(rng: random.Random, key: str, post_time: datetime) -> Account:
    age = timedelta(seconds=rng.randrange(86_400, int(6.5 * 86_400)))
    return Account(key, "weibo", post_time - age, rng.randrange(0, 30), False, 0.0)


def inject(events: list[Event], rng: random.Random, seq: Iterator[int]) -> list[Account]:
    by_id = {e.event_id: e for e in events}
    created: list[Account] = []

    # P-001：集中转发
    event = by_id["EVT-0007"]
    start = event.profile.first_seen + timedelta(hours=8)
    end = event.profile.first_seen + timedelta(hours=14)
    targets = sorted(
        (p for p in event.posts if p.parent is None and p.platform == "weibo" and start <= p.created_at < end),
        key=lambda p: (p.created_at, p.seq),
    )
    if not targets:
        raise RuntimeError("P-001：EVT-0007 第 8–14 小时内没有微博原帖可作为转发目标")
    target = targets[0]
    burst_start = target.created_at + timedelta(seconds=600)
    for k, offset in enumerate(sorted(rng.sample(range(300), 40))):
        when = burst_start + timedelta(seconds=offset)
        account = _new_account(rng, f"plant-P-001-{k:02d}", when)
        created.append(account)
        event.posts.append(
            Post(next(seq), event.profile.index, account, "weibo", when, COORDINATED_TEXT, "positive", target)
        )

    # P-007：分散的新号，约每 75 分钟一个，相邻间隔至少 55 分钟
    event = by_id["EVT-0018"]
    for k in range(12):
        when = event.profile.first_seen + timedelta(seconds=5 * 3600 + k * 75 * 60 + rng.randrange(-600, 600))
        earlier = sorted(
            (p for p in event.posts if p.platform == "weibo" and p.created_at < when and p.account.activity > 0),
            key=lambda p: (p.created_at, p.seq),
        )
        if not earlier:
            raise RuntimeError("P-007：EVT-0018 找不到更早的微博帖子可转发")
        account = _new_account(rng, f"plant-P-007-{k:02d}", when)
        created.append(account)
        sentiment = rng.choice(C.SENTIMENTS)
        event.posts.append(
            Post(next(seq), event.profile.index, account, "weibo", when, "转发", sentiment, rng.choice(earlier))
        )
    return created


# ---------- 反查 ----------


def _t(text: str) -> datetime:
    return datetime.strptime(text, C.TIME_FMT)


def _first_seen(conn: sqlite3.Connection, event_id: str) -> datetime:
    return _t(conn.execute("SELECT first_seen_at FROM events WHERE event_id = ?", (event_id,)).fetchone()[0])


def _hourly(conn: sqlite3.Connection, event_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT post_count FROM metrics_hourly WHERE event_id = ? ORDER BY hour", (event_id,)
    ).fetchall()
    return [r[0] for r in rows]


def _peak(hourly: list[int]) -> int:
    return max(range(len(hourly)), key=lambda h: (hourly[h], -h))


def _posts_with_hour(conn: sqlite3.Connection, event_id: str, columns: str) -> list[tuple]:
    first_seen = _first_seen(conn, event_id)
    rows = conn.execute(f"SELECT created_at, {columns} FROM posts WHERE event_id = ?", (event_id,)).fetchall()
    return [(int((_t(r[0]) - first_seen).total_seconds() // 3600), *r[1:]) for r in rows]


def _new_account_reposts(conn: sqlite3.Connection, event_id: str | None = None) -> list[tuple]:
    sql = (
        "SELECT p.event_id, p.post_id, p.created_at, p.account_id, a.created_at, p.parent_post_id, p.text "
        "FROM posts p JOIN accounts a ON a.account_id = p.account_id WHERE p.parent_post_id IS NOT NULL"
    )
    rows = conn.execute(sql + (" AND p.event_id = ?" if event_id else ""), (event_id,) if event_id else ()).fetchall()
    limit = timedelta(days=C.NEW_ACCOUNT_DAYS)
    return sorted(r for r in rows if _t(r[2]) - _t(r[4]) < limit)


def _first_poster(conn: sqlite3.Connection, event_id: str) -> tuple[str, str]:
    return conn.execute(
        "SELECT post_id, account_id FROM posts WHERE event_id = ? ORDER BY created_at, post_id LIMIT 1",
        (event_id,),
    ).fetchone()


def _windows(rows: list[tuple]) -> dict[tuple[str, str], list[tuple]]:
    """新号转发按 (事件, 被转发帖) 分组，返回每组内 10 分钟窗口里转发最多的那批记录。"""
    groups: dict[tuple[str, str], list[tuple]] = {}
    for row in rows:
        groups.setdefault((row[0], row[5]), []).append(row)
    window = timedelta(minutes=C.COORDINATION_WINDOW_MINUTES)
    best: dict[tuple[str, str], list[tuple]] = {}
    for key, items in groups.items():
        items.sort(key=lambda r: (r[2], r[1]))
        left, top = 0, (0, 0)
        for right in range(len(items)):
            while _t(items[right][2]) - _t(items[left][2]) >= window:
                left += 1
            if right - left + 1 > top[1] - top[0]:
                top = (left, right + 1)
        best[key] = items[top[0]:top[1]]
    return best


def coordinated_groups(conn: sqlite3.Connection) -> dict[tuple[str, str], list[tuple]]:
    """全库满足疑似协同转发判定的分组（与 MTH-002 一致）。"""
    return {k: v for k, v in _windows(_new_account_reposts(conn)).items()
            if len({r[3] for r in v}) >= C.COORDINATION_MIN_ACCOUNTS}


def verify_coordinated(conn: sqlite3.Connection) -> tuple[bool, dict, list[str]]:
    groups = coordinated_groups(conn)
    keys = sorted(groups)
    members = groups.get(keys[0], []) if len(keys) == 1 else []
    times = [_t(r[2]) for r in members]
    accounts = sorted({r[3] for r in members})
    texts = sorted({r[6] for r in members})
    ok = (
        len(keys) == 1
        and keys[0][0] == "EVT-0007"
        and len(accounts) == 40
        and len(texts) == 1
        and (max(times) - min(times)).total_seconds() < 300
    )
    all_new = _new_account_reposts(conn)
    facts = {
        "coordinated_groups": [list(k) for k in keys],
        "target_post_id": keys[0][1] if len(keys) == 1 else None,
        "new_account_count": len(accounts),
        "window_start": min(times).strftime(C.TIME_FMT) if times else None,
        "window_end": max(times).strftime(C.TIME_FMT) if times else None,
        "text": texts[0] if len(texts) == 1 else None,
        "events_with_new_account_reposts": len({r[0] for r in all_new}),
        "new_account_reposts_total": len(all_new),
    }
    return ok, facts, ([keys[0][1]] if len(keys) == 1 else []) + accounts


def verify_decay(conn: sqlite3.Connection) -> tuple[bool, dict, list[str]]:
    hourly = _hourly(conn, "EVT-0012")
    peak = _peak(hourly)
    after = max(hourly[30:])
    ok = peak < 30 and after <= 0.2 * hourly[peak]
    facts = {"peak_hour": peak, "peak_posts": hourly[peak], "max_posts_from_hour_30": after,
             "ratio": round(after / hourly[peak], 3)}
    return ok, facts, ["EVT-0012"]


def verify_amplified(conn: sqlite3.Connection) -> tuple[bool, dict, list[str]]:
    first_post, first_account = _first_poster(conn, "EVT-0003")
    followers, verified = conn.execute(
        "SELECT followers, verified FROM accounts WHERE account_id = ?", (first_account,)
    ).fetchone()
    first_time = _t(conn.execute("SELECT created_at FROM posts WHERE post_id = ?", (first_post,)).fetchone()[0])
    amps = conn.execute(
        "SELECT p.post_id, p.account_id, p.created_at, a.followers FROM posts p "
        "JOIN accounts a ON a.account_id = p.account_id "
        "WHERE p.parent_post_id = ? AND a.verified = 1 AND a.followers >= 1000000 ORDER BY p.created_at",
        (first_post,),
    ).fetchall()
    amps = [a for a in amps if _t(a[2]) - first_time <= timedelta(hours=2)]
    if len(amps) != 1:
        return False, {"amplifier_candidates": len(amps)}, [first_post]
    amp_post, amp_account, amp_time, amp_followers = amps[0]
    subtree = conn.execute(
        "WITH RECURSIVE tree(post_id) AS ("
        "  SELECT post_id FROM posts WHERE parent_post_id = ?"
        "  UNION ALL SELECT p.post_id FROM posts p JOIN tree t ON p.parent_post_id = t.post_id"
        ") SELECT COUNT(*) FROM tree",
        (amp_post,),
    ).fetchone()[0] + 1  # 含媒体号这条转发本身
    total = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE event_id = 'EVT-0003' AND parent_post_id IS NOT NULL"
    ).fetchone()[0]
    share = subtree / total
    ok = followers < 100 and verified == 0 and share > 0.5
    facts = {
        "first_post_id": first_post, "first_account_id": first_account, "first_account_followers": followers,
        "amplifier_post_id": amp_post, "amplifier_account_id": amp_account, "amplifier_followers": amp_followers,
        "minutes_after_first": int((_t(amp_time) - first_time).total_seconds() // 60),
        "amplifier_subtree_reposts": subtree, "total_reposts": total, "share": round(share, 3),
    }
    return ok, facts, [first_post, first_account, amp_post, amp_account]


def verify_rising(conn: sqlite3.Connection) -> tuple[bool, dict, list[str]]:
    hourly = _hourly(conn, "EVT-0021")
    peak = _peak(hourly)
    rising = [r[0] for r in conn.execute("SELECT event_id FROM events WHERE status = 'rising' ORDER BY event_id")]
    ok = peak >= 60 and rising == ["EVT-0021"]
    return ok, {"peak_hour": peak, "peak_posts": hourly[peak], "rising_events": rising}, ["EVT-0021"]


def _share(rows: list[tuple], predicate: Callable[[tuple], bool], value_index: int, value: str) -> tuple[float, int]:
    picked = [r for r in rows if predicate(r)]
    if not picked:
        return 0.0, 0
    return sum(r[value_index] == value for r in picked) / len(picked), len(picked)


def verify_sentiment_flip(conn: sqlite3.Connection) -> tuple[bool, dict, list[str]]:
    rows = _posts_with_hour(conn, "EVT-0015", "sentiment")
    early, n_early = _share(rows, lambda r: r[0] < 24, 1, "negative")
    late, n_late = _share(rows, lambda r: r[0] >= 36, 1, "negative")
    ok = early >= 0.7 and late <= 0.3 and n_late >= 20
    facts = {"negative_share_before_24h": round(early, 3), "posts_before_24h": n_early,
             "negative_share_from_36h": round(late, 3), "posts_from_36h": n_late}
    return ok, facts, ["EVT-0015"]


def verify_migration(conn: sqlite3.Connection) -> tuple[bool, dict, list[str]]:
    rows = _posts_with_hour(conn, "EVT-0009", "platform")
    early, n_early = _share(rows, lambda r: r[0] < 12, 1, "xiaohongshu")
    late, n_late = _share(rows, lambda r: r[0] >= 24, 1, "weibo")
    ok = early >= 0.8 and late >= 0.7
    facts = {"xiaohongshu_share_before_12h": round(early, 3), "posts_before_12h": n_early,
             "weibo_share_from_24h": round(late, 3), "posts_from_24h": n_late}
    return ok, facts, ["EVT-0009"]


def verify_dispersed(conn: sqlite3.Connection) -> tuple[bool, dict, list[str]]:
    rows = _new_account_reposts(conn, "EVT-0018")
    windows = {k: v for k, v in _windows(rows).items()}
    max_same_parent = max((len({r[3] for r in v}) for v in windows.values()), default=0)
    accounts = sorted({r[3] for r in rows})
    ok = len(accounts) >= 12 and max_same_parent <= 2
    facts = {"new_account_count": len(accounts), "max_new_accounts_same_post_10min": max_same_parent}
    return ok, facts, accounts


def verify_serial(conn: sqlite3.Connection) -> tuple[bool, dict, list[str]]:
    event_ids = [r[0] for r in conn.execute("SELECT event_id FROM events ORDER BY event_id")]
    firsts = {eid: _first_poster(conn, eid) for eid in event_ids}
    expected = ["EVT-0005", "EVT-0011", "EVT-0024"]
    account = firsts[expected[0]][1]
    led = [eid for eid in event_ids if firsts[eid][1] == account]
    ok = led == expected
    facts = {"account_id": account, "first_post_ids": [firsts[e][0] for e in expected], "led_events": led}
    return ok, facts, [account] + [firsts[e][0] for e in expected]


VERIFIERS: dict[str, Callable[[sqlite3.Connection], tuple[bool, dict, list[str]]]] = {
    "coordinated_repost": verify_coordinated,
    "decay_after": verify_decay,
    "first_poster_amplified": verify_amplified,
    "still_rising": verify_rising,
    "sentiment_flip": verify_sentiment_flip,
    "platform_migration": verify_migration,
    "dispersed_new_accounts": verify_dispersed,
    "serial_first_poster": verify_serial,
}


def verify_all(conn: sqlite3.Connection) -> list[dict]:
    results = []
    for plant in PLANTS:
        ok, facts, evidence = VERIFIERS[plant["kind"]](conn)
        results.append({**plant, "ok": ok, "facts": facts, "evidence": evidence})
    return results
