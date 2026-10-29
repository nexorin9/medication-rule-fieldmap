#!/usr/bin/env python3
"""规模化回归 + 三态 / 二元基线对比（任务书 T39 · 数据层 4/4）。

本脚本把两件「必须给出可复现数字」的事做成可重跑的产物：

1. **规模化回归**：2000 行规模集 × 生效规则集跑一遍，测三态分布是否稳定、
   表达式解析树缓存命中率、判定吞吐。三项指标进运行摘要
   （``med_rule_fieldmap.run_summary``），并落 ``out/scale_report.json``。
2. **三态 vs 二元基线对比**：以「二元结论基础实现」为基线，量化三态判定在
   **不可判定召回率** 上的增益，以及二元实现换来的两种代价——
   把「缺字段」当成「不合理」硬拦（误拦）、把判不了的处方整条静默丢掉（漏审）。

**基线怎么定义（这一步决定对比可不可信）**

基线不是「随便写个简化版」——那样测出来的是规则覆盖面差距，不是第三态的
价值。本脚本给的是**同链去三态**基线：同一套规则、同一份数据、同一条判定链，
只把结论空间从三态压成二元：

* ``binary_skip``：判定链产出「不可判定」的那些行，在二元语义下**直接不出结论**
  （字段缺失即跳过）——这是基础审方系统最常见的写法：判不了就不报。
* ``binary_violation``：同一批行在二元语义下**判违规**（字段缺失即违规）——
  这是基础审方系统另一种常见写法：宁可信其有。

另附一组**独立二元实现**（``binary_standalone_*``）：只实现「表达式规则」的
最小二元审方（缺字段按 skip / violation，判不了就静默放行），用来同时显示
真实基础实现的两项差距——结论空间表达不了第三态、规则覆盖面也更窄。

两个报告都分成 ``deterministic``（两次跑逐字节一致：行数、三态分布、召回与
代价计数）与 ``measured``（每次会变：耗时、吞吐）两块，因此「可复现」能被
下游直接验证，而不是口头承诺。

**数据来源边界**：规模集来自 ``scripts/gen_synthetic_orders.py``（确定性种子
合成），**只作测试数据层**，不是真实院内医嘱、也不是产品对接边界。
正式对接口径见 ``spec.md`` §2.1（入站）与 §2.3（出站）。

用法::

    python scripts/bench_scale.py --out out
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from med_rule_fieldmap.engine.children_age import is_child_age_rule  # noqa: E402
from med_rule_fieldmap.engine.duplicate import is_duplicate_rule  # noqa: E402
from med_rule_fieldmap.engine.indication import is_indication_rule  # noqa: E402
from med_rule_fieldmap.engine.judge import (  # noqa: E402
    Truth,
    evaluate_expression,
    judge_batch,
    summarize_conclusions,
)
from med_rule_fieldmap.engine.normativity import (  # noqa: E402
    is_normativity_rule,
    is_usage_vagueness_rule,
)
from med_rule_fieldmap.engine.sched import select_candidates  # noqa: E402
from med_rule_fieldmap.engine.skin_test import is_skin_test_rule  # noqa: E402
from med_rule_fieldmap.engine.supernormal import is_supernormal_rule  # noqa: E402
from med_rule_fieldmap.expression.cache import ParseTreeCache  # noqa: E402
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows  # noqa: E402
from med_rule_fieldmap.models import Conclusion, JudgementRow, PrescriptionRow, RuleRow  # noqa: E402
from med_rule_fieldmap.rules.repository import RuleRepository  # noqa: E402
from med_rule_fieldmap.run_summary import (  # noqa: E402
    ScaleMetrics,
    current_summary,
    record_scale,
    summary_snapshot,
)

#: 结论字面量（与 models.Conclusion 同源，不另立一套中文口径）。
COMPLIANT = Conclusion.COMPLIANT.value
VIOLATION = Conclusion.VIOLATION.value
UNDETERMINABLE = Conclusion.UNDETERMINABLE.value
#: 二元基线里「这条规则没出结论」的状态——不是第三态，是**没有结论**。
NO_CONCLUSION = "无结论"

#: 规模化回归的两条验收阈值（任务书 T39）。
CACHE_HIT_RATE_MIN = 0.90
ROWS_PER_SEC_MIN = 200.0

DEFAULT_SEED = 20261028
DEFAULT_ROWS = 2000

__all__ = [
    "CACHE_HIT_RATE_MIN",
    "COMPLIANT",
    "Dataset",
    "NO_CONCLUSION",
    "ROWS_PER_SEC_MIN",
    "TriStateRun",
    "UNDETERMINABLE",
    "VIOLATION",
    "build_baseline_report",
    "build_dataset",
    "build_scale_report",
    "collapse_binary",
    "compare_to_labels",
    "current_summary",
    "load_rules",
    "main",
    "parse_args",
    "read_labels",
    "record_scale",
    "reduce_verdicts",
    "run_benchmark",
    "run_binary_standalone",
    "run_tristate",
    "summary_snapshot",
    "write_json",
]


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------
def _load_generator() -> Any:
    """加载 ``scripts/gen_synthetic_orders.py``（它是脚本，不是包成员）。"""
    path = PROJECT_ROOT / "scripts" / "gen_synthetic_orders.py"
    spec = importlib.util.spec_from_file_location("gen_synthetic_orders", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass 在定义时按 ``__module__`` 回查命名空间，模块必须先进 sys.modules，
    # 否则 ``@dataclass`` 会因找不到模块而炸（``NoneType.__dict__``）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_labels(path: Path) -> dict[str, str]:
    """读逐行标签（处方号 → 设计结论），作召回率对比的基准。"""
    labels: dict[str, str] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            labels[record["prescription_no"]] = record["expected_class"]
    return labels


@dataclass(frozen=True)
class Dataset:
    """一次规模化回归的数据集（规模集 + 基准标签 + 清单）。"""

    prescriptions: tuple[PrescriptionRow, ...]
    labels: dict[str, str]
    manifest: dict[str, Any]
    csv_path: Path
    read_sec: float = 0.0


def build_dataset(dest: Path, *, seed: int = DEFAULT_SEED, rows: int = DEFAULT_ROWS) -> Dataset:
    """生成规模集并读回处方行与基准标签。"""
    generator = _load_generator()
    manifest = generator.generate(dest, seed=seed, rows=rows)
    csv_path = dest / f"orders_{seed}.csv"
    start = time.perf_counter()
    prescriptions = read_prescription_rows(csv_path)
    read_sec = time.perf_counter() - start
    labels = read_labels(dest / f"labels_{seed}.csv")
    return Dataset(
        prescriptions=tuple(prescriptions),
        labels=labels,
        manifest=manifest,
        csv_path=csv_path,
        read_sec=read_sec,
    )


# ---------------------------------------------------------------------------
# 规则集
# ---------------------------------------------------------------------------
def load_rules(rules_dir: Path, *, cache: ParseTreeCache | None = None) -> list[RuleRow]:
    """载入生效规则集（非法规则只记录错误，不影响其余规则）。"""
    repository = RuleRepository(rules_dir, cache=cache)
    repository.load()
    return list(repository.effective_rules)


def _is_special_family(rule: RuleRow) -> bool:
    """这条规则是否属于「专用规则族」（皮试 / 前记后记 / 含糊字句 / 儿童年龄 /
    重复给药 / 超常处方 / 诊断相符性 / 量限矩阵）。

    独立二元基础实现只做**表达式规则**，这些专用族它一概不实现——这正是
    「基础实现」与本轮产品的真实差距，报告里如实标出来。
    """
    return (
        is_skin_test_rule(rule)
        or is_normativity_rule(rule)
        or is_usage_vagueness_rule(rule)
        or is_child_age_rule(rule)
        or is_duplicate_rule(rule)
        or is_supernormal_rule(rule)
        or is_indication_rule(rule)
        or bool(rule.limit_ref)
    )


# ---------------------------------------------------------------------------
# 三态判定（产品本体）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TriStateRun:
    """一次三态判定跑批的结果。"""

    rows: tuple[JudgementRow, ...]
    distribution: dict[str, int]
    verdicts: dict[str, str]
    prescription_count: int
    read_sec: float
    judge_sec: float
    cache_stats: dict[str, float | int]
    rule_count: int
    #: 每张处方命中的候选规则数（核对「逐处方 × 逐规则恰好一行」）。
    candidate_rule_counts: dict[str, int] = dataclass_field(default_factory=dict)

    @property
    def rows_per_sec(self) -> float:
        """判定吞吐（行/秒，只算判定耗时，不含读文件）。"""
        if self.judge_sec <= 0:
            return 0.0
        return self.prescription_count / self.judge_sec

    @property
    def cache_hit_rate(self) -> float:
        return float(self.cache_stats.get("hit_rate", 0.0))

    def metrics(self) -> ScaleMetrics:
        """转成运行摘要里的规模化指标（三项：吞吐 / 缓存命中率 / 三态分布）。"""
        return ScaleMetrics(
            rows=self.prescription_count,
            judgement_rows=len(self.rows),
            elapsed_sec=self.judge_sec,
            rows_per_sec=self.rows_per_sec,
            cache_hit_rate=self.cache_hit_rate,
            compliant=self.distribution.get(COMPLIANT, 0),
            violation=self.distribution.get(VIOLATION, 0),
            undeterminable=self.distribution.get(UNDETERMINABLE, 0),
        )


def reduce_verdicts(rows: Sequence[JudgementRow]) -> dict[str, str]:
    """把逐处方 × 逐规则的判定行压成逐处方结论。

    归约口径（三种实现共用，保证可比）：任一条规则判违规 → 该处方判违规；
    否则任一条判不可判定 → 判不可判定；否则判合规。一条结论都没有的处方
    记「无结论」——这正是二元基线「直接跳过」的典型形态。
    """
    buckets: dict[str, set[str]] = {}
    for row in rows:
        buckets.setdefault(row.prescription_no, set()).add(row.conclusion.value)
    verdicts: dict[str, str] = {}
    for prescription_no, conclusions in buckets.items():
        if VIOLATION in conclusions:
            verdicts[prescription_no] = VIOLATION
        elif UNDETERMINABLE in conclusions:
            verdicts[prescription_no] = UNDETERMINABLE
        else:
            verdicts[prescription_no] = COMPLIANT
    return verdicts


def run_tristate(dataset: Dataset, rules: Sequence[RuleRow]) -> TriStateRun:
    """跑一遍三态判定并计时。"""
    cache = ParseTreeCache()
    # 规则先绑进缓存：绑定时解析一次，之后 2000 行全部命中。
    for rule in rules:
        if rule.expression:
            cache.get_for_rule(rule.rule_id, rule.expression)

    prescriptions = list(dataset.prescriptions)
    start = time.perf_counter()
    judgement = judge_batch(
        prescriptions,
        rules,
        batch_no=dataset.manifest.get("batch_no", ""),
        source_file=dataset.csv_path.name,
        cache=cache,
    )
    judge_sec = time.perf_counter() - start

    rows = tuple(judgement.rows)
    distribution = summarize_conclusions(rows)
    return TriStateRun(
        rows=rows,
        distribution=distribution,
        verdicts=reduce_verdicts(rows),
        prescription_count=len(prescriptions),
        read_sec=dataset.read_sec,
        judge_sec=judge_sec,
        cache_stats=cache.stats.to_dict(),
        rule_count=len(rules),
        candidate_rule_counts=dict(judgement.candidate_rule_counts),
    )


# ---------------------------------------------------------------------------
# 二元基线
# ---------------------------------------------------------------------------
def collapse_binary(
    rows: Sequence[JudgementRow], *, missing_as_violation: bool
) -> dict[str, str]:
    """同链去三态：把三态判定行按二元语义塌缩成逐处方结论。

    ``missing_as_violation=False`` → 判不了的规则**不出结论**（字段缺失即跳过）；
    ``missing_as_violation=True`` → 判不了的规则**判违规**（字段缺失即违规）。

    规则、���据、判定链三者与三态实现完全一致，差别只在结论空间——
    这样测出来的差值才是第三态本身的贡献，而不是规则覆盖面差距。
    """
    buckets: dict[str, set[str]] = {}
    for row in rows:
        if row.conclusion.value == UNDETERMINABLE:
            if not missing_as_violation:
                # 跳过：这条规则在二元实现里根本不报，结论集合里不加它。
                continue
            buckets.setdefault(row.prescription_no, set()).add(VIOLATION)
            continue
        buckets.setdefault(row.prescription_no, set()).add(row.conclusion.value)
    verdicts: dict[str, str] = {}
    for prescription_no, conclusions in buckets.items():
        if VIOLATION in conclusions:
            verdicts[prescription_no] = VIOLATION
        elif COMPLIANT in conclusions:
            verdicts[prescription_no] = COMPLIANT
        else:
            verdicts[prescription_no] = NO_CONCLUSION
    return verdicts


def run_binary_standalone(
    prescriptions: Sequence[PrescriptionRow],
    rules: Sequence[RuleRow],
    *,
    missing_as_violation: bool,
    cache: ParseTreeCache | None = None,
) -> tuple[dict[str, str], dict[str, int]]:
    """独立二元基础实现：只做「表达式规则」的最小二元审方。

    口径：

    * 规则不适用 → 不报（与三态一致）；
    * ``required_fields`` 有缺失 → 按 skip / violation 两种口径处理；
    * 字段齐备 → 表达式二值求值；**判不了**（口径不可比等）在二元语义下
      表达不了，只能静默放行（这一条正是三态要解决的核心场景）；
    * 专用规则族与量限矩阵规则一概不实现（基础实现的覆盖面差距）。

    返回 (逐处方结论, 统计计数)。
    """
    resolved = cache if cache is not None else ParseTreeCache()
    verdicts: dict[str, str] = {}
    counters: Counter[str] = Counter()
    for row in prescriptions:
        conclusions: set[str] = set()
        for schedule in select_candidates(rules, row):
            rule = schedule.rule
            counters["candidate_rules"] += 1
            missing = row.missing_fields(rule.required_fields)
            if missing:
                counters["missing_field_hits"] += 1
                if missing_as_violation:
                    conclusions.add(VIOLATION)
                continue
            if _is_special_family(rule):
                counters["skipped_special_family"] += 1
                continue
            evaluation = evaluate_expression(rule.expression, row, cache=resolved)
            if evaluation.truth is Truth.TRUE:
                conclusions.add(VIOLATION)
            elif evaluation.truth is Truth.FALSE:
                conclusions.add(COMPLIANT)
            else:
                counters["undeterminable_silently_passed"] += 1
                conclusions.add(COMPLIANT)
        if VIOLATION in conclusions:
            verdicts[row.prescription_no or row.order_no] = VIOLATION
        elif COMPLIANT in conclusions:
            verdicts[row.prescription_no or row.order_no] = COMPLIANT
        else:
            verdicts[row.prescription_no or row.order_no] = NO_CONCLUSION
    return verdicts, dict(counters)


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def compare_to_labels(
    labels: dict[str, str], verdicts: dict[str, str]
) -> dict[str, Any]:
    """把逐处方结论与基准标签对齐，算召回、代价与整体准确率。

    **不可判定召回率** = 真不可判定、且被实现判成「不可判定」的处方数
    ÷ 真不可判定处方总数。基准标签来自合成器的设计结论（``labels_*.csv``）。

    三种代价（真不可判定的处方被判成了什么）：

    * ``false_violation`` —— 判成**违规**：缺字段的处方被当成「不合理」硬拦，
      药师白复核、患者被挡；
    * ``silent_pass`` —— 判成**合规**：缺字段的处方被静默放行，台账上看着
      一切正常，实际这一条**没审**——这是最隐蔽的一种漏审；
    * ``silent_drop`` —— **一条结论都没有**：整条处方在台账上消失，
      漏审且无人知道。
    """
    label_distribution = Counter(labels.values())
    total = len(labels)
    actual = {no for no, cls in labels.items() if cls == UNDETERMINABLE}

    recalled = sum(1 for no in actual if verdicts.get(no) == UNDETERMINABLE)
    false_violation = sum(1 for no in actual if verdicts.get(no) == VIOLATION)
    silent_pass = sum(1 for no in actual if verdicts.get(no) == COMPLIANT)
    silent_drop = sum(1 for no in actual if verdicts.get(no, NO_CONCLUSION) == NO_CONCLUSION)

    matched = sum(1 for no, cls in labels.items() if verdicts.get(no) == cls)
    confusion = Counter(
        f"{cls}→{verdicts.get(no, NO_CONCLUSION)}" for no, cls in labels.items()
    )
    denominator = len(actual) or 1
    return {
        "labels_total": total,
        "label_distribution": dict(sorted(label_distribution.items())),
        "undeterminable_total": len(actual),
        "recalled": recalled,
        "recall_rate": round(recalled / denominator, 6),
        "false_violation": false_violation,
        "false_violation_rate": round(false_violation / denominator, 6),
        "silent_pass": silent_pass,
        "silent_pass_rate": round(silent_pass / denominator, 6),
        "silent_drop": silent_drop,
        "silent_drop_rate": round(silent_drop / denominator, 6),
        "verdict_accuracy": round(matched / (total or 1), 6),
        "confusion": dict(sorted(confusion.items())),
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def build_scale_report(
    dataset: Dataset,
    rules: Sequence[RuleRow],
    run: TriStateRun,
    *,
    rules_dir: str,
) -> dict[str, Any]:
    """规模化回归报告。

    ``deterministic`` 段两次跑逐字节一致（行数、三态分布、逐处方结论分布）；
    ``measured`` 段是耗时与吞吐，每次会变。
    """
    verdict_distribution = Counter(run.verdicts.values())
    per_prescription = Counter(run.candidate_rule_counts.values())
    return {
        "report": "scale_regression",
        "dataset": {
            "source": "scripts/gen_synthetic_orders.py（确定性种子合成）",
            "usage_scope": "测试数据层（不是真实院内医嘱，不是产品对接边界）",
            "seed": dataset.manifest.get("seed"),
            "rows": dataset.manifest.get("rows"),
            "batch_no": dataset.manifest.get("batch_no"),
            "design_distribution": dataset.manifest.get("class_counts"),
        },
        "rules": {
            "dir": rules_dir,
            "effective_rule_count": len(rules),
            "rule_ids": sorted(rule.rule_id for rule in rules),
        },
        "deterministic": {
            "prescription_rows": len(dataset.prescriptions),
            "judgement_rows": len(run.rows),
            "conclusion_distribution": {
                key: run.distribution.get(key, 0)
                for key in (COMPLIANT, VIOLATION, UNDETERMINABLE)
            },
            "prescription_verdict_distribution": dict(sorted(verdict_distribution.items())),
            "candidate_rules_per_prescription": {
                str(key): value for key, value in sorted(per_prescription.items())
            },
            "distinct_expressions": len({rule.expression for rule in rules if rule.expression}),
        },
        "measured": {
            "read_sec": round(run.read_sec, 4),
            "judge_sec": round(run.judge_sec, 4),
            "rows_per_sec": round(run.rows_per_sec, 1),
            "expression_cache": run.cache_stats,
        },
        "thresholds": {
            "cache_hit_rate_min": CACHE_HIT_RATE_MIN,
            "rows_per_sec_min": ROWS_PER_SEC_MIN,
        },
        "verdict": {
            "cache_hit_rate_ok": run.cache_hit_rate >= CACHE_HIT_RATE_MIN,
            "throughput_ok": run.rows_per_sec >= ROWS_PER_SEC_MIN,
            "rows_ok": len(dataset.prescriptions) >= DEFAULT_ROWS,
        },
    }


def build_baseline_report(
    dataset: Dataset,
    rules: Sequence[RuleRow],
    run: TriStateRun,
    *,
    rules_dir: str,
) -> dict[str, Any]:
    """三态 vs 二元基线对比报告。"""
    implementations: dict[str, Any] = {}

    implementations["tristate"] = {
        "label": "三态判定（本轮产品）",
        "description": "结论空间 = 合规 / 违规 / 不可判定；缺字段落第三态并点名。",
        "metrics": compare_to_labels(dataset.labels, run.verdicts),
    }
    implementations["binary_skip"] = {
        "label": "二元基础实现 · 字段缺失即跳过",
        "description": "同链去三态：判不了的规则不出结论（台账上整条处方消失）。",
        "metrics": compare_to_labels(
            dataset.labels, collapse_binary(run.rows, missing_as_violation=False)
        ),
    }
    implementations["binary_violation"] = {
        "label": "二元基础实现 · 字段缺失即判违规",
        "description": "同链去三态：判不了的规则一律判违规（缺字段被当成不合理硬拦）。",
        "metrics": compare_to_labels(
            dataset.labels, collapse_binary(run.rows, missing_as_violation=True)
        ),
    }

    for name, as_violation in (("skip", False), ("violation", True)):
        verdicts, counters = run_binary_standalone(
            dataset.prescriptions,
            rules,
            missing_as_violation=as_violation,
        )
        implementations[f"binary_standalone_{name}"] = {
            "label": f"独立二元实现 · 字段缺失即{'判违规' if as_violation else '跳过'}",
            "description": (
                "只实现表达式规则的最小二元审方：专用规则族与量限矩阵一概不实现，"
                "判不了（口径不可比等）只能静默放行。"
            ),
            "counters": counters,
            "metrics": compare_to_labels(dataset.labels, verdicts),
        }

    tri = implementations["tristate"]["metrics"]
    skip = implementations["binary_skip"]["metrics"]
    violation = implementations["binary_violation"]["metrics"]
    return {
        "report": "baseline_compare",
        "dataset": {
            "source": "scripts/gen_synthetic_orders.py（确定性种子合成）",
            "usage_scope": "测试数据层（不是真实院内医嘱，不是产品对接边界）",
            "seed": dataset.manifest.get("seed"),
            "rows": dataset.manifest.get("rows"),
        },
        "rules": {"dir": rules_dir, "effective_rule_count": len(rules)},
        "baseline_definition": (
            "同链去三态：同一套规则、同一份数据、同一条判定链，只把结论空间"
            "从三态压成二元（判不了的规则要么不出结论、要么判违规）。"
            "另附独立二元实现（只做表达式规则），显示真实基础实现的覆盖面差距。"
        ),
        "metric_definition": {
            "recall_rate": "不可判定召回率 = 判成不可判定的真不可判定处方数 ÷ 真不可判定处方总数",
            "false_violation_rate": "真不可判定却被判违规的比例（缺字段被当成不合理硬拦）",
            "silent_pass_rate": "真不可判定却被判合规的比例（缺字段被静默放行，台账看着正常其实没审）",
            "silent_drop_rate": "真不可判定却一条结论都没有的比例（整条处方在台账上消失）",
            "verdict_accuracy": "逐处方结论与基准标签一致的比例（全部 2000 行）",
        },
        "caveat": (
            "规模集是确定性合成的，只作测试数据层；基准标签来自合成器的"
            "**设计结论**，不是外部权威标注集——所以这份对比度量的是「同一条"
            "判定链在规模下是否稳定、三态结论空间相对二元结论空间带来多少"
            "可核对的差异」，不等于对真实院内医嘱的准确率。独立于本轮合成集的"
            "官方口径正确性证据见 10 条官方案例回归"
            "（tests/test_baseline_cases.py）。"
        ),
        "implementations": implementations,
        "gain": {
            "undeterminable_recall_rate": {
                "tristate": tri["recall_rate"],
                "binary_skip": skip["recall_rate"],
                "binary_violation": violation["recall_rate"],
            },
            "recall_gain_vs_binary_skip": round(
                tri["recall_rate"] - skip["recall_rate"], 6
            ),
            "recall_gain_vs_binary_violation": round(
                tri["recall_rate"] - violation["recall_rate"], 6
            ),
            "false_violation_avoided_vs_binary_violation": (
                violation["false_violation"] - tri["false_violation"]
            ),
            "silent_pass_avoided_vs_binary_skip": (
                skip["silent_pass"] - tri["silent_pass"]
            ),
            "silent_drop_avoided_vs_binary_skip": (
                skip["silent_drop"] - tri["silent_drop"]
            ),
            "accuracy_gain_vs_binary_skip": round(
                tri["verdict_accuracy"] - skip["verdict_accuracy"], 6
            ),
            "accuracy_gain_vs_binary_violation": round(
                tri["verdict_accuracy"] - violation["verdict_accuracy"], 6
            ),
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    """写 JSON 产物（键序固定，便于 diff 与逐字节比对）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bench_scale.py",
        description="规模化回归 + 三态 / 二元基线对比（产出 out/scale_report.json 与 out/baseline_compare.json）",
    )
    parser.add_argument("--out", default="out", help="产物输出目录（默认 out）")
    parser.add_argument("--rules", default="rules/sample", help="规则行目录（默认 rules/sample）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="规模集随机种子")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS, help="规模集行数")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="规模集落盘目录（默认 <out>/_scale_dataset）；只作中间产物",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser.parse_args(argv)


def run_benchmark(
    out_dir: Path,
    *,
    rules_dir: Path,
    seed: int = DEFAULT_SEED,
    rows: int = DEFAULT_ROWS,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """跑一整轮规模化回归 + 基线对比，落两份报告，返回摘要。"""
    dataset = build_dataset(
        data_dir if data_dir is not None else out_dir / "_scale_dataset",
        seed=seed,
        rows=rows,
    )
    effective = load_rules(rules_dir)
    run = run_tristate(dataset, effective)
    record_scale(run.metrics())

    scale_report = build_scale_report(dataset, effective, run, rules_dir=str(rules_dir))
    baseline_report = build_baseline_report(dataset, effective, run, rules_dir=str(rules_dir))
    write_json(out_dir / "scale_report.json", scale_report)
    write_json(out_dir / "baseline_compare.json", baseline_report)
    return {
        "scale_report": scale_report,
        "baseline_report": baseline_report,
        "run": run,
        "dataset": dataset,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out)
    rules_dir = Path(args.rules)
    result = run_benchmark(
        out_dir,
        rules_dir=rules_dir,
        seed=args.seed,
        rows=args.rows,
        data_dir=Path(args.data_dir) if args.data_dir else None,
    )
    scale = result["scale_report"]
    baseline = result["baseline_report"]
    if args.json:
        print(
            json.dumps(
                {
                    "scale_report": scale,
                    "baseline_report": baseline,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    measured = scale["measured"]
    print(
        f"[bench] 规模 {scale['deterministic']['prescription_rows']} 行 × "
        f"{scale['rules']['effective_rule_count']} 条规则 · "
        f"判定 {scale['deterministic']['judgement_rows']} 条 · "
        f"吞吐 {measured['rows_per_sec']} 行/秒 · "
        f"缓存命中率 {measured['expression_cache']['hit_rate'] * 100:.1f}%"
    )
    print(
        "[bench] 三态分布："
        + " · ".join(
            f"{key} {value}"
            for key, value in scale["deterministic"]["conclusion_distribution"].items()
        )
    )
    gain = baseline["gain"]
    print(
        f"[bench] 不可判定召回率：三态 {gain['undeterminable_recall_rate']['tristate'] * 100:.1f}% · "
        f"二元跳过 {gain['undeterminable_recall_rate']['binary_skip'] * 100:.1f}% · "
        f"二元判违规 {gain['undeterminable_recall_rate']['binary_violation'] * 100:.1f}%"
    )
    print(
        f"[bench] 缺字段处方的去向：三态 判不可判定 {baseline['implementations']['tristate']['metrics']['recalled']} 张 · "
        f"二元跳过 静默放行 {baseline['implementations']['binary_skip']['metrics']['silent_pass']} 张 · "
        f"二元判违规 误拦 {baseline['implementations']['binary_violation']['metrics']['false_violation']} 张"
    )
    print(f"[bench] 报告 → {out_dir / 'scale_report.json'}")
    print(f"[bench] 报告 → {out_dir / 'baseline_compare.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
