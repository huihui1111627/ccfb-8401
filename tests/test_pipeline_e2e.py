"""端到端：造数 -> 接入 -> 流水线 -> 全量质量断言。"""
import json

import pytest

from ctplatform.normalize.normalize import CHAIN_ORDER


def _scalars(con, sql, params=None):
    return [r[0] for r in con.execute(sql, params or []).fetchall()]


def _flags_of(con, container_no):
    """展开某只箱所有事件的 quality_flags JSON 数组。"""
    rows = con.execute(
        "SELECT quality_flags FROM normalized_event WHERE container_no=?",
        [container_no]).fetchall()
    out = set()
    for (flags_json,) in rows:
        out.update(json.loads(flags_json))
    return out


def test_ingest_summary_and_file_dedup(loaded, con):
    con, result, summaries = loaded
    statuses = [s["status"] for s in summaries]
    assert all(s in ("INGESTED", "DUPLICATE_FILE") for s in statuses)
    # 重复接入同一份文件应被 SHA256 拦截
    from ctplatform.ingest import ingest_file
    from ctplatform.testdata import build_dataset
    name, data = next(iter(build_dataset().items()))
    again = ingest_file(name, data, con=con)
    assert again["status"] == "DUPLICATE_FILE"


def test_code_normalization(loaded, con):
    # 多体系原始码都归一到标准码
    row = con.execute(
        "SELECT container_type, port_code, carrier_code, event_type, size_ft "
        "FROM normalized_event WHERE container_no='EGLV1000001' "
        "AND event_type='GATE_IN' AND NOT is_duplicate LIMIT 1").fetchone()
    assert row is not None
    ctype, port, carrier, etype, size = row
    assert etype == "GATE_IN"
    assert port == "CNSHA"
    assert size == 20
    # CSV 里 EVER/20DV/SHA 也走同一套标准码
    csv_ev = con.execute(
        """SELECT ne.container_type, ne.port_code, ne.carrier_code
           FROM normalized_event ne JOIN source_file sf ON ne.file_id=sf.file_id
           WHERE sf.source_type='CSV' AND ne.container_no='EGLV1000002'
           AND ne.event_type='LOAD' LIMIT 1""").fetchone()
    assert csv_ev == ("20GP", "CNSHA", "COSU")


def test_duplicate_events_dedup(loaded, con):
    # C2 进场：文件内重复 + 跨文件重复，共 3 份原始记录，归一化后只留 1 条非重复
    total = con.execute(
        "SELECT count(*) FROM normalized_event WHERE container_no='EGLV1000002' "
        "AND event_type='GATE_IN'").fetchone()[0]
    unique = con.execute(
        "SELECT count(*) FROM normalized_event WHERE container_no='EGLV1000002' "
        "AND event_type='GATE_IN' AND NOT is_duplicate").fetchone()[0]
    assert total == 3 and unique == 1
    # C1 的 CSV 进场与 EDIFACT 进场自然键相同（同箱同型同分钟同港）-> 去重
    c1_gi = con.execute(
        "SELECT count(*) FROM normalized_event WHERE container_no='EGLV1000001' "
        "AND event_type='GATE_IN'").fetchone()[0]
    c1_gi_uniq = con.execute(
        "SELECT count(*) FROM normalized_event WHERE container_no='EGLV1000001' "
        "AND event_type='GATE_IN' AND NOT is_duplicate").fetchone()[0]
    assert c1_gi == 2 and c1_gi_uniq == 1


def test_out_of_order_detected(loaded, con):
    # C3 卸船时间 09:00 早于装船 10:10
    chain = con.execute(
        "SELECT chain_status, issues FROM event_chain WHERE container_no='EGLV1000003'"
    ).fetchone()
    assert chain[0] == "OUT_OF_ORDER"
    assert any("乱序" in i for i in json.loads(chain[1]))
    diff = con.execute(
        "SELECT count(*) FROM recon_diff WHERE container_no='EGLV1000003' "
        "AND diff_type='OUT_OF_ORDER'").fetchone()[0]
    assert diff >= 1


def test_contradiction_detected(loaded, con):
    # C4 两份装船，时间与港口都不同
    chain = con.execute(
        "SELECT chain_status FROM event_chain WHERE container_no='EGLV1000004'").fetchone()
    assert chain[0] == "CONTRADICTION"
    diff = con.execute(
        "SELECT detail FROM recon_diff WHERE container_no='EGLV1000004' "
        "AND diff_type='CONTRADICTION'").fetchone()
    assert diff is not None
    assert "CNNGB" in json.loads(diff[0])["ports"]


def test_bad_codes_and_missing_segments(loaded, con):
    # C5 未知箱型 -> UNMAPPED_CONTAINER_TYPE；缺 DTM -> MISSING_EVENT_TIME
    flags = _flags_of(con, "EGLV1000005")
    assert "UNMAPPED_CONTAINER_TYPE" in flags
    assert "MISSING_EVENT_TIME" in flags
    # 缺箱号残组仍被留痕（不静默丢弃）
    missing_no = con.execute(
        "SELECT count(*) FROM normalized_event WHERE quality_flags LIKE '%MISSING_CONTAINER_NO%'"
    ).fetchone()[0]
    assert missing_no >= 1
    # 截断报文产生文件级 PARSE_ERROR 差异
    parse_err = con.execute(
        "SELECT count(*) FROM recon_diff WHERE diff_type='PARSE_ERROR'").fetchone()[0]
    assert parse_err >= 1


