"""校时换算、批量写入、重算、冲突检测与摘要供数的核心逻辑。

观测状态机（observations.status）：
  ok                   已校时且无冲突，供分析
  pending_calibration  计数落在本段校时区间外，保留原文、待校时
  conflict             不同开机段落在同一 UTC 毫秒，冲突观测均不供分析

摘要（summaries）只保存提交原文（开机段、起始计数、设备极值）。
其供数结果不物化：每次写入或查询都按当前观测/校时现状即时推导，
因此提交回滚或进程重启后，摘要结果与库内数据始终一致。

所有函数都假定运行在 db.transaction 包裹的同一事务里，
因此“原文 + 校时依据 + 分析供数”同次生效。
"""

from __future__ import annotations

import bisect
import math
import sqlite3
from typing import Iterable

OK = "ok"
PENDING = "pending_calibration"
CONFLICT = "conflict"

# 摘要供数来源
SRC_SUMMARY = "summary"   # 明细未齐，摘要暂供
SRC_DETAIL = "detail"     # 明细收齐，采用明细极值
# 摘要停供
SUSPENDED = "suspended"

# 应采点参数：从起始计数起每隔 60000ms 一点，共 10 点
SAMPLE_COUNT = 10
SAMPLE_STEP_MS = 60_000

# 停供原因
R_PENDING_CAL = "pending_calibration"          # 任一端待校时
R_OUT_OF_RANGE = "observation_out_of_range"    # 明细未齐且有读数超出设备极值
R_CROSS_SEGMENT = "cross_segment_conflict"     # 应采点存在跨段冲突
R_COUNTER_CONFLICT = "counter_value_conflict"  # 同计数异值
R_NON_FINITE = "non_finite_tide_level"         # 应采点潮位非有限数


class SummaryValidationError(ValueError):
    """摘要原文不合法（非有限极值或 min > max）。"""


class SummaryOverlapError(Exception):
    """摘要覆盖区间与同段其他摘要相交；items 携带冲突明细。"""

    def __init__(self, conflicts: list[dict]):
        super().__init__("summary coverage overlaps with existing summaries")
        self.conflicts = conflicts


# ---------------------------------------------------------------- 校时换算

def load_points(conn: sqlite3.Connection, segment: str) -> list[tuple[int, int]]:
    """某开机段的全部校时点，按计数升序：[(counter, utc_ms), ...]"""
    rows = conn.execute(
        "SELECT counter, utc_ms FROM calibration_points "
        "WHERE boot_segment = ? ORDER BY counter",
        (segment,),
    ).fetchall()
    return [(r["counter"], r["utc_ms"]) for r in rows]


def compute_utc(
    points: list[tuple[int, int]], counter: int
) -> tuple[int, tuple[int, int], tuple[int, int]] | None:
    """相邻校时点间线性换算并向下取整。

    返回 (utc_ms, 下界点, 上界点)；计数落在校时区间外返回 None（待校时）。
    恰好命中某个校时点时直接取该点 UTC（上下界相同）。
    """
    if not points:
        return None
    counters = [p[0] for p in points]
    i = bisect.bisect_left(counters, counter)
    if i < len(points) and counters[i] == counter:
        p = points[i]
        return p[1], p, p
    if i == 0 or i == len(points):
        return None
    (c1, t1), (c2, t2) = points[i - 1], points[i]
    # 段内 UTC 单调不倒退已由写入方校验，故各项均非负，// 即向下取整
    utc_ms = t1 + (counter - c1) * (t2 - t1) // (c2 - c1)
    return utc_ms, (c1, t1), (c2, t2)


def _pending_reason(segment: str, counter: int, points: list[tuple[int, int]]) -> str:
    if not points:
        return f"开机段 '{segment}' 尚无校时点，待校时"
    return (
        f"计数 {counter} 超出开机段 '{segment}' 的校时区间 "
        f"[{points[0][0]}, {points[-1][0]}]，待校时"
    )


# ---------------------------------------------------------------- 重算与冲突

