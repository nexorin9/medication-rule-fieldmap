"""受限表达式解析树缓存的测试。

覆盖三条主线：

1. **同一表达式重复求值只解析一次** —— 命中率是本轮判定吞吐的关键指标；
2. **规则文本变更后语义立即变化** —— 旧解析树必须当场作废，不能等 LRU 淘汰；
3. **LRU 超上限按最近最少使用淘汰** —— 缓存上限 512 对齐源产品。

另附两条工程口径：缓存**不吞**配置错误（解析失败的表达式不进缓存）、
运行摘要带得上缓存指标（CLI status 与规模化回归要读它）。
"""

from __future__ import annotations

import threading

import pytest

from med_rule_fieldmap.expression.cache import (
    PARSE_TREE_CACHE_MAX_SIZE,
    ParseTreeCache,
    expression_digest,
)
from med_rule_fieldmap.expression.errors import ParseError
from med_rule_fieldmap.expression.evaluator import evaluate_expression
from med_rule_fieldmap.expression.parser import parse
from med_rule_fieldmap.run_summary import (
    current_summary,
    record_batch,
    reset_summary,
    summary_snapshot,
)
from med_rule_fieldmap.rules.repository import RuleRepository

#: 一条合规的处方上下文（够求值用）。
CONTEXT = {"days_qty": "3", "skin_test_result": "阴性", "dosage_value": "5"}


def _counting_parser(calls: list[str]):
    """返回一个记录调用次数的解析器（协议：``(文本, token 或 None) -> AST``）。"""

    def parser(text: str, tokens=None):
        calls.append(text)
        return parse(text, tokens=tokens)

    return parser


def _rule_yaml(rule_id: str, expression: str, version: str = "2026.10") -> str:
    """一条最小可载入的规则行（字段依赖与表达式对得上）。"""
    return (
        f"rule_id: {rule_id}\n"
        "layer: 适宜性\n"
        "basis: 处方审核规则体系 §1 适宜性审核第 3 项（剂量、用法的正确性）\n"
        f'version: "{version}"\n'
        "effective_from: \"2026-01-01\"\n"
        "effective_to: null\n"
        "applies_when: {}\n"
        "required_fields: [days_qty]\n"
        f'expression: "{expression}"\n'
        "severity: 用药错误\n"
    )


# ---------------------------------------------------------------------------
# 1. 重复求值只解析一次
# ---------------------------------------------------------------------------


def test_repeated_evaluation_parses_once_and_hit_rate_above_99_percent():
    """同一表达式重复求值 100 次：解析只发生 1 次，命中率不低于 99%。"""
    calls: list[str] = []
    cache = ParseTreeCache(parser=_counting_parser(calls))

    for _ in range(100):
        result = evaluate_expression("days_qty > 1", CONTEXT, cache=cache)
        assert result.is_true

    assert calls == ["days_qty > 1"], "100 次求值只应解析一次"
    stats = cache.stats
    assert stats.parses == 1
    assert stats.misses == 1
    assert stats.hits == 99
    assert stats.hit_rate >= 0.99


def test_different_expressions_are_cached_separately():
    """不同表达式各解析一次，互不串味。"""
    calls: list[str] = []
    cache = ParseTreeCache(parser=_counting_parser(calls))

    evaluate_expression("days_qty > 1", CONTEXT, cache=cache)
    evaluate_expression("days_qty > 5", CONTEXT, cache=cache)
    evaluate_expression("dosage_value <= 0", CONTEXT, cache=cache)

    assert sorted(calls) == ["days_qty > 1", "days_qty > 5", "dosage_value <= 0"]
    assert len(cache) == 3
    assert cache.stats.parses == 3


def test_cache_does_not_swallow_configuration_errors():
    """解析失败的表达式不进缓存——配置错误必须在载入期暴露，不能被缓存吞掉。"""
    calls: list[str] = []
    cache = ParseTreeCache(parser=_counting_parser(calls))

    with pytest.raises(ParseError):
        cache.get("days_qty >")

    assert len(cache) == 0
    assert cache.stats.parses == 0
    assert cache.stats.misses == 1


# ---------------------------------------------------------------------------
# 2. 规则文本变更 → 语义立即变化
# ---------------------------------------------------------------------------


