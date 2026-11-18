# Fertilizer RBD Trial API

随机区组试验（至多 12 个区组，处理 A/B）干物质产量分析与**施肥流水核对**服务。
FastAPI + SQLite，交互式文档在 `/docs`（Swagger UI），可直接完成导入、录入、
纠错、核对、核准和查询。

**分析口径**

- 干物质产量 = 鲜重 × (1 − 含水率) ÷ 面积（含水率为小数，如 0.62）
- 仅当某区组 A、B 两小区的面积、鲜重、含水率齐全时纳入分析，计算组内差 B − A
- 发布内容：逐组差、均值差、双侧**精确随机化检验** p 值 —— 枚举各区组 A/B 对调的
  全部 2ⁿ 种排列，统计 |均值差| 不小于实测值的比例（含实测排列本身，故 p ≥ 1/2ⁿ）
- 缺测区组列为待补（`pending`，附缺失原因）；没有任何齐全区组时不发布（HTTP 422）

**版本与过期**

- 每次实际改动（导入/含水率/更正）使 `data_version` +1；无变化的重发不升版本
- 发布在单个 `BEGIN IMMEDIATE` 事务内读取同一快照并落库，区组、差值、p 值、来源
  （provenance：数据版本 + 原始行 + sha256）天然同版
- 数据一旦变化，旧发布自动标记 `stale: true`，但始终可查询，来源完整保留
- 发布失败（如全部缺测）不写入任何记录，旧结论不受影响

## 施肥流水核对

监督员核对各小区**实际施肥量**。一次作业录入料箱起始重量与按序号排列的称重
流水（每笔：小区、作业后余重、本次补料量），系统依据相邻称重推算耗料并合并
多次作业的单位面积施用量。

**耗料口径**

- 首笔参考重量 = 作业起始重量；其后参考重量 = 前笔余重 + 前笔补料
- 单笔耗料 = 参考重量 − 本次余重；同一小区跨作业耗料求和后 ÷ 面积 = 单位面积剂量
- **缺号断链**：缺号之后第一笔的参考重量未知，耗料记为 `null`（待核），
  绝不按零施肥处理；小区在某次作业中漏报同样列为待核
- 待核（`pending`，不阻止核准）：缺号、断链不可算、小区漏报、面积未导入
- 异常（`anomalies`，**阻止核准**）：
  - `weight_increase`：余重 > 前次余重 + 补料（料箱物料不可能凭空增加）
  - `control_consumption`：对照小区（默认处理 A，可用环境变量
    `FERT_CONTROL_TREATMENT=B` 切换）出现正耗料

**更正与重核**

- 中间一笔余重补录（按 `seq` 迟到插入）或更正后，整条作业链重新推算，
  前后相邻称重的参考重量与耗料同步更新，可能新暴露异常
- 小区面积经 `/corrections` 更正后，所有单位面积剂量立即按新面积重算
- 施肥流水与小区面积共用同一个 `data_version`：任何实际改动立即使旧核准报告
  `stale: true`；重核成功才产生新版报告，重核失败（异常未消除）时旧版仍可查询
- 核准报告冻结当时的完整流水与面积快照（provenance：数据版本 + 原始行 +
  sha256），报告与来源保证来自同一已提交事务，导入/更正/核对交错也不会串版
- 无变化的重发不升版本，不会使报告误过期

