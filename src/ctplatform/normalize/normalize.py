"""原始记录 -> 归一化箱事件：代码表转换、自然键去重、字段血缘。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

# 物理动态链上的标准事件及先后次序（PLAN_STOW 为计划类，不进链）
CHAIN_ORDER = ["GATE_IN", "LOAD", "DISCHARGE", "GATE_OUT"]
PHYSICAL_EVENTS = set(CHAIN_ORDER)


def _lookup(con, domain: str, raw_code, preferred_systems: list[str] | None = None):
    """代码表查询：优先来源体系，未命中则全局兜底。返回 (std_code, name, extra, system)。"""
    if raw_code is None or str(raw_code).strip() == "":
        return None, None, None, None
    raw = str(raw_code).strip()
    systems = (preferred_systems or []) + ["EDIFACT", "TABULAR", "ISO", "SCAC",
                                           "UNLOCODE", "INTERNAL", "CARRIER_A", "CARRIER_B"]
    for sys_name in dict.fromkeys(systems):
        row = con.execute(
            "SELECT std_code, std_name, extra, source_system FROM code_table "
            "WHERE domain=? AND source_system=? AND raw_code=?",
            [domain, sys_name, raw],
        ).fetchone()
        if row:
            return row[0], row[1], row[2], row[3]
    return None, None, None, None


def _event_natural_key(rec: dict, event_type, container_no, event_time, port_code,
                       vessel_imo, voyage_no) -> str:
    """去重自然键：同箱 + 同事件类型 + 同事件时间（分钟级）+ 同港口 + 同船次。"""
    minute = event_time.strftime("%Y-%m-%d %H:%M") if isinstance(event_time, datetime) else "NULL"
    raw = "|".join([
        str(container_no), str(event_type), minute,
        str(port_code or "NULL"), str(vessel_imo or "NULL"), str(voyage_no or "NULL"),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


def _lineage_id(event_id, field):
    return "l_" + hashlib.sha256(f"{event_id}|{field}".encode()).hexdigest()[:18]

def rebuild_normalized(con) -> dict:
    """全量重算 normalized_event + field_lineage（幂等）。"""
    con.execute("DELETE FROM field_lineage")
    con.execute("DELETE FROM normalized_event")

    raw_rows = con.execute(
        """SELECT rr.raw_id, rr.file_id, rr.record_index, rr.segment_line,
                  rr.raw_payload, sf.source_type, sf.message_type, sf.template_id, sf.ingested_at
           FROM raw_record rr JOIN source_file sf ON rr.file_id = sf.file_id
           ORDER BY sf.ingested_at, rr.file_id, rr.record_index"""
    ).fetchall()

    # event_id -> 该自然键首次出现的 raw_id，用于判定重复
    first_seen: dict[str, str] = {}
    counts = {"events": 0, "duplicates": 0, "unmapped": 0}
    now = datetime.now()

    for raw_id, file_id, rec_idx, seg_line, payload, source_type, msg_type, tpl_id, ingested in raw_rows:
        rec = json.loads(payload)
        is_edi = source_type == "EDIFACT"
        preferred = ["EDIFACT"] if is_edi else ["TABULAR", "INTERNAL", "CARRIER_A", "CARRIER_B"]
        flags: list[str] = []
        lineage: list[tuple] = []

        container_no = rec.get("container_no")
        if not container_no:
            flags.append("MISSING_CONTAINER_NO")

        # ---- 事件码归一
        raw_event = rec.get("raw_event_code")
        event_type, ev_name, _, ev_sys = _lookup(
            con, "event_code", raw_event,
            ["EDIFACT"] if is_edi else ["TABULAR", "EDIFACT"])
        if not event_type:
            event_type = "UNKNOWN"
            flags.append("UNMAPPED_EVENT")
            counts["unmapped"] += 1
        if raw_event:
            lineage.append(("event_type", "SEGMENT" if is_edi else "COLUMN",
                            "EQD/DE8249" if is_edi else "raw_event_code",
                            seg_line, raw_event, event_type))

        # ---- 箱型 + 尺寸
        raw_type = rec.get("raw_container_type")
        std_type, type_name, extra, type_sys = _lookup(
            con, "container_type", raw_type, preferred)
        size_ft = None
        if std_type:
            if extra:
                size_ft = json.loads(extra).get("size_ft")
        elif raw_type:
            flags.append("UNMAPPED_CONTAINER_TYPE")
            counts["unmapped"] += 1
        if raw_type:
            lineage.append(("container_type", "SEGMENT" if is_edi else "COLUMN",
                            "EQD/C237.0" if is_edi else "raw_container_type",
                            seg_line, raw_type, std_type))

        # ---- 港口
        raw_port = rec.get("raw_port")
        std_port, port_name, _, port_sys = _lookup(
            con, "port", raw_port, ["UNLOCODE", "INTERNAL"])
        if raw_port and not std_port:
            flags.append("UNMAPPED_PORT")
            counts["unmapped"] += 1
        if raw_port:
            lineage.append(("port_code", "SEGMENT" if is_edi else "COLUMN",
                            "LOC/C517.0" if is_edi else "raw_port",
                            (rec.get("_line_loc") or {}).get("port_line")
                            or (rec.get("_line_loc") or {}).get("pol_line") or seg_line,
                            raw_port, std_port))

        # ---- 船公司
        raw_carrier = rec.get("raw_carrier")
        std_carrier, _, _, carrier_sys = _lookup(
            con, "carrier", raw_carrier, ["SCAC", "INTERNAL"])
        if raw_carrier and not std_carrier:
            flags.append("UNMAPPED_CARRIER")
            counts["unmapped"] += 1
        if raw_carrier:
            lineage.append(("carrier_code", "SEGMENT" if is_edi else "COLUMN",
                            "TDT/NAD" if is_edi else "raw_carrier",
                            seg_line, raw_carrier, std_carrier))

        # ---- 重空箱
        raw_laden = rec.get("raw_laden_code")
        std_laden, _, _, _ = _lookup(con, "laden", raw_laden, preferred)
        if raw_laden and not std_laden:
            flags.append("UNMAPPED_LADEN")
        if raw_laden:
            lineage.append(("laden_status", "SEGMENT" if is_edi else "COLUMN",
                            "EQD/DE8249-laden" if is_edi else "raw_laden_code",
                            seg_line, raw_laden, std_laden))

        # ---- 时间
        event_time = rec.get("event_time")
        if isinstance(event_time, str):
            event_time = datetime.fromisoformat(event_time) if event_time else None
        if not event_time:
            flags.append("MISSING_EVENT_TIME")
        lineage.append(("event_time", "SEGMENT" if is_edi else "COLUMN",
                        "DTM/C507.0" if is_edi else "event_time",
                        rec.get("_line_dtm") or seg_line,
                        rec.get("_raw_event_time"),
                        event_time.strftime("%Y-%m-%d %H:%M:%S") if event_time else None))

        # ---- 船名/IMO/航次（直通字段，同样建血缘）
        for field_name, locator in [("vessel_name", "TDT/C222"),
                                    ("vessel_imo", "TDT/C222-146"),
                                    ("voyage_no", "TDT/RFF-VON")]:
            val = rec.get(field_name)
            if val:
                lineage.append((field_name, "SEGMENT" if is_edi else "COLUMN",
                                locator if is_edi else field_name, seg_line, val, val))
        if rec.get("raw_pod"):
            lineage.append(("pod_raw", "SEGMENT", "LOC/C517.0-POD",
                            (rec.get("_line_loc") or {}).get("pod_line"),
                            rec["raw_pod"], None))

        # 接入阶段记录的解析问题：除通用 PARSE_ISSUE 外，按语义补具体质量标记
        parse_issues = rec.get("_issues", [])
        if parse_issues:
            flags.append("PARSE_ISSUE")
            for issue in parse_issues:
                if "DTM" in issue or "时间" in issue:
                    flags.append("MISSING_EVENT_TIME")
                if "箱号" in issue:
                    flags.append("MISSING_CONTAINER_NO")
                if "LOC" in issue or "港口" in issue:
                    flags.append("UNMAPPED_PORT")
                if "箱型" in issue:
                    flags.append("UNMAPPED_CONTAINER_TYPE")
                if "状态码" in issue or "方向" in issue:
                    flags.append("UNMAPPED_EVENT")

        natural_key = _event_natural_key(
            rec, event_type, container_no, event_time, std_port,
            rec.get("vessel_imo"), rec.get("voyage_no"))
        is_duplicate = natural_key in first_seen
        if is_duplicate:
            flags.append("DUPLICATE_MESSAGE")
            counts["duplicates"] += 1
        else:
            first_seen[natural_key] = raw_id
        # event_id 每行唯一，保证重复报文也各自留痕可追溯
        event_id = "e_" + hashlib.sha256(raw_id.encode()).hexdigest()[:24]

        con.execute(
            """INSERT INTO normalized_event
            (event_id, natural_key, raw_id, file_id, message_type, record_index, segment_line,
             container_no, carrier_code, event_type, event_time, port_code,
             vessel_imo, vessel_name, voyage_no, container_type, size_ft, laden_status,
             is_duplicate, quality_flags, is_mapped, ingested_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [event_id, natural_key, raw_id, file_id, msg_type or source_type,
             rec_idx, seg_line,
             container_no or "__MISSING_CONTAINER__", std_carrier, event_type,
             event_time, std_port,
             rec.get("vessel_imo"), rec.get("vessel_name"), rec.get("voyage_no"),
             std_type, size_ft, std_laden, is_duplicate,
             json.dumps(flags, ensure_ascii=False), event_type != "UNKNOWN", now],
        )
        counts["events"] += 1

        for target_field, kind, locator, line, raw_val, std_val in lineage:
            con.execute(
                """INSERT INTO field_lineage
                (lineage_id, event_id, raw_id, target_field, source_kind,
                 source_locator, source_line, raw_value, std_value)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                [_lineage_id(event_id, target_field), event_id, raw_id, target_field,
                 kind, locator, line,
                 None if raw_val is None else str(raw_val),
                 None if std_val is None else str(std_val)],
            )

    return counts
