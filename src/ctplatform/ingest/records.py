"""接入层共享数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParsedRecord:
    """一条原始箱记录（EDIFACT 箱组或表格一行）。

    data 内只放抽取到的原始值，标准代码转换在 normalize 阶段进行；
    下划线前缀的键为行号/原始值等溯源辅助信息，不写入统一模型字段。
    """

    record_index: int
    segment_line: int | None
    data: dict
    issues: list[str] = field(default_factory=list)
