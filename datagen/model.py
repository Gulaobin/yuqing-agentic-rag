"""生成过程中的内存对象。编号（account_id、post_id）在全部数据生成完之后统一分配。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(eq=False)
class Account:
    key: str  # 内部稳定键，如 "base-0001"、"special-media_amp"、"plant-P-001-07"
    platform: str
    created_at: datetime
    followers: int
    verified: bool
    activity: float = 1.0  # 被随机选为发帖人的权重；0 表示不进入随机池
    account_id: str = ""


@dataclass(eq=False)
class Post:
    seq: int  # 全局创建序号，用作同一时刻的排序依据
    event_index: int
    account: Account
    platform: str
    created_at: datetime
    text: str
    sentiment: str
    parent: Optional["Post"] = None
    post_id: str = ""


@dataclass(frozen=True)
class ForcedPost:
    """事件时间线上固定的一条帖子（用于埋入规律）。"""

    offset_seconds: int
    account_key: str
    sentiment: str
    text: str
    parent: Optional[str] = None  # 目前只支持 "first"：转发事件首帖
    boost_subtree: float = 0.0  # >0 时，此后同平台转发以该概率落在这条帖子的转发树内


@dataclass
class EventProfile:
    index: int
    event_id: str
    topic: str
    title: str
    keyword: str
    first_seen: datetime
    volume: int
    peak_hour: float
    rise_sigma: float
    decay_tau: float
    original_prob: float
    platform_schedule: list[tuple[int, dict[str, float]]]
    sentiment_schedule: list[tuple[int, dict[str, float]]]
    cutoff: Optional[tuple[int, float]] = None  # (小时, 系数)：该小时起权重乘以系数
    first_account: Optional[str] = None  # 特殊账号的 key
    forced: list[ForcedPost] = field(default_factory=list)


@dataclass
class Event:
    profile: EventProfile
    posts: list[Post] = field(default_factory=list)
    level: int = 0
    status: str = ""
    hourly: list[tuple[int, int, int]] = field(default_factory=list)  # (帖数, 转发数, 去重账号数)

    @property
    def event_id(self) -> str:
        return self.profile.event_id