施肥相关接口（均在 `/fertilizer` 前缀下，`/docs` 可直接操作）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/fertilizer/operations` | 录入一次作业（起始重量+有序称重流水）；省略 `operation_no` 自动编号 |
| POST | `/fertilizer/corrections` | 更正起始重量/某笔称重；不存在的 `seq` 作迟到补录（须给完整称重） |
| GET | `/fertilizer/status` | 当前核对结果：逐笔耗料、合并剂量、待核与异常、可否核准 |
| GET | `/fertilizer/operations` | 原始作业与称重流水 |
| POST | `/fertilizer/approvals` | 核准并冻结报告；有异常或无作业时 422，失败不留痕 |
| GET | `/fertilizer/approvals` | 全部核准报告（新→旧），含 `stale` 标记 |
| GET | `/fertilizer/approvals/latest` | 最新一次核准（可能已过期） |
| GET | `/fertilizer/approvals/{id}` | 某次核准全文：流水、剂量、待核、异常、冻结来源 |

## 快速开始

```bash
docker compose up --build
# 打开 http://127.0.0.1:8000/docs
```

本地运行（Python 3.11+）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

端到端演示（服务启动后）：

```bash
./scripts/demo.sh            # 或 ./scripts/demo.sh http://127.0.0.1:8000
```

手动 curl 流程：

```bash
curl -X POST localhost:8000/imports   -H 'Content-Type: application/json' -d @examples/import.json
curl -X POST localhost:8000/publications                       # 422：全部缺测，不发布
curl -X POST localhost:8000/moisture  -H 'Content-Type: application/json' -d @examples/moisture.json
curl -X POST localhost:8000/publications                       # 201：4 组齐全，5/6 待补
curl -X POST localhost:8000/moisture  -H 'Content-Type: application/json' -d @examples/moisture_late.json
curl    localhost:8000/publications                            # 旧结论 stale: true
curl -X POST localhost:8000/publications                       # 201：6 组，新 p 值
curl -X POST localhost:8000/corrections -H 'Content-Type: application/json' -d @examples/correction.json
curl -X POST localhost:8000/publications                       # 201：更正后的同版结论

# 施肥流水核对
curl -X POST localhost:8000/fertilizer/operations -H 'Content-Type: application/json' -d @examples/fertilizer_operation.json
curl    localhost:8000/fertilizer/status                       # 逐笔耗料、合并剂量、待核/异常
curl -X POST localhost:8000/fertilizer/approvals               # 201：核准并冻结报告
curl -X POST localhost:8000/fertilizer/operations -H 'Content-Type: application/json' -d @examples/fertilizer_operation_gap.json
curl    localhost:8000/fertilizer/status                       # 缺号 -> 待核，耗料 null 而非 0
curl -X POST localhost:8000/fertilizer/corrections -H 'Content-Type: application/json' -d @examples/fertilizer_late_entry.json
curl    localhost:8000/fertilizer/approvals                    # 旧报告 stale: true
curl -X POST localhost:8000/fertilizer/approvals               # 重核成功后启用新版
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/imports` | 批量导入/重导小区面积+鲜重（可附带含水率）；已有含水率默认保留 |
| POST | `/moisture` | 录入已导入小区的含水率（迟到检测同此入口） |
| POST | `/corrections` | 更正面积/鲜重/含水率；`"moisture": null` 清除错误含水率（该组回到待补） |
| GET | `/plots` | 当前原始数据与 `data_version` |
| GET | `/blocks` | 当前齐全/待补区组及缺失原因 |
| POST | `/publications` | 发布当前结论；无齐全区组时 422，旧结论不受影响 |
| GET | `/publications` | 全部发布（新→旧），含 `stale` 标记 |
| GET | `/publications/latest` | 最新一次发布（可能已过期） |
| GET | `/publications/{id}` | 某次发布全文：逐组差、均值差、p 值、待补、来源 |

校验规则：区组号 1–12；处理仅 A/B；面积 > 0；鲜重 ≥ 0；含水率 ∈ [0, 1)；
同一请求内 (block_id, treatment) 不得重复；含水率/更正只接受已导入的小区。

## 项目结构

```
app/
  main.py               # FastAPI 路由与发布事务
  analysis.py           # 干物质、组内差、精确随机化检验（纯函数）
  fertilizer.py         # 耗料/断链/异常/合并剂量核对（纯函数）
  fertilizer_routes.py  # 施肥录入、更正、核准路由与同版事务
  fertilizer_schemas.py # 施肥相关 Pydantic 模型
  db.py                 # SQLite 连接、schema、data_version
  schemas.py            # 试验数据 Pydantic 模型
examples/               # 示例数据（导入、含水率、迟到、更正、施肥流水）
scripts/demo.sh         # 端到端演示（含施肥核对）
tests/                  # pytest 端到端测试
Dockerfile / docker-compose.yml
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

覆盖：完整流程（导入→缺测拒发→部分齐全→迟到→更正→重发布）、精确 p 值
（Fraction 精确枚举独立复核 + 已知真值用例）、失败发布保留旧结论、
同版来源校验、无操作改动不误判过期、参数校验与 404；施肥侧覆盖补料耗料推算、
跨作业合并剂量、缺号断档按待核而非零、对照耗料与余重异常阻止核准、
迟到补录重核相邻称重、面积更正更新剂量、报告过期与失败重核保留旧版、
来源同版快照。
