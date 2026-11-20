"""摘要（潮位极值暂供）端到端测试。

覆盖：原文校验、覆盖区间相交与回滚、同起点更正、两端校时、
摘要暂供/越界停供、明细收齐切换与出入对比、同计数同值/异值、
非有限潮位、跨段冲突波及、提交响应的摘要变更清单、重启持久化。
"""

import json
import math
import os
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


# TestClient 默认严格 JSON（不接受 NaN/Infinity），用 allow_nan 编码原始载荷
def post_raw_obs(items, expect=200):
    r = client.post(
        "/observations",
        content=json.dumps({"observations": items}, allow_nan=True),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == expect, r.text
    return r.json()


@pytest.fixture(autouse=True)
def clean_tables():
    conn = db.get_conn()
    with db.lock():
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM calibration_points")
        conn.execute("DELETE FROM summaries")
    yield


# ---------------------------------------------------------------- 工具

def post_obs(items, expect=200):
    r = client.post("/observations", json={"observations": items})
    assert r.status_code == expect, r.text
    return r.json()


def post_cal(items, expect=200):
    r = client.post("/calibrations", json={"calibrations": items})
    assert r.status_code == expect, r.text
    return r.json()


def post_raw_summary(items, expect=422):
    r = client.post(
        "/summaries",
        content=json.dumps({"summaries": items}, allow_nan=True),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == expect, r.text
    return r.json()


def post_summary(items, expect=200):
    r = client.post("/summaries", json={"summaries": items})
    assert r.status_code == expect, r.text
    return r.json()


def get_summary(segment, start, expect=200):
    r = client.get("/summaries", params={"boot_segment": segment, "start_counter": start})
    assert r.status_code == expect, r.text
    return r.json()["items"][0] if r.status_code == 200 else r.json()


def calibrate(segment, lo=0, hi=540000, t0=1_000_000):
    """1:1 校时：counter c -> utc t0 + c（hi 与应采末端对齐）。"""
    post_cal([
        {"boot_segment": segment, "counter": lo, "utc_ms": t0 + lo},
        {"boot_segment": segment, "counter": hi, "utc_ms": t0 + hi},
    ])


def grid_obs(segment, values, start=0, skip=(), tide_override=None):
    """在 10 个应采计数上提交逐笔读数。"""
    items = []
    for k, v in enumerate(values):
        counter = start + k * 60000
        if k in skip:
            continue
        level = tide_override.get(counter, v) if tide_override else v
        items.append({
            "obs_id": f"{segment}-{counter}",
            "boot_segment": segment, "counter": counter, "tide_level": level,
        })
    if items:
        post_obs(items)


# ---------------------------------------------------------------- 原文校验

def test_summary_rejects_non_finite_and_wrong_order():
    for bad in (float("nan"), float("inf"), float("-inf")):
        r = client.post(
            "/summaries",
            content=json.dumps({"summaries": [{
                "boot_segment": "A", "start_counter": 0,
                "min_tide": bad, "max_tide": 5.0,
            }]}, allow_nan=True),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 422, r.text
        assert "有限数" in r.text

    r = client.post("/summaries", json={"summaries": [{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 5.0, "max_tide": 4.0,
    }]})
    assert r.status_code == 422
    assert "最小潮位" in r.text


def test_overlap_rejected_with_rollback_and_old_result_kept():
    calibrate("A")
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 1.0, "max_tide": 5.0,
    }])
    before = get_summary("A", 0)
    assert before["serving"] is True

    # 起点 30000 的覆盖区间 [30000, 570000] 与 [0, 540000] 相交
    r = post_summary([{
        "boot_segment": "A", "start_counter": 30000,
        "min_tide": 1.0, "max_tide": 5.0,
    }], expect=409)
    assert r["detail"]["conflicts"][0]["existing_start_counter"] == 0

    # 新摘要未落库，旧摘要原文与供数结果原样保留
    assert get_summary("A", 30000, expect=404)["detail"]
    again = get_summary("A", 0)
    assert again["serving"] is True and again["min_tide"] == 1.0

    # 不相交的摘要可以提交（[600000, 1140000] 与 [0, 540000] 仅相邻）
    post_summary([{
        "boot_segment": "A", "start_counter": 600000,
        "min_tide": 1.0, "max_tide": 5.0,
    }])
    assert get_summary("A", 600000)["end_counter"] == 1_140_000


def test_invalid_batch_does_not_touch_other_items_in_batch():
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 1.0, "max_tide": 5.0,
    }])
    # 同批一条非法 -> 整批回滚，合法的那条也不落库
    post_raw_summary([
        {"boot_segment": "B", "start_counter": 0, "min_tide": 0.0, "max_tide": 9.0},
        {"boot_segment": "B", "start_counter": 60000, "min_tide": 9.0, "max_tide": float("inf")},
    ])
    assert client.get(
        "/summaries", params={"boot_segment": "B", "start_counter": 0}
    ).status_code == 404


