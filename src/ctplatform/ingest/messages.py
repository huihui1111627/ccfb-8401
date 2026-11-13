"""BAPLIE / COARRI / CODECO 报文业务解析。

解析器输出"原始记录"（raw record）：只抽取、不做标准代码转换，
代码归一化在 normalize 阶段完成，保证原始值与血缘完整可回溯。

支持的报文子集（造数器与解析器严格一致，均为标准 EDIFACT D.95B 常见段式）：
  头段：UNH / TDT（船名、IMO、航次、承运人）/ RFF+VON / NAD+MS /
        DTM（报文时间）/ LOC（COARRI 装卸港、CODECO 码头）
  箱组：EQD+CN（箱号、箱型、状态码）/ LOC（POL/POD/码头）/
        DTM（实际事件时间）/ RFF / NAD
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .edifact import Interchange, Message, Segment
from .records import ParsedRecord

SUPPORTED_TYPES = {"BAPLIE", "COARRI", "CODECO"}


@dataclass
class ParsedMessage:
    message_type: str
    records: list[ParsedRecord] = field(default_factory=list)
    header: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)      # 报文级错误（截断等）
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 通用工具

def _seg(segments: list[Segment], tag: str) -> Segment | None:
    for s in segments:
        if s.tag == tag:
            return s
    return None


def _all(segments: list[Segment], tag: str) -> list[Segment]:
    return [s for s in segments if s.tag == tag]


def parse_dt(raw: str | None, qualifier: str | None) -> tuple[datetime | None, str | None]:
    """返回 (datetime, 错误信息)。支持 303/102/203 等常见格式。"""
    if not raw:
        return None, None
    raw = raw.strip()
    fmts = {
        "303": ["%y%m%d%H%M", "%y%m%d%H%M00", "%y%m%d"],
        "102": ["%Y%m%d"],
        "203": ["%Y%m%d%H%M", "%Y%m%d%H%M%S", "%Y%m%d"],
    }
    candidates = fmts.get(qualifier or "", []) + ["%Y%m%d%H%M", "%y%m%d%H%M", "%Y%m%d", "%y%m%d"]
    for fmt in dict.fromkeys(candidates):
        try:
            return datetime.strptime(raw, fmt), None
        except ValueError:
            continue
    return None, f"无法解析的时间值 '{raw}'（限定符 {qualifier}）"


def _extract_vessel(header_segments: list[Segment]) -> dict:
    """从 TDT 抽船名/IMO/航次/承运人；RFF+VON 作为航次兜底。"""
    out = {"vessel_name": None, "vessel_imo": None, "voyage_no": None, "raw_carrier": None}
    tdt = _seg(header_segments, "TDT")
    if tdt is not None:
        out["voyage_no"] = tdt.element(2)          # TDT 第2业务元素：航程号
        # builder 约定：船名元素 = [名称, '103', IMO, '146']；承运人 = [代码, '172']
        for elem in tdt.elements[2:]:
            if len(elem) >= 4 and elem[3] == "146":      # IMO 编号限定符
                out["vessel_imo"] = elem[2]
            if len(elem) >= 2 and elem[1] == "103":      # 船名
                out["vessel_name"] = elem[0]
            if len(elem) >= 2 and elem[1] == "172":      # 承运人代码
                out["raw_carrier"] = elem[0]
    nad = _seg(header_segments, "NAD")
    if nad is not None and nad.element(2) and not out["raw_carrier"]:
        out["raw_carrier"] = nad.elements[2][0]
    for rff in _all(header_segments, "RFF"):
        e1 = rff.element(2, default="") or ""
        if e1.startswith("VON:") and not out["voyage_no"]:
            out["voyage_no"] = e1.split(":", 1)[1]
    return out


def _header_time(header_segments: list[Segment]) -> tuple[datetime | None, list[str]]:
    issues: list[str] = []
    for dtm in _all(header_segments, "DTM"):
        # C507: [2005限定符, 2380时间值, 2379格式]
        e1 = dtm.elements[1] if len(dtm.elements) > 1 else []
        val = e1[1] if len(e1) > 1 else None
        fmt = e1[2] if len(e1) > 2 else None
        ts, err = parse_dt(val, fmt)
        if ts:
            return ts, issues
        if err:
            issues.append(f"头段 DTM(行{dtm.line_no}) {err}")
    return None, issues


def _split_groups(message: Message) -> tuple[list[Segment], list[tuple[Segment, list[Segment]]]]:
    """按 EQD+CN 切箱组，返回 (头段列表, [(EQD段, 组内其他段)...])。"""
    header: list[Segment] = []
    groups: list[tuple[Segment, list[Segment]]] = []
    cur_eqd: Segment | None = None
    cur_body: list[Segment] = []
    for seg in message.segments:
        if seg.tag in ("UNH", "UNT", "UNB", "UNZ", "UNA"):
            header.append(seg)
            continue
        # 标准 EQD+CN+箱号... CN 限定词在 element(1)；残组可能缺箱号
        if seg.tag == "EQD" and seg.element(1) == "CN":
            if cur_eqd is not None:
                groups.append((cur_eqd, cur_body))
            cur_eqd, cur_body = seg, []
            continue
        if cur_eqd is None:
            header.append(seg)
        else:
            cur_body.append(seg)
    if cur_eqd is not None:
        groups.append((cur_eqd, cur_body))
    return header, groups


def _group_loc(body: list[Segment], qualifiers: set[str]) -> tuple[str | None, int | None]:
    for loc in _all(body, "LOC"):
        if loc.element(1) in qualifiers and loc.element(2):
            return loc.elements[2][0], loc.line_no
    return None, None


def _group_time(body: list[Segment], qualifiers: set[str]) -> tuple[datetime | None, str | None, int | None, list[str]]:
    """返回 (时间, 原始时间串, DTM行号, 问题)。限定符优先匹配，否则取第一个 DTM。"""
    issues: list[str] = []
    fallback: Segment | None = None
    for dtm in _all(body, "DTM"):
        e1 = dtm.elements[1] if len(dtm.elements) > 1 else []
        q2005 = e1[0] if e1 else None          # 业务限定符（200/132/133...）
        val = e1[1] if len(e1) > 1 else None
        fmt = e1[2] if len(e1) > 2 else None   # 格式限定符（303/102/203）
        if q2005 in qualifiers:
            if not val:
                issues.append(f"DTM(行{dtm.line_no}) 限定符 {q2005} 缺少时间值")
                return None, None, dtm.line_no, issues
            ts, err = parse_dt(val, fmt)
            if err:
                issues.append(f"DTM(行{dtm.line_no}) {err}")
            return ts, val, dtm.line_no, issues
        fallback = fallback or dtm
    if fallback is not None:
        e1 = fallback.elements[1] if len(fallback.elements) > 1 else []
        val = e1[1] if len(e1) > 1 else None
        fmt = e1[2] if len(e1) > 2 else None
        ts, err = parse_dt(val, fmt)
        if err:
            issues.append(f"DTM(行{fallback.line_no}) {err}")
        return ts, val, fallback.line_no, issues
    return None, None, None, issues


# ---------------------------------------------------------------- 三种报文

def _parse_baplie(message: Message) -> ParsedMessage:
    pm = ParsedMessage("BAPLIE")
    header_seg, groups = _split_groups(message)
    pm.header.update(_extract_vessel(header_seg))
    hdr_time, hdr_issues = _header_time(header_seg)
    pm.header["header_time"] = hdr_time
    pm.warnings.extend(hdr_issues)

    for idx, (eqd, body) in enumerate(groups):
        issues: list[str] = []
        container_no = eqd.element(2)
        if not container_no:
            issues.append(f"EQD(行{eqd.line_no}) 缺少箱号")
        rec = {
            "container_no": container_no,
            "raw_container_type": eqd.element(3),
            "raw_laden_code": eqd.element(5),
            "raw_carrier": pm.header.get("raw_carrier"),
            "vessel_name": pm.header.get("vessel_name"),
            "vessel_imo": pm.header.get("vessel_imo"),
            "voyage_no": pm.header.get("voyage_no"),
            "raw_event_code": "BAPLIE",
        }
        pol, pol_line = _group_loc(body, {"147", "7"})
        pod, pod_line = _group_loc(body, {"9", "11"})
        rec["raw_port"] = pol
        rec["raw_pod"] = pod
        rec["_line_loc"] = {"pol_line": pol_line, "pod_line": pod_line}
        ts, ts_raw, dtm_line, t_issues = _group_time(body, {"200", "11", "137"})
        issues.extend(t_issues)
        if ts is None:
            ts = hdr_time
            if hdr_time is None:
                issues.append(f"箱 {container_no or '?'} 缺少 DTM 事件时间段")
        rec["event_time"] = ts
        rec["_line_dtm"] = dtm_line
        rec["_raw_event_time"] = ts_raw
        if not rec["raw_container_type"]:
            issues.append(f"箱 {container_no or '?'} EQD 缺少箱型代码")
        pm.records.append(ParsedRecord(idx, eqd.line_no, rec, issues))
    return pm


def _parse_coarri(message: Message) -> ParsedMessage:
    pm = ParsedMessage("COARRI")
    header_seg, groups = _split_groups(message)
    pm.header.update(_extract_vessel(header_seg))
    hdr_time, hdr_issues = _header_time(header_seg)
    pm.header["header_time"] = hdr_time
    pm.warnings.extend(hdr_issues)

    hdr_loc = _seg(header_seg, "LOC")
    direction_qual = hdr_loc.element(1) if hdr_loc else None
    if direction_qual == "11":
        direction, raw_event_code = "LOAD", "COARRI_LOAD"
    elif direction_qual == "9":
        direction, raw_event_code = "DISCHARGE", "COARRI_DISCHARGE"
    else:
        direction, raw_event_code = None, None
        pm.warnings.append("头段缺少 LOC+11/9 装卸港，装卸方向需从箱组 LOC 推断")
    hdr_port = hdr_loc.element(2) if hdr_loc else None

    for idx, (eqd, body) in enumerate(groups):
        issues: list[str] = []
        container_no = eqd.element(2)
        if not container_no:
            issues.append(f"EQD(行{eqd.line_no}) 缺少箱号")
        rec = {
            "container_no": container_no,
            "raw_container_type": eqd.element(3),
            "raw_laden_code": eqd.element(5),
            "raw_carrier": pm.header.get("raw_carrier"),
            "vessel_name": pm.header.get("vessel_name"),
            "vessel_imo": pm.header.get("vessel_imo"),
            "voyage_no": pm.header.get("voyage_no"),
        }
        port, port_line = _group_loc(body, {"147", "7", "9", "11"})
        loc_qual = None
        for loc in _all(body, "LOC"):
            if loc.element(2):
                loc_qual = loc.element(1)
                break
        if port is None:
            port, port_line = hdr_port, (hdr_loc.line_no if hdr_loc else None)
            if port is None:
                issues.append(f"箱 {container_no or '?'} 缺少 LOC 港口段")
        rec["raw_port"] = port
        rec["_line_loc"] = {"port_line": port_line}

        if direction is None:
            if loc_qual == "11":
                rec["raw_event_code"] = "COARRI_LOAD"
            elif loc_qual == "9":
                rec["raw_event_code"] = "COARRI_DISCHARGE"
            else:
                rec["raw_event_code"] = None
                issues.append(f"箱 {container_no or '?'} 无法判定装/卸方向")
        else:
            rec["raw_event_code"] = raw_event_code

        ts, ts_raw, dtm_line, t_issues = _group_time(body, {"200", "11", "132", "133"})
        issues.extend(t_issues)
        if ts is None:
            ts = hdr_time
            if hdr_time is None:
                issues.append(f"箱 {container_no or '?'} 缺少 DTM 实际作业时间段")
        rec["event_time"] = ts
        rec["_line_dtm"] = dtm_line
        rec["_raw_event_time"] = ts_raw
        if not rec["raw_container_type"]:
            issues.append(f"箱 {container_no or '?'} EQD 缺少箱型代码")
        pm.records.append(ParsedRecord(idx, eqd.line_no, rec, issues))
    return pm


def _parse_codeco(message: Message) -> ParsedMessage:
    pm = ParsedMessage("CODECO")
    header_seg, groups = _split_groups(message)
    pm.header.update(_extract_vessel(header_seg))
    hdr_time, hdr_issues = _header_time(header_seg)
    pm.header["header_time"] = hdr_time
    pm.warnings.extend(hdr_issues)

    hdr_loc = _seg(header_seg, "LOC")
    hdr_port = hdr_loc.element(2) if hdr_loc else None

    for idx, (eqd, body) in enumerate(groups):
        issues: list[str] = []
        container_no = eqd.element(2)
        if not container_no:
            issues.append(f"EQD(行{eqd.line_no}) 缺少箱号")
        rec = {
            "container_no": container_no,
            "raw_container_type": eqd.element(3),
            "raw_laden_code": None,
            "raw_carrier": pm.header.get("raw_carrier"),
            "vessel_name": pm.header.get("vessel_name"),
            "vessel_imo": pm.header.get("vessel_imo"),
            "voyage_no": pm.header.get("voyage_no"),
            "raw_event_code": eqd.element(5),       # GIN / GOUT / EI / AE ...
        }
        if not rec["raw_event_code"]:
            issues.append(f"箱 {container_no or '?'} EQD 缺少进出闸状态码(DE8249)")
        port, port_line = _group_loc(body, {"147", "162", "7"})
        if port is None:
            port, port_line = hdr_port, (hdr_loc.line_no if hdr_loc else None)
            if port is None:
                issues.append(f"箱 {container_no or '?'} 缺少 LOC 港口/码头段")
        rec["raw_port"] = port
        rec["_line_loc"] = {"port_line": port_line}
        ts, ts_raw, dtm_line, t_issues = _group_time(body, {"132", "133", "200", "11"})
        issues.extend(t_issues)
        if ts is None:
            ts = hdr_time
            if hdr_time is None:
                issues.append(f"箱 {container_no or '?'} 缺少 DTM 进出闸时间段")
        rec["event_time"] = ts
        rec["_line_dtm"] = dtm_line
        rec["_raw_event_time"] = ts_raw
        if not rec["raw_container_type"]:
            issues.append(f"箱 {container_no or '?'} EQD 缺少箱型代码")
        pm.records.append(ParsedRecord(idx, eqd.line_no, rec, issues))
    return pm


_PARSERS = {"BAPLIE": _parse_baplie, "COARRI": _parse_coarri, "CODECO": _parse_codeco}


def parse_messages(ic: Interchange) -> list[ParsedMessage]:
    """把交换包里的每份 UNH 报文解析成 ParsedMessage。"""
    result: list[ParsedMessage] = []
    for msg in ic.messages:
        mtype = msg.message_type
        if mtype not in SUPPORTED_TYPES:
            pm = ParsedMessage(mtype or "UNKNOWN")
            pm.errors.append(f"不支持的报文类型: {mtype}")
            result.append(pm)
            continue
        pm = _PARSERS[mtype](msg)
        pm.errors.extend(ic.errors)
        result.append(pm)
    if not ic.messages and ic.errors:
        pm = ParsedMessage("UNKNOWN")
        pm.errors.extend(ic.errors)
        result.append(pm)
    return result
