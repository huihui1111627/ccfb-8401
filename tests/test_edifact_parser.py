"""EDIFACT 词法层：自定义分隔符、转义、截断、多报文。"""
from ctplatform.ingest.edifact import parse_interchange


def test_default_segment_tokenize():
    text = "UNB+UNOA:3+A+B+261110:0800+X'\nUNH+1+BAPLIE:D:95B:UN'\nEQD+CN+EGLV1000001+22G1'\nUNT+3+1'\nUNZ+1+X'\n"
    ic = parse_interchange(text)
    assert not ic.errors
    assert len(ic.messages) == 1
    assert ic.messages[0].message_type == "BAPLIE"
    eqd = [s for s in ic.messages[0].segments if s.tag == "EQD"][0]
    assert eqd.element(1) == "CN"
    assert eqd.element(2) == "EGLV1000001"
    assert eqd.element(3) == "22G1"
    assert eqd.line_no == 2


def test_una_custom_delimiters_and_escape():
    # UNA 定义成分隔符 ^，元素分隔 |，转义 \，段结束 #
    text = "UNA^|.\\ #\nUNH|1#CODECO#\nEQD|CN|TEST\\'BOX|22G1#\nUNT|2|1#"
    ic = parse_interchange(text)
    eqd = [s for s in ic.messages[0].segments if s.tag == "EQD"][0]
    assert eqd.element(2) == "TEST'BOX"      # 转义后的段结束符按字面量保留


def test_truncated_message_reports_error():
    text = "UNH+1+CODECO:D:95B:UN'\nEQD+CN+X1'\n"
    ic = parse_interchange(text)
    assert ic.errors and "UNT" in ic.errors[0]


def test_multiple_messages_in_one_interchange():
    text = ("UNB+UNOA:3+A+B+T+1'\n"
            "UNH+1+CODECO:D:95B:UN'\nEQD+CN+X1'\nUNT+2+1'\n"
            "UNH+2+CODECO:D:95B:UN'\nEQD+CN+X2'\nUNT+2+2'\nUNZ+2+1'\n")
    ic = parse_interchange(text)
    assert len(ic.messages) == 2


def test_empty_input():
    ic = parse_interchange("")
    assert ic.errors and not ic.messages
