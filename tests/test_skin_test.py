"""判定内核 4/10：皮试判定（空值 ≠ 显式否）。

对应任务书 T16。《处方审核规则体系》§1 适宜性审核第 1 项：「规定必须做皮试的药品，
是否注明过敏试验及结果判定」。落到现场数据上，「没写皮试结果」有两种截然不同的含义：

* **字段留空** —— 系统判不了 → 落**不可判定**并点名 ``skin_test_result``，
  进字段缺口台账，信息科补接口字段后可重判；
* **写「未注明」/「未做」** —— 已知不合格 → 落**违规**，可直接自动拦截；
* **写「阴性」** —— 合规。

两者若混成「非空即有值」，最常见的现场错误就是「没填皮试结果」被当成「皮试阴性」
放过——一条本该拦下的处方就这么进了划价收费与调配环节。

本册覆盖五组硬断言（任务书点名 + 本轮自加）：

1. ``domain/cases/prescription_review_cases.json`` 的 **rx-004** 逐条一致（应拦截）；
2. 显式「未注明」→ **违规**；空字符串 → **不可判定**（点名 ``skin_test_result``）；
3. 别名「未做」/「未知」/「不详」归一到**同一分支**（与「未注明」同结论）；
4. 结论**由规则行的受限表达式决定**，不在代码里写死——换一条表达式结论跟着变；
   规则行把「字段没落地」声明成「违规」时**显式报配置错误**，不静默放行；
5. 归因文本带规则号 / 依据出处 / 缺失字段名，且不带患者可识别明细；
   皮试范围（哪些药品必须做皮试）由**规则行**汇总，不在代码里写死药品清单。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.judge import judge_prescription
from med_rule_fieldmap.expression.evaluator import FieldState
from med_rule_fieldmap.engine.skin_test import (
    SKIN_TEST_BASIS,
    SKIN_TEST_FIELD,
    SkinTestConfigurationError,
    SkinTestStatus,
    classify_skin_test_value,
    evaluate_skin_test,
    is_skin_test_required,
    is_skin_test_rule,
    render_skin_test_reason,
    skin_test_scope,
    validate_skin_test_rule,
)
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.models import Conclusion, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"
BATCH_FILE = BATCH_DIR / "demo_batch_001.csv"

#: 官方案例集（``domain/cases/prescription_review_cases.json``）。
CASES_FILE = PROJECT_ROOT.parents[2] / "domain" / "cases" / "prescription_review_cases.json"

AS_OF = date(2026, 10, 28)

#: 官方案例输入里的中文键 → 医嘱落地列口径。
CASE_FIELD_MAP = {
    "药品": "drug_name",
    "皮试结果": "skin_test_result",
    "处方类型": "rx_type",
    "药品类别": "drug_category",
    "开具量": "days_qty",
}


@pytest.fixture(scope="module")
def rules():
    repository = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repository.load()
    assert repository.errors == (), "样例规则集不应有载入错误"
    return repository.effective_rules


@pytest.fixture(scope="module")
def skin_rule(rules):
    """样例规则集里的皮试规则（未注明 → 违规）。"""
    matched = [rule for rule in rules if rule.rule_id == "RX-APPR-SKIN-TEST-UNSPECIFIED"]
    assert matched, "样例规则集应含皮试规则 RX-APPR-SKIN-TEST-UNSPECIFIED"
    return matched[0]


def _row(**values: str) -> PrescriptionRow:
    """按医嘱落地列口径造一条处方行。"""
    return PrescriptionRow(**values)


def _case_row(values: dict[str, str]) -> PrescriptionRow:
    """官方案例输入 → 处方行。"""
    fields: dict[str, str] = {}
    for key, raw in values.items():
        field = CASE_FIELD_MAP.get(key)
        if field:
            fields[field] = raw
    return PrescriptionRow(**fields)


def _official_case(case_id: str) -> dict:
    """从官方案例集里取一条案例。"""
    assert CASES_FILE.exists(), f"官方案例集缺失：{CASES_FILE}"
    payload = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for case in payload["cases"]:
        if case["case_id"] == case_id:
            return case
    raise AssertionError(f"官方案例集里没有 {case_id}")


# ---------------------------------------------------------------------------
# 官方案例：rx-004
# ---------------------------------------------------------------------------


def test_official_case_rx_004_is_blocked() -> None:
    """rx-004（青霉素类注射剂 + 皮试结果「未注明」）按官方案例应拦截。"""
    case = _official_case("rx-004")
    assert case["expect"]["拦截"] is True
    assert "皮试" in case["expect"]["规则"]
    assert case["input"]["皮试结果"] == "未注明"


def test_official_case_rx_004_conclusion_matches_domain(rules) -> None:
    """rx-004 逐条一致：皮试规则落违规，其余规则不误伤。"""
    case = _official_case("rx-004")
    row = _case_row(case["input"])
    judgements = judge_prescription(row, rules, batch_no="CASE-BATCH")

    skin_rows = [item for item in judgements if item.rule_id == "RX-APPR-SKIN-TEST-UNSPECIFIED"]
    assert len(skin_rows) == 1, "rx-004 应恰好命中一条皮试规则"
    assert skin_rows[0].conclusion is Conclusion.VIOLATION
    assert skin_rows[0].missing_fields == ()
    assert "未注明" in skin_rows[0].reason
    assert "RX-APPR-SKIN-TEST-UNSPECIFIED" in skin_rows[0].reason
    assert skin_rows[0].basis == SKIN_TEST_BASIS


# ---------------------------------------------------------------------------
# 空值 ≠ 显式否
# ---------------------------------------------------------------------------


def test_explicit_unspecified_is_violation(skin_rule) -> None:
    """显式写「未注明」→ 违规（已知不合格，可直接自动拦截）。"""
    row = _row(drug_name="青霉素钠注射剂", skin_test_result="未注明")
    evaluation = evaluate_skin_test(skin_rule, row)

    assert evaluation.status is SkinTestStatus.VIOLATION
    assert evaluation.conclusion is Conclusion.VIOLATION
    assert evaluation.missing_fields == ()
    assert evaluation.value is not None and evaluation.value.is_explicit_no


def test_empty_field_is_undeterminable_not_violation(skin_rule) -> None:
    """字段留空 → 不可判定并点名 skin_test_result（不判违规、也不静默判合规）。"""
    row = _row(drug_name="青霉素钠注射剂", skin_test_result="")
    evaluation = evaluate_skin_test(skin_rule, row)

    assert evaluation.status is SkinTestStatus.UNDETERMINABLE
    assert evaluation.conclusion is Conclusion.UNDETERMINABLE
    assert evaluation.missing_fields == (SKIN_TEST_FIELD,)
    assert evaluation.value is not None and evaluation.value.is_missing


def test_whitespace_only_field_is_undeterminable(skin_rule) -> None:
    """纯空白等同留空 → 不可判定。"""
    row = _row(drug_name="青霉素钠注射剂", skin_test_result="   ")
    evaluation = evaluate_skin_test(skin_rule, row)
    assert evaluation.status is SkinTestStatus.UNDETERMINABLE
    assert evaluation.missing_fields == (SKIN_TEST_FIELD,)


def test_negative_result_is_compliant(skin_rule) -> None:
    """「阴性」→ 合规。"""
    row = _row(drug_name="青霉素钠注射剂", skin_test_result="阴性")
    evaluation = evaluate_skin_test(skin_rule, row)

    assert evaluation.status is SkinTestStatus.COMPLIANT
    assert evaluation.conclusion is Conclusion.COMPLIANT
    assert evaluation.missing_fields == ()


@pytest.mark.parametrize("alias", ["未做", "未知", "不详", "未行", "未做皮试"])
def test_aliases_fold_into_the_same_branch(skin_rule, alias: str) -> None:
    """别名「未做 / 未知 / 不详 …」归一到「未注明」，与「未注明」同结论。"""
    row = _row(drug_name="青霉素钠注射剂", skin_test_result=alias)
    evaluation = evaluate_skin_test(skin_rule, row)

    assert evaluation.status is SkinTestStatus.VIOLATION
    assert evaluation.conclusion is Conclusion.VIOLATION
    assert evaluation.value is not None
    assert evaluation.value.canonical == "未注明", "别名应归一到同一规范值"
    assert evaluation.value.state is FieldState.EXPLICIT_NO


def test_alias_and_empty_stay_on_different_branches(skin_rule) -> None:
    """别名（显式否）与留空（缺失）必须落在两个不同结论上。"""
    alias_row = _row(drug_name="青霉素钠注射剂", skin_test_result="未做")
    empty_row = _row(drug_name="青霉素钠注射剂", skin_test_result="")

    alias_result = evaluate_skin_test(skin_rule, alias_row)
    empty_result = evaluate_skin_test(skin_rule, empty_row)

    assert alias_result.conclusion is not empty_result.conclusion
    assert alias_result.conclusion is Conclusion.VIOLATION
    assert empty_result.conclusion is Conclusion.UNDETERMINABLE


def test_positive_result_is_violation(rules) -> None:
    """皮试结果「阳性」仍予开具 → 违规（结论写在规则行里，不在代码里）。"""
    positive_rules = [
        rule for rule in rules if rule.rule_id == "RX-APPR-SKIN-TEST-POSITIVE"
    ]
    assert positive_rules, "样例规则集应含皮试阳性规则"

    row = _row(drug_name="青霉素钠注射剂", skin_test_result="阳性")
    evaluation = evaluate_skin_test(positive_rules[0], row)
    assert evaluation.status is SkinTestStatus.VIOLATION

    # 走完整判定链也是违规
    judgements = judge_prescription(row, rules, batch_no="CASE-BATCH")
    positive_rows = [
        item for item in judgements if item.rule_id == "RX-APPR-SKIN-TEST-POSITIVE"
    ]
    assert positive_rows and positive_rows[0].conclusion is Conclusion.VIOLATION


def test_classify_skin_test_value_three_states() -> None:
    """三档空值语义：缺失 / 显式否 / 有值。"""
    missing = classify_skin_test_value(_row(skin_test_result=""))
    explicit_no = classify_skin_test_value(_row(skin_test_result="未注明"))
    value = classify_skin_test_value(_row(skin_test_result="阴性"))

    assert missing.is_missing and not missing.is_explicit_no
    assert explicit_no.is_explicit_no and not explicit_no.is_missing
    assert value.is_value and not value.is_explicit_no
    assert explicit_no.canonical == "未注明"
    assert value.canonical == "阴性"


# ---------------------------------------------------------------------------
# 结论由规则行决定（不在代码里写死分支）
# ---------------------------------------------------------------------------


def test_conclusion_follows_the_rule_expression(skin_rule) -> None:
    """换一条表达式，结论跟着变——证明结论不是代码写死的。"""
    row = _row(drug_name="青霉素钠注射剂", skin_test_result="阴性")
    flipped = replace(skin_rule, expression="skin_test_result == '阴性'")

    assert evaluate_skin_test(skin_rule, row).status is SkinTestStatus.COMPLIANT
    assert evaluate_skin_test(flipped, row).status is SkinTestStatus.VIOLATION


def test_explicit_no_never_counts_as_satisfying_a_condition(skin_rule) -> None:
    """显式否定值证明不了任何条件 → 违规，不静默判合规。

    「未注明」换一条正向表达式仍判违规：显式否不是「没数据」，它是已知的
    不合格信息，不能被当成满足条件放过去（受限求值器的空值语义）。
    """
    row = _row(drug_name="青霉素钠注射剂", skin_test_result="未注明")
    flipped = replace(skin_rule, expression="skin_test_result == '阴性'")

    assert evaluate_skin_test(flipped, row).status is SkinTestStatus.VIOLATION


def test_rule_without_declared_dependency_is_a_configuration_error() -> None:
    """规则行没声明 skin_test_result → 显式配置错误，不静默判成合规。"""
    broken = RuleRow(
        rule_id="RX-APPR-SKIN-TEST-BROKEN",
        layer="适宜性",
        basis=SKIN_TEST_BASIS,
        version="2026.10",
        required_fields=(),
        expression="skin_test_result == '未注明'",
        severity="不合理用药",
    )
    assert not is_skin_test_rule(broken)
    assert validate_skin_test_rule(broken)

    row = _row(drug_name="青霉素钠注射剂", skin_test_result="未注明")
    with pytest.raises(SkinTestConfigurationError):
        evaluate_skin_test(broken, row)


def test_rule_declaring_missing_as_violation_is_a_configuration_error(skin_rule) -> None:
    """把「字段没落地」声明成「违规」＝把空值与显式否混成一档 → 显式拒。"""
    wrong = RuleRow(
        rule_id=skin_rule.rule_id,
        layer=skin_rule.layer,
        basis=skin_rule.basis,
        version=skin_rule.version,
        applies_when=skin_rule.applies_when,
        required_fields=skin_rule.required_fields,
        expression=skin_rule.expression,
        severity=skin_rule.severity,
        missing_conclusion=Conclusion.VIOLATION,
    )
    issues = validate_skin_test_rule(wrong)
    assert issues and "不可判定" in issues[0]

    row = _row(drug_name="青霉素钠注射剂", skin_test_result="")
    with pytest.raises(SkinTestConfigurationError):
        evaluate_skin_test(wrong, row)


def test_unevaluable_expression_raises_configuration_error(skin_rule) -> None:
    """表达式引用了不存在的字段 → 配置错误，不降级成「不可判定」。"""
    broken = RuleRow(
        rule_id=skin_rule.rule_id,
        layer=skin_rule.layer,
        basis=skin_rule.basis,
        version=skin_rule.version,
        applies_when=skin_rule.applies_when,
        required_fields=skin_rule.required_fields,
        expression="no_such_field == '未注明'",
        severity=skin_rule.severity,
    )
    row = _row(drug_name="青霉素钠注射剂", skin_test_result="未注明")
    with pytest.raises(SkinTestConfigurationError):
        evaluate_skin_test(broken, row)


def test_rule_not_applicable_produces_no_judgement_row(rules) -> None:
    """适用条件不命中 → 不适用，不产生判定行。"""
    row = _row(drug_name="注射用头孢呋辛钠", skin_test_result="未注明")
    judgements = judge_prescription(row, rules, batch_no="CASE-BATCH")
    assert not [item for item in judgements if "SKIN-TEST" in item.rule_id]


# ---------------------------------------------------------------------------
# 归因文本
# ---------------------------------------------------------------------------


def test_reason_text_carries_rule_id_basis_and_missing_field(skin_rule) -> None:
    """归因文本：规则号 + 依据出处 + 缺失字段名。"""
    missing_row = _row(drug_name="青霉素钠注射剂", skin_test_result="")
    evaluation = evaluate_skin_test(skin_rule, missing_row)
    reason = render_skin_test_reason(evaluation, skin_rule, missing_row)

    assert "RX-APPR-SKIN-TEST-UNSPECIFIED" in reason
    assert skin_rule.basis in reason
    assert SKIN_TEST_FIELD in reason
    assert "不可判定" in reason


def test_reason_text_explains_explicit_no_is_not_missing(skin_rule) -> None:
    """显式否的归因要说清「不是字段没落地」，药师才看得懂两条结论的差别。"""
    row = _row(drug_name="青霉素钠注射剂", skin_test_result="未注明")
    evaluation = evaluate_skin_test(skin_rule, row)
    reason = render_skin_test_reason(evaluation, skin_rule, row)

    assert "显式否定值" in reason
    assert "违规" in reason
    assert "缺少字段" not in reason


def test_reason_text_carries_no_patient_identifier(skin_rule) -> None:
    """归因文本不得带出患者姓名、身份证号等可识别明细。"""
    row = _row(
        drug_name="青霉素钠注射剂",
        skin_test_result="未注明",
        patient_ref="P-000123",
    )
    evaluation = evaluate_skin_test(skin_rule, row)
    reason = render_skin_test_reason(evaluation, skin_rule, row)
    assert "P-000123" not in reason


# ---------------------------------------------------------------------------
# 皮试范围（规则驱动，不在代码里写死药品清单）
# ---------------------------------------------------------------------------


def test_skin_test_scope_is_derived_from_rules(rules) -> None:
    """皮试范围由规则行的 applies_when 汇总，不在代码里写死药品清单。"""
    scope = skin_test_scope(rules)
    assert not scope.is_empty
    assert "RX-APPR-SKIN-TEST-UNSPECIFIED" in scope.rule_ids
    descriptions = [entry.description for entry in scope.entries]
    assert any("青霉素" in text for text in descriptions)


def test_is_skin_test_required_follows_rule_scope(rules) -> None:
    """命中皮试范围 → 该处方须做皮试；不命中 → 不须。"""
    penicillin = _row(drug_name="青霉素钠注射剂", skin_test_result="")
    other = _row(drug_name="注射用头孢呋辛钠", skin_test_result="")

    assert is_skin_test_required(penicillin, rules) is True
    assert is_skin_test_required(other, rules) is False


# ---------------------------------------------------------------------------
# 主干一致（样例批次里的三张皮试处方）
# ---------------------------------------------------------------------------


def test_sample_batch_skin_test_rows_match_the_three_states(rules) -> None:
    """样例批次：缺皮试结果 → 不可判定；写「未注明」→ 违规；「阴性」→ 合规。"""
    rows = read_prescription_rows(BATCH_FILE)
    judgements = judge_prescription(
        next(row for row in rows if row.prescription_no == "RX-0020"),
        rules,
        batch_no="DEMO-BATCH-001",
    )
    by_rule = {item.rule_id: item for item in judgements}

    missing = by_rule["RX-APPR-SKIN-TEST-UNSPECIFIED"]
    assert missing.conclusion is Conclusion.UNDETERMINABLE
    assert SKIN_TEST_FIELD in missing.missing_fields

    explicit = judge_prescription(
        next(row for row in rows if row.prescription_no == "RX-0021"),
        rules,
        batch_no="DEMO-BATCH-001",
    )
    explicit_row = {item.rule_id: item for item in explicit}[
        "RX-APPR-SKIN-TEST-UNSPECIFIED"
    ]
    assert explicit_row.conclusion is Conclusion.VIOLATION

    negative = judge_prescription(
        next(row for row in rows if row.prescription_no == "RX-0022"),
        rules,
        batch_no="DEMO-BATCH-001",
    )
    negative_row = {item.rule_id: item for item in negative}[
        "RX-APPR-SKIN-TEST-UNSPECIFIED"
    ]
    assert negative_row.conclusion is Conclusion.COMPLIANT


def test_sample_rule_set_has_no_skin_test_configuration_issue(rules) -> None:
    """样例规则集里的皮试规则全部通过配置校验。"""
    skin_rules = [rule for rule in rules if is_skin_test_rule(rule)]
    assert skin_rules
    for rule in skin_rules:
        assert validate_skin_test_rule(rule) == (), f"规则 {rule.rule_id} 配置不合口径"
