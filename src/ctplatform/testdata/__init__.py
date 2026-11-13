"""测试报文造数器。

围绕一条船次（IMO 9472916 / 航次 V-0521E，上海->宁波）构造 8 只箱的完整生命周期，
并刻意注入码头真实数据中常见的脏数据：
  C1 正常全链（BAPLIE+CODECO+COARRI×2+CODECO）
  C2 重复报文（同一进场发两次，内容一致，自然键相同 -> 去重）
  C3 迟到/乱序（卸船报文时间早于装船，且后到）
  C4 矛盾报文（两份装船，港口/时间不一致）
  C5 EDIFACT 错码 + 缺段（未知箱型、缺 DTM、报文截断缺 UNT）
  C6 断链箱（只有进场+装船，缺卸船/出场 -> GAP/INCOMPLETE）
  C7 未知代码箱（未知港口/船公司 -> UNMAPPED 差异）
  C8 船图与实物不符（BAPLIE 有配载但无 COARRI 装船）
另含一份混乱 CSV（内部码体系、中文事件词、时间格式不一）和一份 Excel。
"""
from __future__ import annotations

from datetime import datetime, timedelta

VESSEL_IMO = "9472916"
VESSEL = "EVER_GLORY"
VOYAGE = "V-0521E"
POL, POD = "CNSHA", "CNNGB"
DAY = datetime(2026, 11, 10)


# ------------------------------------------------------------- EDIFACT 拼装

class EdifactBuilder:
    """按 D.95B 常见段式拼报文，分隔符默认 + : ' ?。"""

    def __init__(self, msg_type: str, ref: str, chars: str = "+:?'"):
        self.lines: list[str] = []
        self.msg_type = msg_type
        self.ref = ref
        self._unh(msg_type, ref)

    def _unh(self, msg_type, ref):
        self.lines.append(
            f"UNH+{ref}+{msg_type}:D:95B:UN:SMDG20'")

    def unt(self):
        self.lines.append(f"UNT+{len(self.lines) + 1}+{self.ref}'")

    def tdt(self, carrier_raw: str = "EGLV"):
        self.lines.append(
            f"TDT+20+{VOYAGE}+1+++{carrier_raw}:172+++"
            f"{VESSEL}:103:{VESSEL_IMO}:146'")

    def rff_von(self):
        self.lines.append(f"RFF+VON:{VOYAGE}'")

    def nad_ms(self, carrier_raw: str):
        self.lines.append(f"NAD+MS+{carrier_raw}:172:ZZZ'")

    def dtm_header(self, dt: datetime):
        self.lines.append(f"DTM+137:{dt:%y%m%d%H%M}:303'")

    def loc(self, qualifier: str, port: str):
        self.lines.append(f"LOC+{qualifier}+{port}:139:6'")

    def eqd(self, container_no: str, ctype: str | None, status: str | None,
            include_no: bool = True):
        # EQD+CN+箱号+箱型尺寸和类型+集装设备提供者+状态码(DE8249)
        no = container_no if include_no else ""
        parts = ["EQD", "CN", no, ctype or "", "", status or ""]
        self.lines.append("+".join(parts) + "'")

    def dtm(self, qualifier: str, dt: datetime | None):
        if dt is None:
            self.lines.append(f"DTM+{qualifier}::303'")   # 缺值段
        else:
            self.lines.append(f"DTM+{qualifier}:{dt:%y%m%d%H%M}:303'")

    def text(self, raw_segment: str):
        self.lines.append(raw_segment)

    def render(self, envelope: bool = True, truncate_unt: bool = False) -> str:
        body = list(self.lines)
        if not truncate_unt:
            body.append(f"UNT+{len(body) + 1}+{self.ref}'")
        out = []
        if envelope:
            out.append("UNB+UNOA:3+EGLV:ZZZ+TERMINAL:ZZZ+261110:0800+CTRL001'")
        out.extend(body)
        if envelope and not truncate_unt:
            out.append("UNZ+1+CTRL001'")
        return "\n".join(out) + "\n"


# ------------------------------------------------------------- 四类报文

def make_baplie(containers: list[dict], ref: str = "BAP01") -> str:
    b = EdifactBuilder("BAPLIE", ref)
    b.tdt("EGLV")
    b.rff_von()
    b.dtm_header(DAY - timedelta(days=1))
    b.loc(147, POL)
    b.loc(9, POD)
    for c in containers:
        b.eqd(c["no"], c.get("type", "22G1"), c.get("laden", "FULL"))
        b.loc(147, c.get("pol", POL))
        b.loc(9, c.get("pod", POD))
        b.dtm(200, c.get("plan_time", DAY - timedelta(hours=12)))
    b.unt()
    return b.render()


