# 光伏回测样本服务

把历史辐照预报与实测发电量配成回测样本的 API 服务（FastAPI + SQLite）。
核心保证：**事后发布（迟到）的预报不会进入样本**；生成结果来自同一个已提交快照；
同数据集号重试幂等；导入同键异值整批拒收；**场站更名后旧号可归并到正式号，归并事务原子、重复归并幂等**。

## 快速开始

```bash
docker compose up --build -d
curl http://localhost:8000/health
# 交互式文档（含迟到预报、重试、冲突示例，可直接在页面里执行）
open http://localhost:8000/docs
```

可选：再启动第二个共享同一 SQLite 数据卷的服务进程，验证多进程交错下的快照隔离：

```bash
docker compose --profile multi up -d   # api:8000 与 api2:8001 共用 /data/app.db
```

本地开发（不用 Docker）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                  # 端到端测试（导入/配对/快照隔离/场站归并）
uvicorn app.main:app --reload           # 数据库默认写到 ./data/app.db
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/imports/forecasts` | 导入预报 CSV（`text/csv` 请求体） |
| POST | `/imports/actuals` | 导入实测 CSV |
| POST | `/stations/merge` | 场站号归并：把旧号历史记录并入正式号 |
| GET | `/stations/aliases` | 查询旧号 → 正式号归并关系 |
| POST | `/datasets/{数据集号}/generate` | 生成回测样本并保存 |
| GET | `/datasets/{数据集号}` | 读回已保存结果 |
| GET | `/datasets` | 列出全部数据集 |
| GET | `/health` | 健康检查 |

## CSV 格式

预报（业务键 = 场站+目标小时+发布时刻）：

```csv
station,target_hour,issued_at,irradiance
ST01,2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0
ST01,2026-03-01T00:00:00Z,2026-02-28T06:00:00Z,550.0
```

实测（业务键 = 场站+目标小时）：

```csv
station,target_hour,energy
ST01,2026-03-01T00:00:00Z,48.2
```

- 时间按 UTC 解析，支持 `Z` 后缀、`+00:00`、空格分隔、裸时间（视为 UTC）；目标小时必须整点。
- 表头支持中英文别名（如 `场站,目标UTC小时,发布UTC时刻,预测辐照` / `电量`），可带 BOM。
- **同键同值**：幂等跳过（响应里的 `skipped_duplicates`）。
- **同键异值**（批内或与库中已有数据冲突）：**整批拒收**，返回 409 与冲突明细，一行都不写入。
- 行格式错误：整批拒收，返回 422 与逐行错误。

## 配对规则与迟到预报

生成参数：`station`、`start_hour`、`end_hour`（均含）、`lead_hours`（提前小时数）。

对每个目标小时：

```
发布截止时刻 cutoff = target_hour - lead_hours
采用 issued_at <= cutoff 的预报中最新的一条；issued_at > cutoff 的即迟到预报，不参与配对
```

结果每行带回 `cutoff_issued_at` 与实际采用的 `issued_at`（来源时刻）。
以上面的示例数据、`lead_hours=24` 为例：目标小时 `2026-03-01T00:00:00Z` 的截止时刻是
`2026-02-28T00:00:00Z`，`550.0` 那条发布于 `2026-02-28T06:00:00Z`，属于迟到预报，
配对结果采用 `500.0`。

缺配原因 `missing_reason`：`no_forecast`（截止前无已发布预报或全部迟到）、
`no_actual`（无实测）、`no_forecast+no_actual`、`null`（已配对），
另有中文字段 `missing_reason_cn`。

## 数据集号语义

- **同号同参数重试**：直接读回首次保存的结果（`replayed=true`），即使期间又导入了新数据。
- **同号异参数**：409，响应带回已存参数与请求参数，不覆盖。

## 场站号归并（旧号 → 正式号）

电站更名后，旧号与正式号各自存了一部分预报/实测，直接按号生成会漏配。
`POST /stations/merge` 请求体 `{"old_station": "ST01-OLD", "canonical_station": "ST01"}`
在**单个 `BEGIN IMMEDIATE` 写事务**内完成：

- **同键同值**：删除旧号行、保留正式号行（去重合并，全库只剩正式号一份）。
- **同键异值**：预报或实测任意一条冲突，整次归并报 409 并回滚，**不留半份更改**，
  响应逐条列出冲突键与双方取值。
- **仅一方存在的行**：迁入正式号；归并后旧号名下不再有任何原始记录。
- **幂等与身份规则**：
  - 同一（旧号, 正式号）重复归并直接成功（`idempotent=true`），不重复改数据；
  - 已归并的旧号不能改绑到其他正式号（409）；
  - 正式号不能是已归并的旧号，旧号也不能是现役正式号——即禁止链式归并（409）；
  - 旧号与正式号相同或为空 → 422。

归并登记到 `station_aliases`（单跳映射），随后：

- **两种 CSV 再导入旧号**：在导入事务内解析为正式号判重——同键同值幂等跳过，
  同键异值整批 409，新键也写入正式号，**归并后不可能写入旧号名下的新记录**。
- **新数据集生成**：请求场站号在生成事务内解析为正式号取数，结果行 `station` 与
  `params.station` 均为正式号；用旧号或正式号请求等价。
- **既有数据集**：参数按请求字面量保存，归并不改动已存快照；按**原参数**重试与
  `GET` 查询仍读回原结果（`replayed=true`）。
- **多实例交错**：导入、归并、生成都通过 `BEGIN IMMEDIATE` 串行化，新数据集只能来自
  归并前或归并后的完整已提交状态，不会读到半归并数据。

`GET /stations/aliases` 返回全部归并关系（含登记时间）。

## 一致性与原子性

- 导入与生成都在 `BEGIN IMMEDIATE` 写事务中执行：事务开始即持有写锁，
  其他进程/连接的写入只能等待（`busy_timeout`）或超时失败（映射为 503 可重试），
  因此生成读到的永远是**同一个已提交快照**，不会混入交错导入的半新数据。
- 任一环节失败即整体回滚：导入不会写半批，生成不会留下半份结果（有对应测试）。
- SQLite 开启 WAL，查询（GET）与写入互不阻塞。

## 端到端示例（curl）

```bash
# 1. 导入预报（含一条迟到预报）
curl -X POST http://localhost:8000/imports/forecasts \
  -H 'Content-Type: text/csv' --data-binary @- <<'CSV'