def test_same_key_resubmission_corrects_extrema_and_restores_serving():
    calibrate("A")
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 0.0, "max_tide": 10.0,
    }])
    # 一条读数 11 越界 -> 停供
    post_obs([{"obs_id": "o1", "boot_segment": "A", "counter": 60000, "tide_level": 11.0}])
    suspended = get_summary("A", 0)
    assert suspended["serving"] is False
    assert suspended["reason_code"] == "observation_out_of_range"

    # 同段同起点重报更正极值 -> 回到暂供
    resp = post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 0.0, "max_tide": 12.0,
    }])
    assert resp["changed_summaries_count"] == 1
    fixed = get_summary("A", 0)
    assert fixed["serving"] is True and fixed["source"] == "summary"
    assert fixed["max_tide"] == 12.0 and fixed["served_max"] == 12.0


# ---------------------------------------------------------------- 校时与暂供

def test_pending_calibration_suspends_with_reason():
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 1.0, "max_tide": 5.0,
    }])
    body = get_summary("A", 0)
    assert body["serving"] is False
    assert body["reason_code"] == "pending_calibration"
    assert body["start_utc_ms"] is None and body["end_utc_ms"] is None
    assert "待校时" in body["reason"]

    # 只有一个校时点时末端仍待校时
    post_cal([{"boot_segment": "A", "counter": 0, "utc_ms": 1_000_000}])
    body = get_summary("A", 0)
    assert body["serving"] is False
    assert body["reason_code"] == "pending_calibration"

    # 补齐区间 -> 暂供，两端 UTC 正确（1:1 校时）
    post_cal([{"boot_segment": "A", "counter": 540000, "utc_ms": 1_540_000}])
    body = get_summary("A", 0)
    assert body["serving"] is True and body["source"] == "summary"
    assert body["start_utc_ms"] == 1_000_000
    assert body["end_utc_ms"] == 1_540_000
    assert body["served_min"] == 1.0 and body["served_max"] == 5.0
    assert body["detail_min"] is None and body["discrepancy"] is None


def test_partial_details_in_range_keep_summary_serving():
    calibrate("A")
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 0.0, "max_tide": 10.0,
    }])
    grid_obs("A", [1.0, 2.0, 3.0], skip=tuple(range(3, 10)))
    body = get_summary("A", 0)
    assert body["serving"] is True and body["source"] == "summary"
    assert body["served_min"] == 0.0 and body["served_max"] == 10.0


def test_out_of_range_reading_suspends_then_detail_completes():
    calibrate("A")
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 0.0, "max_tide": 10.0,
    }])
    grid_obs("A", [5.0, 11.0], skip=tuple(range(2, 10)))
    body = get_summary("A", 0)
    assert body["serving"] is False
    assert body["reason_code"] == "observation_out_of_range"
    assert "越界" in body["reason"] or "超出设备极值" in body["reason"]

    # 明细收齐（含越界读数本身）-> 改用明细极值，恢复供数并列出两组值
    grid_obs("A", [5.0, 11.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 2.0])
    body = get_summary("A", 0)
    assert body["serving"] is True and body["source"] == "detail"
    assert body["detail_min"] == 2.0 and body["detail_max"] == 11.0
    assert body["served_min"] == 2.0 and body["served_max"] == 11.0
    assert body["discrepancy"] == {
        "summary": {"min_tide": 0.0, "max_tide": 10.0},
        "detail": {"min_tide": 2.0, "max_tide": 11.0},
    }


def test_complete_detail_matches_summary_no_discrepancy():
    calibrate("A")
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 1.0, "max_tide": 10.0,
    }])
    grid_obs("A", [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    body = get_summary("A", 0)
    assert body["source"] == "detail" and body["serving"] is True
    assert body["detail_min"] == 1.0 and body["detail_max"] == 10.0
    assert body["discrepancy"] is None


def test_same_counter_same_value_counts_as_one_point():
    calibrate("A")
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 0.0, "max_tide": 10.0,
    }])
    # 计数 0 两条同值读数；其余 9 个应采点补齐 -> 10 点齐，走明细
    post_obs([
        {"obs_id": "dup-1", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "dup-2", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
    ])
    grid_obs("A", [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0], skip=(0,))
    body = get_summary("A", 0)
    assert body["source"] == "detail"
    assert body["detail_min"] == 1.0 and body["detail_max"] == 10.0


def test_same_counter_different_values_suspends():
    calibrate("A")
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 0.0, "max_tide": 10.0,
    }])
    post_obs([
        {"obs_id": "v1", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "v2", "boot_segment": "A", "counter": 0, "tide_level": 1.5},
    ])
    body = get_summary("A", 0)
    assert body["serving"] is False
    assert body["reason_code"] == "counter_value_conflict"
    assert "同计数异值" in body["reason"]

    # 覆盖原文消除异值后恢复（把异值读数改成应采点 60000 上的新读数，
    # 计数 0 只剩一个值；60000 本无读数，不会再触发同计数异值）
    post_obs([{"obs_id": "v2", "boot_segment": "A", "counter": 60000, "tide_level": 1.5}])
    body = get_summary("A", 0)
    assert body["serving"] is True and body["source"] == "summary"


