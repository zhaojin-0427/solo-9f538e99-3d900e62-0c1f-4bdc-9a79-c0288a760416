"""单浮标潮位校时与极值摘要 API。

浮标在海上先报潮位极值（摘要），回收后才补传逐笔读数（观测）。
本服务把「开机段 + 段内计数」换算成可信的 UTC 毫秒时间线，并维护
观测与摘要的可用状态供潮位分析：

  POST /observations   分批提交观测（ID、开机段、计数、潮位）
  POST /calibrations   分批提交校时点（开机段、计数、UTC 毫秒）
  POST /summaries      分批提交/更正潮位极值摘要
  GET  /summaries      按开机段和起始计数查摘要供数结果
  GET  /tide-levels    按 UTC 毫秒范围查可用潮位
  GET  /observations/{obs_id}  按观测 ID 查状态与原因
  GET  /docs           Swagger UI
"""

from __future__ import annotations

import json
import math

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import core, db


class LenientJSONResponse(JSONResponse):
    """允许 NaN/±Infinity 出现在响应中的 JSON 响应（观测原文需往返一致）。"""

    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=True,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


app = FastAPI(
    title="单浮标潮位校时服务",
    description=__doc__,
    version="1.1.0",
    default_response_class=LenientJSONResponse,
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """模型校验失败（如摘要极值非有限、min>max）统一返回 422 并保留中文信息。"""
    errors = []
    for err in exc.errors():
        safe = {k: (str(v) if k == "ctx" else v) for k, v in err.items()}
        errors.append(safe)
    return LenientJSONResponse(
        status_code=422,
        content={"message": "请求参数校验失败", "errors": errors},
    )


# ---------------------------------------------------------------- 模型

class ObservationIn(BaseModel):
    obs_id: str = Field(min_length=1, description="观测 ID")
    boot_segment: str = Field(min_length=1, description="开机段")
    counter: int = Field(ge=0, description="段内毫秒计数")
    tide_level: float = Field(description="潮位（允许 NaN/±Infinity，非有限读数会触发停供）")


class ObservationBatch(BaseModel):
    observations: list[ObservationIn] = Field(min_length=1)


class CalibrationIn(BaseModel):
    boot_segment: str = Field(min_length=1, description="开机段")
    counter: int = Field(ge=0, description="段内毫秒计数")
    utc_ms: int = Field(ge=0, description="对应的 UTC 毫秒")


class CalibrationBatch(BaseModel):
    calibrations: list[CalibrationIn] = Field(min_length=1)


class SummaryIn(BaseModel):
    boot_segment: str = Field(min_length=1, description="开机段")
    start_counter: int = Field(ge=0, description="起始计数（第 0 个应采点）")
    min_tide: float = Field(description="最小潮位（设备极值，必须为有限数）")
    max_tide: float = Field(description="最大潮位（设备极值，必须为有限数）")

    @field_validator("min_tide", "max_tide")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("极值必须为有限数（不接受 NaN 或 ±Infinity）")
        return v

    @field_validator("max_tide")
    @classmethod
    def _ordered(cls, v: float, info) -> float:
        mn = info.data.get("min_tide")
        if mn is not None and v < mn:
            raise ValueError("最小潮位不得大于最大潮位")
        return v


class SummaryBatch(BaseModel):
    summaries: list[SummaryIn] = Field(min_length=1)


class ChangedState(BaseModel):
    obs_id: str
    utc_ms: int | None
    status: str


class Discrepancy(BaseModel):
    summary: dict
    detail: dict


class SummaryResult(BaseModel):
    boot_segment: str
    start_counter: int
    end_counter: int = Field(description="覆盖区间右端计数（含），= 起点 + 9*60000")
    min_tide: float = Field(description="提交原文：设备最小潮位")
    max_tide: float = Field(description="提交原文：设备最大潮位")
    serving: bool = Field(description="当前是否供数")
    source: str | None = Field(
        description="供数来源：summary=摘要暂供 / detail=明细极值；停供为 null"
    )
    reason_code: str | None = Field(description="停供原因码；供数中为 null")
    reason: str | None = Field(description="停供原因文字说明；供数中为 null")
    start_utc_ms: int | None = Field(description="起点 UTC 毫秒；待校时为 null")
    end_utc_ms: int | None = Field(description="末端 UTC 毫秒；待校时为 null")
    served_min: float | None = Field(description="当前供数的最小潮位；停供为 null")
    served_max: float | None = Field(description="当前供数的最大潮位；停供为 null")
    detail_min: float | None = Field(description="明细收齐时的明细最小潮位")
    detail_max: float | None = Field(description="明细收齐时的明细最大潮位")
    discrepancy: Discrepancy | None = Field(
        description="明细极值与摘要极值有出入时列出两组值"
    )


class BatchResult(BaseModel):
    changed_count: int = Field(description="本次提交中时刻或状态变化的观测数")
    changed: list[ChangedState] = Field(description="时刻或状态变化的观测")
    changed_summaries_count: int = Field(
        0, description="本次提交后供数结果发生变化的摘要数"
    )
    changed_summaries: list[SummaryResult] = Field(
        default_factory=list,
        description="结果变化的摘要及其新结果（只含本批直接影响或跨段冲突波及的开机段）",
    )


class TidePoint(BaseModel):
    obs_id: str
    utc_ms: int
    tide_level: float


class TideQueryResult(BaseModel):
    count: int
    items: list[TidePoint]


class CalibrationEndpoint(BaseModel):
    counter: int
    utc_ms: int


class CalibrationBasis(BaseModel):
    lo: CalibrationEndpoint = Field(description="下界校时点")
    hi: CalibrationEndpoint = Field(description="上界校时点")


class ObservationDetail(BaseModel):
    obs_id: str
    boot_segment: str
    counter: int
    tide_level: float
    utc_ms: int | None
    status: str = Field(description="ok / pending_calibration / conflict")
    reason: str | None
    calibration: CalibrationBasis | None = Field(description="校时依据；待校时为 null")


class SummaryQueryResult(BaseModel):
    count: int
    items: list[SummaryResult]


# ---------------------------------------------------------------- 提交

@app.post("/observations", response_model=BatchResult, summary="分批提交观测")
def submit_observations(batch: ObservationBatch) -> BatchResult:
    """写入观测原文并按当前校时点换算；同一 obs_id 重复提交会覆盖原文并重算。

    响应列出本次提交中时刻或状态发生变化的观测 ID（含新观测，
    以及因新观测落入同一 UTC 毫秒而被卷入/解除冲突的观测），
    并另列供数结果发生变化的摘要（仅本批直接影响或跨段冲突波及的开机段）。
    """
    conn = db.get_conn()
    items = [o.model_dump() for o in batch.observations]
    direct_segments = core.segments_touched_by_observation_batch(items)
    with db.lock():
        with db.transaction(conn):
            before_partners = core.cross_segment_partners(conn, direct_segments)
            affected_before = core.summary_segments(
                conn, direct_segments | before_partners
            )
            before = core.snapshot_segment_summaries(conn, affected_before)
            ids = core.upsert_observations(conn, items)
            changed = core.fetch_states(conn, ids)
            after_partners = core.cross_segment_partners(conn, direct_segments)
            affected = core.summary_segments(
                conn, direct_segments | before_partners | after_partners
            )
            after_results = core.fetch_segment_summary_results(conn, affected)
            summary_results = core.diff_summary_results(before, after_results)
    return BatchResult(
        changed_count=len(changed), changed=changed,
        changed_summaries_count=len(summary_results),
        changed_summaries=summary_results,
    )


@app.post("/calibrations", response_model=BatchResult, summary="分批提交校时点")
def submit_calibrations(batch: CalibrationBatch) -> BatchResult:
    """写入校时点；同段同计数的新校时点替换旧值，并重算受影响观测。

    若合并后任一开机段内 UTC 随计数倒退，则整批拒绝（409），旧数据照常供数。
    响应列出时刻或状态变化的观测，以及供数结果发生变化的摘要。
    """
    conn = db.get_conn()
    items = [c.model_dump() for c in batch.calibrations]
    direct_segments = {it["boot_segment"] for it in items}
    with db.lock():
        with db.transaction(conn):
            before_partners = core.cross_segment_partners(conn, direct_segments)
            affected_before = core.summary_segments(
                conn, direct_segments | before_partners
            )
            before = core.snapshot_segment_summaries(conn, affected_before)
            try:
                ids = core.upsert_calibrations(conn, items)
            except core.MonotonicViolation as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "校时点使段内 UTC 倒退，整批拒绝，旧数据照常供数",
                        "violations": exc.violations,
                    },
                )
            changed = core.fetch_states(conn, ids)
            after_partners = core.cross_segment_partners(conn, direct_segments)
            affected = core.summary_segments(
                conn, direct_segments | before_partners | after_partners
            )
            after_results = core.fetch_segment_summary_results(conn, affected)
            summary_results = core.diff_summary_results(before, after_results)
    return BatchResult(
        changed_count=len(changed), changed=changed,
        changed_summaries_count=len(summary_results),
        changed_summaries=summary_results,
    )


