# 单浮标潮位校时与极值摘要服务

浮标每次重启都会把毫秒计数归零，潮位分析却需要一条可信的 UTC 时间线。
本服务（FastAPI + SQLite）接收「开机段 + 段内计数」的观测与校时点，
把计数线性换算成 UTC 毫秒，并维护观测的可用状态供分析查询。

浮标在海上先报**潮位极值摘要**，回收后才补传逐笔读数：服务同时接收
摘要原文，在校时与明细齐备前按设备极值**暂供**，明细收齐后自动切换为
明细极值，并在两者有出入时同时列出。

## 快速开始

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互式文档在 http://localhost:8000/docs
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行测试
```

数据默认存放在命名卷 `buoy-data` 的 `/data/buoy.db`（可用环境变量
`BUOY_DB` 覆盖）。SQLite 为单写者模型，容器以单进程运行。

## 概念与规则

- **开机段（boot_segment）**：一次开机周期。计数只在段内单调，重启归零。
- **校时点（calibration point）**：`(开机段, 计数) -> UTC 毫秒`。
  同段同计数重复提交时新值替换旧值。
- **观测（observation）**：`(观测 ID, 开机段, 计数, 潮位)`。
  同一观测 ID 重复提交会覆盖原文并重算。

### 校时换算

- 计数落在相邻两个校时点之间时线性换算并**向下取整**：
  `utc = t_lo + (c - c_lo) * (t_hi - t_lo) // (c_hi - c_lo)`。
- 恰好命中校时点时直接取该点 UTC；只有一个校时点时仅该计数可换算。
- 计数落在本段校时区间之外：保留原文，状态置 `pending_calibration`（待校时）。

### 观测状态机

| 状态 | 含义 | 是否供分析 |
|---|---|---|
| `ok` | 已校时且无冲突 | ✅ |
| `pending_calibration` | 计数在校时区间外，待校时 | ❌ |
| `conflict` | 不同开机段落在同一 UTC 毫秒 | ❌ |

- **补传/更正校时点**会重算受影响观测（区间扩大可让待校时观测转为可用）。
- **冲突**：同一 UTC 毫秒被 ≥2 个不同开机段的观测占用时，这些观测全部
  置为 `conflict` 不供分析；更正校时点或覆盖观测解除冲突后自动恢复。
  同一开机段内多条观测共用同一毫秒不算冲突。
- **整批拒绝**：校时点合并后若使任一段内 UTC 随计数倒退，整个校时批次
  以 `409` 拒绝并回滚，旧数据照常供数。

### 原子性

每批提交（原文 + 校时点 + 重算 + 冲突刷新）都在**单个 SQLite 事务**中
完成，并由进程级锁串行化读写：原文、校时依据、分析供数同次生效，
查询和（崩溃后的）重启都不会看到半次重算的中间态。

## 极值摘要（先报极值、回收补明细）

### 摘要原文与覆盖区间

- 每份摘要填写 `boot_segment`（开机段）、`start_counter`（起始计数）、
  `min_tide` / `max_tide`（设备极值）。
- 极值必须是**有限数**（拒绝 NaN / ±Infinity），且最小值不大于最大值，
  否则整批以 `422` 拒绝。
- 从起始计数起每隔 **60000 毫秒**取一个应采点，共 **10** 点，覆盖闭区间
  `[start_counter, start_counter + 9*60000]`。
- **同段同起点重报**即更正设备极值；与同段其他摘要的覆盖区间**相交**
  （共用任一应采计数即相交；网格下起点相差 60000 的两份恰好紧邻、
  不相交）时整批以 `409` 拒绝并回滚，上次提交的原文、校时点、摘要和
  供数结果全部原样保留。

### 供数判定（按当前观测/校时现状即时推导，不落物化状态）

查询与每次提交都重新推导，因此重启后结果与库内数据始终一致：

1. **两端校时**：起点与末端应采点都能换算 UTC 才继续；任一端待校时
   （`pending_calibration`）即停供，原因码 `pending_calibration`。
2. **逐应采点检查**，任一应采点出现下列情况即停供：
   - 与其他开机段落在同一 UTC 毫秒 → `cross_segment_conflict`；
   - 同计数存在不同潮位值（同计数同值的重复读数只算一点）→
     `counter_value_conflict`；
   - 读数潮位非有限数（NaN / ±Infinity）→ `non_finite_tide_level`。
3. **明细未齐**（10 个应采点未全部有读数）：已到读数都在设备极值
   `[min_tide, max_tide]` 内才用**摘要暂供**（`source=summary`，
   供数极值即设备极值）；只要有读数越界即停供，原因码
   `observation_out_of_range`。非应采点上的读数不参与判定。
4. **明细收齐**：改用**明细极值**（`source=detail`）；明细极值与摘要
   极值有出入时，响应的 `discrepancy` 同时列出两组值。

