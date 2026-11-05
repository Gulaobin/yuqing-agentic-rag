-- 只读业务库。时间均为 Asia/Shanghai 本地时间，格式 YYYY-MM-DD HH:MM:SS。

CREATE TABLE accounts (
    account_id  TEXT PRIMARY KEY,                -- A00001 起，按注册时间递增
    platform    TEXT NOT NULL CHECK (platform IN ('weibo', 'douyin', 'xiaohongshu', 'news')),
    created_at  TEXT NOT NULL,
    followers   INTEGER NOT NULL CHECK (followers >= 0),
    verified    INTEGER NOT NULL CHECK (verified IN (0, 1))
);

CREATE TABLE events (
    event_id      TEXT PRIMARY KEY,              -- EVT-0001 起，按首帖时间递增
    title         TEXT NOT NULL,
    topic         TEXT NOT NULL,
    level         INTEGER NOT NULL CHECK (level BETWEEN 1 AND 4),   -- 1 级最高，由总帖数派生
    status        TEXT NOT NULL CHECK (status IN ('rising', 'declining', 'subsided')),
    first_seen_at TEXT NOT NULL                  -- 等于该事件首帖的 created_at
);

CREATE TABLE posts (
    post_id        TEXT PRIMARY KEY,             -- P<事件序号>-<事件内序号>
    event_id       TEXT NOT NULL REFERENCES events (event_id),
    account_id     TEXT NOT NULL REFERENCES accounts (account_id),
    platform       TEXT NOT NULL,                -- 与发帖账号的平台一致
    created_at     TEXT NOT NULL,
    text           TEXT NOT NULL,
    sentiment      TEXT NOT NULL CHECK (sentiment IN ('positive', 'neutral', 'negative')),
    parent_post_id TEXT REFERENCES posts (post_id)  -- 非空表示转发；被转发帖同事件、同平台、时间更早
);

CREATE INDEX idx_posts_event_time ON posts (event_id, created_at);
CREATE INDEX idx_posts_account ON posts (account_id);
CREATE INDEX idx_posts_parent ON posts (parent_post_id);

CREATE TABLE metrics_hourly (
    event_id        TEXT NOT NULL REFERENCES events (event_id),
    hour            INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 71),  -- 距首帖的整小时数
    hour_start      TEXT NOT NULL,
    post_count      INTEGER NOT NULL,            -- 该小时全部帖子（含转发）
    repost_count    INTEGER NOT NULL,            -- 其中的转发
    unique_accounts INTEGER NOT NULL,            -- 该小时发帖的去重账号数
    PRIMARY KEY (event_id, hour)
);