def test_chain_gap_and_incomplete(loaded, con):
    # C6 只有 GATE_IN + LOAD -> GAP（缺 DISCHARGE/...）+ INCOMPLETE
    chain = con.execute(
        "SELECT chain_status, issues FROM event_chain WHERE container_no='EGLV1000006'"
    ).fetchone()
    assert chain[0] in ("GAP", "INCOMPLETE")
    issues = json.loads(chain[1])
    assert any("DISCHARGE" in i for i in issues)
    gap_diff = con.execute(
        "SELECT count(*) FROM recon_diff WHERE container_no='EGLV1000006' "
        "AND diff_type='CHAIN_GAP'").fetchone()[0]
    assert gap_diff >= 1


def test_unmapped_port_and_carrier(loaded, con):
    flags = _flags_of(con, "EGLV1000007")
    assert "UNMAPPED_PORT" in flags
    # CSV 里 UNKNOWNCO 船公司
    assert con.execute(
        "SELECT count(*) FROM recon_diff WHERE diff_type='UNMAPPED_CARRIER'").fetchone()[0] >= 1


def test_manifest_vs_actual(loaded, con):
    # C8 BAPLIE 有配载，无 COARRI 装船
    diff = con.execute(
        "SELECT count(*) FROM recon_diff WHERE container_no='EGLV1000008' "
        "AND diff_type='MANIFEST_NO_ACTUAL'").fetchone()[0]
    assert diff == 1
    plan = con.execute(
        "SELECT count(*) FROM normalized_event WHERE container_no='EGLV1000008' "
        "AND event_type='PLAN_STOW'").fetchone()[0]
    assert plan == 1


def test_chain_integrity_no_gap_no_dup_for_clean_container(loaded, con):
    """C1 是干净全链：链上事件严格按标准次序、无重复、状态 OK。"""
    row = con.execute(
        "SELECT event_types, chain_status, event_count FROM event_chain "
        "WHERE container_no='EGLV1000001'").fetchone()
    types = json.loads(row[0])
    assert row[1] == "OK"
    assert types == CHAIN_ORDER            # 不断：四个环节齐全且有序
    assert len(types) == len(set(types))   # 不重：链上无重复环节
    assert row[2] == 4


def test_every_chain_has_no_duplicate_steps(loaded, con):
    """硬性要求：每只箱的动态链上，同一物理事件类型最多出现一次（矛盾另表标记）。"""
    for container, types_json in con.execute(
            "SELECT container_no, event_types FROM event_chain").fetchall():
        types = json.loads(types_json)
        assert len(types) == len(set(types)), f"{container} 链上出现重复环节: {types}"


def test_every_chain_event_traceable(loaded, con):
    """链上每个事件都必须能追到来源文件与行号（血缘完整性）。"""
    missing = con.execute(
        """SELECT count(*) FROM normalized_event ne
           WHERE ne.event_type IN ('GATE_IN','LOAD','DISCHARGE','GATE_OUT')
           AND NOT EXISTS (
               SELECT 1 FROM field_lineage fl
               WHERE fl.event_id=ne.event_id AND fl.source_line IS NOT NULL)"""
    ).fetchone()[0]
    assert missing == 0


def test_field_level_lineage(loaded, con):
    row = con.execute(
        """SELECT fl.target_field, fl.source_kind, fl.source_locator, fl.source_line,
                  fl.raw_value, fl.std_value, sf.filename
           FROM field_lineage fl
           JOIN raw_record rr ON fl.raw_id=rr.raw_id
           JOIN source_file sf ON rr.file_id=sf.file_id
           JOIN normalized_event ne ON ne.event_id=fl.event_id
           WHERE ne.container_no='EGLV1000001' AND fl.target_field='port_code'
           LIMIT 1""").fetchone()
    assert row is not None
    field, kind, locator, line, raw_value, std_value, filename = row
    assert kind in ("SEGMENT", "COLUMN")
    assert isinstance(line, int) and line > 0
    assert raw_value and std_value == "CNSHA"


def test_voyage_stats(loaded, con):
    # 上海港装船：C1,C2,C3,C4(矛盾去重计1),C5,C6,C7 + Excel C10 = 8 个箱；
    # 缺箱号残组（占位符）不计入统计
    shanghai = con.execute(
        "SELECT load_count, load_teu FROM voyage_stats WHERE port_code='CNSHA'").fetchone()
    assert shanghai is not None
    assert shanghai[0] == 8
    assert shanghai[1] > 0
    # 宁波卸船：C1,C2,C3,C4,C5,C7 = 6（C10 无卸船记录）
    ningbo = con.execute(
        "SELECT discharge_count FROM voyage_stats WHERE port_code='CNNGB'").fetchone()
    assert ningbo[0] == 6


def test_pipeline_idempotent(loaded, con):
    """重跑流水线结果必须一致（全量重算幂等）。"""
    from ctplatform.normalize import run_pipeline
    before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("normalized_event", "field_lineage", "event_chain",
                        "recon_diff", "voyage_stats")}
    run_pipeline(con)
    after = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
             for t in before}
    assert before == after


def test_excel_ingestion(loaded, con):
    rows = con.execute(
        """SELECT event_type FROM normalized_event ne
           JOIN source_file sf ON ne.file_id=sf.file_id
           WHERE sf.source_type='EXCEL' AND ne.container_no='EGLV1000010'
           ORDER BY event_time""").fetchall()
    assert [r[0] for r in rows] == ["GATE_IN", "LOAD"]