### 提交响应中的摘要变更清单

`POST /observations`、`POST /calibrations`、`POST /summaries` 的响应都
另列 `changed_summaries`：本批之后**供数结果发生变化**的摘要及其新结果。
重算范围只限本批直接影响的开机段，以及因跨段冲突被波及的其他开机段
（写入前后各扫描一次，冲突产生与解除两个方向都覆盖）。

## API

均可在 `/docs` 中直接调用。

### `POST /observations` — 分批提交观测

```json
{
  "observations": [
    {"obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500, "tide_level": 2.34}
  ]
}
```

### `POST /calibrations` — 分批提交校时点

```json
{
  "calibrations": [
    {"boot_segment": "boot-1", "counter": 1000, "utc_ms": 1700000000000},
    {"boot_segment": "boot-1", "counter": 2000, "utc_ms": 1700000001000}
  ]
}
```

两个提交接口的响应都会列出**本次提交中时刻或状态发生变化的观测 ID**
（含新观测，以及被卷入/解除冲突的其他观测）：

```json
{
  "changed_count": 1,
  "changed": [{"obs_id": "obs-1", "utc_ms": 1700000000500, "status": "ok"}],
  "changed_summaries_count": 0,
  "changed_summaries": []
}
```

### `POST /summaries` — 分批提交/更正极值摘要

```json
{
  "summaries": [
    {"boot_segment": "boot-1", "start_counter": 100000,
     "min_tide": 0.12, "max_tide": 3.45}
  ]
}
```

- 极值非有限或 `min_tide > max_tide` → `422`；覆盖区间与同段其他摘要
  相交 → `409`，均整批回滚、旧数据照常供数。
- 同段同起点重报即更正极值。响应的 `changed_summaries` 给出更正后
  （或新登记）摘要的当前供数结果；结果未变的重报不会列出。

### `GET /summaries?boot_segment=...&start_counter=...` — 查询摘要供数结果

按开机段和起始计数查摘要两端 UTC、供数来源与极值；摘要不存在返回
`404`。供数中 `serving=true`，停供时 `reason_code` / `reason` 说明原因：

```json
{
  "count": 1,
  "items": [{
    "boot_segment": "boot-1",
    "start_counter": 100000,
    "end_counter": 640000,
    "min_tide": 0.12,
    "max_tide": 3.45,
    "serving": true,
    "source": "summary",
    "reason_code": null,
    "reason": null,
    "start_utc_ms": 1700000000000,
    "end_utc_ms": 1700000540000,
    "served_min": 0.12,
    "served_max": 3.45,
    "detail_min": null,
    "detail_max": null,
    "discrepancy": null
  }]
}
```

明细收齐后 `source` 变为 `detail`，`served_*` 取明细极值；若与摘要
极值有出入，`discrepancy` 同时给出 `summary` 与 `detail` 两组值。
停供原因码：`pending_calibration` / `cross_segment_conflict` /
`counter_value_conflict` / `non_finite_tide_level` /
`observation_out_of_range`。

### `GET /tide-levels?start_ms=...&end_ms=...` — 按 UTC 范围查可用潮位

只返回 `status=ok` 的观测，区间含端点，按 UTC 毫秒升序。

### `GET /observations/{obs_id}` — 按观测 ID 查状态和原因

返回原文、换算结果、状态、原因与校时依据（用于换算的上下界校时点）：

```json
{
  "obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500,
  "tide_level": 2.34, "utc_ms": 1700000000500, "status": "ok",
  "reason": null,
  "calibration": {
    "lo": {"counter": 1000, "utc_ms": 1700000000000},
    "hi": {"counter": 2000, "utc_ms": 1700000001000}
  }
}
```

### 其他

- `GET /health` — 健康检查
- `GET /docs` — Swagger UI；`GET /openapi.json` — OpenAPI 模式

## 目录结构

```
app/
  main.py   # FastAPI 路由与请求/响应模型
  core.py   # 校时换算、批量写入、重算、冲突检测、摘要供数推导、变更快照
  db.py     # SQLite 连接、模式、迁移、事务与锁
tests/
  test_api.py        # 观测/校时端到端测试
  test_summaries.py  # 摘要提交、供数判定、停供原因、持久化测试
Dockerfile
docker-compose.yml
```

## 数据持久化与升级

- 摘要只保存提交原文；供数结果（供/停、来源、极值、停供原因）全部由
  观测与校时现状即时推导，不做物化，因此重启后查询结果天然一致。
- 旧版本数据库（无 `tide_finite` 列、摘要表）在首次打开时自动就地迁移：
  历史潮位均视为有限数，既有数据不丢失。
- 观测潮位允许 NaN / ±Infinity；SQLite 中 NaN 以 NULL 落库并由
  `tide_finite=0` 标记，接口读取时还原为 NaN。
