"""箱动态数据平台（Container Movement Platform）。

模块划分：
- ingest    : 数据源接入（EDIFACT / CSV / Excel），原始留痕
- normalize : 代码表统一、事件归一化、去重、动态链、对账、统计
- web       : Flask 网页控制台
- testdata  : 测试报文造数器
"""

__version__ = "0.1.0"