def recalc_segment(conn: sqlite3.Connection, segment: str) -> None:
    """按当前校时点重算某开机段全部观测的 utc_ms / 状态 / 校时依据。"""
    points = load_points(conn, segment)
    rows = conn.execute(
        "SELECT obs_id, counter FROM observations WHERE boot_segment = ?",
        (segment,),
    ).fetchall()
    for row in rows:
        result = compute_utc(points, row["counter"])
        if result is None:
            conn.execute(
                "UPDATE observations SET utc_ms = NULL, status = ?, reason = ?, "
                "cal_lo_counter = NULL, cal_lo_utc_ms = NULL, "
                "cal_hi_counter = NULL, cal_hi_utc_ms = NULL "
                "WHERE obs_id = ?",
                (PENDING, _pending_reason(segment, row["counter"], points), row["obs_id"]),
            )
        else:
            utc_ms, (c_lo, t_lo), (c_hi, t_hi) = result
            conn.execute(
                "UPDATE observations SET utc_ms = ?, status = ?, reason = NULL, "
                "cal_lo_counter = ?, cal_lo_utc_ms = ?, "
                "cal_hi_counter = ?, cal_hi_utc_ms = ? "
                "WHERE obs_id = ?",
                (utc_ms, OK, c_lo, t_lo, c_hi, t_hi, row["obs_id"]),
            )


def conflict_ms_set(conn: sqlite3.Connection) -> set[int]:
    """被 >=2 个不同开机段的观测占用的 UTC 毫秒集合。"""
    rows = conn.execute(
        "SELECT utc_ms FROM observations WHERE utc_ms IS NOT NULL "
        "GROUP BY utc_ms HAVING COUNT(DISTINCT boot_segment) > 1"
    ).fetchall()
    return {r["utc_ms"] for r in rows}


def resolve_conflicts(conn: sqlite3.Connection) -> None:
    """全局刷新冲突状态：新冲突标记 conflict，已解除的恢复 ok。"""
    ms_set = conflict_ms_set(conn)
    conflicted_ids: set[str] = set()
    if ms_set:
        ph = ",".join("?" for _ in ms_set)
        rows = conn.execute(
            f"SELECT obs_id, utc_ms, boot_segment FROM observations "
            f"WHERE utc_ms IN ({ph})",
            tuple(sorted(ms_set)),
        ).fetchall()
        by_ms: dict[int, list[sqlite3.Row]] = {}
        for r in rows:
            by_ms.setdefault(r["utc_ms"], []).append(r)
        for ms, group in by_ms.items():
            segments = {g["boot_segment"] for g in group}
            for g in group:
                others = "、".join(sorted(segments - {g["boot_segment"]}))
                reason = f"UTC 毫秒 {ms} 与开机段 {others} 的观测冲突，不供分析"
                conn.execute(
                    "UPDATE observations SET status = ?, reason = ? WHERE obs_id = ?",
                    (CONFLICT, reason, g["obs_id"]),
                )
                conflicted_ids.add(g["obs_id"])
    # 解除冲突后恢复
    if conflicted_ids:
        ph = ",".join("?" for _ in conflicted_ids)
        conn.execute(
            f"UPDATE observations SET status = ?, reason = NULL "
            f"WHERE status = ? AND obs_id NOT IN ({ph})",
            (OK, CONFLICT, *sorted(conflicted_ids)),
        )
    else:
        conn.execute(
            "UPDATE observations SET status = ?, reason = NULL WHERE status = ?",
            (OK, CONFLICT),
        )


# ---------------------------------------------------------------- 变更快照

def _snapshot(conn: sqlite3.Connection, where: str, params: Iterable) -> dict[str, tuple]:
    """obs_id -> (utc_ms, status)，用于事务内前后对比。"""
    rows = conn.execute(
        f"SELECT obs_id, utc_ms, status FROM observations WHERE {where}",
        tuple(params),
    ).fetchall()
    return {r["obs_id"]: (r["utc_ms"], r["status"]) for r in rows}


