"""统一代码表与默认映射模板的种子数据。

每个 domain 维护多套来源体系（source_system）到 GLOBAL 标准码的映射，
报文里的原始码 + 来源体系共同决定标准码；未命中即标记 UNMAPPED_* 并出对账差异。
"""
from __future__ import annotations

import hashlib
from datetime import datetime

# domain, source_system, raw_code, std_code, std_name, extra
CODE_SEED = [
    # ---------------- 箱型（含尺寸，用于 TEU 统计） ----------------
    ("container_type", "ISO",   "22G1", "20GP", "20英尺通用干货箱", '{"size_ft": 20}'),
    ("container_type", "ISO",   "2201", "20GP", "20英尺通用干货箱", '{"size_ft": 20}'),
    ("container_type", "ISO",   "45G1", "40HC", "40英尺高箱",       '{"size_ft": 40}'),
    ("container_type", "ISO",   "4501", "40HC", "40英尺高箱",       '{"size_ft": 40}'),
    ("container_type", "ISO",   "42G1", "40GP", "40英尺通用干货箱", '{"size_ft": 40}'),
    ("container_type", "CARRIER_A", "20DV", "20GP", "20英尺干货箱", '{"size_ft": 20}'),
    ("container_type", "CARRIER_A", "40HQ", "40HC", "40英尺高箱",   '{"size_ft": 40}'),
    ("container_type", "CARRIER_B", "G20",  "20GP", "20GP",         '{"size_ft": 20}'),
    ("container_type", "CARRIER_B", "H40",  "40HC", "40HC",         '{"size_ft": 40}'),

    # ---------------- 港口（UN/LOCODE 与内部码并存） ----------------
    ("port", "UNLOCODE", "CNSHA", "CNSHA", "上海港", None),
    ("port", "UNLOCODE", "CNNGB", "CNNGB", "宁波舟山港", None),
    ("port", "UNLOCODE", "CNQIN", "CNQIN", "青岛港", None),
    ("port", "INTERNAL", "SHA",   "CNSHA", "上海港", None),
    ("port", "INTERNAL", "NB",    "CNNGB", "宁波舟山港", None),
    ("port", "INTERNAL", "QIN",   "CNQIN", "青岛港", None),

    # ---------------- 船公司（SCAC 风格码 / 内部码） ----------------
    ("carrier", "SCAC",     "COSU", "COSU", "中远海运", None),
    ("carrier", "SCAC",     "EGLV", "EGLV", "长荣海运", None),
    ("carrier", "SCAC",     "MSCU", "MSCU", "地中海航运", None),
    ("carrier", "INTERNAL", "COSCO", "COSU", "中远海运", None),
    ("carrier", "INTERNAL", "EVER",  "EGLV", "长荣海运", None),
    ("carrier", "INTERNAL", "MSC",   "MSCU", "地中海航运", None),

    # ---------------- 事件码归一 ----------------
    # EDIFACT 报文类型/限定符
    ("event_code", "EDIFACT", "BAPLIE",          "PLAN_STOW", "船图配载（计划）", None),
    ("event_code", "EDIFACT", "COARRI_LOAD",     "LOAD",      "装船", None),
    ("event_code", "EDIFACT", "COARRI_DISCHARGE","DISCHARGE", "卸船", None),
    # CODECO DE8249 状态码
    ("event_code", "EDIFACT", "GIN",  "GATE_IN",  "进场（重箱进闸）", None),
    ("event_code", "EDIFACT", "EI",   "GATE_IN",  "进闸", None),
    ("event_code", "EDIFACT", "1",    "GATE_IN",  "进闸", None),
    ("event_code", "EDIFACT", "GOUT", "GATE_OUT", "出场（提箱出闸）", None),
    ("event_code", "EDIFACT", "AE",   "GATE_OUT", "出闸", None),
    ("event_code", "EDIFACT", "2",    "GATE_OUT", "出闸", None),

    # ---------------- 重/空箱状态 ----------------
    ("laden", "EDIFACT", "FULL", "FULL", "重箱", None),
    ("laden", "EDIFACT", "F",    "FULL", "重箱", None),
    ("laden", "EDIFACT", "EMPTY","EMPTY", "空箱", None),
    ("laden", "EDIFACT", "E",    "EMPTY", "空箱", None),
    ("laden", "EDIFACT", "5",    "FULL", "重箱", None),
    ("laden", "EDIFACT", "4",    "EMPTY", "空箱", None),
]

