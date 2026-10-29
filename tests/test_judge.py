"""判定内核 2/10：规则调度 + ``required_fields`` 缺失 → 不可判定。

对应任务书 T14。本轮把判定链补成完整形态：

#. **调度**（``engine/sched.py``）：适用条件命中 → 候选规则；不适用 → 不产生
   判定行，不进台账；
#. **依赖检查**：``required_fields`` 里任一字段缺失 → 结论「不可判定」，
   并在 ``missing_fields`` 里点名该字段；
#. **口径检查**：依赖字段落地了但口径不可辨（``days_qty`` 写成 ``3mg``）→
   同样落「不可判定」，不静默判违规；
#. **表达式求值**：字段齐备 → 受限表达式求值得「合规 / 违规」。

覆盖四组硬断言（任务书点名）：

1. ``applies_when`` 字段缺失时规则**不适用**（不判不可判定、无判定行）；
2. ``required_fields`` 缺失 → **不可判定**且 ``missing_fields`` 点名正确；
3. 显式「未注明」与字段为空**产生不同结论**（违规 vs 不可判定）；
4. 归因文本含**规则号**与**依据出处**（basis）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.judge import (
    judge_batch,
    judge_prescription,
    summarize_conclusions,
)
from med_rule_fieldmap.engine.sched import (
    ScheduleStatus,
    candidate_rules,
    match_rule,
    schedule_rules,
    select_candidates,
)
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.models import Conclusion, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"
BATCH_FILE = BATCH_DIR / "demo_batch_001.csv"

AS_OF = date(2026, 10, 28)

BASIS = "处方审核规则体系 §3 特殊药品处方量限"

#: 依赖字段含 ``rx_type`` / ``drug_category`` 的量限规则（适用条件也用这两个字段）。
LIMIT_RULE = RuleRow(
    rule_id="RX-LEGAL-MA-OUT-DAYS",
    layer="合法性",
    basis=BASIS,
    version="2026.10",
    applies_when={"rx_type": "门急诊一般", "drug_category": "麻醉药品注射剂"},
    required_fields=("rx_type", "drug_category", "days_qty"),
    expression="days_qty > 1",
    severity="严重不合理用药",
)

#: 适用条件字段**不在**依赖声明里（缺失时本规则不适用，不判不可判定）。
DECORATION_RULE = RuleRow(
    rule_id="RX-NORM-DECOR",
    layer="规范性",
    basis="处方审核规则体系 §1 规范性审核",
    version="2026.10",
    applies_when={"dept_name": "呼吸内科门诊"},
    required_fields=("clinical_diag",),
    expression="clinical_diag == ''",
    severity="不规范处方",
)

#: 皮试规则（用于「显式未注明 vs 字段为空」的分流断言）。
SKIN_TEST_RULE = RuleRow(
    rule_id="RX-APPR-SKIN-TEST-UNSPECIFIED",
    layer="适宜性",
    basis="处方审核规则体系 §1 适宜性审核第 1 项",
    version="2026.10",
    applies_when={},
    required_fields=("skin_test_result",),
    expression="skin_test_result == '未注明'",
    severity="不合理用药",
)


@pytest.fixture(scope="module")
def rules():
    repository = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repository.load()
    assert repository.errors == (), "样例规则集不应有载入错误"
    return repository.effective_rules


@pytest.fixture(scope="module")
def prescription_rows():
    return read_prescription_rows(BATCH_FILE)


def _limit_row(**overrides: str) -> PrescriptionRow:
    """一条命中量限规则的医嘱行（默认合规：日量 1）。"""
    values = {
        "batch_no": "B-1",
        "prescription_no": "RX-1",
        "drug_name": "盐酸吗啡注射液",
        "rx_type": "门急诊一般",
        "drug_category": "麻醉药品注射剂",
        "days_qty": "1",
        "clinical_diag": "高血压",
    }
    values.update(overrides)
    return PrescriptionRow(**values)


# ---------------------------------------------------------------------------
# 1. 规则调度：适用 / 不适用两档分流
# ---------------------------------------------------------------------------


def test_applies_when_field_missing_makes_rule_not_applicable() -> None:
    """适用条件字段缺失且**不是**声明依赖 → 规则不适用，不判不可判定。"""
    row = _limit_row(rx_type="", drug_category="麻醉药品注射剂")
    # 把依赖声明去掉，只留适用条件：此时 rx_type 缺失与规则结论无关
    rule = RuleRow(
        rule_id="RX-DECOR",
        layer="规范性",
        basis=BASIS,
        version="2026.10",
        applies_when={"rx_type": "门急诊一般"},
        required_fields=("days_qty",),
        expression="days_qty > 1",
        severity="严重不合理用药",
    )
    schedule = match_rule(rule, row)
    assert schedule.status is ScheduleStatus.NOT_APPLICABLE
    assert schedule.is_candidate is False
    assert "rx_type" in schedule.reason
    assert "不适用" in schedule.reason

    # 不适用 → 不产生判定行
    assert judge_prescription(row, [rule]) == []


def test_rule_with_value_but_mismatch_is_not_applicable() -> None:
    """适用条件字段有值但不匹配 → 规则不适用（不进台账）。"""
    row = _limit_row(rx_type="住院")
    schedule = match_rule(LIMIT_RULE, row)
    assert schedule.status is ScheduleStatus.NOT_APPLICABLE
    assert "rx_type" in schedule.unmatched_fields
    assert judge_prescription(row, [LIMIT_RULE]) == []


def test_matching_rule_becomes_candidate() -> None:
    """适用条件命中 → 候选规则。"""
    row = _limit_row()
    schedule = match_rule(LIMIT_RULE, row)
    assert schedule.status is ScheduleStatus.CANDIDATE
    assert schedule.is_candidate is True
    assert schedule.matched_fields == ("rx_type", "drug_category")
    assert schedule.unknown_fields == ()


def test_missing_dependency_field_keeps_rule_as_candidate() -> None:
    """适用条件字段缺失但**是**声明依赖 → 仍是候选（判定层落不可判定并点名）。

    这一档决定字段补齐顺序表能不能说清「补哪个字段能解锁哪几条规则」。
    """
    row = _limit_row(rx_type="")
    schedule = match_rule(LIMIT_RULE, row)
    assert schedule.status is ScheduleStatus.CANDIDATE
    assert schedule.unknown_fields == ("rx_type",)

    rows = judge_prescription(row, [LIMIT_RULE])
    assert len(rows) == 1
    assert rows[0].conclusion is Conclusion.UNDETERMINABLE
    assert rows[0].missing_fields == ("rx_type",)


def test_applies_when_uses_normalized_values() -> None:
    """适用条件两侧都过归一层：HIS 写法「门诊一般患者」能命中规则口径。"""
    row = _limit_row(rx_type="门诊一般患者")
    schedule = match_rule(LIMIT_RULE, row)
    assert schedule.status is ScheduleStatus.CANDIDATE
    assert judge_prescription(row, [LIMIT_RULE])[0].conclusion is Conclusion.COMPLIANT


def test_schedule_and_candidate_selection_agree() -> None:
    """调度结果与候选规则集口径一致（候选 ⊆ 全部调度结果）。"""
    row = _limit_row(rx_type="住院", drug_category="麻醉药品")
    schedules = schedule_rules([LIMIT_RULE, DECORATION_RULE], row)
    candidates = select_candidates([LIMIT_RULE, DECORATION_RULE], row)
    assert len(schedules) == 2
    assert [item.rule.rule_id for item in candidates] == [
        item.rule.rule_id for item in schedules if item.is_candidate
    ]
    assert candidate_rules([LIMIT_RULE, DECORATION_RULE], row) == tuple(
        item.rule for item in candidates
    )


# ---------------------------------------------------------------------------
# 2. required_fields 缺失 → 不可判定且点名字段
# ---------------------------------------------------------------------------


def test_missing_required_field_yields_undeterminable_with_named_field() -> None:
    row = _limit_row(days_qty="")
    rows = judge_prescription(row, [LIMIT_RULE])
    assert len(rows) == 1
    judgement = rows[0]
    assert judgement.conclusion is Conclusion.UNDETERMINABLE
    assert judgement.missing_fields == ("days_qty",)
    assert "days_qty" in judgement.reason


def test_every_missing_required_field_is_named() -> None:
    row = _limit_row(days_qty="", drug_category="")
    judgement = judge_prescription(row, [LIMIT_RULE])[0]
    assert judgement.conclusion is Conclusion.UNDETERMINABLE
    assert set(judgement.missing_fields) == {"drug_category", "days_qty"}
    for name in judgement.missing_fields:
        assert name in judgement.reason


def test_blank_field_counts_as_missing() -> None:
    """纯空白等同缺失（不是有值）。"""
    row = _limit_row(days_qty="   ")
    judgement = judge_prescription(row, [LIMIT_RULE])[0]
    assert judgement.conclusion is Conclusion.UNDETERMINABLE
    assert judgement.missing_fields == ("days_qty",)


def test_uncomparable_unit_is_undeterminable_not_violation() -> None:
    """日量写成 ``3mg`` → 口径不可比 → 不可判定，不静默判违规。"""
    row = _limit_row(days_qty="3mg")
    judgement = judge_prescription(row, [LIMIT_RULE])[0]
    assert judgement.conclusion is Conclusion.UNDETERMINABLE
    assert judgement.missing_fields == ("days_qty",)
    assert "口径不可比" in judgement.reason


# ---------------------------------------------------------------------------
# 3. 显式「未注明」与字段为空 → 不同结论
# ---------------------------------------------------------------------------


def test_explicit_no_and_blank_field_produce_different_conclusions() -> None:
    blank = judge_prescription(_limit_row(skin_test_result=""), [SKIN_TEST_RULE])[0]
    explicit = judge_prescription(
        _limit_row(skin_test_result="未注明"), [SKIN_TEST_RULE]
    )[0]
    negative = judge_prescription(
        _limit_row(skin_test_result="阴性"), [SKIN_TEST_RULE]
    )[0]

    assert blank.conclusion is Conclusion.UNDETERMINABLE
    assert blank.missing_fields == ("skin_test_result",)
    assert explicit.conclusion is Conclusion.VIOLATION
    assert explicit.missing_fields == ()
    assert negative.conclusion is Conclusion.COMPLIANT


def test_missing_conclusion_violation_rule_still_names_the_field() -> None:
    """缺项类规则可把结论定为「违规」，但缺项仍要点名。"""
    row = _limit_row(dept_name="呼吸内科门诊", clinical_diag="")
    judgement = judge_prescription(row, [DECORATION_RULE])[0]
    assert judgement.conclusion is Conclusion.UNDETERMINABLE
    assert judgement.missing_fields == ("clinical_diag",)


# ---------------------------------------------------------------------------
# 4. 归因文本：规则号 + 依据出处
# ---------------------------------------------------------------------------


def test_reason_text_carries_rule_id_and_basis() -> None:
    row = _limit_row(days_qty="")
    judgement = judge_prescription(row, [LIMIT_RULE])[0]
    assert LIMIT_RULE.rule_id in judgement.reason
    assert LIMIT_RULE.version in judgement.reason
    assert BASIS in judgement.reason
    assert judgement.basis == BASIS


def test_reason_text_carries_rule_id_and_basis_on_sample_batch(
    rules, prescription_rows
) -> None:
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")
    assert judgement.rows
    for row in judgement.rows:
        assert row.rule_id in row.reason
        assert row.basis in row.reason
        assert row.basis


# ---------------------------------------------------------------------------
# 5. 分流：不适用的不进台账，不可判定的点名进台账
# ---------------------------------------------------------------------------


def test_not_applicable_rules_produce_no_rows_but_undeterminable_ones_do(
    rules, prescription_rows
) -> None:
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")

    undeterminable = [
        row for row in judgement.rows if row.conclusion is Conclusion.UNDETERMINABLE
    ]
    assert undeterminable, "样例批次应含不可判定项"
    for row in undeterminable:
        assert row.missing_fields, "不可判定行必须点名缺失字段"
        for name in row.missing_fields:
            assert name in row.reason

    # 不适用 → 该 (处方, 规则) 组合在台账上一行不留
    keys = {(row.prescription_no, row.rule_id) for row in judgement.rows}
    for row in prescription_rows:
        for rule in rules:
            schedule = match_rule(rule, row)
            if not schedule.is_candidate:
                assert (row.prescription_no, rule.rule_id) not in keys


def test_exactly_one_row_per_prescription_rule_pair(rules, prescription_rows) -> None:
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")
    keys = [(row.prescription_no, row.rule_id) for row in judgement.rows]
    assert len(keys) == len(set(keys)), "同一 (处方, 规则) 不得重复出行"
    assert len(judgement.rows) == sum(judgement.candidate_rule_counts.values())

    counts = summarize_conclusions(judgement.rows)
    assert sum(counts.values()) == len(judgement.rows)
    assert counts[Conclusion.COMPLIANT.value] > 0
    assert counts[Conclusion.VIOLATION.value] > 0
    assert counts[Conclusion.UNDETERMINABLE.value] > 0


def test_sample_batch_keeps_the_documented_main_path_conclusions(
    rules, prescription_rows
) -> None:
    """主干口径回归：缺 rx_type 的处方落不可判定并点名，各档量限规则都要在。"""
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")
    named = [
        row
        for row in judgement.rows
        if row.conclusion is Conclusion.UNDETERMINABLE and "rx_type" in row.missing_fields
    ]
    assert named
    assert {row.prescription_no for row in named} == {"RX-0007"}
    assert {row.rule_id for row in named} == {
        "RX-LEGAL-MA-OUT-DAYS",
        "RX-LEGAL-MA-CANCER-OUT-DAYS",
        "RX-LEGAL-MA-INPATIENT-DAYS",
    }
