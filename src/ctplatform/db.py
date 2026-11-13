"""DuckDB 连接管理与表结构。

所有派生表（normalized_event / event_chain / recon_diff ...）均可由
source_file + raw_record + 映射模板/代码表重建，pipeline 每次全量重算，
保证重复运行结果幂等。
"""
from __future__ import annotations

import duckdb

from .config import Config, CONFIG

SCHEMA_SQL = """
-- 数据源文件（原始留痕）
CREATE TABLE IF NOT EXISTS source_file (
    file_id           VARCHAR PRIMARY KEY,
    filename          VARCHAR NOT NULL,
    source_type       VARCHAR NOT NULL,          -- EDIFACT / CSV / EXCEL
    message_type      VARCHAR,                   -- BAPLIE/COARRI/CODECO，表格为 NULL
    template_id       VARCHAR,                   -- 表格文件使用的模板
    content           VARCHAR NOT NULL,
    sha256            VARCHAR NOT NULL,
    byte_size         BIGINT NOT NULL,
    parse_status      VARCHAR NOT NULL,          -- OK / ERROR / PARTIAL
    parse_errors      VARCHAR,                   -- JSON 数组
    ingested_at       TIMESTAMP NOT NULL
);

-- 原始记录：每个箱/每行一条，保存原始 JSON 与行号
CREATE TABLE IF NOT EXISTS raw_record (
    raw_id            VARCHAR PRIMARY KEY,
    file_id           VARCHAR NOT NULL,
    record_index      INTEGER NOT NULL,
    segment_line      INTEGER,                   -- EDIFACT EQD 段行号
    raw_payload       VARCHAR NOT NULL,          -- JSON
    record_status     VARCHAR NOT NULL DEFAULT 'OK'
);

-- 表格映射模板
CREATE TABLE IF NOT EXISTS mapping_template (
    template_id       VARCHAR PRIMARY KEY,
    template_name     VARCHAR NOT NULL,
    source_type       VARCHAR NOT NULL,          -- CSV / EXCEL
    header_row        INTEGER NOT NULL DEFAULT 1,
    sheet_name        VARCHAR,
    created_at        TIMESTAMP NOT NULL,
    updated_at        TIMESTAMP NOT NULL
);

-- 模板列映射：源列 -> 统一字段
CREATE TABLE IF NOT EXISTS mapping_field (
    mapping_id        VARCHAR PRIMARY KEY,
    template_id       VARCHAR NOT NULL,
    source_column     VARCHAR NOT NULL,
    target_field      VARCHAR NOT NULL,
    transform         VARCHAR,                   -- 可选转换标识
    created_at        TIMESTAMP NOT NULL
);

-- 统一代码表（domain: container_type / port / carrier / event_code ...）
CREATE TABLE IF NOT EXISTS code_table (
    code_id           VARCHAR PRIMARY KEY,
    domain            VARCHAR NOT NULL,
    source_system      VARCHAR NOT NULL,         -- 来源体系，GLOBAL 为标准
    raw_code          VARCHAR NOT NULL,
    std_code          VARCHAR NOT NULL,
    std_name          VARCHAR,
    extra             VARCHAR,                   -- JSON，如 {"size_ft": 20}
    updated_at        TIMESTAMP NOT NULL
);

-- 归一化箱事件（统一模型核心表）
CREATE TABLE IF NOT EXISTS normalized_event (
    event_id          VARCHAR PRIMARY KEY,      -- 每行唯一（哈希到 raw 记录）
    natural_key       VARCHAR NOT NULL,         -- 去重自然键，同键多份报文即重复
    raw_id            VARCHAR NOT NULL,
    file_id           VARCHAR NOT NULL,
    message_type      VARCHAR NOT NULL,
    record_index      INTEGER NOT NULL,
    segment_line      INTEGER,

    container_no      VARCHAR NOT NULL,
    carrier_code      VARCHAR,                  -- 已归一化
    event_type        VARCHAR NOT NULL,         -- GATE_IN/LOAD/DISCHARGE/GATE_OUT/PLAN_STOW
    event_time        TIMESTAMP,
    port_code         VARCHAR,                  -- 已归一化
    vessel_imo        VARCHAR,
    vessel_name       VARCHAR,
    voyage_no         VARCHAR,
    container_type    VARCHAR,                  -- 已归一化
    size_ft           INTEGER,
    laden_status      VARCHAR,

    is_duplicate      BOOLEAN NOT NULL DEFAULT FALSE,
    quality_flags     VARCHAR NOT NULL DEFAULT '[]',  -- JSON 数组
    is_mapped         BOOLEAN NOT NULL DEFAULT TRUE,
    ingested_at       TIMESTAMP NOT NULL
);

-- 字段级血缘：统一字段 -> 报文/文件 + 段/列 + 行号 + 原始值
CREATE TABLE IF NOT EXISTS field_lineage (
    lineage_id        VARCHAR PRIMARY KEY,
    event_id          VARCHAR NOT NULL,
    raw_id            VARCHAR NOT NULL,
    target_field      VARCHAR NOT NULL,
    source_kind       VARCHAR NOT NULL,         -- SEGMENT / COLUMN
    source_locator    VARCHAR NOT NULL,         -- 段标签如 EQD/C507.0 或源列名
    source_line       INTEGER,
    raw_value         VARCHAR,
    std_value         VARCHAR
);

-- 箱动态链：每箱一条，链上事件按序存放
CREATE TABLE IF NOT EXISTS event_chain (
    chain_id          VARCHAR PRIMARY KEY,
    container_no      VARCHAR NOT NULL,
    event_count       INTEGER NOT NULL,
    event_ids         VARCHAR NOT NULL,         -- JSON 数组，按 event_time 排序
    event_types       VARCHAR NOT NULL,         -- JSON 数组
    chain_status      VARCHAR NOT NULL,         -- OK/GAP/OUT_OF_ORDER/DUPLICATE/CONTRADICTION/INCOMPLETE
    issues            VARCHAR NOT NULL DEFAULT '[]'
);

-- 对账差异
CREATE TABLE IF NOT EXISTS recon_diff (
    diff_id           VARCHAR PRIMARY KEY,
    diff_type         VARCHAR NOT NULL,
    severity          VARCHAR NOT NULL,         -- ERROR / WARN / INFO
    container_no      VARCHAR,
    file_id           VARCHAR,
    event_id          VARCHAR,
    description       VARCHAR NOT NULL,
    detail            VARCHAR,                  -- JSON
    detected_at       TIMESTAMP NOT NULL
);

-- 船次航线装卸量统计
CREATE TABLE IF NOT EXISTS voyage_stats (
    stats_id          VARCHAR PRIMARY KEY,
    vessel_imo        VARCHAR,
    vessel_name       VARCHAR,
    voyage_no         VARCHAR,
    port_code         VARCHAR,
    route_key         VARCHAR NOT NULL,
    load_count        INTEGER NOT NULL DEFAULT 0,
    discharge_count   INTEGER NOT NULL DEFAULT 0,
    load_teu          DOUBLE NOT NULL DEFAULT 0,
    discharge_teu     DOUBLE NOT NULL DEFAULT 0,
    gate_in_count     INTEGER NOT NULL DEFAULT 0,
    gate_out_count    INTEGER NOT NULL DEFAULT 0
);

-- 流水线运行日志
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id            VARCHAR PRIMARY KEY,
    started_at        TIMESTAMP NOT NULL,
    finished_at       TIMESTAMP,
    files_seen        INTEGER NOT NULL DEFAULT 0,
    files_failed      INTEGER NOT NULL DEFAULT 0,
    raw_records       INTEGER NOT NULL DEFAULT 0,
    events            INTEGER NOT NULL DEFAULT 0,
    duplicates        INTEGER NOT NULL DEFAULT 0,
    diffs             INTEGER NOT NULL DEFAULT 0,
    status            VARCHAR NOT NULL DEFAULT 'RUNNING'
);
"""


def connect(config: Config | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    cfg = config or CONFIG
    cfg.ensure_dirs()
    con = duckdb.connect(str(cfg.db_path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA_SQL)
    return con