def test_rule_text_change_changes_semantics_immediately():
    """改规则文本后语义立即变化，旧解析树当场作废。"""
    cache = ParseTreeCache()
    context = {"days_qty": "3"}

    before = evaluate_expression("days_qty > 1", context, cache=cache)
    cache.get_for_rule("RX-A", "days_qty > 1")
    assert before.is_true

    # 药事审核岗把这条规则的阈值从 1 改到 5
    after = evaluate_expression("days_qty > 5", context, cache=cache)
    cache.get_for_rule("RX-A", "days_qty > 5")

    assert before.is_true and after.is_false, "改文本后结论必须随之改变"
    assert cache.stats.invalidations == 1, "旧解析树应被显式作废"
    assert cache.stats.parses == 2


def test_invalidated_tree_is_not_reused():
    """旧解析树作废后再取同一段文本，必须重新解析（不等 LRU 自然淘汰）。"""
    cache = ParseTreeCache()
    cache.get_for_rule("RX-A", "days_qty > 1")
    cache.get_for_rule("RX-A", "days_qty > 5")

    misses_before = cache.stats.misses
    cache.get("days_qty > 1")
    assert cache.stats.misses == misses_before + 1, "作废后不得复用旧树"


def test_shared_expression_survives_one_rule_change():
    """两条规则共用同一段表达式文本时，其中一条改文本不动另一条的树。"""
    cache = ParseTreeCache()
    cache.get_for_rule("RX-A", "days_qty > 1")
    cache.get_for_rule("RX-B", "days_qty > 1")
    assert len(cache) == 1

    cache.get_for_rule("RX-A", "days_qty > 5")

    assert "days_qty > 1" in cache, "RX-B 仍在用的解析树不能被作废"
    misses_before = cache.stats.misses
    cache.get("days_qty > 1")
    assert cache.stats.misses == misses_before, "RX-B 的树应当直接命中"


def test_rule_binding_key_is_rule_id_plus_content_hash():
    """绑定键 = 规则号 + 表达式内容哈希；同文本不同规则各自成键。"""
    digest = expression_digest("days_qty > 1")
    assert digest == expression_digest("  days_qty > 1  ")
    assert digest != expression_digest("days_qty > 5")

    cache = ParseTreeCache()
    cache.get_for_rule("RX-A", "days_qty > 1")
    cache.get_for_rule("RX-B", "days_qty > 1")
    assert cache.bound_rule_ids() == ("RX-A", "RX-B")


def test_removed_rule_releases_its_tree():
    """规则下线（解绑）后，它独占的解析树被作废。"""
    cache = ParseTreeCache()
    cache.get_for_rule("RX-A", "days_qty > 1")

    assert cache.invalidate_rule("RX-A") is True
    assert cache.stats.invalidations == 1
    assert len(cache) == 0
    assert cache.invalidate_rule("RX-A") is False


# ---------------------------------------------------------------------------
# 3. LRU 淘汰
# ---------------------------------------------------------------------------


def test_lru_evicts_least_recently_used_above_max_size():
    """超出上限 512 时按最近最少使用淘汰。"""
    cache = ParseTreeCache()
    assert cache.max_size == PARSE_TREE_CACHE_MAX_SIZE == 512

    for index in range(PARSE_TREE_CACHE_MAX_SIZE):
        cache.get(f"days_qty > {index}")
    assert len(cache) == PARSE_TREE_CACHE_MAX_SIZE
    assert cache.stats.evictions == 0

    # 命中最早那条，把它挪到"最近使用"
    cache.get("days_qty > 0")
    # 再加一条新的 → 淘汰现在最久未用的那条（days_qty > 1）
    cache.get(f"days_qty > {PARSE_TREE_CACHE_MAX_SIZE}")
    assert cache.stats.evictions == 1
    assert len(cache) == PARSE_TREE_CACHE_MAX_SIZE

    # 被淘汰的那条需要重新解析
    misses_before = cache.stats.misses
    cache.get("days_qty > 1")
    assert cache.stats.misses == misses_before + 1

    # 刚用过的还在缓存里
    misses_before = cache.stats.misses
    cache.get("days_qty > 0")
    assert cache.stats.misses == misses_before


def test_lru_eviction_prefers_least_recently_used_not_first_inserted():
    """淘汰的是最近最少使用，不是先进先出。"""
    cache = ParseTreeCache(max_size=3)
    for index in range(3):
        cache.get(f"days_qty > {index}")

    cache.get("days_qty > 0")  # 刷新第 0 条 → 它变成最近使用
    cache.get("days_qty > 3")  # 触发淘汰

    assert "days_qty > 0" in cache
    assert "days_qty > 1" not in cache, "最久未用的应是 days_qty > 1"


