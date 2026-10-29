"""运行摘要：批次计数 + 表达式解析缓存指标。

守护进程（T33）、运维 CLI（T35）与规模化回归（T39）读的是同一份运行状态：
「这一轮处理了多少批次、多少行医嘱、三态结论各多少条、表达式解析树缓存命中多少、
解析了多少次」。口径集中在这里，避免各处各算一套、报出来的数对不上。

**为什么要把缓存指标放进运行摘要**：解析树缓存是本轮判定吞吐的关键路径
（一条医嘱行要逐规则求值，规则文本没变时不该重复解析）。缓存命中率一旦塌了，
表现是「机器变慢」而不是「报错」，没有指标就没人发现。因此命中率必须和批次
结论一起进摘要、进 CLI 输出、进规模化回归报告。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .expression.cache import ParseTreeCache, default_cache
from .models import Conclusion

__all__ = [
    "RunSummary",
    "ScaleMetrics",
    "current_summary",
    "record_batch",
    "record_scale",
    "reset_summary",
    "summary_snapshot",
]


@dataclass(frozen=True)
class ScaleMetrics:
    """规模化回归的三项指标（吞吐 / 缓存命中率 / 三态分布）。

    **为什么要单独一张表**：批次计数只回答「审了多少条」，回答不了
    「审得快不快」「解析树缓存还灵不灵」「结论分布有没有跑偏」。
    这三项一旦塌了，现象是「机器变慢」「结论变少」，不是报错——
    没有指标就没人发现，所以它们必须进运行摘要、进 CLI 输出、进
    规模化回归报告（``out/scale_report.json``）。

    字段口径：

    * ``rows`` —— 本轮喂进判定的医嘱行数；
    * ``judgement_rows`` —— 产出的判定行数（逐处方 × 逐规则）；
    * ``elapsed_sec`` / ``rows_per_sec`` —— 判定耗时与吞吐（行/秒）；
    * ``cache_hit_rate`` —— 表达式解析树缓存命中率（0–1）；
    * ``compliant`` / ``violation`` / ``undeterminable`` —— 三态结论分布。
    """

    rows: int = 0
    judgement_rows: int = 0
    elapsed_sec: float = 0.0
    rows_per_sec: float = 0.0
    cache_hit_rate: float = 0.0
    compliant: int = 0
    violation: int = 0
    undeterminable: int = 0

    def to_dict(self) -> dict[str, float | int]:
        """转成可直接进 JSON 的字典。"""
        return {
            "rows": self.rows,
            "judgement_rows": self.judgement_rows,
            "elapsed_sec": round(float(self.elapsed_sec), 3),
            "rows_per_sec": round(float(self.rows_per_sec), 1),
            "cache_hit_rate": round(float(self.cache_hit_rate), 6),
            "compliant": self.compliant,
            "violation": self.violation,
            "undeterminable": self.undeterminable,
        }

    def render(self) -> str:
        """一行可读摘要（规模化回归报告与 CLI 摘要共用同一口径）。"""
        return (
            f"规模 {self.rows} 行 · 判定 {self.judgement_rows} 条 · "
            f"吞吐 {self.rows_per_sec:.1f} 行/秒 · "
            f"缓存命中率 {self.cache_hit_rate * 100:.1f}% · "
            f"三态（合规 {self.compliant} · 违规 {self.violation} · "
            f"不可判定 {self.undeterminable}）"
        )


@dataclass
class RunSummary:
    """一次运行（或一个批次）的汇总计数。"""

    batches: int = 0
    prescription_rows: int = 0
    judgement_rows: int = 0
    compliant: int = 0
    violation: int = 0
    undeterminable: int = 0
    writeback_sent: int = 0
    writeback_failed: int = 0
    elapsed_sec: float = 0.0
    #: 规模化回归的三项指标（吞吐 / 缓存命中率 / 三态分布）；未跑规模化回归时为 None。
    scale: ScaleMetrics | None = None

    def record(
        self,
        *,
        received_rows: int,
        judgement_rows: int,
        counts: Mapping[str, int] | None = None,
        writeback_sent: int = 0,
        writeback_failed: int = 0,
        elapsed_sec: float = 0.0,
    ) -> None:
        """记一个批次的计数（``counts`` 的键是结论名：合规 / 违规 / 不可判定）。"""
        self.batches += 1
        self.prescription_rows += int(received_rows)
        self.judgement_rows += int(judgement_rows)
        self.writeback_sent += int(writeback_sent)
        self.writeback_failed += int(writeback_failed)
        self.elapsed_sec += float(elapsed_sec)
        if not counts:
            return
        self.compliant += int(counts.get(Conclusion.COMPLIANT.value, 0))
        self.violation += int(counts.get(Conclusion.VIOLATION.value, 0))
        self.undeterminable += int(counts.get(Conclusion.UNDETERMINABLE.value, 0))

    def to_dict(self, cache: ParseTreeCache | None = None) -> dict[str, object]:
        """转成可直接进 JSON 的字典（含表达式缓存统计）。"""
        resolved = cache if cache is not None else default_cache()
        return {
            "batches": self.batches,
            "prescription_rows": self.prescription_rows,
            "judgement_rows": self.judgement_rows,
            "compliant": self.compliant,
            "violation": self.violation,
            "undeterminable": self.undeterminable,
            "writeback_sent": self.writeback_sent,
            "writeback_failed": self.writeback_failed,
            "elapsed_sec": round(float(self.elapsed_sec), 3),
            "expression_cache": resolved.stats.to_dict(),
            "scale": self.scale.to_dict() if self.scale is not None else None,
        }

    def render(self, cache: ParseTreeCache | None = None) -> str:
        """一行可读摘要（终端摘要行用）。"""
        resolved = cache if cache is not None else default_cache()
        line = (
            f"批次 {self.batches} · 医嘱 {self.prescription_rows} 行 · "
            f"判定 {self.judgement_rows} 条"
            f"（合规 {self.compliant} · 违规 {self.violation} · "
            f"不可判定 {self.undeterminable}） · "
            f"回写 {self.writeback_sent} 条 · "
            f"表达式解析缓存：{resolved.stats.render()}"
        )
        if self.scale is not None:
            line += f" · 规模化回归：{self.scale.render()}"
        return line

    def record_scale(self, metrics: ScaleMetrics) -> ScaleMetrics:
        """把规模化回归的三项指标记进运行摘要。"""
        self.scale = metrics
        return metrics


# ---------------------------------------------------------------------------
# 进程级运行摘要
# ---------------------------------------------------------------------------

_SUMMARY = RunSummary()


def current_summary() -> RunSummary:
    """当前运行摘要（进程级单例）。"""
    return _SUMMARY


def reset_summary() -> RunSummary:
    """清零运行摘要（守护进程重启或测试隔离时调用）。"""
    global _SUMMARY
    _SUMMARY = RunSummary()
    return _SUMMARY


def record_batch(
    *,
    received_rows: int,
    judgement_rows: int,
    counts: Mapping[str, int] | None = None,
    writeback_sent: int = 0,
    writeback_failed: int = 0,
    elapsed_sec: float = 0.0,
) -> RunSummary:
    """把一个批次的计数记进运行摘要。"""
    _SUMMARY.record(
        received_rows=received_rows,
        judgement_rows=judgement_rows,
        counts=counts,
        writeback_sent=writeback_sent,
        writeback_failed=writeback_failed,
        elapsed_sec=elapsed_sec,
    )
    return _SUMMARY


def summary_snapshot(cache: ParseTreeCache | None = None) -> dict[str, object]:
    """运行摘要快照（含表达式缓存统计），供 CLI ``--json`` 与守护进程心跳读取。"""
    return _SUMMARY.to_dict(cache)


def record_scale(metrics: ScaleMetrics) -> ScaleMetrics:
    """把规模化回归的三项指标记进运行摘要（进程级单例）。"""
    return _SUMMARY.record_scale(metrics)