@app.post("/summaries", response_model=BatchResult, summary="分批提交/更正极值摘要")
def submit_summaries(batch: SummaryBatch) -> BatchResult:
    """提交潮位极值摘要。

    每份摘要填写开机段、起始计数和最小/最大潮位；极值须为有限数且
    最小值不大于最大值。从起点起每隔 60000 毫秒取一点，共 10 点。
    同段同起点重报即更正极值；其余摘要的覆盖区间不得相交，否则整批
    拒绝（409）。提交失败时上次提交的原文、校时点、摘要和供数结果
    全部原样保留。
    """
    conn = db.get_conn()
    items = [s.model_dump() for s in batch.summaries]
    with db.lock():
        with db.transaction(conn):
            keys = [core.summary_key(it["boot_segment"], it["start_counter"]) for it in items]
            before = {
                core.summary_key(r["boot_segment"], r["start_counter"]): r
                for r in core.fetch_summary_results(conn, keys)
            }
            try:
                core.upsert_summaries(conn, items)
            except core.SummaryOverlapError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "摘要覆盖区间与同段其他摘要相交，整批拒绝，旧数据照常供数",
                        "conflicts": exc.conflicts,
                    },
                )
            after_results = core.fetch_summary_results(conn, keys)
            summary_results = core.diff_summary_results(before, after_results)
    return BatchResult(
        changed_count=0, changed=[],
        changed_summaries_count=len(summary_results),
        changed_summaries=summary_results,
    )