# 表格模板里的事件词 -> 标准事件（domain=event_code, source_system=TABULAR）
TABULAR_EVENT_CODES = [
    ("GATE_IN", "GATE_IN", "进场"), ("GATEIN", "GATE_IN", "进场"),
    ("IN", "GATE_IN", "进场"), ("进场", "GATE_IN", "进场"),
    ("GATE_OUT", "GATE_OUT", "出场"), ("GATEOUT", "GATE_OUT", "出场"),
    ("OUT", "GATE_OUT", "出场"), ("出场", "GATE_OUT", "出场"),
    ("LOAD", "LOAD", "装船"), ("装船", "LOAD", "装船"),
    ("DISCHARGE", "DISCHARGE", "卸船"), ("DISCH", "DISCHARGE", "卸船"),
    ("卸船", "DISCHARGE", "卸船"),
]

DEFAULT_TEMPLATES = [
    {
        "template_id": "tpl_gate_csv",
        "template_name": "闸口 CSV 标准模板",
        "source_type": "CSV",
        "fields": [
            ("箱号", "container_no"), ("船公司", "raw_carrier"),
            ("箱型", "raw_container_type"), ("事件", "raw_event_code"),
            ("时间", "event_time"), ("港口", "raw_port"),
            ("船名", "vessel_name"), ("航次", "voyage_no"),
            ("IMO", "vessel_imo"), ("箱态", "raw_laden_code"),
        ],
    },
    {
        "template_id": "tpl_ops_excel",
        "template_name": "作业 Excel 标准模板",
        "source_type": "EXCEL",
        "fields": [
            ("Container", "container_no"), ("Carrier", "raw_carrier"),
            ("Type", "raw_container_type"), ("Activity", "raw_event_code"),
            ("Timestamp", "event_time"), ("Port", "raw_port"),
            ("Vessel", "vessel_name"), ("Voyage", "voyage_no"),
            ("IMO", "vessel_imo"), ("Laden", "raw_laden_code"),
        ],
    },
]


def _code_id(domain, system, raw):
    return "c_" + hashlib.sha256(f"{domain}|{system}|{raw}".encode()).hexdigest()[:16]


def seed_code_tables(con) -> int:
    now = datetime.now()
    rows = list(CODE_SEED)
    for raw, std, name in TABULAR_EVENT_CODES:
        rows.append(("event_code", "TABULAR", raw, std, name, None))
    inserted = 0
    for domain, system, raw_code, std_code, std_name, extra in rows:
        cid = _code_id(domain, system, raw_code)
        exists = con.execute("SELECT 1 FROM code_table WHERE code_id=?", [cid]).fetchone()
        if exists:
            continue
        con.execute(
            """INSERT INTO code_table
            (code_id, domain, source_system, raw_code, std_code, std_name, extra, updated_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            [cid, domain, system, raw_code, std_code, std_name, extra, now],
        )
        inserted += 1
    return inserted


def seed_templates(con) -> int:
    now = datetime.now()
    inserted = 0
    for tpl in DEFAULT_TEMPLATES:
        if con.execute("SELECT 1 FROM mapping_template WHERE template_id=?",
                       [tpl["template_id"]]).fetchone():
            continue
        con.execute(
            """INSERT INTO mapping_template
            (template_id, template_name, source_type, header_row, sheet_name, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?)""",
            [tpl["template_id"], tpl["template_name"], tpl["source_type"], 1,
             "Sheet1" if tpl["source_type"] == "EXCEL" else None, now, now],
        )
        for col, target in tpl["fields"]:
            mid = "m_" + hashlib.sha256(
                f"{tpl['template_id']}|{col}|{target}".encode()).hexdigest()[:16]
            con.execute(
                """INSERT INTO mapping_field
                (mapping_id, template_id, source_column, target_field, transform, created_at)
                VALUES (?,?,?,?,?,?)""",
                [mid, tpl["template_id"], col, target, None, now],
            )
        inserted += 1
    return inserted


def seed_all(con) -> dict:
    return {"codes": seed_code_tables(con), "templates": seed_templates(con)}
