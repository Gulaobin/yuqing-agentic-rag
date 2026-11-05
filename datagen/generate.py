"""生成流程：账号 → 事件画像 → 逐事件模拟帖子 → 注入规律 → 分配编号 → 聚合指标 → 写库 → 校验。

确定性约束：
- 只用 random.Random 实例，按 "种子:用途" 派生子生成器，不用全局 random、不读当前时间；
- 不遍历 set/dict 的键集合来决定顺序，需要顺序的地方都显式排序；
- 一致性按 sqlite3 iterdump() 的哈希判断，而不是 .db 文件字节。
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import accumulate, count
from typing import Iterator
from pathlib import Path

from . import checks, plants
from . import config as C
from .model import Account, Event, EventProfile, Post
from .text import original_text, repost_text

DB_NAME = "yuqing.sqlite"
MANIFEST_NAME = "manifest.json"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass
class Result:
    db_path: Path
    manifest_path: Path
    manifest: dict


def generate(seed: int, out_dir: Path) -> Result:
    out_dir.mkdir(parents=True, exist_ok=True)

    accounts = make_base_accounts(random.Random(f"{seed}:accounts"))
    accounts += make_background_accounts(random.Random(f"{seed}:background"))
    specials = plants.special_accounts()
    pools = build_pools(accounts)

    seq = count(1)  # 帖子的全局创建序号，每次生成从 1 开始
    events = []
    for profile in make_profiles(seed):
        plants.customize(profile)
        posts = simulate_event(profile, pools, specials, random.Random(f"{seed}:{profile.event_id}"), seq)
        events.append(Event(profile=profile, posts=posts))

    planted_accounts = plants.inject(events, random.Random(f"{seed}:plants"), seq)
    all_accounts = accounts + list(specials.values()) + planted_accounts
    assign_ids(all_accounts, events)
    for event in events:
        aggregate(event)

    tmp_path = out_dir / f".{DB_NAME}.tmp"
    if tmp_path.exists():
        tmp_path.unlink()
    conn = sqlite3.connect(tmp_path)
    try:
        write_db(conn, all_accounts, events)
        problems = checks.integrity(conn)
        if problems:
            raise RuntimeError("完整性检查失败：\n" + "\n".join(problems[:20]))
        plant_results = plants.verify_all(conn)
        failed = [r for r in plant_results if not r["ok"]]
        if failed:
            raise RuntimeError("埋入规律反查失败：" + json.dumps(failed, ensure_ascii=False))
        dump_sha = dump_hash(conn)
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("events", "accounts", "posts", "metrics_hourly")
        }
    finally:
        conn.close()

    db_path = out_dir / DB_NAME
    os.replace(tmp_path, db_path)
    manifest = build_manifest(seed, counts, plant_results, dump_sha)
    manifest_path = out_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return Result(db_path=db_path, manifest_path=manifest_path, manifest=manifest)


# ---------- 账号 ----------


def random_time(rng: random.Random, start: datetime, end: datetime) -> datetime:
    return start + timedelta(seconds=rng.randrange(int((end - start).total_seconds())))


def make_base_accounts(rng: random.Random) -> list[Account]:
    platforms = list(C.ACCOUNT_PLATFORM_WEIGHTS)
    weights = [C.ACCOUNT_PLATFORM_WEIGHTS[p] for p in platforms]
    accounts = []
    for i in range(C.N_BASE_ACCOUNTS):
        platform = rng.choices(platforms, weights=weights)[0]
        if platform == "news":  # 媒体号：粉丝多、多数认证
            followers = int(rng.lognormvariate(math.log(80_000), 1.2))
            verified = rng.random() < 0.8
        else:
            followers = int(rng.lognormvariate(math.log(300), 1.6))
            verified = rng.random() < 0.03
        accounts.append(
            Account(
                key=f"base-{i:04d}",
                platform=platform,
                created_at=random_time(rng, C.ACCOUNT_CREATED_FROM, C.ACCOUNT_CREATED_UNTIL),
                followers=followers,
                verified=verified,
                activity=rng.lognormvariate(0.0, 1.0),
            )
        )
    return accounts


def make_background_accounts(rng: random.Random) -> list[Account]:
    """事件期间正常注册的账号：粉丝少、很少认证，注册后才会被选为发帖人。"""
    platforms = list(C.ACCOUNT_PLATFORM_WEIGHTS)
    weights = [C.ACCOUNT_PLATFORM_WEIGHTS[p] for p in platforms]
    accounts = []
    for i in range(C.N_BACKGROUND_ACCOUNTS):
        accounts.append(
            Account(
                key=f"bg-{i:04d}",
                platform=rng.choices(platforms, weights=weights)[0],
                created_at=random_time(rng, C.BACKGROUND_CREATED_FROM, C.BACKGROUND_CREATED_UNTIL),
                followers=int(rng.lognormvariate(math.log(60), 1.3)),
                verified=rng.random() < 0.01,
                activity=rng.lognormvariate(0.0, 1.0),
            )
        )
    return accounts


class Pool:
    """某平台的随机发帖人池，按 activity 加权抽样；只会选到发帖时已经注册的账号。"""

    MAX_TRIES = 20

    def __init__(self, accounts: list[Account]):
        self.accounts = accounts
        self.cum = list(accumulate(a.activity for a in accounts))

    def pick(self, rng: random.Random, before: datetime) -> Account:
        for _ in range(self.MAX_TRIES):
            account = self.accounts[bisect.bisect_right(self.cum, rng.random() * self.cum[-1])]
            if account.created_at < before:
                return account
        # 连续抽到尚未注册的账号时，退回到池中第一个已注册的账号（确定性的兜底）
        return next(a for a in self.accounts if a.created_at < before)


def build_pools(accounts: list[Account]) -> dict[str, Pool]:
    return {
        platform: Pool([a for a in accounts if a.platform == platform and a.activity > 0])
        for platform in C.PLATFORMS
    }


# ---------- 事件画像 ----------


def make_profiles(seed: int) -> list[EventProfile]:
    rng = random.Random(f"{seed}:profiles")
    profiles = []
    for index, (topic, title, keyword) in enumerate(C.EVENT_CATALOG):
        first_seen = C.BASE_TIME + timedelta(
            days=2 * index, hours=rng.randrange(7, 23), minutes=rng.randrange(60), seconds=rng.randrange(60)
        )
        weibo = rng.uniform(0.45, 0.70)
        news = rng.uniform(0.03, 0.08)
        douyin = rng.uniform(0.1, 1 - weibo - news - 0.05)
        negative = rng.uniform(0.35, 0.70)
        positive = rng.uniform(0.05, 0.20)
        profiles.append(
            EventProfile(
                index=index,
                event_id=C.event_id_for(index),
                topic=topic,
                title=title,
                keyword=keyword,
                first_seen=first_seen,
                volume=int(min(650, max(80, rng.lognormvariate(math.log(180), 0.55)))),
                # 普通事件的峰值都在前 24 小时、衰减较快，rising 状态只来自埋入的 P-004
                peak_hour=rng.uniform(3, 24),
                rise_sigma=rng.uniform(2, 6),
                decay_tau=rng.uniform(5, 14),
                original_prob=rng.uniform(0.25, 0.40),
                platform_schedule=[
                    (0, {"weibo": weibo, "douyin": douyin, "news": news,
                         "xiaohongshu": 1 - weibo - douyin - news})
                ],
                sentiment_schedule=[
                    (0, {"negative": negative, "positive": positive, "neutral": 1 - negative - positive})
                ],
            )
        )
    return profiles


def hourly_weights(profile: EventProfile) -> list[float]:
    weights = []
    for hour in range(C.WINDOW_HOURS):
        x = hour + 0.5
        if x <= profile.peak_hour:
            w = math.exp(-((x - profile.peak_hour) ** 2) / (2 * profile.rise_sigma**2))
        else:
            w = math.exp(-(x - profile.peak_hour) / profile.decay_tau)
        if profile.cutoff and hour >= profile.cutoff[0]:
            w *= profile.cutoff[1]
        weights.append(w + 0.002)
    return weights


def scheduled(schedule: list[tuple[int, dict[str, float]]], hour: int) -> dict[str, float]:
    current = schedule[0][1]
    for start, weights in schedule:
        if hour >= start:
            current = weights
    return current


def pick_weighted(rng: random.Random, weights: dict[str, float]) -> str:
    keys = sorted(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys])[0]


# ---------- 逐事件模拟 ----------


def simulate_event(
    profile: EventProfile,
    pools: dict[str, Pool],
    specials: dict[str, Account],
    rng: random.Random,
    seq: Iterator[int],
) -> list[Post]:
    window = C.WINDOW_HOURS * 3600
    hours = rng.choices(range(C.WINDOW_HOURS), weights=hourly_weights(profile), k=profile.volume - 1)
    slots = [(0, 0, None)] + [(h * 3600 + rng.randrange(3600), 1, None) for h in hours]
    slots += [(f.offset_seconds, 0, f) for f in profile.forced]
    slots.sort(key=lambda s: (s[0], s[1]))
    # 时间严格递增：同一秒的帖子顺延 1 秒，保证转发一定晚于被转发帖
    for i in range(1, len(slots)):
        if slots[i][0] <= slots[i - 1][0]:
            slots[i] = (slots[i - 1][0] + 1, *slots[i][1:])

    posts: list[Post] = []
    by_platform: dict[str, list[Post]] = {p: [] for p in C.PLATFORMS}
    received: dict[int, int] = {}
    subtree: list[Post] = []
    subtree_ids: set[int] = set()
    boost = 0.0

    for i, (offset, _, forced) in enumerate(slots):
        if offset >= window:
            continue
        created_at = profile.first_seen + timedelta(seconds=offset)
        hour = offset // 3600
        sentiment = pick_weighted(rng, scheduled(profile.sentiment_schedule, hour))
        parent = None

        if forced is not None:
            account = specials[forced.account_key]
            platform = account.platform
            sentiment = forced.sentiment
            if forced.parent == "first":
                parent = posts[0]
            text = forced.text.format(title=profile.title, kw=profile.keyword)
        else:
            if i == 0 and profile.first_account:
                account = specials[profile.first_account]
                platform = account.platform
            else:
                platform = pick_weighted(rng, scheduled(profile.platform_schedule, hour))
                account = pools[platform].pick(rng, created_at)
            if i > 0 and rng.random() >= profile.original_prob:
                parent = choose_parent(rng, by_platform[platform], received, subtree, boost)
            if parent is None:
                text = original_text(rng, sentiment, profile.title, profile.keyword)
            else:
                text = repost_text(rng, sentiment, profile.keyword)

        post = Post(
            seq=next(seq),
            event_index=profile.index,
            account=account,
            platform=platform,
            created_at=created_at,
            text=text,
            sentiment=sentiment,
            parent=parent,
        )
        posts.append(post)
        by_platform[platform].append(post)
        if parent is not None:
            received[parent.seq] = received.get(parent.seq, 0) + 1
            if parent.seq in subtree_ids:
                subtree.append(post)
                subtree_ids.add(post.seq)
        if forced is not None and forced.boost_subtree > 0:
            subtree.append(post)
            subtree_ids.add(post.seq)
            boost = forced.boost_subtree
    return posts


def choose_parent(
    rng: random.Random, candidates: list[Post], received: dict[int, int], subtree: list[Post], boost: float
) -> Post | None:
    """转发只发生在同平台的更早帖子之间；被转发越多越容易被再转发。"""
    if not candidates:
        return None
    if subtree and boost and subtree[0].platform == candidates[0].platform and rng.random() < boost:
        return rng.choice(subtree)
    weights = [1 + 3 * received.get(c.seq, 0) for c in candidates]
    return rng.choices(candidates, weights=weights)[0]


# ---------- 编号与聚合 ----------


def assign_ids(accounts: list[Account], events: list[Event]) -> None:
    """账号按注册时间编号（编号越大注册越晚），帖子按事件、时间编号。"""
    for n, account in enumerate(sorted(accounts, key=lambda a: (a.created_at, a.key)), start=1):
        account.account_id = f"A{n:05d}"
    for event in events:
        event.posts.sort(key=lambda p: (p.created_at, p.seq))
        for n, post in enumerate(event.posts, start=1):
            post.post_id = f"P{event.profile.index + 1:04d}-{n:04d}"


def hour_of(event: Event, when: datetime) -> int:
    return int((when - event.profile.first_seen).total_seconds() // 3600)


def aggregate(event: Event) -> None:
    buckets = [[0, 0, set()] for _ in range(C.WINDOW_HOURS)]
    for post in event.posts:
        bucket = buckets[hour_of(event, post.created_at)]
        bucket[0] += 1
        bucket[1] += post.parent is not None
        bucket[2].add(post.account.account_id)
    event.hourly = [(b[0], b[1], len(b[2])) for b in buckets]
    counts = [h[0] for h in event.hourly]
    event.level = C.level_for(sum(counts))
    event.status = C.status_for(counts)


# ---------- 写库 ----------


def fmt(when: datetime) -> str:
    return when.strftime(C.TIME_FMT)


def write_db(conn: sqlite3.Connection, accounts: list[Account], events: list[Event]) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    with conn:
        conn.executemany(
            "INSERT INTO accounts VALUES (?, ?, ?, ?, ?)",
            [
                (a.account_id, a.platform, fmt(a.created_at), a.followers, int(a.verified))
                for a in sorted(accounts, key=lambda a: a.account_id)
            ],
        )
        conn.executemany(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
            [
                (e.event_id, e.profile.title, e.profile.topic, e.level, e.status, fmt(e.profile.first_seen))
                for e in events
            ],
        )
        conn.executemany(
            "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (p.post_id, e.event_id, p.account.account_id, p.platform, fmt(p.created_at), p.text,
                 p.sentiment, p.parent.post_id if p.parent else None)
                for e in events
                for p in e.posts
            ],
        )
        conn.executemany(
            "INSERT INTO metrics_hourly VALUES (?, ?, ?, ?, ?, ?)",
            [
                (e.event_id, hour, fmt(e.profile.first_seen + timedelta(hours=hour)), *e.hourly[hour])
                for e in events
                for hour in range(C.WINDOW_HOURS)
            ],
        )


def dump_hash(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for line in conn.iterdump():
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_manifest(seed: int, counts: dict, plant_results: list[dict], dump_sha: str) -> dict:
    return {
        "generator_version": C.GENERATOR_VERSION,
        "seed": seed,
        "timezone": "Asia/Shanghai（时间字段不带时区）",
        "window_hours": C.WINDOW_HOURS,
        "counts": counts,
        "rules": {
            "heat": "metrics_hourly.post_count：该小时全部帖子数（含转发）；repost_count 为其中的转发数",
            "level": "72 小时总帖数 >= 600 为 1 级，>= 350 为 2 级，>= 150 为 3 级，其余 4 级",
            "status": "峰值小时 >= 48 为 rising；否则最后 12 小时帖数占比 <= 2% 为 subsided；其余 declining",
            "new_account": f"发帖时注册不足 {C.NEW_ACCOUNT_DAYS} 天",
        },
        "plants": plant_results,
        "sha256": {f"{DB_NAME}.iterdump": dump_sha},
    }
