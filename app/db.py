"""SQLite 连接、模式与事务助手。

设计：单进程单连接 + 进程级写锁。所有写路径在 BEGIN IMMEDIATE ...
COMMIT 的单个事务中执行，因此原文、校时依据、分析供数状态同次生效；
查询与（崩溃后）重启都不会看到半次重算的中间态。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

DEFAULT_DB_PATH = "./buoy.db"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_points (
    boot_segment TEXT NOT NULL,      -- 开机段
    counter      INTEGER NOT NULL,   -- 段内毫秒计数
    utc_ms       INTEGER NOT NULL,   -- 对应 UTC 毫秒
    PRIMARY KEY (boot_segment, counter)
);

CREATE TABLE IF NOT EXISTS observations (
    obs_id         TEXT PRIMARY KEY, -- 观测 ID
    boot_segment   TEXT NOT NULL,    -- 原文：开机段
    counter        INTEGER NOT NULL, -- 原文：段内计数
    tide_level     REAL,             -- 原文：潮位（非有限值按 SQLite 规则可能为 NULL）
    tide_finite    INTEGER NOT NULL, -- 原文潮位是否为有限数（0=NaN/±Inf）
    utc_ms         INTEGER,          -- 换算结果；NULL 表示待校时
    status         TEXT NOT NULL,    -- ok / pending_calibration / conflict
    reason         TEXT,             -- 状态原因
    cal_lo_counter INTEGER,          -- 校时依据：下界校时点
    cal_lo_utc_ms  INTEGER,
    cal_hi_counter INTEGER,          -- 校时依据：上界校时点
    cal_hi_utc_ms  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_observations_utc_ms ON observations(utc_ms);
CREATE INDEX IF NOT EXISTS idx_observations_segment ON observations(boot_segment);

-- 摘要只保存提交原文；供数结果全部由观测/校时现状即时推导，
-- 因此不存在会过期的物化状态，重启后结果天然一致。
CREATE TABLE IF NOT EXISTS summaries (
    boot_segment TEXT NOT NULL,      -- 开机段
    start_counter INTEGER NOT NULL,  -- 起始计数（第 0 个应采点）
    min_tide     REAL NOT NULL,      -- 设备极值：最小潮位（提交时校验为有限数）
    max_tide     REAL NOT NULL,      -- 设备极值：最大潮位（min <= max）
    PRIMARY KEY (boot_segment, start_counter)
);

CREATE INDEX IF NOT EXISTS idx_summaries_segment ON summaries(boot_segment);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """对旧版本库做就地迁移（新增列/表均为附加式，不动既有数据）。"""
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(observations)")}
    if not cols:
        return
    if "tide_finite" not in cols:
        # 旧库中的潮位在写入时即受有限数约束，故历史行一律视为有限；
        # 旧表 tide_level 为 NOT NULL，而 NaN 需以 NULL 落库 -> 重建为可空列
        conn.executescript(
            """
            ALTER TABLE observations RENAME TO observations_legacy;
            CREATE TABLE observations (
                obs_id         TEXT PRIMARY KEY,
                boot_segment   TEXT NOT NULL,
                counter        INTEGER NOT NULL,
                tide_level     REAL,
                tide_finite    INTEGER NOT NULL,
                utc_ms         INTEGER,
                status         TEXT NOT NULL,
                reason         TEXT,
                cal_lo_counter INTEGER,
                cal_lo_utc_ms  INTEGER,
                cal_hi_counter INTEGER,
                cal_hi_utc_ms  INTEGER
            );
            INSERT INTO observations
                (obs_id, boot_segment, counter, tide_level, tide_finite, utc_ms,
                 status, reason, cal_lo_counter, cal_lo_utc_ms,
                 cal_hi_counter, cal_hi_utc_ms)
            SELECT obs_id, boot_segment, counter, tide_level, 1, utc_ms,
                   status, reason, cal_lo_counter, cal_lo_utc_ms,
                   cal_hi_counter, cal_hi_utc_ms FROM observations_legacy;
            DROP TABLE observations_legacy;
            CREATE INDEX IF NOT EXISTS idx_observations_utc_ms ON observations(utc_ms);
            CREATE INDEX IF NOT EXISTS idx_observations_segment ON observations(boot_segment);
            """
        )


def get_conn() -> sqlite3.Connection:
    """返回进程内共享连接（惰性创建并建表）。"""
    global _conn
    if _conn is None:
        path = os.environ.get("BUOY_DB", DEFAULT_DB_PATH)
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        _migrate(conn)
        _conn = conn
    return _conn


def lock() -> threading.RLock:
    """读写共用的进程级锁：写事务进行中，查询不会读到中间态。"""
    return _lock


@contextmanager
def transaction(conn: sqlite3.Connection):
    """单事务包裹一批写入与重算；异常即整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def reset_for_tests() -> None:
    """测试用：丢弃当前连接（下次 get_conn 重新建库）。"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
