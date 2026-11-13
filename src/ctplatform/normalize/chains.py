"""箱动态链构建与对账差异检测。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .normalize import CHAIN_ORDER, PHYSICAL_EVENTS


def _diff_id(*parts):
    return "d_" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:18]


def _add_diff(con, diff_type, severity, container_no, file_id, event_id, desc, detail):
    did = _diff_id(diff_type, container_no, event_id, json.dumps(detail, ensure_ascii=False, default=str))
    con.execute(
        """INSERT OR REPLACE INTO recon_diff
        (diff_id, diff_type, severity, container_no, file_id, event_id,
         description, detail, detected_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        [did, diff_type, severity, container_no, file_id, event_id, desc,
         json.dumps(detail, ensure_ascii=False, default=str), datetime.now()],
    )


def rebuild_chains(con) -> dict:
    """每箱构建一条物理事件链，检测乱序/重复/矛盾/断链。"""
    con.execute("DELETE FROM event_chain")
    rows = con.execute(
        """SELECT event_id, raw_id, file_id, container_no, event_type, event_time,
                  port_code, is_duplicate, quality_flags
           FROM normalized_event
           WHERE event_type IN ('GATE_IN','LOAD','DISCHARGE','GATE_OUT')
           ORDER BY container_no, event_time NULLS LAST, event_id"""
    ).fetchall()

    by_container: dict[str, list] = {}
    for r in rows:
        by_container.setdefault(r[3], []).append(r)

    stats = {"chains": 0, "gap": 0, "out_of_order": 0, "duplicate": 0,
             "contradiction": 0, "incomplete": 0}

    for container_no, evs in by_container.items():
        issues: list[str] = []
        statuses = {"OK"}
        ordered_idx: list[int] = []
        seen_types: dict[str, int] = {}

        # 1) 重复报文：同自然键的重复事件不进链本体（链不重），仅在事件表/对账体现
        unique_evs = [e for e in evs if not e[7]]
        dup_count = sum(1 for e in evs if e[7])
        if dup_count:
            issues.append(f"存在 {dup_count} 条重复报文事件，已按自然键去重，未进入动态链")
            stats["duplicate"] += 1

        # 2) 同类型事件互相矛盾（同箱同类型出现 2 次以上，时间/港口不一致）
        type_groups: dict[str, list] = {}
        for e in unique_evs:
            type_groups.setdefault(e[4], []).append(e)
        for etype, grp in type_groups.items():
            if len(grp) > 1:
                times = {str(g[5]) for g in grp}
                ports = {g[6] for g in grp}
                if len(times) > 1 or len(ports) > 1:
                    stats["contradiction"] += 1
                    statuses.add("CONTRADICTION")
                    desc = f"{etype} 事件存在 {len(grp)} 份互相矛盾的报文"
                    issues.append(desc)
                    for g in grp:
                        _add_diff(con, "CONTRADICTION", "ERROR", container_no, g[2], g[0],
                                  desc, {"event_type": etype,
                                         "times": sorted(times), "ports": sorted(ports)})

        # 3) 链上每类环节只保留最早一条（链不重）；矛盾的其余事件留在事件表+差异表
        chain_evs: list = []
        for etype, grp in type_groups.items():
            grp_sorted = sorted(grp, key=lambda e: (e[5] or datetime.max, e[0]))
            chain_evs.append(grp_sorted[0])

        # 4) 按事件时间串链；判定乱序（物理时间逆序于标准次序）
        sorted_evs = sorted(chain_evs, key=lambda e: (e[5] or datetime.max, e[0]))
        prev_order = -1
        prev_time = None
        for e in sorted_evs:
            etype, etime = e[4], e[5]
            cur_order = CHAIN_ORDER.index(etype)
            if etime is None:
                issues.append(f"{etype} 缺少事件时间，无法定位链上位置，置于链尾")
            elif cur_order < prev_order:
                stats["out_of_order"] += 1
                statuses.add("OUT_OF_ORDER")
                desc = (f"{etype}({etime:%Y-%m-%d %H:%M}) 晚到/乱序："
                        f"标准次序应在 {CHAIN_ORDER[prev_order]} 之前")
                issues.append(desc)
                _add_diff(con, "OUT_OF_ORDER", "WARN", container_no, e[2], e[0],
                          desc, {"event_type": etype, "event_time": str(etime)})
            if prev_time and etime and etime < prev_time:
                stats["out_of_order"] += 1
                statuses.add("OUT_OF_ORDER")
            prev_order = max(prev_order, cur_order)
            prev_time = etime or prev_time
            ordered_idx.append(cur_order)
            seen_types[etype] = seen_types.get(etype, 0) + 1

        # 4) 断链检查：从首个环节到链尾之间缺失即 GAP；首环节前缺失为 INCOMPLETE
        present = [CHAIN_ORDER.index(t) for t in seen_types]
        if present:
            # 内部断裂
            for i in range(min(present), max(present) + 1):
                if i not in present:
                    missing = CHAIN_ORDER[i]
                    stats["gap"] += 1
                    statuses.add("GAP")
                    desc = f"动态链断裂：缺少 {missing} 环节"
                    issues.append(desc)
                    _add_diff(con, "CHAIN_GAP", "ERROR", container_no, None, None,
                              desc, {"missing_event": missing, "gap_kind": "INTERNAL"})
            # 尾部缺失（已发生前序环节却没有后续环节，链未闭合）同样出差异
            for i in range(max(present) + 1, len(CHAIN_ORDER)):
                missing = CHAIN_ORDER[i]
                stats["gap"] += 1
                statuses.add("GAP")
                desc = f"动态链未闭合：缺少后续 {missing} 环节"
                issues.append(desc)
                _add_diff(con, "CHAIN_GAP", "ERROR", container_no, None, None,
                          desc, {"missing_event": missing, "gap_kind": "TAIL"})
            if min(present) != 0 or max(present) != len(CHAIN_ORDER) - 1:
                stats["incomplete"] += 1
                statuses.add("INCOMPLETE")
                missing_ends = [CHAIN_ORDER[i]
                                for i in list(range(0, min(present))) +
                                list(range(max(present) + 1, len(CHAIN_ORDER)))]
                issues.append(f"链不完整，缺少端到端环节: {','.join(missing_ends)}")

        chain_status = "OK"
        for level in ("CONTRADICTION", "GAP", "OUT_OF_ORDER", "INCOMPLETE"):
            if level in statuses:
                chain_status = level
                break
        chain_id = "ch_" + hashlib.sha256(container_no.encode()).hexdigest()[:16]
        con.execute(
            """INSERT INTO event_chain
            (chain_id, container_no, event_count, event_ids, event_types,
             chain_status, issues) VALUES (?,?,?,?,?,?,?)""",
            [chain_id, container_no, len(sorted_evs),
             json.dumps([e[0] for e in sorted_evs]),
             json.dumps([e[4] for e in sorted_evs]),
             chain_status, json.dumps(issues, ensure_ascii=False)],
        )
        stats["chains"] += 1
    return stats