def _changed_ids(before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
    """时刻或状态发生变化（含新出现）的观测 ID。"""
    return sorted(oid for oid, state in after.items() if before.get(oid) != state)


def fetch_states(conn: sqlite3.Connection, obs_ids: list[str]) -> list[dict]:
    """读取给定观测的当前 (obs_id, utc_ms, status)，供提交响应使用。"""
    if not obs_ids:
        return []
    ph = ",".join("?" for _ in obs_ids)
    rows = conn.execute(
        f"SELECT obs_id, utc_ms, status FROM observations "
        f"WHERE obs_id IN ({ph}) ORDER BY obs_id",
        obs_ids,
    ).fetchall()
    return [{"obs_id": r["obs_id"], "utc_ms": r["utc_ms"], "status": r["status"]} for r in rows]


# ---------------------------------------------------------------- 批量写入

def upsert_observations(conn: sqlite3.Connection, items: list[dict]) -> list[str]:
    """分批写入观测（同 ID 覆盖原文并重算），返回时刻/状态变化的观测 ID。

    新观测可能与其他开机段落在同一 UTC 毫秒而触发冲突，
    因此写入后做全局冲突刷新，并把受波及的观测一并纳入变更清单。
    """
    if not items:
        return []
    segments = {it["boot_segment"] for it in items}
    points_by_seg = {s: load_points(conn, s) for s in segments}
    ids = [it["obs_id"] for it in items]
    id_ph = ",".join("?" for _ in ids)

    # 被覆盖观测的旧 UTC 毫秒（其上的冲突可能因此解除）
    old_ms = {
        r["utc_ms"]
        for r in conn.execute(
            f"SELECT utc_ms FROM observations "
            f"WHERE obs_id IN ({id_ph}) AND utc_ms IS NOT NULL",
            ids,
        )
    }

    # 同批同 ID 后者覆盖前者
    computed: dict[str, tuple[dict, tuple | None]] = {}
    for it in items:
        computed[it["obs_id"]] = (it, compute_utc(points_by_seg[it["boot_segment"]], it["counter"]))

    new_ms = {res[0] for _, res in computed.values() if res is not None}
    affected_ms = old_ms | new_ms
    if affected_ms:
        ms_ph = ",".join("?" for _ in affected_ms)
        where = f"obs_id IN ({id_ph}) OR utc_ms IN ({ms_ph})"
        params: tuple = (*ids, *sorted(affected_ms))
    else:
        where = f"obs_id IN ({id_ph})"
        params = tuple(ids)
    before = _snapshot(conn, where, params)

    for obs_id, (it, result) in computed.items():
        finite = 1 if math.isfinite(it["tide_level"]) else 0
        if result is None:
            reason = _pending_reason(it["boot_segment"], it["counter"],
                                     points_by_seg[it["boot_segment"]])
            conn.execute(
                "INSERT INTO observations (obs_id, boot_segment, counter, tide_level, "
                "tide_finite, utc_ms, status, reason) VALUES (?,?,?,?,?,NULL,?,?) "
                "ON CONFLICT(obs_id) DO UPDATE SET "
                "boot_segment=excluded.boot_segment, counter=excluded.counter, "
                "tide_level=excluded.tide_level, tide_finite=excluded.tide_finite, "
                "utc_ms=NULL, status=excluded.status, reason=excluded.reason, "
                "cal_lo_counter=NULL, cal_lo_utc_ms=NULL, "
                "cal_hi_counter=NULL, cal_hi_utc_ms=NULL",
                (it["obs_id"], it["boot_segment"], it["counter"], it["tide_level"],
                 finite, PENDING, reason),
            )
        else:
            utc_ms, (c_lo, t_lo), (c_hi, t_hi) = result
            conn.execute(
                "INSERT INTO observations (obs_id, boot_segment, counter, tide_level, "
                "tide_finite, utc_ms, status, reason, cal_lo_counter, cal_lo_utc_ms, "
                "cal_hi_counter, cal_hi_utc_ms) VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?) "
                "ON CONFLICT(obs_id) DO UPDATE SET "
                "boot_segment=excluded.boot_segment, counter=excluded.counter, "
                "tide_level=excluded.tide_level, tide_finite=excluded.tide_finite, "
                "utc_ms=excluded.utc_ms, status=excluded.status, reason=NULL, "
                "cal_lo_counter=excluded.cal_lo_counter, "
                "cal_lo_utc_ms=excluded.cal_lo_utc_ms, "
                "cal_hi_counter=excluded.cal_hi_counter, "
                "cal_hi_utc_ms=excluded.cal_hi_utc_ms",
                (it["obs_id"], it["boot_segment"], it["counter"], it["tide_level"],
                 finite, utc_ms, OK, c_lo, t_lo, c_hi, t_hi),
            )

    resolve_conflicts(conn)
    after = _snapshot(conn, where, params)
    return _changed_ids(before, after)


class MonotonicViolation(Exception):
    """校时点使段内 UTC 倒退；violations 携带明细。"""

    def __init__(self, violations: list[dict]):
        super().__init__("calibration points make UTC go backwards within a segment")
        self.violations = violations


def upsert_calibrations(conn: sqlite3.Connection, items: list[dict]) -> list[str]:
    """分批写入校时点（同段同计数替换旧值），重算受影响观测。

    合并新旧校时点后，任一开机段内 UTC 随计数倒退则抛 MonotonicViolation，
    由调用方回滚整批，旧数据照常供数。
    """
    if not items:
        return []
    segments = {it["boot_segment"] for it in items}
    for it in items:
        conn.execute(
            "INSERT INTO calibration_points (boot_segment, counter, utc_ms) "
            "VALUES (?,?,?) "
            "ON CONFLICT(boot_segment, counter) DO UPDATE SET utc_ms=excluded.utc_ms",
            (it["boot_segment"], it["counter"], it["utc_ms"]),
        )

    violations: list[dict] = []
    for seg in sorted(segments):
        points = load_points(conn, seg)
        for (c1, t1), (c2, t2) in zip(points, points[1:]):
            if t2 < t1:
                violations.append({
                    "boot_segment": seg,
                    "counter": c2, "utc_ms": t2,
                    "prev_counter": c1, "prev_utc_ms": t1,
                })
    if violations:
        raise MonotonicViolation(violations)

    seg_ph = ",".join("?" for _ in segments)
    seg_params = tuple(sorted(segments))

    ms_before = conflict_ms_set(conn)
    if ms_before:
        ms_ph = ",".join("?" for _ in ms_before)
        where = f"boot_segment IN ({seg_ph}) OR utc_ms IN ({ms_ph})"
        params = (*seg_params, *sorted(ms_before))
    else:
        where = f"boot_segment IN ({seg_ph})"
        params = seg_params
    before = _snapshot(conn, where, params)

    for seg in segments:
        recalc_segment(conn, seg)
    resolve_conflicts(conn)

    # 冲突集合可能变化，前后两批毫秒上的观测都可能翻转状态
    ms_all = ms_before | conflict_ms_set(conn)
    if ms_all:
        ms_ph = ",".join("?" for _ in ms_all)
        where = f"boot_segment IN ({seg_ph}) OR utc_ms IN ({ms_ph})"
        params = (*seg_params, *sorted(ms_all))
    else:
        where = f"boot_segment IN ({seg_ph})"
        params = seg_params
    after = _snapshot(conn, where, params)
    return _changed_ids(before, after)


# ================================================================
# 摘要（潮位极值暂供）
#
# 每份摘要覆盖固定网格：start_counter + k*60000（k=0..9），共 10 个
# 应采点。同段同起点重报即更正设备极值；不同起点的摘要覆盖区间
# （闭区间）不得相交。供数结果按当前观测/校时现状即时推导。
# ================================================================

def sample_counters(start_counter: int) -> list[int]:
    """摘要的 10 个应采计数。"""
    return [start_counter + k * SAMPLE_STEP_MS for k in range(SAMPLE_COUNT)]


def coverage_end(start_counter: int) -> int:
    """摘要覆盖区间的右端计数（含）。"""
    return start_counter + (SAMPLE_COUNT - 1) * SAMPLE_STEP_MS


def _validate_summary(item: dict) -> None:
    mn, mx = item["min_tide"], item["max_tide"]
    if not math.isfinite(mn) or not math.isfinite(mx):
        raise SummaryValidationError("极值必须为有限数（不接受 NaN 或 ±Infinity）")
    if mn > mx:
        raise SummaryValidationError("最小潮位不得大于最大潮位")


def _overlap_rows(conn: sqlite3.Connection, item: dict) -> list[sqlite3.Row]:
    """同段内与本摘要覆盖区间相交（闭区间）的既有摘要，排除同起点自身。"""
    end = coverage_end(item["start_counter"])
    rows = conn.execute(
        "SELECT boot_segment, start_counter, min_tide, max_tide FROM summaries "
        "WHERE boot_segment = ? AND NOT (? < start_counter OR ? > "
        f"start_counter + {SAMPLE_COUNT - 1} * {SAMPLE_STEP_MS}) "
        "AND start_counter != ?",
        (item["boot_segment"], end, item["start_counter"], item["start_counter"]),
    ).fetchall()
    return rows


def upsert_summaries(conn: sqlite3.Connection, items: list[dict]) -> list[str]:
    """写入/更正摘要原文，返回受影响（需在响应中另列结果）的摘要键。

    原文校验失败或覆盖区间相交时抛异常，由调用方回滚整批，
    上次提交的原文与供数结果原样保留。
    返回 ["boot_segment|start_counter", ...] 已排序。
    """
    if not items:
        return []

    # 整批先做原文校验，任何一条不合法都不动库
    for it in items:
        _validate_summary(it)

    # 同批内按键归并（同键后者覆盖前者），并检查批内自相重叠
    merged: dict[tuple[str, int], dict] = {}
    for it in items:
        merged[(it["boot_segment"], it["start_counter"])] = it
    keys = list(merged.values())
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if a["boot_segment"] != b["boot_segment"]:
                continue
            a_end = coverage_end(a["start_counter"])
            b_end = coverage_end(b["start_counter"])
            if not (a_end < b["start_counter"] or b_end < a["start_counter"]):
                raise SummaryOverlapError([{
                    "boot_segment": a["boot_segment"],
                    "start_counter_a": a["start_counter"],
                    "start_counter_b": b["start_counter"],
                }])

    conflicts: list[dict] = []
    for it in merged.values():
        for r in _overlap_rows(conn, it):
            conflicts.append({
                "boot_segment": it["boot_segment"],
                "start_counter": it["start_counter"],
                "existing_start_counter": r["start_counter"],
                "existing_end_counter": coverage_end(r["start_counter"]),
            })
    if conflicts:
        raise SummaryOverlapError(conflicts)

    for it in merged.values():
        conn.execute(
            "INSERT INTO summaries (boot_segment, start_counter, min_tide, max_tide) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(boot_segment, start_counter) DO UPDATE SET "
            "min_tide=excluded.min_tide, max_tide=excluded.max_tide",
            (it["boot_segment"], it["start_counter"], it["min_tide"], it["max_tide"]),
        )

    # 本批直接影响的开机段
    return sorted(f"{seg}|{sc}" for seg, sc in merged)


def segments_touched_by_observation_batch(items: list[dict]) -> set[str]:
    return {it["boot_segment"] for it in items}


def cross_segment_partners(conn: sqlite3.Connection, segments: set[str]) -> set[str]:
    """与给定开机段在同一 UTC 毫秒上共存的其他开机段。

    写入前后各调用一次即可同时覆盖“新卷入冲突”和“冲突解除”两个方向，
    从而把跨段冲突波及段的摘要一并纳入本批重算清单。
    """
    if not segments:
        return set()
    seg_ph = ",".join("?" for _ in segments)
    rows = conn.execute(
        f"SELECT DISTINCT o2.boot_segment AS seg FROM observations o1 "
        f"JOIN observations o2 ON o1.utc_ms = o2.utc_ms AND o1.utc_ms IS NOT NULL "
        f"WHERE o1.boot_segment IN ({seg_ph}) AND o2.boot_segment NOT IN ({seg_ph})",
        (*sorted(segments), *sorted(segments)),
    ).fetchall()
    return {r["seg"] for r in rows}


def summary_segments(conn: sqlite3.Connection, segments: set[str]) -> set[str]:
    """只保留确实登记过摘要的开机段。"""
    if not segments:
        return set()
    ph = ",".join("?" for _ in segments)
    rows = conn.execute(
        f"SELECT DISTINCT boot_segment AS seg FROM summaries WHERE boot_segment IN ({ph})",
        tuple(sorted(segments)),
    ).fetchall()
    return {r["seg"] for r in rows}


# ---------------------------------------------------------------- 单点评估

def _point_violation(
    conn: sqlite3.Connection,
    segment: str,
    counter: int,
    expected_utc: int,
) -> tuple[str | None, dict | None]:
    """检查某个应采点的明细完整性，返回 (停供原因码, 原因明细)。

    同计数同值的多条读数只算一点；以下任一情况停供：
      跨段冲突 / 同计数异值 / 非有限潮位。
    """
    rows = conn.execute(
        "SELECT obs_id, boot_segment, counter, tide_level, tide_finite, utc_ms, status "
        "FROM observations WHERE boot_segment = ? AND counter = ?",
        (segment, counter),
    ).fetchall()

    # 同计数异值（同值重复只算一点，不算冲突）
    distinct_values = {
        (r["tide_level"] if r["tide_finite"] else None, r["tide_finite"])
        for r in rows
    }
    if len(distinct_values) > 1:
        return R_COUNTER_CONFLICT, {
            "boot_segment": segment, "counter": counter,
            "obs_ids": sorted(r["obs_id"] for r in rows),
        }

    # 非有限潮位
    if any(not r["tide_finite"] for r in rows):
        return R_NON_FINITE, {
            "boot_segment": segment, "counter": counter,
            "obs_ids": sorted(r["obs_id"] for r in rows if not r["tide_finite"]),
        }

    # 跨段冲突：本应采点确有本段读数，且同一 UTC 毫秒被其他开机段占用
    if rows:
        other = conn.execute(
            "SELECT DISTINCT boot_segment AS seg FROM observations "
            "WHERE utc_ms = ? AND boot_segment != ?",
            (expected_utc, segment),
        ).fetchall()
        if other:
            return R_CROSS_SEGMENT, {
                "boot_segment": segment, "counter": counter, "utc_ms": expected_utc,
                "other_segments": sorted(r["seg"] for r in other),
            }
    return None, None


def evaluate_summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """按当前库内现状推导一份摘要的完整供数结果（不物化）。"""
    segment = row["boot_segment"]
    start_counter = row["start_counter"]
    dev_min, dev_max = row["min_tide"], row["max_tide"]
    points = load_points(conn, segment)
    counters = sample_counters(start_counter)

    result: dict = {
        "boot_segment": segment,
        "start_counter": start_counter,
        "end_counter": counters[-1],
        "min_tide": dev_min,
        "max_tide": dev_max,
        "serving": False,
        "source": None,
        "reason_code": None,
        "reason": None,
        "start_utc_ms": None,
        "end_utc_ms": None,
        "served_min": None,
        "served_max": None,
        "detail_min": None,
        "detail_max": None,
        "discrepancy": None,
    }

    # 1) 两端校时：任一端待校时即停供
    end_utc_result = compute_utc(points, counters[-1])
    start_utc_result = compute_utc(points, counters[0])
    if start_utc_result is None or end_utc_result is None:
        result["reason_code"] = R_PENDING_CAL
        result["reason"] = _pending_reason(segment, counters[0], points) \
            if start_utc_result is None else \
            _pending_reason(segment, counters[-1], points)
        return result
    # 两端均可换算时，区间内全部应采点都可按校时点逐点换算
    utc_for: dict[int, int] = {
        c: compute_utc(points, c)[0] for c in counters
    }
    start_utc = utc_for[counters[0]]
    end_utc = utc_for[counters[-1]]
    result["start_utc_ms"] = start_utc
    result["end_utc_ms"] = end_utc

    # 2) 收集本段覆盖区间内的已到读数（明细）
    detail_rows = conn.execute(
        "SELECT obs_id, counter, tide_level, tide_finite, utc_ms, status "
        "FROM observations WHERE boot_segment = ? AND counter BETWEEN ? AND ?",
        (segment, counters[0], counters[-1]),
    ).fetchall()

    # 3) 逐应采点检查（跨段冲突 / 同计数异值 / 非有限潮位）
    for c in counters:
        code, detail = _point_violation(conn, segment, c, utc_for[c])
        if code is not None:
            result["reason_code"] = code
            result["reason"] = _reason_text(code, detail)
            result["reason_detail"] = detail
            return result

    # 同计数同值去重后的明细点（应采点上的非有限值已在逐点检查中拦截；
    # 非应采点的非有限读数不参与极值，跳过）
    distinct_points: dict[int, float] = {}
    for r in detail_rows:
        if not r["tide_finite"]:
            continue
        distinct_points.setdefault(r["counter"], r["tide_level"])
    complete = len(distinct_points) == SAMPLE_COUNT and all(
        c in distinct_points for c in counters
    )

    if complete:
        # 4) 明细收齐：改用明细极值
        values = [distinct_points[c] for c in counters]
        detail_min, detail_max = min(values), max(values)
        result.update(
            serving=True, source=SRC_DETAIL,
            detail_min=detail_min, detail_max=detail_max,
            served_min=detail_min, served_max=detail_max,
        )
        if detail_min != dev_min or detail_max != dev_max:
            result["discrepancy"] = {
                "summary": {"min_tide": dev_min, "max_tide": dev_max},
                "detail": {"min_tide": detail_min, "max_tide": detail_max},
            }
        return result

    # 5) 明细未齐：已到读数都在设备极值内才用摘要暂供，否则停供
    # （非有限读数已在逐点检查中拦截，这里只剩有限值；非应采点的非有限
    # 读数不影响供数，亦不参与越界判断）
    out_of_range = [
        {"obs_id": r["obs_id"], "counter": r["counter"], "tide_level": r["tide_level"]}
        for r in detail_rows
        if r["tide_finite"] and not (dev_min <= r["tide_level"] <= dev_max)
    ]
    if out_of_range:
        result["reason_code"] = R_OUT_OF_RANGE
        result["reason"] = (
            f"明细未齐且有 {len(out_of_range)} 条读数超出设备极值 "
            f"[{dev_min}, {dev_max}]，停供"
        )
        result["reason_detail"] = {"violations": out_of_range}
        return result

    result.update(
        serving=True, source=SRC_SUMMARY,
        served_min=dev_min, served_max=dev_max,
    )
    return result


def _reason_text(code: str, detail: dict | None) -> str:
    if code == R_CROSS_SEGMENT:
        return (
            f"应采点计数 {detail['counter']}（UTC {detail['utc_ms']}）"
            f"与开机段 {'、'.join(detail['other_segments'])} 跨段冲突，停供"
        )
    if code == R_COUNTER_CONFLICT:
        return (
            f"应采点计数 {detail['counter']} 存在同计数异值的读数"
            f"（{', '.join(detail['obs_ids'])}），停供"
        )
    if code == R_NON_FINITE:
        return (
            f"应采点计数 {detail['counter']} 的读数潮位非有限数"
            f"（{', '.join(detail['obs_ids'])}），停供"
        )
    return "停供"


# ---------------------------------------------------------------- 批量读取

def summary_key(segment: str, start_counter: int) -> str:
    return f"{segment}|{start_counter}"


def fetch_summary_results(
    conn: sqlite3.Connection, keys: Iterable[str]
) -> list[dict]:
    """按 "boot_segment|start_counter" 键列表读取摘要并推导当前结果。"""
    out: list[dict] = []
    for key in sorted(set(keys)):
        seg, _, sc = key.partition("|")
        row = conn.execute(
            "SELECT * FROM summaries WHERE boot_segment = ? AND start_counter = ?",
            (seg, int(sc)),
        ).fetchone()
        if row is not None:
            out.append(evaluate_summary(conn, row))
    return out


def snapshot_segment_summaries(
    conn: sqlite3.Connection, segments: set[str]
) -> dict[str, dict]:
    """受影响开机段的摘要结果快照：摘要键 -> 推导结果，用于前后对比。"""
    return {
        summary_key(r["boot_segment"], r["start_counter"]): r
        for r in fetch_segment_summary_results(conn, segments)
    }


def diff_summary_results(
    before: dict[str, dict], after: list[dict]
) -> list[dict]:
    """挑出结果发生变化（含新出现）的摘要，保持 after 的排序。"""
    return [
        r for r in after
        if before.get(summary_key(r["boot_segment"], r["start_counter"])) != r
    ]


def fetch_segment_summary_results(conn: sqlite3.Connection, segments: set[str]) -> list[dict]:
    """读取给定开机段下全部摘要的当前结果，按 (段, 起点) 排序。"""
    if not segments:
        return []
    ph = ",".join("?" for _ in segments)
    rows = conn.execute(
        f"SELECT * FROM summaries WHERE boot_segment IN ({ph}) "
        f"ORDER BY boot_segment, start_counter",
        tuple(sorted(segments)),
    ).fetchall()
    return [evaluate_summary(conn, r) for r in rows]


def get_summary(conn: sqlite3.Connection, segment: str, start_counter: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM summaries WHERE boot_segment = ? AND start_counter = ?",
        (segment, start_counter),
    ).fetchone()
    if row is None:
        return None
    return evaluate_summary(conn, row)
