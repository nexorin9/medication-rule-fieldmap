"""合理用药审方字段对照系统 · 产品包。

形态：常驻守护进程（``med_rule_fieldmap.daemon``）+ 运维 CLI（``med_rule_fieldmap.cli``）。
判定链全部确定性求值（规则行 → 受限表达式 → 三态结论），可复算、可审计；
本轮不设模型网关、不设「预留 LLM」。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
