"""CSV / Excel 按可配模板解析为原始记录。

与 EDIFACT 解析输出保持同一结构（ParsedRecord），后续归一化逻辑完全复用。
模板由数据库 mapping_template / mapping_field 定义：
  source_column -> target_field（container_no / event_time / event_code / ...）
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, date

from openpyxl import load_workbook

from .records import ParsedRecord


def _coerce_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    return s or None


def parse_csv(content: str, column_map: dict[str, str]) -> list[ParsedRecord]:
    """column_map: {源列名: 统一字段名}。首行为表头。"""
    reader = csv.DictReader(io.StringIO(content))
    records: list[ParsedRecord] = []
    for idx, row in enumerate(reader):
        data, issues, line_meta = _map_row(row, column_map)
        rec = ParsedRecord(record_index=idx, segment_line=idx + 2, data=data, issues=issues)
        rec.data["_line_loc"] = line_meta
        records.append(rec)
    return records


def parse_excel(content_bytes: bytes, column_map: dict[str, str],
                sheet_name: str | None = None) -> list[ParsedRecord]:
    wb = load_workbook(io.BytesIO(content_bytes), read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header = [_coerce_str(c) for c in next(rows)]
    except StopIteration:
        return []
    records: list[ParsedRecord] = []
    idx = 0
    for row_no, values in enumerate(rows, start=2):
        if values is None or all(v is None or str(v).strip() == "" for v in values):
            continue
        row = {header[i]: values[i] if i < len(values) else None for i in range(len(header))}
        data, issues, line_meta = _map_row(row, column_map, coerce=True)
        rec = ParsedRecord(record_index=idx, segment_line=row_no, data=data, issues=issues)
        rec.data["_line_loc"] = line_meta
        records.append(rec)
        idx += 1
    return records


def _map_row(row: dict, column_map: dict[str, str], coerce: bool = False):
    data: dict = {"_raw_columns": {}}
    issues: list[str] = []
    line_meta: dict[str, int] = {}
    for source_col, target in column_map.items():
        raw_value = row.get(source_col)
        if coerce:
            raw_value = _coerce_str(raw_value)
        elif raw_value is not None:
            raw_value = str(raw_value).strip() or None
        data["_raw_columns"][target] = raw_value
        line_meta[target] = None  # 行号统一由 segment_line 承载，列名进血缘
        if raw_value is None:
            if target in ("container_no", "event_code"):
                issues.append(f"列 '{source_col}' 缺少必填字段 {target}")
            continue
        if target == "event_time":
            ts, err = _parse_flexible_time(raw_value)
            data["event_time"] = ts
            data["_raw_event_time"] = raw_value
            if err:
                issues.append(err)
        else:
            data[target] = raw_value
    if not data.get("container_no"):
        issues.append("记录缺少箱号 container_no")
    return data, issues, line_meta


_TIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y.%m.%d %H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
    "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d%H%M", "%Y%m%d",
]


def _parse_flexible_time(raw: str):
    raw = str(raw).strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt), None
        except ValueError:
            continue
    return None, f"无法解析的时间值 '{raw}'"