def make_codeco(events: list[dict], ref: str, port: str = POL,
                truncate_unt: bool = False) -> str:
    """events: [{no,type,status(GIN/GOUT),time,laden}]"""
    b = EdifactBuilder("CODECO", ref)
    b.tdt("EGLV")
    b.dtm_header(events[0]["time"] if events else DAY)
    b.loc(147, port)
    for c in events:
        b.eqd(c["no"], c.get("type", "22G1"), c["status"])
        b.loc(147, c.get("port", port))
        if c.get("missing_dtm"):
            b.dtm(132, None)
        else:
            b.dtm(132, c["time"])
    return b.render(truncate_unt=truncate_unt)


def make_coarri(events: list[dict], direction: str, ref: str,
                port: str | None = POL) -> str:
    """direction: LOAD -> LOC+11；DISCHARGE -> LOC+9。"""
    b = EdifactBuilder("COARRI", ref)
    b.tdt("EGLV")
    b.dtm_header(events[0]["time"] if events else DAY)
    if port is not None:
        b.loc(11 if direction == "LOAD" else 9, port)
    for c in events:
        b.eqd(c["no"], c.get("type", "22G1"), c.get("laden", "FULL"))
        if port is None and c.get("port"):
            b.loc(11 if direction == "LOAD" else 9, c["port"])
        b.dtm(200, c["time"])
    b.unt()
    return b.render()


# ------------------------------------------------------------- 场景数据集

def build_dataset() -> dict[str, bytes]:
    """返回 {文件名: 文件字节}。"""
    files: dict[str, bytes] = {}
    t = lambda h, m=0: DAY + timedelta(hours=h, minutes=m)  # noqa: E731

    c1, c2, c3, c4 = "EGLV1000001", "EGLV1000002", "EGLV1000003", "EGLV1000004"
    c5, c6, c7, c8 = "EGLV1000005", "EGLV1000006", "EGLV1000007", "EGLV1000008"

    # ---- BAPLIE：C1..C5、C7、C8 有配载；C6 也配载；C8 故意无实物装船
    baplie_containers = [
        {"no": c1}, {"no": c2}, {"no": c3}, {"no": c4},
        {"no": c5, "type": "ZZ99"},                 # 未知箱型错码
        {"no": c6}, {"no": c7, "type": "45G1"}, {"no": c8},
    ]
    files["BAPLIE_20261109.edi"] = make_baplie(baplie_containers).encode()

    # ---- CODECO 进场：C1..C7；C2 重复在第二份文件；C5 缺 DTM；C7 未知港口
    gate_in_1 = make_codeco([
        {"no": c1, "status": "GIN", "time": t(6, 0)},
        {"no": c3, "status": "GIN", "time": t(6, 20)},
        {"no": c4, "status": "GIN", "time": t(6, 30)},
        {"no": c6, "status": "GIN", "time": t(6, 40)},
        {"no": c8, "status": "GIN", "time": t(6, 50)},
    ], "CODECO_GI1")
    files["CODECO_GATEIN_A.edi"] = gate_in_1.encode()

    gate_in_2 = make_codeco([
        {"no": c2, "status": "GIN", "time": t(7, 0)},
        {"no": c2, "status": "GIN", "time": t(7, 0)},   # 文件内重复箱组
        {"no": c5, "status": "GIN", "time": t(7, 10), "missing_dtm": True},
        {"no": c7, "status": "GIN", "time": t(7, 20), "port": "ZZ999"},  # 未知港口
    ], "CODECO_GI2", truncate_unt=True)                  # 截断：缺 UNT
    files["CODECO_GATEIN_B_BAD.edi"] = gate_in_2.encode()

    # 迟到的重复进场（C2，自然键与上面完全一致 -> 跨文件去重）
    files["CODECO_GATEIN_C2_LATE.edi"] = make_codeco(
        [{"no": c2, "status": "GIN", "time": t(7, 0)}], "CODECO_GI3").encode()

    # ---- COARRI 装船：C1..C4、C6、C7；C4 两份矛盾；C5 缺段箱；C8 缺席
    load_1 = make_coarri([
        {"no": c1, "time": t(10, 0)},
        {"no": c2, "time": t(10, 5)},
        {"no": c3, "time": t(10, 10)},
        {"no": c6, "time": t(10, 20)},
        {"no": c7, "time": t(10, 25)},
    ], "LOAD", "COARRI_L1")
    files["COARRI_LOAD_A.edi"] = load_1.encode()

    # C4 第一份装船（上海 10:30）
    files["COARRI_LOAD_C4_V1.edi"] = make_coarri(
        [{"no": c4, "time": t(10, 30)}], "LOAD", "COARRI_L2").encode()
    # C4 第二份装船（时间与港口都不同 -> 矛盾）
    b = EdifactBuilder("COARRI", "COARRI_L3")
    b.tdt("EGLV")
    b.dtm_header(t(11))
    b.loc(11, "CNNGB")                               # 矛盾港口
    b.eqd(c4, "22G1", "FULL")
    b.dtm(200, t(15, 30))                            # 矛盾时间
    b.unt()
    files["COARRI_LOAD_C4_V2_CONFLICT.edi"] = b.render().encode()

    # C5 装船：箱型错码 + EQD 缺箱号的残组（额外注入一条坏记录）
    b = EdifactBuilder("COARRI", "COARRI_L4")
    b.tdt("EGLV")
    b.dtm_header(t(11))
    b.loc(11, POL)
    b.eqd(c5, "ZZ99", "FULL")
    b.dtm(200, t(10, 40))
    b.text("EQD+CN+++FULL'")                         # 缺箱号残组
    b.dtm(200, t(10, 45))
    b.unt()
    files["COARRI_LOAD_C5_BAD.edi"] = b.render().encode()

    # ---- COARRI 卸船（宁波）：C3 时间早于其装船 -> 乱序；C1/C2/C4/C5/C7 正常
    discharge = make_coarri([
        {"no": c1, "time": t(20, 0)},
        {"no": c2, "time": t(20, 5)},
        {"no": c3, "time": t(9, 0)},                  # 早于 C3 装船 10:10
        {"no": c4, "time": t(20, 15)},
        {"no": c5, "time": t(20, 20)},
        {"no": c7, "time": t(20, 25)},
    ], "DISCHARGE", "COARRI_D1", port=POD)
    files["COARRI_DISCHARGE.edi"] = discharge.encode()

    # ---- CODECO 出场：C1..C5、C7；C6 故意不出场（链不完整）
    gate_out = make_codeco([
        {"no": c1, "status": "GOUT", "time": t(22, 0)},
        {"no": c2, "status": "GOUT", "time": t(22, 5)},
        {"no": c3, "status": "GOUT", "time": t(22, 10)},
        {"no": c4, "status": "GOUT", "time": t(22, 15)},
        {"no": c5, "status": "GOUT", "time": t(22, 20)},
        {"no": c7, "status": "GOUT", "time": t(22, 30), "port": POD},
    ], "CODECO_GO1", port=POD)
    files["CODECO_GATEOUT.edi"] = gate_out.encode()

    # ---- 混乱 CSV：内部码体系、中文事件词、斜杠时间格式；C1 又一条进场（重复）
    csv_lines = [
        "箱号,船公司,箱型,事件,时间,港口,船名,航次,IMO,箱态",
        f"{c1},EVER,20DV,进场,2026/11/10 06:00:00,SHA,{VESSEL},{VOYAGE},{VESSEL_IMO},F",
        f"{c2},COSCO,G20,装船,2026/11/10 10:05:00,SHA,{VESSEL},{VOYAGE},{VESSEL_IMO},重箱",
        f"{c7},MSC,H40,卸船,2026/11/10 20:25:00,NB,{VESSEL},{VOYAGE},{VESSEL_IMO},FULL",
        "EGLV1000009,UNKNOWNCO,20DV,出场,2026/11/11 09:00:00,QIN,XXXSHIP,V-X1,IMO0001,E",
    ]
    files["gate_ops_20261110.csv"] = ("\n".join(csv_lines) + "\n").encode()

    return files


