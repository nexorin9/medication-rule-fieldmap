"""判定内核 9/10：严重度 + 点评分级打标（不适宜 / 不规范 / 超常）。

对应任务书 T21。两个问题、两组断言：

1. **严重度从哪来** —— 规则行写了就用规则行的（严重度是药事审核岗的口径，
   程序不替人决定）；规则行没写才按**审核层次**回落，且回落来源必须写进
   **归因文本**——药师要能看出这个严重度是引擎推的，不是审核岗给的。
2. **点评分级打在哪** —— §5 引擎化要点给的三类（不适宜处方 / 不规范处方 /
   超常处方）互斥且穷尽，**只打标不改结论**：打标是给处置流分流用的标签，
   判定结论仍然是合规 / 违规 / 不可判定三态。

口径依据：``../../domain/rules/处方审核规则体系.md`` §4（严重不合理用药与
用药错误的处置档）与 §5（点评分级三类）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.judge import judge_batch, judge_prescription
from med_rule_fieldmap.engine.severity import (
    GRADE_BASIS,
    LAYER_SEVERITY_FALLBACK,
    SEVERITY_GRADE_HINT,
    SEVERITY_NON_STANDARD,
    SEVERITY_SEVERE_UNREASONABLE,
    SEVERITY_SUPERNORMAL,
    SEVERITY_UNREASONABLE,
    Grade,
    classify_grade,
    grade_conflict,
    grade_distribution,
    mark_grade,
    mark_grades,
    render_severity_note,
    resolve_severity,
)
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.models import Conclusion, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"
BATCH_FILE = BATCH_DIR / "demo_batch_001.csv"

AS_OF = date(2026, 10, 28)

SUPERNORMAL_RULE_ID = "RX-APPR-SUPERNORMAL-PRESCRIBING-RIGHT"


@pytest.fixture(scope="module")
def rules():
    repository = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repository.load()
    assert repository.errors == (), "样例规则集不应有载入错误"
    return repository.effective_rules


@pytest.fixture(scope="module")
def prescription_rows():
    return read_prescription_rows(BATCH_FILE)


def _rule(layer: str, severity: str | None = None, rule_id: str = "RX-TEST-0001") -> RuleRow:
    """构造一条最小规则行（只给判定内核看得见的字段）。"""
    return RuleRow(
        rule_id=rule_id,
        layer=layer,
        basis="处方审核规则体系 §1 审核三层次",
        version="2026.10",
        applies_when={},
        required_fields=("days_qty",),
        expression="days_qty > 1",
        severity=severity,
    )


# ---------------------------------------------------------------------------
# 一、严重度：规则行为准
# ---------------------------------------------------------------------------


def test_severity_comes_from_rule_row_when_declared() -> None:
    resolution = resolve_severity(_rule("适宜性", "用药错误"))

    assert resolution.value == "用药错误"
    assert resolution.source.value == "规则行"
    assert resolution.is_fallback is False
    assert resolution.note == ""
    assert render_severity_note(resolution) == ""


@pytest.mark.parametrize(
    ("layer", "expected"),
    [
        ("合法性", SEVERITY_SEVERE_UNREASONABLE),
        ("规范性", SEVERITY_NON_STANDARD),
        ("适宜性", SEVERITY_UNREASONABLE),
    ],
)
def test_severity_falls_back_to_layer_when_missing(layer: str, expected: str) -> None:
    resolution = resolve_severity(_rule(layer, None))

    assert resolution.value == expected
    assert resolution.source.value == "层次回落"
    assert resolution.is_fallback is True
    assert LAYER_SEVERITY_FALLBACK[layer] == expected


def test_fallback_note_names_the_source_and_the_layer() -> None:
    resolution = resolve_severity(_rule("规范性", None))

    note = resolution.note
    assert "回落" in note
    assert "规范性" in note
    assert SEVERITY_NON_STANDARD in note
    assert "severity" in note


def test_unknown_layer_falls_back_without_raising() -> None:
    resolution = resolve_severity(_rule("未声明的层次", None))

    assert resolution.is_fallback is True
    assert resolution.value  # 严重度不留空
    assert "未声明" in resolution.note


def test_layer_alias_is_normalized_before_fallback() -> None:
    """层次写成「合法性审核」这类别名时，回落口径仍然对得上。"""
    resolution = resolve_severity(_rule("合法性审核", None))

    assert resolution.layer == "合法性"
    assert resolution.value == SEVERITY_SEVERE_UNREASONABLE


# ---------------------------------------------------------------------------
# 二、归因文本注明回落来源
# ---------------------------------------------------------------------------


def _sample_row() -> PrescriptionRow:
    return PrescriptionRow(
        batch_no="DEMO-BATCH-001",
        prescription_no="RX-SEV-0001",
        visit_no="V-1",
        drug_code="D-1",
        days_qty="5",
        event_time="2026-10-28 09:00",
    )


def test_reason_notes_severity_fallback_source() -> None:
    rule = _rule("规范性", None)
    rows = judge_prescription(_sample_row(), [rule])

    assert len(rows) == 1
    row = rows[0]
    assert row.severity == SEVERITY_NON_STANDARD
    assert "回落" in row.reason
    assert "规范性" in row.reason
    assert SEVERITY_NON_STANDARD in row.reason


def test_reason_has_no_fallback_note_when_rule_declares_severity() -> None:
    rule = _rule("规范性", SEVERITY_NON_STANDARD)
    rows = judge_prescription(_sample_row(), [rule])

    assert len(rows) == 1
    assert rows[0].severity == SEVERITY_NON_STANDARD
    assert "回落" not in rows[0].reason


def test_fallback_note_does_not_change_the_conclusion() -> None:
    """同一条规则，带 / 不带 severity 两种写法，结论必须一模一样。"""
    with_severity = judge_prescription(_sample_row(), [_rule("规范性", SEVERITY_NON_STANDARD)])
    without_severity = judge_prescription(_sample_row(), [_rule("规范性", None)])

    assert with_severity[0].conclusion is without_severity[0].conclusion
    assert with_severity[0].conclusion is Conclusion.VIOLATION
    assert with_severity[0].severity == without_severity[0].severity


def test_sample_rule_set_never_falls_back() -> None:
    """样例规则集每条都显式写了严重度 → 判定链不该出现回落说明。"""
    repository = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repository.load()
    for rule in repository.effective_rules:
        resolution = resolve_severity(rule)
        assert resolution.is_fallback is False, f"{rule.rule_id} 缺严重度"
        assert resolution.value == rule.severity


# ---------------------------------------------------------------------------
# 三、点评分级：三类互斥且穷尽
# ---------------------------------------------------------------------------


def test_grade_has_exactly_three_classes() -> None:
    assert [member.value for member in Grade] == ["不适宜处方", "不规范处方", "超常处方"]
    assert GRADE_BASIS


def test_three_grades_are_mutually_exclusive_and_exhaustive(rules) -> None:
    """每条规则只归一类；三类合起来覆盖全部规则（互斥 + 穷尽）。"""
    markings = mark_grades(rules)

    assert len(markings) == len(rules)
    for rule in rules:
        grade = classify_grade(rule)
        assert isinstance(grade, Grade)
        # 互斥：一条规则只能有一个归类结果（打标不产生第二个标签）。
        assert markings[rule.rule_id].grade is grade

    distribution = grade_distribution(rules)
    assert set(distribution) == {member.value for member in Grade}
    assert sum(distribution.values()) == len(rules)
    # 穷尽：样例规则集覆盖三层，三类都必须出现。
    assert distribution[Grade.INAPPROPRIATE.value] > 0
    assert distribution[Grade.NON_STANDARD.value] > 0
    assert distribution[Grade.SUPERNORMAL.value] > 0


def test_supernormal_rule_is_graded_as_supernormal(rules) -> None:
    supernormal = next(rule for rule in rules if rule.rule_id == SUPERNORMAL_RULE_ID)

    assert supernormal.layer == "适宜性"
    assert classify_grade(supernormal) is Grade.SUPERNORMAL
    assert mark_grade(supernormal).value == "超常处方"


def test_normativity_rules_are_graded_as_non_standard(rules) -> None:
    normativity = [rule for rule in rules if rule.layer == "规范性"]

    assert normativity
    for rule in normativity:
        assert classify_grade(rule) is Grade.NON_STANDARD


def test_legality_and_other_appropriateness_rules_are_graded_as_inappropriate(rules) -> None:
    others = [
        rule
        for rule in rules
        if rule.layer in ("合法性", "适宜性") and rule.rule_id != SUPERNORMAL_RULE_ID
    ]

    assert others
    for rule in others:
        assert classify_grade(rule) is Grade.INAPPROPRIATE


def test_grade_marking_carries_basis_and_rule_id(rules) -> None:
    marking = mark_grade(next(iter(rules)))

    assert marking.rule_id
    assert marking.basis == GRADE_BASIS
    assert "§5" in marking.basis
    assert marking.to_dict()["grade"] in {member.value for member in Grade}


def test_grade_conflict_is_reported_when_severity_disagrees() -> None:
    """严重度写「超常处方」但规则形态不是超常处方 → 口径漂移要说出来。"""
    conflicted = _rule("规范性", SEVERITY_SUPERNORMAL)

    conflict = grade_conflict(conflicted)
    assert conflict is not None
    assert conflicted.rule_id in conflict
    assert SEVERITY_SUPERNORMAL in conflict

    # 严重度与分级本来就一致时不报。
    assert grade_conflict(_rule("规范性", SEVERITY_NON_STANDARD)) is None
    assert grade_conflict(_rule("合法性", SEVERITY_SEVERE_UNREASONABLE)) is None


def test_severity_grade_hint_covers_only_the_two_named_severities() -> None:
    assert set(SEVERITY_GRADE_HINT) == {SEVERITY_SUPERNORMAL, SEVERITY_NON_STANDARD}


# ---------------------------------------------------------------------------
# 四、打标不改结论
# ---------------------------------------------------------------------------


def test_grading_does_not_change_any_conclusion(rules, prescription_rows) -> None:
    """打标前后逐处方 × 逐规则的结论字段逐条相同。"""
    before = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")

    markings = mark_grades(rules)
    assert markings

    after = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")

    assert len(before.rows) == len(after.rows)
    for old, new in zip(before.rows, after.rows, strict=True):
        assert old.conclusion is new.conclusion
        assert old.rule_id == new.rule_id
        assert old.prescription_no == new.prescription_no
        assert old.severity == new.severity
        assert old.reason == new.reason


def test_grading_does_not_mutate_the_rule_row(rules) -> None:
    rule = next(iter(rules))
    before = (rule.severity, rule.layer, rule.rule_id)

    mark_grade(rule)

    assert (rule.severity, rule.layer, rule.rule_id) == before


def test_marking_is_additive_and_keeps_the_three_state_conclusions(
    rules, prescription_rows
) -> None:
    """打标是标签：判定结论仍只有合规 / 违规 / 不可判定三态。"""
    rows = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001").rows

    assert rows
    assert {row.conclusion for row in rows} <= set(Conclusion)
    # 打标不新增第四种结论：三类点评分级与三态结论互不相干。
    assert {member.value for member in Grade}.isdisjoint(
        {member.value for member in Conclusion}
    )