def rebuild_reconciliation(con) -> dict:
    """除链上差异外的对账：未映射代码、解析错误、船图与实物不一致。"""
    # 未映射代码 / 缺字段（来自 normalized_event.quality_flags）
    flag_rows = con.execute(
        "SELECT event_id, file_id, container_no, quality_flags FROM normalized_event"
    ).fetchall()
    for event_id, file_id, container_no, flags_json in flag_rows:
        for flag in json.loads(flags_json):
            if flag.startswith("UNMAPPED") or flag in ("MISSING_EVENT_TIME",
                                                       "MISSING_CONTAINER_NO", "PARSE_ISSUE"):
                _add_diff(con, flag, "WARN", container_no, file_id, event_id,
                          f"事件 {event_id[:10]}.. 存在质量问题 {flag}",
                          {"flag": flag})

    # 报文文件级解析错误（缺段、截断等）
    files = con.execute(
        "SELECT file_id, filename, parse_status, parse_errors FROM source_file"
    ).fetchall()
    for file_id, filename, status, errors_json in files:
        for err in json.loads(errors_json or "[]"):
            _add_diff(con, "PARSE_ERROR", "ERROR", None, file_id, None,
                      f"文件 {filename} 解析问题: {err}", {"error": err})

    # 船图（BAPLIE 计划配载）与实际装卸（COARRI）对账
    plan = con.execute(
        """SELECT container_no, vessel_imo, voyage_no
           FROM normalized_event WHERE event_type='PLAN_STOW'"""
    ).fetchall()
    actual_loads = con.execute(
        """SELECT DISTINCT container_no, vessel_imo, voyage_no
           FROM normalized_event WHERE event_type='LOAD'"""
    ).fetchall()
    actual_set = {(c, v, vo) for c, v, vo in actual_loads}
    plan_set = {(c, v, vo) for c, v, vo in plan}

    for c, v, vo in plan_set - actual_set:
        _add_diff(con, "MANIFEST_NO_ACTUAL", "WARN", c, None, None,
                  f"箱 {c} 在 BAPLIE 船图中配载，但无对应实际装船(COARRI)记录",
                  {"vessel_imo": v, "voyage_no": vo})
    for c, v, vo in actual_set - plan_set:
        _add_diff(con, "ACTUAL_NO_MANIFEST", "WARN", c, None, None,
                  f"箱 {c} 有实际装船(COARRI)记录，但 BAPLIE 船图中无配载",
                  {"vessel_imo": v, "voyage_no": vo})
    return {"diffs": con.execute("SELECT count(*) FROM recon_diff").fetchone()[0]}