def build_excel_sample() -> tuple[str, bytes]:
    """单独造一份 Excel（openpyxl 现场写），走 tpl_ops_excel 模板。"""
    import io
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["Container", "Carrier", "Type", "Activity", "Timestamp",
               "Port", "Vessel", "Voyage", "IMO", "Laden"])
    ws.append(["EGLV1000010", "EGLV", "40HQ", "GATE_IN", "2026-11-10 06:05:00",
               "CNSHA", VESSEL, VOYAGE, VESSEL_IMO, "FULL"])
    ws.append(["EGLV1000010", "EGLV", "40HQ", "LOAD", "2026-11-10 10:10:00",
               "CNSHA", VESSEL, VOYAGE, VESSEL_IMO, "FULL"])
    buf = io.BytesIO()
    wb.save(buf)
    return "ops_excel_20261110.xlsx", buf.getvalue()


EXPECTED_SCENARIOS = {
    "EGLV1000001": "OK 全链，且 CSV 重复进场被去重",
    "EGLV1000002": "重复报文（文件内+跨文件）去重",
    "EGLV1000003": "卸船乱序（OUT_OF_ORDER）",
    "EGLV1000004": "装船矛盾（CONTRADICTION）",
    "EGLV1000005": "错码+缺 DTM+缺箱号残组+截断报文",
    "EGLV1000006": "断链/不完整（GAP + INCOMPLETE）",
    "EGLV1000007": "未知港口/船公司代码（UNMAPPED）",
    "EGLV1000008": "船图有配载、无实物装船（MANIFEST_NO_ACTUAL）",
}
