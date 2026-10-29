"""规模化回归（任务书 T39 · 数据层 4/4 · 1/2）。

对应任务书要求：以 2000 行 × 规则集跑通，断言三态分布稳定、解析树缓存
命中率不低于 90%、吞吐不低于 200 行/秒，三项指标写入运行摘要。

**为什么这三项都要有数**：判定内核的复杂度不在「能不能跑」，而在「跑起来
还稳不稳」。规则族从 12 条长到 19 条、医嘱行从 26 行长到 2000 行之后，
最容易悄悄坏掉的三件事是——表达式被反复解析（吞吐塌了）、解析树缓存失效
（命中率塌了）、结论分布悄悄漂移（合规变多、不可判定变少，说明某一族规则
失灵了）。这三样都不报错，只会「变慢」「变少」，所以必须每轮都量。

**规模集是合成的**：数据来自 ``scripts/gen_synthetic_orders.py``（固定种子，
同种子逐字节可复现），**只作测试数据层**，不是真实院内医嘱、也不是产品
对接边界。正式对接口径见 ``spec.md`` §2.1（入站）与 §2.3（出站）。
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
# dataclass 在定义时按 ``__module__`` 回查命名空间，模块必须先进 sys.modules。
sys.modules[_spec.name] = bench
_spec.loader.exec_module(bench)


@pytest.fixture(scope="module")
def scale_run(tmp_path_factory):
    """跑一遍规模化回归，返回报告、判定结果与运行摘要快照。"""
    out_dir = tmp_path_factory.mktemp("scale_out")
    data_dir = tmp_path_factory.mktemp("scale_data")
    result = bench.run_benchmark(
        out_dir, rules_dir=RULES_DIR, data_dir=data_dir
    )
    # 运行摘要就在跑完之后这一刻取：别的测试可能重置进程级摘要。
    summary = bench.current_summary()
    return {
        "result": result,
        "out_dir": out_dir,
        "report": result["scale_report"],
        "run": result["run"],
        "summary_scale": summary.scale,
        "summary_dict": bench.summary_snapshot(),
    }


@pytest.fixture(scope="module")
def scale_run_repeat(tmp_path_factory):
    """再跑一遍（换目录、换缓存），用来核对可复现性。"""
    out_dir = tmp_path_factory.mktemp("scale_out_repeat")
    data_dir = tmp_path_factory.mktemp("scale_data_repeat")
    result = bench.run_benchmark(
        out_dir, rules_dir=RULES_DIR, data_dir=data_dir
    )
    return result["scale_report"]


# ---------------------------------------------------------------------------
# 规模与口径
# ---------------------------------------------------------------------------
def test_规模集行数为2000(scale_run):
    report = scale_run["report"]
    assert report["deterministic"]["prescription_rows"] == 2000
    assert report["dataset"]["rows"] == 2000
    assert report["dataset"]["usage_scope"] == "测试数据层（不是真实院内医嘱，不是产品对接边界）"


def test_生效规则集不少于12条且逐条列明(scale_run):
    """计划书写「12 条规则」；实现期各规则族陆续补齐，实际生效条数只会更多。"""
    report = scale_run["report"]
    count = report["rules"]["effective_rule_count"]
    assert count >= 12
    assert len(report["rules"]["rule_ids"]) == count
    assert len(set(report["rules"]["rule_ids"])) == count


def test_规则层次覆盖三层(scale_run):
    """规则集覆盖合法性 / 规范性 / 适宜性三层（《处方审核规则体系》§1）。"""
    rules = scale_run["result"]["run"]
    assert rules.rule_count >= 12
    # run 对象不携带层次，逐条回读规则集。
    from med_rule_fieldmap.rules.repository import RuleRepository

    repository = RuleRepository(RULES_DIR)
    repository.load()
    layers = {rule.layer for rule in repository.effective_rules}
    assert {"合法性", "规范性", "适宜性"} <= layers


def test_逐处方乘逐规则恰好一行(scale_run):
    report = scale_run["report"]
    run = scale_run["run"]
    expected = sum(run.candidate_rule_counts.values())
    assert report["deterministic"]["judgement_rows"] == expected
    assert len(run.rows) == expected
    # 同一 (处方, 规则) 不重复。
    keys = {(row.prescription_no, row.rule_id) for row in run.rows}
    assert len(keys) == len(run.rows)
    # 每张处方都出了结论，没有哪张被漏掉。
    assert len(run.candidate_rule_counts) == 2000


# ---------------------------------------------------------------------------
# 三态分布稳定
# ---------------------------------------------------------------------------
def test_三态分布与逐条判定口径一致(scale_run):
    report = scale_run["report"]
    distribution = report["deterministic"]["conclusion_distribution"]
    assert set(distribution) == {"合规", "违规", "不可判定"}
    assert sum(distribution.values()) == report["deterministic"]["judgement_rows"]
    assert distribution["合规"] > 0
    assert distribution["违规"] > 0
    assert distribution["不可判定"] > 0


def test_两次跑三态分布逐项一致(scale_run, scale_run_repeat):
    """规模集同种子 + 判定链确定性 → 分布必须逐项相同（漂了就说明有隐藏状态）。"""
    first = scale_run["report"]["deterministic"]
    second = scale_run_repeat["deterministic"]
    assert first["conclusion_distribution"] == second["conclusion_distribution"]
    assert first["judgement_rows"] == second["judgement_rows"]
    assert first["prescription_verdict_distribution"] == second[
        "prescription_verdict_distribution"
    ]
    assert first["candidate_rules_per_prescription"] == second[
        "candidate_rules_per_prescription"
    ]


def test_逐处方结论分布与设计配比吻合(scale_run):
    """合成器的设计配比是 合规 1200 / 违规 500 / 不可判定 300。"""
    verdicts = scale_run["report"]["deterministic"]["prescription_verdict_distribution"]
    assert verdicts["合规"] == 1200
    assert verdicts["违规"] == 500
    assert verdicts["不可判定"] == 300


# ---------------------------------------------------------------------------
# 缓存命中率与吞吐
# ---------------------------------------------------------------------------
def test_解析树缓存命中率不低于90(scale_run):
    cache = scale_run["report"]["measured"]["expression_cache"]
    assert cache["hit_rate"] >= bench.CACHE_HIT_RATE_MIN
    assert cache["misses"] == cache["parses"]
    assert cache["evictions"] == 0
    assert cache["size"] <= cache["max_size"] == 512


def test_吞吐不低于200行每秒(scale_run):
    measured = scale_run["report"]["measured"]
    assert measured["rows_per_sec"] >= bench.ROWS_PER_SEC_MIN
    assert measured["judge_sec"] > 0


def test_报告自带的阈值判定为真(scale_run):
    verdict = scale_run["report"]["verdict"]
    assert verdict["cache_hit_rate_ok"] is True
    assert verdict["throughput_ok"] is True
    assert verdict["rows_ok"] is True


# ---------------------------------------------------------------------------
# 三项指标写入运行摘要
# ---------------------------------------------------------------------------
def test_三项指标写入运行摘要(scale_run):
    scale = scale_run["summary_scale"]
    assert scale is not None
    assert scale.rows == 2000
    assert scale.rows_per_sec >= bench.ROWS_PER_SEC_MIN
    assert scale.cache_hit_rate >= bench.CACHE_HIT_RATE_MIN
    assert scale.compliant > 0 and scale.violation > 0 and scale.undeterminable > 0
    assert scale.judgement_rows == scale_run["report"]["deterministic"]["judgement_rows"]


def test_运行摘要快照带规模化指标段(scale_run):
    payload = scale_run["summary_dict"]
    assert payload["scale"] is not None
    assert payload["scale"]["rows"] == 2000
    assert payload["scale"]["rows_per_sec"] >= bench.ROWS_PER_SEC_MIN
    assert "expression_cache" in payload


# ---------------------------------------------------------------------------
# 产物落盘
# ---------------------------------------------------------------------------
def test_规模报告落盘且结构完整(scale_run):
    path = scale_run["out_dir"] / "scale_report.json"
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["report"] == "scale_regression"
    for key in ("dataset", "rules", "deterministic", "measured", "thresholds", "verdict"):
        assert key in payload
    assert payload["thresholds"]["cache_hit_rate_min"] == bench.CACHE_HIT_RATE_MIN
    assert payload["thresholds"]["rows_per_sec_min"] == bench.ROWS_PER_SEC_MIN


def test_报告可复现段两次跑逐字节一致(scale_run, scale_run_repeat):
    """deterministic 段不含耗时，两次跑必须完全相同（measured 段每次会变）。"""
    first = json.dumps(
        scale_run["report"]["deterministic"], ensure_ascii=False, sort_keys=True
    )
    second = json.dumps(
        scale_run_repeat["deterministic"], ensure_ascii=False, sort_keys=True
    )
    assert first == second