# ---------------------------------------------------------------- 查询

@app.get(
    "/summaries",
    response_model=SummaryQueryResult,
    summary="按开机段和起始计数查极值摘要供数结果",
)
def query_summaries(
    boot_segment: str = Query(min_length=1, description="开机段"),
    start_counter: int = Query(ge=0, description="起始计数"),
) -> SummaryQueryResult:
    """查询摘要两端 UTC、供数来源与极值；停供时 reason_code/reason 说明原因。"""
    conn = db.get_conn()
    with db.lock():
        result = core.get_summary(conn, boot_segment, start_counter)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"开机段 '{boot_segment}' 起始计数 {start_counter} 的摘要不存在"
            ),
        )
    return SummaryQueryResult(count=1, items=[result])


@app.get("/tide-levels", response_model=TideQueryResult, summary="按 UTC 范围查可用潮位")
def tide_levels(
    start_ms: int = Query(ge=0, description="起始 UTC 毫秒（含）"),
    end_ms: int = Query(ge=0, description="结束 UTC 毫秒（含）"),
) -> TideQueryResult:
    """只返回已校时且无冲突（status=ok）的观测，按 UTC 毫秒升序。"""
    if end_ms < start_ms:
        raise HTTPException(status_code=422, detail="end_ms 不得小于 start_ms")
    conn = db.get_conn()
    with db.lock():
        rows = conn.execute(
            "SELECT obs_id, utc_ms, tide_level FROM observations "
            "WHERE status = ? AND utc_ms BETWEEN ? AND ? "
            "ORDER BY utc_ms, obs_id",
            (core.OK, start_ms, end_ms),
        ).fetchall()
    items = [
        TidePoint(obs_id=r["obs_id"], utc_ms=r["utc_ms"], tide_level=r["tide_level"])
        for r in rows
    ]
    return TideQueryResult(count=len(items), items=items)


@app.get(
    "/observations/{obs_id}",
    response_model=ObservationDetail,
    summary="按观测 ID 查状态和原因",
)
def get_observation(obs_id: str) -> ObservationDetail:
    conn = db.get_conn()
    with db.lock():
        row = conn.execute(
            "SELECT * FROM observations WHERE obs_id = ?", (obs_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"观测 '{obs_id}' 不存在")
    calibration = None
    if row["cal_lo_counter"] is not None:
        calibration = CalibrationBasis(
            lo=CalibrationEndpoint(
                counter=row["cal_lo_counter"], utc_ms=row["cal_lo_utc_ms"]
            ),
            hi=CalibrationEndpoint(
                counter=row["cal_hi_counter"], utc_ms=row["cal_hi_utc_ms"]
            ),
        )
    # NaN 在 SQLite 中以 NULL 落库，按 tide_finite 标记还原原文
    tide_level = row["tide_level"] if row["tide_finite"] else float("nan")
    return ObservationDetail(
        obs_id=row["obs_id"],
        boot_segment=row["boot_segment"],
        counter=row["counter"],
        tide_level=tide_level,
        utc_ms=row["utc_ms"],
        status=row["status"],
        reason=row["reason"],
        calibration=calibration,
    )


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "up"}