station,target_hour,issued_at,irradiance
ST01,2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0
ST01,2026-03-01T00:00:00Z,2026-02-28T06:00:00Z,550.0
ST01,2026-03-01T01:00:00Z,2026-02-28T00:30:00Z,510.0
ST01,2026-03-01T03:00:00Z,2026-02-27T09:00:00Z,520.0
CSV

# 2. 导入实测（03 时故意缺测）
curl -X POST http://localhost:8000/imports/actuals \
  -H 'Content-Type: text/csv' --data-binary @- <<'CSV'
station,target_hour,energy
ST01,2026-03-01T00:00:00Z,48.2
ST01,2026-03-01T01:00:00Z,49.1
ST01,2026-03-01T02:00:00Z,47.5
CSV

# 3. 生成：00/01 时配对，02 时 no_forecast，03 时 no_actual
curl -X POST http://localhost:8000/datasets/demo-20260301/generate \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST01","start_hour":"2026-03-01T00:00:00Z","end_hour":"2026-03-01T03:00:00Z","lead_hours":24}'

# 4. 同号同参数重试 → 读回原结果（replayed=true）
curl -X POST http://localhost:8000/datasets/demo-20260301/generate \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST01","start_hour":"2026-03-01T00:00:00Z","end_hour":"2026-03-01T03:00:00Z","lead_hours":24}'

# 5. 同号异参数 → 409 冲突
curl -X POST http://localhost:8000/datasets/demo-20260301/generate \
  -H 'Content-Type: application/json' \
  -d '{"station":"ST01","start_hour":"2026-03-01T00:00:00Z","end_hour":"2026-03-01T03:00:00Z","lead_hours":12}'

# 6. 场站更名：把旧号 ST01-OLD 的历史预报/实测并入正式号 ST01
curl -X POST http://localhost:8000/stations/merge \
  -H 'Content-Type: application/json' \
  -d '{"old_station":"ST01-OLD","canonical_station":"ST01"}'

# 7. 查询归并关系
curl http://localhost:8000/stations/aliases
```

## 项目结构

```
app/
  main.py    # FastAPI 路由、/docs 示例、事务边界
  logic.py   # 时间规范化、CSV 解析、配对逻辑、场站号归并
  db.py      # SQLite 连接（WAL、busy_timeout）与建表
tests/
  test_api.py           # 端到端测试（导入、配对、快照隔离、失败回滚）
  test_station_merge.py # 场站号归并测试（合并、冲突回滚、幂等、交错并发）
Dockerfile
docker-compose.yml
```
