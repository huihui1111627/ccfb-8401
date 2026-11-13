# 箱动态数据平台（Container Movement Platform）

面向码头数据组的箱动态数据平台：把船公司发来的 **EDIFACT（BAPLIE / COARRI / CODECO）、Excel、CSV**
接入后，按可配置映射规则归一到统一箱事件模型，完成代码表统一、动态链串联、重复/乱序/矛盾治理、
对账差异识别、字段级血缘追溯和船次航线装卸量统计。全部数据落在 **DuckDB**，网页控制台用 **Flask**。

## 功能与需求对照

| # | 需求 | 实现位置 |
|---|------|----------|
| 1 | 解析 BAPLIE / COARRI / CODECO | `ingest/edifact.py`（词法）+ `ingest/messages.py`（业务解析） |
| 2 | Excel/CSV 按可配模板映射统一模型 | `ingest/tabular.py` + `normalize/codes.py` 种子模板，库表 `mapping_template/mapping_field` |
| 3 | 箱型/港口/船公司代码统一转换 | 库表 `code_table`，多来源体系（ISO/UNLOCODE/SCAC/INTERNAL…）→ GLOBAL 标准码 |
| 4 | 进场/装船/卸船按时间串动态链 | `normalize/chains.py`，标准次序 `GATE_IN→LOAD→DISCHARGE→GATE_OUT` |
| 5 | 迟到/重复/乱序去重排序 | 自然键去重（`normalized_event.natural_key`）、时间串链、`OUT_OF_ORDER` 检测 |
| 6 | 互相矛盾的报文标出 | 同箱同类事件时间/港口不一致 → `CONTRADICTION` 差异 |
| 7 | 字段追到哪份报文哪行 | `field_lineage`：文件 + 段/列定位 + 行号 + 原始值 → 标准值 |
| 8 | 按船次航线统计装卸量 | `normalize/stats.py` → `voyage_stats`（自然箱 + TEU，按箱去重） |

## 目录结构

```
src/ctplatform/
├── config.py              # 运行配置（CTPLATFORM_HOME 环境变量隔离）
├── db.py                  # DuckDB 连接与全部表结构
├── cli.py                 # 命令行入口
├── ingest/
│   ├── edifact.py         # EDIFACT 词法（UNA 自定义分隔符、转义、截断、多报文）
│   ├── messages.py        # BAPLIE/COARRI/CODECO 业务解析
│   ├── tabular.py         # CSV/Excel 模板映射
│   ├── records.py         # 共享原始记录结构
│   └── __init__.py        # 接入总入口（SHA256 文件去重、原始留痕）
├── normalize/
│   ├── codes.py           # 代码表/模板种子数据
│   ├── normalize.py       # 归一化、自然键去重、字段血缘
│   ├── chains.py          # 动态链、乱序/矛盾/断链对账
│   ├── stats.py           # 船次航线装卸量（TEU）
│   └── pipeline.py        # 全量幂等流水线编排
├── testdata/              # 测试报文造数器（含脏数据注入）
└── web/                   # Flask 控制台（5 个页面 + 总览）
tests/                     # 35 个 pytest 用例（含链不断不重硬校验）
```

## 快速开始

```bash
pip install -r requirements.txt
pip install -e .

# 1. 建库 + 种子代码表/模板
ctplatform init-db

# 2. 生成测试报文（12 个文件，含错码/缺段/重复/迟到/矛盾）
ctplatform generate-samples

# 3. 接入（CSV/Excel 必须指定模板）
ctplatform ingest data/samples/BAPLIE_20261109.edi
ctplatform ingest data/samples/gate_ops_20261110.csv -t tpl_gate_csv
ctplatform ingest data/samples/ops_excel_20261110.xlsx -t tpl_ops_excel

# 4. 跑全量流水线（派生表全量重算，可反复执行，结果幂等）
ctplatform run

# 5. 启动网页控制台
ctplatform web --port 5000
```

## 测试报文造数场景（8 只箱 + 2 只表格箱）

| 箱号 | 注入问题 | 预期链状态/差异 |
|------|----------|----------------|
| EGLV1000001 | 干净全链，CSV 重复进场 | 链 OK，重复事件去重 |
| EGLV1000002 | 文件内 + 跨文件重复进场 | 链 OK，`DUPLICATE_MESSAGE` |
| EGLV1000003 | 卸船时间早于装船 | `OUT_OF_ORDER` |
| EGLV1000004 | 两份装船时间/港口矛盾 | `CONTRADICTION` |
| EGLV1000005 | 未知箱型、缺 DTM、报文截断、缺箱号残组 | `UNMAPPED_*` / `MISSING_*` / `PARSE_ERROR` |
| EGLV1000006 | 只有进场+装船 | `CHAIN_GAP` + `INCOMPLETE` |
| EGLV1000007 | 未知港口/船公司代码 | `UNMAPPED_PORT` / `UNMAPPED_CARRIER` |
| EGLV1000008 | 船图有配载、无实物装船 | `MANIFEST_NO_ACTUAL` |
| EGLV1000009 | CSV 未知船公司，仅出场 | `INCOMPLETE` + `UNMAPPED_CARRIER` |
| EGLV1000010 | Excel 模板链路 | 进场+装船 |

**硬性质量保证**（`tests/test_pipeline_e2e.py`）：
- 每只箱动态链上同一物理事件类型最多出现一次（链不重）
- 相邻环节缺失必出 `CHAIN_GAP`（链不断可检）
- 链上每个事件都能经 `field_lineage` 追到来源文件与行号
- 流水线重跑前后各表行数完全一致（幂等）

## 网页页面

- **总览** `/`：文件/事件/箱量/重复/差异 KPI、链状态与差异分布
- **数据源接入** `/sources`：上传文件（自动跑流水线）、文件清单、解析状态、原始报文留痕
- **映射规则** `/mappings`：表格模板列映射、统一代码表全量浏览
- **箱动态** `/movements`：按箱动态链、事件明细、质量标记，可按箱号/事件类型筛选
- **对账差异** `/recon`：差异筛选、JSON 详情、船次航线装卸量统计
- **血缘** `/lineage`：事件 → 字段 → 来源文件/段/列/行号/原始值/标准值

## 数据模型要点

- `source_file` / `raw_record`：原始报文与原始记录**全量留痕**，任何派生结果可重建
- `normalized_event`：统一箱事件；`natural_key` 为去重自然键，`is_duplicate` 标记重复报文，
  `event_id` 每行唯一保证重复报文也可追溯
- `field_lineage`：字段级血缘，EDIFACT 定位到段标签+行号，表格定位到源列+行号
- `event_chain`：每箱一条链，事件按时间排序，状态 `OK/GAP/OUT_OF_ORDER/CONTRADICTION/INCOMPLETE`
- `recon_diff`：全部对账差异（解析错误、未映射、缺字段、乱序、矛盾、断链、船图对账）
- `voyage_stats`：按 船IMO+航次+港口 聚合的装卸自然箱数与 TEU

## 运行测试

```bash
python3 -I -m pytest tests/ -q
# 35 passed
```
