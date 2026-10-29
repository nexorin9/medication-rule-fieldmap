"""三态判定 vs 二元结论基础实现（任务书 T39 · 数据层 4/4 · 2/2）。

对应任务书要求：以二元结论基础实现（字段缺失即判违规或直接跳过）为基线，
对比三态判定在不可判定召回率上的增益，产出可复现的对比数值并写入 ``out/``。

**基线怎么定，决定这份对比可不可信**。如果基线是「随便写个简化版」，
测出来的是规则覆盖面差距，不是第三态的价值。所以基线取**同链去三态**：
同一套规则、同一份数据、同一条判定链，只把结论空间从三态压成二元——
「不可判定」在二元语义下要么不出结论（字段缺失即跳过）、要么判违规
（字段缺失即违规）。另附一组**独立二元实现**（只做表达式规则的最小二元
审方），用来显示真实基础实现除结论空间外还窄在哪。

**这套数字说明什么**：真正不可判定的 300 张处方（合成器设计标签里标着的
「缺字段」那批），三态判定全部进「不可判定」通道——信息科据此排接口补齐
顺序、药师据此人工复核；二元跳过把它们全部静默放行成「合规」（台账看着
一切正常，其实这批一条没审），二元判违规把它们全部硬拦（药师白复核、
患者被挡）。两种代价都不是「少报一点」，是**结论方向就错了**。

**数据来源边界**：规模集是确定性合成的，只作测试数据层；基准标签是合成器
的**设计结论**，不是外部权威标注集，因此这份对比度量的是「同一条判定链在
规模下是否稳定、三态相对二元的差异有多大」，不等于对真实院内医嘱的准确率。
独立于合成集的官方口径正确性证据见 10 条官方案例回归
（``tests/test_baseline_cases.py``）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "bench_scale.py"
RULES_DIR = PROJECT_ROOT / "rules" / "sample"

_spec = importlib.util.spec_from_file_location("bench_scale", SCRIPT_PATH)
assert _spec and _spec.loader
bench = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bench
_spec.loader.exec_module(bench)


@pytest.fixture(scope="module")
def compare_run(tmp_path_factory):
    """跑一遍规模化回归 + 基线对比，返回对比报告与产物路径。"""
    out_dir = tmp_path_factory.mktemp("compare_out")
    data_dir = tmp_path_factory.mktemp("compare_data")
    result = bench.run_benchmark(
        out_dir, rules_dir=RULES_DIR, data_dir=data_dir
    )
    return {
        "result": result,
        "out_dir": out_dir,
        "report": result["baseline_report"],
        "run": result["run"],
    }


def _metrics(report: dict, name: str) -> dict:
    return report["implementations"][name]["metrics"]


# ---------------------------------------------------------------------------
# 不可判定召回率
# ---------------------------------------------------------------------------
def test_真不可判定处方共300张(compare_run):
    metrics = _metrics(compare_run["report"], "tristate")
    assert metrics["undeterminable_total"] == 300
    assert metrics["labels_total"] == 2000
    assert metrics["label_distribution"] == {"合规": 1200, "违规": 500, "不可判定": 300}


def test_三态判定把不可判定的全部召回(compare_run):
    metrics = _metrics(compare_run["report"], "tristate")
    assert metrics["recalled"] == 300
    assert metrics["recall_rate"] == 1.0
    # 代价必须同时为零：既不误拦、也不静默放行。
    assert metrics["false_violation"] == 0
    assert metrics["silent_pass"] == 0
    assert metrics["silent_drop"] == 0


def test_二元基线的不可判定召回率为零(compare_run):
    """二元结论空间里根本没有「判不了」这个位置，召回必然为 0。"""
    report = compare_run["report"]
    for name in ("binary_skip", "binary_violation"):
        metrics = _metrics(report, name)
        assert metrics["recalled"] == 0
        assert metrics["recall_rate"] == 0.0


def test_召回增益为满分(compare_run):
    gain = compare_run["report"]["gain"]
    assert gain["recall_gain_vs_binary_skip"] == 1.0
    assert gain["recall_gain_vs_binary_violation"] == 1.0
    assert gain["undeterminable_recall_rate"]["tristate"] == 1.0


# ---------------------------------------------------------------------------
# 二元实现的两种代价
# ---------------------------------------------------------------------------
def test_二元跳过的代价是静默放行(compare_run):
    """字段缺失即跳过 → 这批处方在台账上判成「合规」，看着正常其实一条没审。"""
    metrics = _metrics(compare_run["report"], "binary_skip")
    assert metrics["silent_pass"] == 300
    assert metrics["silent_pass_rate"] == 1.0
    assert metrics["false_violation"] == 0
    assert metrics["confusion"]["不可判定→合规"] == 300


def test_二元判违规的代价是误拦(compare_run):
    """字段缺失即违规 → 缺字段被当成「不合理」硬拦，药师白复核、患者被挡。"""
    metrics = _metrics(compare_run["report"], "binary_violation")
    assert metrics["false_violation"] == 300
    assert metrics["false_violation_rate"] == 1.0
    assert metrics["silent_pass"] == 0
    assert metrics["confusion"]["不可判定→违规"] == 300


def test_三态把两种代价都消掉(compare_run):
    gain = compare_run["report"]["gain"]
    assert gain["false_violation_avoided_vs_binary_violation"] == 300
    assert gain["silent_pass_avoided_vs_binary_skip"] == 300


def test_三态的混淆矩阵里不可判定只落不可判定(compare_run):
    confusion = _metrics(compare_run["report"], "tristate")["confusion"]
    assert confusion["不可判定→不可判定"] == 300
    assert "不可判定→合规" not in confusion
    assert "不可判定→违规" not in confusion
    # 其余两类也各归其位。
    assert confusion["合规→合规"] == 1200
    assert confusion["违规→违规"] == 500


# ---------------------------------------------------------------------------
# 整体准确率
# ---------------------------------------------------------------------------
def test_三态逐处方准确率高于两个二元变体(compare_run):
    report = compare_run["report"]
    tri = _metrics(report, "tristate")["verdict_accuracy"]
    assert tri == 1.0
    for name in ("binary_skip", "binary_violation"):
        assert _metrics(report, name)["verdict_accuracy"] < tri
    gain = report["gain"]
    assert gain["accuracy_gain_vs_binary_skip"] == 0.15
    assert gain["accuracy_gain_vs_binary_violation"] == 0.15


def test_独立二元实现的覆盖面更窄且准确率更低(compare_run):
    """真实基础实现除了表达不了第三态，规则覆盖面也更窄（专用规则族一概没做）。"""
    report = compare_run["report"]
    tri = _metrics(report, "tristate")["verdict_accuracy"]
    for name in ("binary_standalone_skip", "binary_standalone_violation"):
        implementation = report["implementations"][name]
        assert implementation["counters"]["skipped_special_family"] > 0
        assert implementation["metrics"]["verdict_accuracy"] < tri
    # 独立实现同样表达不了第三态：召回同样为 0。
    assert _metrics(report, "binary_standalone_skip")["recall_rate"] == 0.0
    assert _metrics(report, "binary_standalone_violation")["recall_rate"] == 0.0


# ---------------------------------------------------------------------------
# 产物落盘与可复现
# ---------------------------------------------------------------------------
def test_基线对比报告落盘且结构完整(compare_run):
    path = compare_run["out_dir"] / "baseline_compare.json"
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["report"] == "baseline_compare"
    for key in (
        "dataset",
        "rules",
        "baseline_definition",
        "metric_definition",
        "caveat",
        "implementations",
        "gain",
    ):
        assert key in payload
    assert set(payload["implementations"]) == {
        "tristate",
        "binary_skip",
        "binary_violation",
        "binary_standalone_skip",
        "binary_standalone_violation",
    }


def test_对比数值可复现(compare_run, tmp_path):
    """换目录重跑，整份对比报告（除 measured 段外）逐字节一致。"""
    repeat_dir = tmp_path / "repeat"
    data_dir = tmp_path / "repeat_data"
    result = bench.run_benchmark(
        repeat_dir, rules_dir=RULES_DIR, data_dir=data_dir
    )
    first = compare_run["report"]
    second = result["baseline_report"]
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )


def test_报告写清基线定义与口径边界(compare_run):
    report = compare_run["report"]
    assert "结论空间" in report["baseline_definition"]
    assert "测试数据层" in report["caveat"]
    assert "recall_rate" in report["metric_definition"]
    assert "false_violation_rate" in report["metric_definition"]
    assert "silent_pass_rate" in report["metric_definition"]
