"""数据源接入总入口。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from ..db import connect
from .edifact import parse_interchange
from .messages import parse_messages
from .records import ParsedRecord
from .tabular import parse_csv, parse_excel


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_source_type(filename: str, content: str) -> str:
    head = content.lstrip()[:12].upper()
    if head.startswith(("UNA", "UNB", "UNH")):
        return "EDIFACT"
    suffix = Path(filename).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return "EXCEL"
    if suffix == ".csv" or "," in content[:200]:
        return "CSV"
    return "UNKNOWN"


def _edifact_records(content: str):
    ic = parse_interchange(content)
    parsed = parse_messages(ic)
    records: list[ParsedRecord] = []
    message_types: list[str] = []
    file_errors: list[str] = list(ic.errors)
    parse_status = "OK"
    for pm in parsed:
        message_types.append(pm.message_type)
        file_errors.extend(f"[{pm.message_type}] {e}" for e in pm.errors)
        file_errors.extend(f"[{pm.message_type}] {w}" for w in pm.warnings)
        if pm.errors:
            parse_status = "ERROR"
        elif pm.warnings or any(r.issues for r in pm.records):
            parse_status = "PARTIAL" if parse_status != "ERROR" else parse_status
        for rec in pm.records:
            rec.data["_message_type"] = pm.message_type
            records.append(rec)
    return records, message_types[0] if message_types else None, file_errors, parse_status


def _load_template(con, template_id: str):
    row = con.execute(
        "SELECT template_id, source_type, sheet_name FROM mapping_template WHERE template_id=?",
        [template_id],
    ).fetchone()
    if not row:
        raise ValueError(f"映射模板不存在: {template_id}")
    cols = con.execute(
        "SELECT source_column, target_field FROM mapping_field WHERE template_id=?",
        [template_id],
    ).fetchall()
    return {"template_id": row[0], "source_type": row[1], "sheet_name": row[2],
            "column_map": {c: t for c, t in cols}}


def ingest_file(filename: str, raw_bytes: bytes, template_id: str | None = None,
                con=None) -> dict:
    """接入单个文件，原始内容与原始记录全部留痕。返回处理摘要。"""
    own_con = con is None
    con = con or connect()
    sha = _sha256_bytes(raw_bytes)
    dup = con.execute("SELECT file_id FROM source_file WHERE sha256=?", [sha]).fetchone()
    if dup:
        return {"status": "DUPLICATE_FILE", "file_id": dup[0], "records": 0}

    content = raw_bytes.decode("utf-8", errors="replace")
    source_type = detect_source_type(filename, content)
    file_id = "f_" + sha[:16]
    now = datetime.now()

    if source_type == "EDIFACT":
        records, message_type, errors, parse_status = _edifact_records(content)
    elif source_type in ("CSV", "EXCEL"):
        if not template_id:
            raise ValueError(f"{source_type} 文件必须指定映射模板 template_id")
        tpl = _load_template(con, template_id)
        if source_type == "CSV":
            records = parse_csv(content, tpl["column_map"])
        else:
            records = parse_excel(raw_bytes, tpl["column_map"], tpl["sheet_name"])
        for rec in records:
            rec.data["_message_type"] = source_type
        message_type, errors, parse_status = None, [], (
            "PARTIAL" if any(r.issues for r in records) else "OK"
        )
    else:
        raise ValueError(f"无法识别的文件类型: {filename}")

    con.execute(
        """INSERT INTO source_file
        (file_id, filename, source_type, message_type, template_id, content, sha256,
         byte_size, parse_status, parse_errors, ingested_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [file_id, filename, source_type, message_type, template_id, content, sha,
         len(raw_bytes), parse_status, json.dumps(errors, ensure_ascii=False), now],
    )
    for rec in records:
        raw_id = "r_" + hashlib.sha256(
            f"{file_id}:{rec.record_index}:{rec.segment_line}".encode()
        ).hexdigest()[:20]
        payload = dict(rec.data)
        payload["_issues"] = rec.issues
        con.execute(
            """INSERT INTO raw_record
            (raw_id, file_id, record_index, segment_line, raw_payload, record_status)
            VALUES (?,?,?,?,?,?)""",
            [raw_id, file_id, rec.record_index, rec.segment_line,
             json.dumps(payload, ensure_ascii=False, default=str),
             "ERROR" if rec.issues else "OK"],
        )
    if own_con:
        con.close()
    return {
        "status": "INGESTED", "file_id": file_id, "source_type": source_type,
        "message_type": message_type, "records": len(records),
        "parse_status": parse_status, "errors": errors,
    }