def test_non_finite_reading_at_required_point_suspends():
    calibrate("A")
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 0.0, "max_tide": 10.0,
    }])
    post_raw_obs([
        {"obs_id": "nan1", "boot_segment": "A", "counter": 120000, "tide_level": float("nan")},
    ])
    body = get_summary("A", 0)
    assert body["serving"] is False
    assert body["reason_code"] == "non_finite_tide_level"
    assert "非有限数" in body["reason"]


def test_non_finite_reading_off_grid_does_not_suspend():
    calibrate("A")
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 0.0, "max_tide": 10.0,
    }])
    # 非应采点上的 NaN 不触发停供；应采点仍缺 -> 摘要暂供
    post_raw_obs([
        {"obs_id": "off", "boot_segment": "A", "counter": 12345, "tide_level": float("inf")},
    ])
    body = get_summary("A", 0)
    assert body["serving"] is True and body["source"] == "summary"


# ---------------------------------------------------------------- 跨段冲突

def test_cross_segment_conflict_suspends_and_ripples_into_batch_response():
    calibrate("A")
    calibrate("B", t0=1_000_000)  # B 段 counter 0 与 A 段 counter 0 同一 UTC
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 0.0, "max_tide": 10.0,
    }])
    post_obs([{"obs_id": "a0", "boot_segment": "A", "counter": 0, "tide_level": 1.0}])
    assert get_summary("A", 0)["serving"] is True

    # B 段观测落到 A 段应采点的 UTC 毫秒 -> A 摘要停供，
    # 且 /observations 响应把跨段波及的 A 段摘要列入变更清单
    resp = post_obs([{"obs_id": "b0", "boot_segment": "B", "counter": 0, "tide_level": 2.0}])
    keys = {(s["boot_segment"], s["start_counter"]) for s in resp["changed_summaries"]}
    assert ("A", 0) in keys
    body = get_summary("A", 0)
    assert body["serving"] is False
    assert body["reason_code"] == "cross_segment_conflict"
    assert "B" in body["reason"]

    # 校时更正把 B 段挪走 -> A 摘要恢复，/calibrations 同样列出波及摘要
    resp = post_cal([{"boot_segment": "B", "counter": 540000, "utc_ms": 2_540_000},
                     {"boot_segment": "B", "counter": 0, "utc_ms": 2_000_000}])
    keys = {(s["boot_segment"], s["start_counter"]) for s in resp["changed_summaries"]}
    assert ("A", 0) in keys
    body = get_summary("A", 0)
    assert body["serving"] is True and body["source"] == "summary"


def test_changed_summaries_scoped_to_affected_segments():
    calibrate("A")
    calibrate("C")
    for seg in ("A", "C"):
        post_summary([{
            "boot_segment": seg, "start_counter": 0,
            "min_tide": 0.0, "max_tide": 10.0,
        }])

    # 只动 A 段且不改变其供数结果（暂供、极值不变）-> 不列摘要
    resp = post_obs([{"obs_id": "a-x", "boot_segment": "A", "counter": 60000, "tide_level": 1.0}])
    assert resp["changed_summaries"] == []

    # 让 A 段摘要停供（读数越界）-> 只列 A 段，C 段不受影响
    resp = post_obs([{"obs_id": "a-z", "boot_segment": "A", "counter": 120000, "tide_level": 99.0}])
    keys = {s["boot_segment"] for s in resp["changed_summaries"]}
    assert keys == {"A"}
    assert resp["changed_summaries"][0]["reason_code"] == "observation_out_of_range"

    # 动没有摘要的 D 段 -> 不列任何摘要
    resp = post_cal([{"boot_segment": "D", "counter": 0, "utc_ms": 999}])
    assert resp["changed_summaries"] == []


# ---------------------------------------------------------------- 查询与持久化

def test_summary_query_404():
    r = client.get("/summaries", params={"boot_segment": "X", "start_counter": 0})
    assert r.status_code == 404


