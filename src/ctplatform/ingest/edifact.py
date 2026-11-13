"""EDIFACT 通用词法层。

只负责把报文文本切成结构化段（segment），不解释业务含义：
- 支持 UNA 自定义服务字符（component / element / decimal / escape / repetition）
- 支持转义符（如 ?' ?+ ??:）
- 每个段保留起始行号，血缘可回溯到报文具体行
- 支持一个交换包（UNB...UNZ）内多份报文（UNH...UNT）
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_CHARS = {
    "component": ":",
    "element": "+",
    "decimal": ".",
    "escape": "?",
    "repetition": "*",
    "segment": "'",
}


@dataclass
class Segment:
    tag: str
    elements: list[list[str]]              # elements[i][j]：第 i 元素的第 j 成分
    line_no: int
    raw: str

    def element(self, idx: int, comp: int = 0, default: str | None = None) -> str | None:
        if idx < len(self.elements) and comp < len(self.elements[idx]):
            val = self.elements[idx][comp]
            return val if val != "" else default
        return default


@dataclass
class Message:
    message_type: str                       # BAPLIE / COARRI / CODECO
    segments: list[Segment] = field(default_factory=list)
    index: int = 0                          # 交换包内序号


@dataclass
class Interchange:
    segments: list[Segment] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    service_chars: dict = field(default_factory=lambda: dict(DEFAULT_CHARS))
    errors: list[str] = field(default_factory=list)


def _detect_service_chars(text: str) -> dict:
    chars = dict(DEFAULT_CHARS)
    if text.startswith("UNA"):
        # UNA:+.? '  —— 位置 3..8 共 6 个服务字符
        svc = text[3:9]
        if len(svc) >= 5:
            chars["component"] = svc[0]
            chars["element"] = svc[1]
            chars["decimal"] = svc[2]
            chars["escape"] = svc[3]
            if len(svc) >= 6 and svc[5] != " ":
                chars["segment"] = svc[5]
    return chars


def _tokenize(text: str, chars: dict) -> list[Segment]:
    """切分段，处理转义符，记录每个段首字符所在行号（1 起始）。"""
    segments: list[Segment] = []
    line_no = 1
    seg_start_line = 1
    buf: list[str] = []
    i = 0
    n = len(text)
    esc = chars["escape"]
    seg_term = chars["segment"]
    elem_sep = chars["element"]
    comp_sep = chars["component"]

    while i < n:
        ch = text[i]
        if ch == "\n":
            line_no += 1
        if ch == esc and i + 1 < n:
            # 转义：下一个字符按字面量收进来
            buf.append(text[i + 1])
            i += 2
            continue
        if ch == seg_term:
            raw = "".join(buf)
            if raw.strip():
                segments.append(_build_segment(raw, seg_start_line, elem_sep, comp_sep))
            buf = []
            seg_start_line = line_no
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf)
    if tail.strip():
        # 缺结束符的残段也要保留，交由上层报错而不是静默丢弃
        segments.append(_build_segment(tail, seg_start_line, elem_sep, comp_sep))
    return segments


def _build_segment(raw: str, line_no: int, elem_sep: str, comp_sep: str) -> Segment:
    parts = raw.split(elem_sep)
    elements: list[list[str]] = []
    for p in parts:
        elements.append([c.strip() for c in p.split(comp_sep)])
    tag = elements[0][0] if elements and elements[0] else ""
    return Segment(tag=tag, elements=elements, line_no=line_no, raw=raw.strip())


def parse_interchange(text: str) -> Interchange:
    """解析整个交换包，按 UNH/UNT 切分报文。"""
    ic = Interchange()
    if not text or not text.strip():
        ic.errors.append("空报文")
        return ic

    chars = _detect_service_chars(text)
    ic.service_chars = chars
    segments = _tokenize(text, chars)
    ic.segments = segments

    current: Message | None = None
    depth = 0
    for seg in segments:
        if seg.tag == "UNH":
            current = Message(message_type="UNKNOWN", index=len(ic.messages))
            # UNH+报文参考号+BAPLIE:D:95B:UN:... —— 报文类型在第 3 个元素的首成分
            if len(seg.elements) > 2 and seg.elements[2]:
                current.message_type = seg.elements[2][0].strip().upper()
            ic.messages.append(current)
            depth += 1
        elif seg.tag == "UNT":
            if current is not None:
                current.segments.append(seg)
                current = None
            depth -= 1
            if depth < 0:
                ic.errors.append(f"第 {seg.line_no} 行: UNT 缺少匹配的 UNH")
                depth = 0
        elif current is not None:
            current.segments.append(seg)

    if current is not None:
        ic.errors.append(
            f"报文 {current.message_type} 缺少 UNT 结束段（在第 {current.segments[-1].line_no} 行后截断）"
        )
    if not ic.messages:
        ic.errors.append("未找到任何 UNH 报文头")
    return ic