# ---------------------------------------------------------------------------
# 4. 运行摘要带得上缓存指标
# ---------------------------------------------------------------------------


def test_run_summary_carries_expression_cache_stats():
    """运行摘要里带表达式缓存统计（CLI status 与规模化回归读它）。"""
    reset_summary()
    cache = ParseTreeCache(parser=_counting_parser([]))

    for _ in range(10):
        evaluate_expression("days_qty > 1", CONTEXT, cache=cache)
    record_batch(
        received_rows=10,
        judgement_rows=10,
        counts={"合规": 8, "违规": 1, "不可判定": 1},
    )

    snapshot = summary_snapshot(cache)
    assert snapshot["batches"] == 1
    assert snapshot["prescription_rows"] == 10
    assert snapshot["compliant"] == 8
    assert snapshot["violation"] == 1
    assert snapshot["undeterminable"] == 1

    cache_stats = snapshot["expression_cache"]
    assert cache_stats["hits"] == 9
    assert cache_stats["parses"] == 1
    assert cache_stats["hit_rate"] >= 0.9
    assert "表达式解析缓存" in current_summary().render(cache)


# ---------------------------------------------------------------------------
# 5. 规则仓库热重载 → 旧解析树失效
# ---------------------------------------------------------------------------


def test_repository_reload_invalidates_changed_rule_expression(tmp_path):
    """规则文本改写后重新载入：改过的那条重新解析，没改的直接复用。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "limits.yaml").write_text(
        _rule_yaml("RX-A", "days_qty > 1"), encoding="utf-8"
    )
    (rules_dir / "other.yaml").write_text(
        _rule_yaml("RX-B", "days_qty > 7"), encoding="utf-8"
    )

    calls: list[str] = []
    cache = ParseTreeCache(parser=_counting_parser(calls))
    repository = RuleRepository(rules_dir, cache=cache)

    repository.load()
    assert cache.stats.parses == 2, "两条规则各解析一次"

    # 只改 RX-A 的阈值
    (rules_dir / "limits.yaml").write_text(
        _rule_yaml("RX-A", "days_qty > 5"), encoding="utf-8"
    )
    assert repository.reload_if_changed() is True

    assert cache.stats.invalidations == 1, "RX-A 的旧解析树应被作废"
    # 重新载入时 RX-A 要重新解析（RX-B 命中缓存，不必再解析）
    assert cache.stats.parses == 3
    assert "days_qty > 5" in cache
    assert "days_qty > 1" not in cache


def test_repository_reload_without_change_reuses_cached_trees(tmp_path):
    """规则文件没改时重新载入：不重新解析（热重载不该白白吃 CPU）。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "limits.yaml").write_text(
        _rule_yaml("RX-A", "days_qty > 1"), encoding="utf-8"
    )

    calls: list[str] = []
    cache = ParseTreeCache(parser=_counting_parser(calls))
    repository = RuleRepository(rules_dir, cache=cache)

    repository.load()
    assert cache.stats.parses == 1

    repository.reload_if_changed()
    assert cache.stats.parses == 1, "文本没变就不该重新解析"
    assert cache.stats.invalidations == 0


# ---------------------------------------------------------------------------
# 6. 并发与清理
# ---------------------------------------------------------------------------


def test_cache_is_safe_under_concurrent_access():
    """多线程共用一个缓存：每段表达式只解析一次，缓存不损坏。

    守护进程（T33）会用线程池跑批次，命中后 ``move_to_end`` 与 LRU 淘汰
    都是非原子操作，必须串起来。
    """
    expressions = [f"days_qty > {index}" for index in range(8)]
    calls: list[str] = []
    cache = ParseTreeCache(parser=_counting_parser(calls))

    def worker() -> None:
        for _ in range(50):
            for text in expressions:
                evaluate_expression(text, CONTEXT, cache=cache)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(calls) == sorted(expressions), "每段表达式只应解析一次"
    assert len(cache) == len(expressions)
    assert cache.stats.hits + cache.stats.misses == 4 * 50 * len(expressions)


def test_clear_and_reset_stats():
    """清空内容保留统计；清零统计不动内容。"""
    cache = ParseTreeCache()
    cache.get("days_qty > 1")
    assert len(cache) == 1

    cache.clear()
    assert len(cache) == 0
    assert cache.stats.parses == 1, "清空内容不清统计"

    cache.reset_stats()
    assert cache.stats.parses == 0
