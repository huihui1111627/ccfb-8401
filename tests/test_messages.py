"""三种报文业务解析与脏数据捕获。"""
from ctplatform.ingest.edifact import parse_interchange
from ctplatform.ingest.messages import parse_messages
from ctplatform.testdata import build_dataset


def _parse(content: str):
    return parse_messages(parse_interchange(content))


def test_baplie_structure():
    files = build_dataset()
    pms = _parse(files["BAPLIE_20261109.edi"].decode())
    assert len(pms) == 1 and pms[0].message_type == "BAPLIE"
    rec = pms[0].records[0]
    assert rec.data["container_no"] == "EGLV1000001"
    assert rec.data["raw_container_type"] == "22G1"
    assert rec.data["vessel_imo"] == "9472916"
    assert rec.data["voyage_no"] == "V-0521E"
    assert rec.data["raw_port"] == "CNSHA"
    assert rec.data["raw_pod"] == "CNNGB"
    assert rec.data["event_time"] is not None


def test_coarri_load_and_discharge_direction():
    files = build_dataset()
    load = _parse(files["COARRI_LOAD_A.edi"].decode())[0]
    assert all(r.data["raw_event_code"] == "COARRI_LOAD" for r in load.records)
    dis = _parse(files["COARRI_DISCHARGE.edi"].decode())[0]
    assert all(r.data["raw_event_code"] == "COARRI_DISCHARGE" for r in dis.records)
    assert dis.records[0].data["raw_port"] == "CNNGB"


def test_codeco_gate_events():
    files = build_dataset()
    gi = _parse(files["CODECO_GATEIN_A.edi"].decode())[0]
    assert gi.records[0].data["raw_event_code"] == "GIN"
    go = _parse(files["CODECO_GATEOUT.edi"].decode())[0]
    assert go.records[0].data["raw_event_code"] == "GOUT"


def test_missing_dtm_and_truncation_captured():
    files = build_dataset()
    pms = _parse(files["CODECO_GATEIN_B_BAD.edi"].decode())
    # 截断缺 UNT -> 报文级错误
    assert any("UNT" in e for e in pms[0].errors)
    # C5 缺 DTM 值 -> 记录级 issue
    c5 = [r for r in pms[0].records if r.data["container_no"] == "EGLV1000005"][0]
    assert any("DTM" in i for i in c5.issues)


def test_missing_container_no_record():
    files = build_dataset()
    pm = _parse(files["COARRI_LOAD_C5_BAD.edi"].decode())[0]
    bad = [r for r in pm.records if not r.data["container_no"]]
    assert bad and any("箱号" in i for i in bad[0].issues)


def test_unknown_type_kept_as_raw():
    files = build_dataset()
    pms = _parse(files["BAPLIE_20261109.edi"].decode())[0]
    c5 = [r for r in pms.records if r.data["container_no"] == "EGLV1000005"][0]
    assert c5.data["raw_container_type"] == "ZZ99"   # 错码原样保留，归一化阶段再标记