def test_touching_coverage_intervals_are_allowed_and_same_key_is_correction():
    calibrate("A")
    # 第一份覆盖 [0, 540000]，第二份 [600000, 1140000] 紧邻不相交 -> 允许
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 1.0, "max_tide": 5.0,
    }])
    post_summary([{
        "boot_segment": "A", "start_counter": 600000,
        "min_tide": 2.0, "max_tide": 6.0,
    }])
    assert get_summary("A", 600000)["start_counter"] == 600000
    # 同起点重报是更正而非相交冲突
    post_summary([{
        "boot_segment": "A", "start_counter": 600000,
        "min_tide": 2.0, "max_tide": 7.0,
    }])
    assert get_summary("A", 600000)["max_tide"] == 7.0
    # 而共用一个应采计数（540000）的起点仍判相交
    post_summary([{
        "boot_segment": "A", "start_counter": 540000,
        "min_tide": 2.0, "max_tide": 6.0,
    }], expect=409)


def test_calibration_batch_lists_summary_change_from_pending_to_serving():
    post_summary([{
        "boot_segment": "A", "start_counter": 0,
        "min_tide": 1.0, "max_tide": 5.0,
    }])
    assert get_summary("A", 0)["serving"] is False

    # 补齐两端校时 -> 摘要由停供转暂供，/calibrations 响应列出该摘要
    resp = post_cal([
        {"boot_segment": "A", "counter": 0, "utc_ms": 1_000_000},
        {"boot_segment": "A", "counter": 540000, "utc_ms": 1_540_000},
    ])
    keys = {(s["boot_segment"], s["start_counter"]) for s in resp["changed_summaries"]}
    assert keys == {("A", 0)}
    assert resp["changed_summaries"][0]["serving"] is True
    assert resp["changed_summaries"][0]["source"] == "summary"


def test_docs_include_summary_paths():
    paths = client.get("/openapi.json").json()["paths"]
    assert "/summaries" in paths
    assert client.get("/docs").status_code == 200


def test_restart_persistence(monkeypatch):
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "persist.db")
    monkeypatch.setenv("BUOY_DB", path)
    db.reset_for_tests()
    try:
        calibrate("A")
        post_summary([{
            "boot_segment": "A", "start_counter": 0,
            "min_tide": 1.0, "max_tide": 5.0,
        }])
        post_obs([{"obs_id": "keep", "boot_segment": "A", "counter": 60000, "tide_level": 4.0}])
        before = get_summary("A", 0)
        assert before["serving"] is True

        # 模拟重启：丢弃进程内连接后重新打开同一文件
        db.reset_for_tests()
        after = get_summary("A", 0)
        assert after["boot_segment"] == "A"
        assert after["start_utc_ms"] == before["start_utc_ms"]
        assert after["end_utc_ms"] == before["end_utc_ms"]
        assert after["serving"] is True and after["source"] == "summary"
        assert after["served_min"] == 1.0 and after["served_max"] == 5.0
    finally:
        monkeypatch.undo()
        db.reset_for_tests()


def test_migration_from_legacy_schema(monkeypatch):
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "legacy.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE calibration_points (
            boot_segment TEXT NOT NULL, counter INTEGER NOT NULL, utc_ms INTEGER NOT NULL,
            PRIMARY KEY (boot_segment, counter));
        CREATE TABLE observations (
            obs_id TEXT PRIMARY KEY, boot_segment TEXT NOT NULL, counter INTEGER NOT NULL,
            tide_level REAL NOT NULL, utc_ms INTEGER, status TEXT NOT NULL, reason TEXT,
            cal_lo_counter INTEGER, cal_lo_utc_ms INTEGER,
            cal_hi_counter INTEGER, cal_hi_utc_ms INTEGER);
        INSERT INTO observations VALUES ('old','A',0,3.5,1000000,'ok',NULL,0,1000000,0,1000000);
        INSERT INTO calibration_points VALUES ('A',0,1000000);
        INSERT INTO calibration_points VALUES ('A',540000,1540000);
        """
    )
    raw.commit()
    raw.close()

    monkeypatch.setenv("BUOY_DB", path)
    db.reset_for_tests()
    try:
        r = client.get("/observations/old")
        assert r.status_code == 200
        assert r.json()["tide_level"] == 3.5
        # 迁移后仍可写入含非有限值的新观测（SQLite 中 NaN 以 NULL 落库，
        # tide_finite=0 标记其非有限；查询时由服务侧还原为 NaN）
        post_raw_obs([{"obs_id": "new", "boot_segment": "A", "counter": 60000,
                       "tide_level": float("nan")}])
        assert client.get("/observations/new").json()["status"] == "ok"
        assert math.isnan(client.get("/observations/new").json()["tide_level"])
    finally:
        monkeypatch.undo()
        db.reset_for_tests()
