"""判定内核 6/10：规范性审核 —— 用法用量含糊字句 + 儿童实际年龄。

对应任务书 T18。《处方审核规则体系》§1 规范性审核原文两段：

* 「用法用量表述清楚，不得用『遵医嘱』『自用』含糊字句」——正文段；
* 「儿童处方写实际年龄，婴幼儿写日/月龄或体重」——正文段。

覆盖任务书点名的三组断言，外加本轮自加的配置、边界与主干回归：

1. **rx-003 逐条一致**——``domain/cases/prescription_review_cases.json`` 的
   rx-003（处方用法用量「遵医嘱」+ 药品「阿莫西林胶囊」）→ 拦截（违规）；
2. **命中字句回显**——归因文本里要出现命中的那个字句（「遵医嘱」），药师才知道
   退回去要改哪一句；
3. **0 岁不误判**——``age_value = "0"`` 是**有值**不是缺失：结论是「婴幼儿写成
   周岁口径」（违规），不是「年龄缺失」，也不是「判不了」；
4. **词表是规则行配置项**——药事审核岗可增补字句；漏掉 §1 明列的字句属配置错误
   （载入期拒、判定期显式报错），不得静默判成合规；
5. **儿童年龄各分支**——缺实际年龄 → 违规；数值有而单位缺 → 不可判定（口径不可辨，
   点名 ``age_unit`` 进字段对照台账）；婴幼儿写成岁 → 违规；已写月龄 / 日龄 → 合规；
6. **儿童人群按科室口径圈定**——非儿科科室不产生判定行（本轮落地文件未带患者
   主数据年龄，边界写明不藏）；
7. **归因文本**带规则号 / 版本 / 依据出处 / 缺哪一列，且不带患者可识别明细。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.children_age import (
    AgeReading,
    ChildAgeConfigurationError,
    ChildAgeStatus,
    evaluate_child_age,
    is_child_age_rule,
    read_age,
    render_child_age_reason,
    validate_child_age_rule,
)
from med_rule_fieldmap.engine.evidence import detect_pii_leaks
from med_rule_fieldmap.engine.judge import ExpressionError, judge_prescription
from med_rule_fieldmap.engine.normativity import (
    VAGUE_USAGE_BASELINE_TERMS,
    NormativityConfigurationError,
    NormativityStatus,
    baseline_terms_missing,
    evaluate_usage_vagueness,
    find_usage_vagueness_hits,
    is_usage_vagueness_rule,
    render_usage_vagueness_reason,
    validate_usage_vagueness_rule,
)
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.models import Conclusion, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository
from med_rule_fieldmap.rules.schema import RuleValidationError, validate_rule

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"
BATCH_FILE = BATCH_DIR / "demo_batch_001.csv"
CHILD_BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "child_batch"
CHILD_BATCH_FILE = CHILD_BATCH_DIR / "child_batch_001.csv"

#: 官方案例集（``domain/cases/prescription_review_cases.json``）。
CASES_FILE = PROJECT_ROOT.parents[2] / "domain" / "cases" / "prescription_review_cases.json"

AS_OF = date(2026, 10, 28)

USAGE_BASIS = (
    "处方审核规则体系 §1 规范性审核（用法用量表述须清楚，不得用「遵医嘱」「自用」含糊字句）"
)
CHILD_AGE_BASIS = (
    "处方审核规则体系 §1 规范性审核（儿童处方写实际年龄，婴幼儿写日/月龄或体重）"
)

#: 官方案例输入字段 → 医嘱落地列。
CASE_FIELD_MAP = {
    "处方用法用量": "dosage_text",
    "药品": "drug_name",
    "处方类型": "rx_type",
    "药品类别": "drug_category",
    "开具量": "days_qty",
    "皮试结果": "skin_test_result",
}


@pytest.fixture(scope="module")
def rules():
    repository = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repository.load()
    assert repository.errors == (), "样例规则集不应有载入错误"
    return repository.effective_rules


@pytest.fixture(scope="module")
def usage_rule(rules):
    matched = [rule for rule in rules if rule.rule_id == "RX-NORM-USAGE-VAGUE"]
    assert matched, "样例规则集应含用法用量含糊字句规则 RX-NORM-USAGE-VAGUE"
    return matched[0]


@pytest.fixture(scope="module")
def child_age_rule(rules):
    matched = [rule for rule in rules if rule.rule_id == "RX-NORM-CHILD-AGE"]
    assert matched, "样例规则集应含儿童年龄规则 RX-NORM-CHILD-AGE"
    return matched[0]


@pytest.fixture(scope="module")
def official_cases():
    payload = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    return {case["case_id"]: case for case in payload["cases"]}


def _usage_rule(**overrides: object) -> RuleRow:
    """按审方规则行口径造一条用法用量含糊字句规则。"""
    base: dict[str, object] = {
        "rule_id": "RX-NORM-TEST-USAGE",
        "layer": "规范性",
        "basis": USAGE_BASIS,
        "version": "2026.10",
        "required_fields": ("dosage_text",),
        "expression": "dosage_text != ''",
        "severity": "不规范处方",
        "forbidden_terms": VAGUE_USAGE_BASELINE_TERMS,
    }
    base.update(overrides)
    return RuleRow(**base)  # type: ignore[arg-type]


def _child_age_rule(**overrides: object) -> RuleRow:
    """按审方规则行口径造一条儿童实际年龄规则。"""
    base: dict[str, object] = {
        "rule_id": "RX-NORM-TEST-CHILD-AGE",
        "layer": "规范性",
        "basis": CHILD_AGE_BASIS,
        "version": "2026.10",
        "applies_when": {"dept_name": {"contains": "儿科"}},
        "required_fields": ("age_value", "age_unit"),
        "expression": "age_value == '' or age_unit == ''",
        "severity": "不规范处方",
        "missing_conclusion": Conclusion.VIOLATION,
    }
    base.update(overrides)
    return RuleRow(**base)  # type: ignore[arg-type]


def _row(**values: str) -> PrescriptionRow:
    """按医嘱落地列口径造一条处方行（栏位齐备、无值即视为空值）。"""
    defaults = {
        "batch_no": "BATCH-TEST",
        "prescription_no": "RX-TEST-0001",
        "visit_no": "VISIT-TEST",
        "patient_ref": "PT-TEST",
        "dept_code": "D101",
        "dept_name": "呼吸内科门诊",
        "prescriber_id": "PR-001",
        "drug_code": "DC-001",
        "drug_name": "阿莫西林胶囊",
        "dosage_text": "0.25g 口服 每日3次",
        "clinical_diag": "小儿肺炎",
        "age_value": "3",
        "age_unit": "岁",
        "event_time": "2026-10-28 09:15:00",
    }
    defaults.update(values)
    return PrescriptionRow(**defaults)  # type: ignore[arg-type]


def _judge(row: PrescriptionRow, rules) -> list:
    return judge_prescription(row, rules, batch_no="BATCH-TEST")


# ---------------------------------------------------------------------------
# 1. 官方案例 rx-003 逐条一致
# ---------------------------------------------------------------------------


def test_rx003_official_case_is_intercepted(official_cases, rules):
    """rx-003：处方用法用量「遵医嘱」→ 拦截（与官方案例一致）。"""
    case = official_cases["rx-003"]
    assert case["expect"]["拦截"] is True, "官方案例 rx-003 应为拦截"

    values = {
        CASE_FIELD_MAP[key]: str(value)
        for key, value in case["input"].items()
        if key in CASE_FIELD_MAP
    }
    row = _row(**values)
    judgements = _judge(row, rules)
    usage_rows = [j for j in judgements if j.rule_id == "RX-NORM-USAGE-VAGUE"]

    assert len(usage_rows) == 1, "rx-003 应恰好命中一条用法用量规则判定行"
    judgement = usage_rows[0]
    assert judgement.conclusion is Conclusion.VIOLATION, "rx-003 应落违规（拦截）"
    assert case["expect"]["拦截"] is (judgement.conclusion is Conclusion.VIOLATION)


def test_rx003_hit_term_is_echoed_in_reason(official_cases, rules):
    """命中字句回显：归因文本里要出现命中的那个字句。"""
    case = official_cases["rx-003"]
    values = {
        CASE_FIELD_MAP[key]: str(value)
        for key, value in case["input"].items()
        if key in CASE_FIELD_MAP
    }
    row = _row(**values)
    judgements = _judge(row, rules)
    judgement = next(j for j in judgements if j.rule_id == "RX-NORM-USAGE-VAGUE")
    assert "遵医嘱" in judgement.reason, "归因文本必须回显命中的字句"
    assert "RX-NORM-USAGE-VAGUE" in judgement.reason, "归因文本必须点名规则号"
    assert judgement.basis, "判定行必须带依据出处"


# ---------------------------------------------------------------------------
# 2. 含糊字句判定本身
# ---------------------------------------------------------------------------


def test_usage_vagueness_rule_identified_by_structure(usage_rule):
    """识别靠结构判据（层次 + 词表 + 正文段字段），不靠规则号前缀。"""
    assert is_usage_vagueness_rule(usage_rule) is True
    assert is_usage_vagueness_rule(_usage_rule(rule_id="RX-ANYTHING")) is True
    assert is_usage_vagueness_rule(_usage_rule(layer="合法性")) is False
    assert is_usage_vagueness_rule(_usage_rule(forbidden_terms=())) is False
    assert is_usage_vagueness_rule(_usage_rule(required_fields=("clinical_diag",))) is False


def test_usage_vagueness_hit_and_no_hit(usage_rule):
    """命中 → 违规并回显字句；未命中 → 合规。"""
    hit = evaluate_usage_vagueness(usage_rule, _row(dosage_text="遵医嘱"))
    assert hit.status is NormativityStatus.VIOLATION
    assert hit.hit_terms == ("遵医嘱",)
    assert hit.hits == (("dosage_text", "遵医嘱"),)
    assert "遵医嘱" in hit.detail

    clean = evaluate_usage_vagueness(usage_rule, _row(dosage_text="0.25g 口服 每日3次"))
    assert clean.status is NormativityStatus.COMPLIANT
    assert clean.hit_terms == ()


def test_usage_vagueness_finds_term_in_any_declared_field():
    """词表在规则声明的字段里逐字段查（不限于第一个字段）。"""
    rule = _usage_rule(required_fields=("dosage_text", "freq"))
    hits = find_usage_vagueness_hits(rule, _row(dosage_text="按说明服用", freq="自用"))
    assert hits == (("freq", "自用"),)


def test_usage_vagueness_missing_column_is_undeterminable(usage_rule):
    """整列未落地 → 不可判定（接口缺口），点名字段进字段对照台账。"""
    row = _row(dosage_text="遵医嘱")
    row.present_columns = frozenset(
        name for name in row.__dict__ if name != "present_columns" and name != "batch_no"
    )
    row.present_columns = row.present_columns - {"dosage_text"}

    evaluation = evaluate_usage_vagueness(usage_rule, row)
    assert evaluation.status is NormativityStatus.UNDETERMINABLE
    assert evaluation.missing_fields == ("dosage_text",)
    assert "整列未落地" in evaluation.detail


def test_usage_vagueness_blank_field_follows_rule_declaration(usage_rule):
    """栏位在但没填：结论由规则行声明，引擎不替规则作者决定。"""
    default = evaluate_usage_vagueness(usage_rule, _row(dosage_text=""))
    assert default.status is NormativityStatus.UNDETERMINABLE
    assert default.empty_fields == ("dosage_text",)

    strict = evaluate_usage_vagueness(
        _usage_rule(missing_conclusion=Conclusion.VIOLATION), _row(dosage_text="")
    )
    assert strict.status is NormativityStatus.VIOLATION
    assert strict.conclusion is Conclusion.VIOLATION


# ---------------------------------------------------------------------------
# 3. 词表是规则行配置项（可增补 / 漏基线即配置错误）
# ---------------------------------------------------------------------------


def test_word_list_is_configurable_and_covers_baseline(usage_rule):
    """生效词表来自规则行；§1 明列字句必须被覆盖。"""
    assert set(usage_rule.forbidden_terms) >= set(VAGUE_USAGE_BASELINE_TERMS)
    assert baseline_terms_missing(usage_rule) == ()

    extended = _usage_rule(forbidden_terms=(*VAGUE_USAGE_BASELINE_TERMS, "酌情"))
    assert baseline_terms_missing(extended) == ()
    hits = find_usage_vagueness_hits(extended, _row(dosage_text="酌情服用"))
    assert hits == (("dosage_text", "酌情"),), "药事审核岗增补的字句应生效"


def test_dropping_baseline_term_is_configuration_error():
    """漏掉 §1 明列字句属配置错误：载入期拒、判定期显式报错。"""
    weakened = _usage_rule(forbidden_terms=("遵医嘱",))
    assert baseline_terms_missing(weakened) == ("自用",)
    issues = validate_usage_vagueness_rule(weakened)
    assert issues and "自用" in issues[0]

    with pytest.raises(NormativityConfigurationError) as excinfo:
        evaluate_usage_vagueness(weakened, _row(dosage_text="自用"))
    assert "自用" in str(excinfo.value)

    with pytest.raises(RuleValidationError) as load_exc:
        validate_rule(
            {
                "rule_id": "RX-NORM-TEST-USAGE",
                "layer": "规范性",
                "basis": USAGE_BASIS,
                "version": "2026.10",
                "required_fields": ["dosage_text"],
                "expression": "dosage_text != ''",
                "severity": "不规范处方",
                "forbidden_terms": ["遵医嘱"],
            }
        )
    assert "自用" in str(load_exc.value)

    # 判定链上同样显式失败（不降级成「不可判定」把配置问题藏进数据侧）。
    with pytest.raises(ExpressionError) as judge_exc:
        judge_prescription(_row(dosage_text="自用"), [weakened], batch_no="BATCH-TEST")
    assert "自用" in str(judge_exc.value)


def test_usage_rule_without_word_list_is_configuration_error():
    """没有词表的含糊字句规则无从判起（显式报错，不静默放行）。"""
    issues = validate_usage_vagueness_rule(_usage_rule(forbidden_terms=()))
    assert issues and "forbidden_terms" in issues[0]
    with pytest.raises(NormativityConfigurationError):
        evaluate_usage_vagueness(_usage_rule(forbidden_terms=()), _row())


# ---------------------------------------------------------------------------
# 4. 儿童实际年龄：分支表
# ---------------------------------------------------------------------------


def test_child_age_rule_identified_by_structure(child_age_rule):
    """识别靠结构判据（层次 + 两个年龄列），不靠规则号前缀。"""
    assert is_child_age_rule(child_age_rule) is True
    assert is_child_age_rule(_child_age_rule(rule_id="RX-ANYTHING")) is True
    assert is_child_age_rule(_child_age_rule(layer="合法性")) is False
    assert is_child_age_rule(_child_age_rule(required_fields=("age_value",))) is False


def test_child_age_missing_actual_age_is_violation(child_age_rule):
    """儿童处方未写实际年龄 → 违规（退回医师补写，不是等信息科改接口）。"""
    row = _row(dept_name="儿科门诊", age_value="", age_unit="")
    evaluation = evaluate_child_age(child_age_rule, row)
    assert evaluation.status is ChildAgeStatus.VIOLATION
    assert evaluation.conclusion is Conclusion.VIOLATION
    assert evaluation.empty_fields == ("age_value", "age_unit")
    assert "未写实际年龄" in evaluation.detail


def test_child_age_zero_years_is_not_missing(child_age_rule):
    """0 岁是**有值**不是缺失：落「婴幼儿写成周岁口径」，不是「年龄缺失」。"""
    row = _row(dept_name="儿科门诊", age_value="0", age_unit="岁")
    assert row.is_missing("age_value") is False, "0 不是缺失"

    evaluation = evaluate_child_age(child_age_rule, row)
    assert evaluation.status is ChildAgeStatus.VIOLATION
    assert evaluation.conclusion is Conclusion.VIOLATION
    assert "周岁口径" in evaluation.detail
    assert "未写" not in evaluation.detail, "0 岁不得被写成「年龄未写」"
    assert "缺失" not in evaluation.detail, "0 岁不得被判成缺失"
    assert evaluation.age.is_zero is True


def test_child_age_zero_months_is_compliant(child_age_rule):
    """0 月龄是真实取值（有值），婴幼儿已按月龄写明 → 合规。"""
    row = _row(dept_name="儿科门诊", age_value="0", age_unit="月")
    evaluation = evaluate_child_age(child_age_rule, row)
    assert evaluation.status is ChildAgeStatus.COMPLIANT
    assert evaluation.conclusion is Conclusion.COMPLIANT


def test_child_age_actual_age_is_compliant(child_age_rule):
    """儿童写了实际年龄 → 合规。"""
    row = _row(dept_name="儿科门诊", age_value="3", age_unit="岁")
    evaluation = evaluate_child_age(child_age_rule, row)
    assert evaluation.status is ChildAgeStatus.COMPLIANT
    assert "实际年龄" in evaluation.detail


def test_child_age_infant_month_or_day_is_compliant(child_age_rule):
    """婴幼儿已按月龄 / 日龄写明 → 合规。"""
    month = evaluate_child_age(
        child_age_rule, _row(dept_name="儿科门诊", age_value="6", age_unit="月")
    )
    assert month.status is ChildAgeStatus.COMPLIANT
    day = evaluate_child_age(
        child_age_rule, _row(dept_name="儿科门诊", age_value="20", age_unit="日")
    )
    assert day.status is ChildAgeStatus.COMPLIANT


def test_child_age_unit_missing_is_undeterminable(child_age_rule):
    """数值有而单位缺 → 不可判定（口径不可辨），点名 age_unit。"""
    row = _row(dept_name="儿科门诊", age_value="3", age_unit="")
    evaluation = evaluate_child_age(child_age_rule, row)
    assert evaluation.status is ChildAgeStatus.UNDETERMINABLE
    assert evaluation.missing_fields == ("age_unit",)
    assert "口径不可比" in evaluation.detail


def test_child_age_missing_columns_is_undeterminable(child_age_rule):
    """年龄两列整列未落地 → 不可判定（接口缺口），点名字段进字段对照台账。"""
    row = _row(dept_name="儿科门诊")
    row.present_columns = frozenset(
        {
            "batch_no",
            "order_no",
            "prescription_no",
            "visit_no",
            "patient_ref",
            "dept_code",
            "dept_name",
            "prescriber_id",
            "prescriber_name",
            "drug_code",
            "drug_name",
            "spec",
            "dosage_form",
            "dosage_text",
            "dosage_value",
            "dosage_unit",
            "route",
            "freq",
            "quantity",
            "rx_type",
            "drug_category",
            "days_qty",
            "extend_reason",
            "skin_test_result",
            "clinical_diag",
            "event_time",
        }
    )
    evaluation = evaluate_child_age(child_age_rule, row)
    assert evaluation.status is ChildAgeStatus.UNDETERMINABLE
    assert evaluation.missing_fields == ("age_value", "age_unit")
    assert "整列未落地" in evaluation.detail


def test_child_age_unreadable_number_is_undeterminable(child_age_rule):
    """年龄数值读不出来（写成「三」）→ 不可判定，不静默当成 0。"""
    row = _row(dept_name="儿科门诊", age_value="三", age_unit="岁")
    evaluation = evaluate_child_age(child_age_rule, row)
    assert evaluation.status is ChildAgeStatus.UNDETERMINABLE
    assert evaluation.age.number is None


def test_child_age_non_pediatric_dept_not_applicable(child_age_rule):
    """非儿科科室不产生判定行（儿童人群按科室口径圈定）。"""
    row = _row(dept_name="呼吸内科门诊", age_value="", age_unit="")
    evaluation = evaluate_child_age(child_age_rule, row)
    assert evaluation.status is ChildAgeStatus.NOT_APPLICABLE
    assert evaluation.conclusion is None


def test_child_age_configuration_error(child_age_rule):
    """规则行只声明一个年龄列 → 配置错误，不降级成「不可判定」。"""
    broken = _child_age_rule(required_fields=("age_value",))
    assert validate_child_age_rule(broken)
    with pytest.raises(ChildAgeConfigurationError):
        evaluate_child_age(broken, _row(dept_name="儿科门诊"))

    mixed = _child_age_rule(required_fields=("age_value", "age_unit", "clinical_diag"))
    assert validate_child_age_rule(mixed)
    with pytest.raises(ChildAgeConfigurationError):
        evaluate_child_age(mixed, _row(dept_name="儿科门诊"))


def test_read_age_reports_zero_as_value():
    """读取层：0 是有值，缺失与读不出来分得清。"""
    reading = read_age(_row(age_value="0", age_unit="岁"))
    assert isinstance(reading, AgeReading)
    assert reading.number_readable is True
    assert reading.is_zero is True
    assert reading.value_missing is False

    missing = read_age(_row(age_value="", age_unit=""))
    assert missing.value_missing is True
    assert missing.unit_missing is True
    assert missing.number_readable is False


# ---------------------------------------------------------------------------
# 5. 判定链接线与归因文本
# ---------------------------------------------------------------------------


def test_usage_and_child_age_paths_are_wired_into_judge(rules, child_age_rule):
    """两条专用路径都在判定链上生效（不是孤立函数）。"""
    vague = _judge(_row(dosage_text="遵医嘱"), rules)
    assert [j.conclusion for j in vague if j.rule_id == "RX-NORM-USAGE-VAGUE"] == [
        Conclusion.VIOLATION
    ]

    child = _judge(_row(dept_name="儿科门诊", age_value="0", age_unit="岁"), rules)
    assert [j.conclusion for j in child if j.rule_id == "RX-NORM-CHILD-AGE"] == [
        Conclusion.VIOLATION
    ]


def test_reason_text_carries_rule_basis_and_named_field(child_age_rule):
    """归因文本带规则号 / 版本 / 依据出处 / 缺哪一列。"""
    row = _row(dept_name="儿科门诊", age_value="", age_unit="")
    evaluation = evaluate_child_age(child_age_rule, row)
    reason = render_child_age_reason(evaluation, child_age_rule, row)
    assert "RX-NORM-CHILD-AGE" in reason
    assert child_age_rule.version in reason
    assert child_age_rule.basis in reason
    assert "age_value" in reason and "age_unit" in reason


def test_reason_text_has_no_patient_identifier(child_age_rule, usage_rule):
    """归因文本不带患者可识别明细（患者标识与开方医师姓名都不得外泄）。"""
    row = _row(
        dept_name="儿科门诊",
        patient_ref="PT-TEST",
        prescriber_name="医师甲",
        age_value="0",
        age_unit="岁",
    )
    child_reason = render_child_age_reason(
        evaluate_child_age(child_age_rule, row), child_age_rule, row
    )
    usage_reason = render_usage_vagueness_reason(
        evaluate_usage_vagueness(usage_rule, _row(dosage_text="遵医嘱")), usage_rule, row
    )
    for text in (child_reason, usage_reason):
        assert "PT-TEST" not in text, "患者标识不得出现在归因文本里"
        assert "医师甲" not in text, "开方医师姓名不得出现在归因文本里"
        assert detect_pii_leaks(text, row) == []


# ---------------------------------------------------------------------------
# 6. 主干回归：样例批次结论不变
# ---------------------------------------------------------------------------


def test_main_batch_conclusions_unchanged(rules):
    """主样例批次的判定结论不被新规则误伤（判定行数按现规则集重算）。

    判定内核 7/10 起样例规则集多两条跨行规则（重复给药、超常处方），
    判定行数随之变化；这里盯的是**结论分布**：违规与不可判定的条数不变
    （新规则在本批次不产生任何违规），合规增加的是重复给药与超常处方的
    合规行（样例批次内没有重复开药，也没有医师超常越线）。
    """
    rows = read_prescription_rows(BATCH_FILE)
    judgements = [j for row in rows for j in _judge(row, rules)]
    assert len(judgements) == 192
    counts = {c.value: 0 for c in Conclusion}
    for judgement in judgements:
        counts[judgement.conclusion.value] += 1
    assert counts == {"合规": 170, "违规": 11, "不可判定": 11}
    assert not [j for j in judgements if j.rule_id == "RX-NORM-CHILD-AGE"], (
        "非儿科科室不应产生儿童年龄判定行"
    )
    assert [j for j in judgements if j.rule_id == "RX-NORM-USAGE-VAGUE" and j.conclusion is Conclusion.VIOLATION]


def test_child_batch_covers_every_branch(rules):
    """儿科样例批次逐条对上分支表。"""
    rows = read_prescription_rows(CHILD_BATCH_FILE)
    by_prescription: dict[str, list] = {}
    for row in rows:
        for judgement in _judge(row, rules):
            by_prescription.setdefault(judgement.prescription_no, []).append(judgement)

    def child_conclusion(prescription_no: str):
        matched = [
            j
            for j in by_prescription.get(prescription_no, [])
            if j.rule_id == "RX-NORM-CHILD-AGE"
        ]
        return matched[0].conclusion if matched else None

    assert child_conclusion("RX-C001") is Conclusion.VIOLATION  # 年龄两栏都没填
    assert child_conclusion("RX-C002") is Conclusion.VIOLATION  # 0 岁 → 婴幼儿写成岁
    assert child_conclusion("RX-C003") is Conclusion.COMPLIANT  # 6 月龄
    assert child_conclusion("RX-C004") is Conclusion.COMPLIANT  # 3 岁
    assert child_conclusion("RX-C005") is Conclusion.UNDETERMINABLE  # 有数值无单位
    assert child_conclusion("RX-C006") is Conclusion.COMPLIANT  # 0 月龄
    assert child_conclusion("RX-C007") is None  # 非儿科科室 → 不适用
