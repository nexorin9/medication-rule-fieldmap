"""判定内核 5/10：规范性审核 —— 前记 / 后记缺项。

对应任务书 T17。《处方审核规则体系》§1 规范性审核原文：「前记（机构、患者身份、
科别、临床诊断、日期）、正文（药品名称、剂型、数量、用法用量）、后记（签章）
完整清晰」。本册只管**前记与后记**两段，正文段（用法用量含糊字句、儿童实际年龄）
由判定内核 6/10 承担。

本册覆盖任务书点名的三组断言，外加本轮自加的配置与主干回归：

1. **前记缺 ``clinical_diag`` → 违规并点名 ``clinical_diag``**（缺前记即不合格
   处方，退回医师补写，不是等信息科改接口）；
2. **缺项与空值区分**——同样是「这一栏没有值」，两种成因结论不同：

   * 栏位在、内容为空 → **违规**（处方本身不合格）；
   * 整列未落地（接口没回传这一列）→ **不可判定**（系统判不了，进字段对照
     台账等信息科补字段后重判）；

   这条区分是本轮 10x 楔子的地基：不分清，字段补齐顺序表就会把「医师漏填」
   当成「接口没接」排到信息科的改造清单上；
3. **后记缺签章单独成条**——前记缺项与后记缺签章是两种处置（前者退回医师补写
   前记，后者退回补签），必须各占一条判定行，药师看得出该退给谁；
4. 配置错误**显式报错**（前记与后记字段混用、把缺项声明成「不可判定」等），
   不得降级成「不可判定」把问题藏进数据侧；
5. 归因文本带规则号 / 版本 / 依据出处 / 缺哪一项，且不带患者可识别明细。
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.judge import ExpressionError, judge_prescription
from med_rule_fieldmap.engine.normativity import (
    BACK_MATTER_FIELDS,
    BACK_MATTER_ITEMS,
    FRONT_MATTER_FIELDS,
    FRONT_MATTER_ITEMS,
    NormativityConfigurationError,
    NormativityStatus,
    evaluate_normativity,
    is_normativity_rule,
    items_for_rule,
    render_normativity_reason,
    validate_normativity_rule,
)
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.models import Conclusion, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"
BATCH_FILE = BATCH_DIR / "demo_batch_001.csv"

AS_OF = date(2026, 10, 28)

FRONT_BASIS = (
    "处方审核规则体系 §1 规范性审核（前记：机构、患者身份、科别、临床诊断、日期完整清晰）"
)
BACK_BASIS = "处方审核规则体系 §1 规范性审核（后记：签章完整清晰）"


@pytest.fixture(scope="module")
def rules():
    repository = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repository.load()
    assert repository.errors == (), "样例规则集不应有载入错误"
    return repository.effective_rules


@pytest.fixture(scope="module")
def front_diag_rule(rules):
    matched = [rule for rule in rules if rule.rule_id == "RX-NORM-FRONT-DIAG"]
    assert matched, "样例规则集应含前记临床诊断规则 RX-NORM-FRONT-DIAG"
    return matched[0]


@pytest.fixture(scope="module")
def back_signature_rule(rules):
    matched = [rule for rule in rules if rule.rule_id == "RX-NORM-BACK-SIGNATURE"]
    assert matched, "样例规则集应含后记签章规则 RX-NORM-BACK-SIGNATURE"
    return matched[0]


def _rule(**overrides: object) -> RuleRow:
    """按审方规则行口径造一条规范性缺项规则。"""
    base: dict[str, object] = {
        "rule_id": "RX-NORM-TEST-FRONT",
        "layer": "规范性",
        "basis": FRONT_BASIS,
        "version": "2026.10",
        "required_fields": ("clinical_diag",),
        "expression": "clinical_diag == ''",
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
        "clinical_diag": "社区获得性肺炎",
        "event_time": "2026-10-28 09:15:00",
    }
    defaults.update(values)
    return PrescriptionRow(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 口径表
# ---------------------------------------------------------------------------


def test_front_matter_covers_the_five_items_and_back_matter_the_signature() -> None:
    """前记五项（机构 / 患者身份 / 科别 / 临床诊断 / 日期）+ 后记签章。"""
    assert [item.name for item in FRONT_MATTER_ITEMS] == [
        "机构（开方科室）",
        "患者身份",
        "科别",
        "临床诊断",
        "日期",
    ]
    assert [item.name for item in BACK_MATTER_ITEMS] == ["签章"]
    assert FRONT_MATTER_FIELDS == (
        "dept_name",
        "patient_ref",
        "dept_code",
        "clinical_diag",
        "event_time",
    )
    assert BACK_MATTER_FIELDS == ("prescriber_id",)


def test_normativity_rule_is_identified_by_declaration_not_by_rule_id() -> None:
    """识别靠「层次 + 依赖字段落在口径表内」，不靠规则号前缀。

    「栏位在但没填」的结论由规则行声明（违规 / 不可判定），不参与识别——
    识别只回答「这条规则走哪条判定路径」。
    """
    assert is_normativity_rule(_rule()) is True
    # 层次不是规范性 → 不是
    assert is_normativity_rule(_rule(layer="合法性")) is False
    # 依赖字段落在前记 / 后记口径表外 → 不是（走通用表达式路径）
    assert is_normativity_rule(_rule(required_fields=("dosage_text",))) is False
    # 没声明依赖字段 → 不是
    assert is_normativity_rule(_rule(required_fields=())) is False
    # 缺项结论声明成「不可判定」仍是规范性规则（结论口径由规则作者定）
    assert (
        is_normativity_rule(_rule(missing_conclusion=Conclusion.UNDETERMINABLE)) is True
    )


def test_empty_value_conclusion_follows_the_rule_declaration() -> None:
    """「栏位在但没填」的结论跟着规则行的 ``missing_conclusion`` 走。"""
    rule = _rule(missing_conclusion=Conclusion.UNDETERMINABLE)

    evaluation = evaluate_normativity(rule, _row(clinical_diag=""))

    assert evaluation.status is NormativityStatus.UNDETERMINABLE
    assert evaluation.conclusion is Conclusion.UNDETERMINABLE
    assert evaluation.empty_fields == ("clinical_diag",)


# ---------------------------------------------------------------------------
# 断言 1：前记缺 clinical_diag → 违规并点名 clinical_diag
# ---------------------------------------------------------------------------


def test_missing_clinical_diag_is_violation_and_names_the_field(front_diag_rule) -> None:
    """前记缺临床诊断 → 违规（不合格处方），并点名缺的是哪一项。"""
    row = _row(clinical_diag="")

    evaluation = evaluate_normativity(front_diag_rule, row)

    assert evaluation.status is NormativityStatus.VIOLATION
    assert evaluation.conclusion is Conclusion.VIOLATION
    assert evaluation.empty_fields == ("clinical_diag",)
    assert evaluation.missing_fields == (), "栏位在但内容为空不属于「整列未落地」"
    assert "clinical_diag" in evaluation.named_fields
    assert [item.name for item in evaluation.named_items] == ["临床诊断"]

    reason = render_normativity_reason(evaluation, front_diag_rule, row)
    assert front_diag_rule.rule_id in reason
    assert front_diag_rule.version in reason
    assert front_diag_rule.basis in reason
    assert "clinical_diag" in reason, "归因文本必须点名缺哪个字段"
    assert "临床诊断" in reason, "归因文本必须点名缺前记的哪一项"
    assert "前记" in reason


def test_front_matter_violation_enters_the_ledger_with_the_field_named(
    front_diag_rule,
) -> None:
    """判定链上：前记缺项的结论落违规，missing_fields 点名字段。"""
    row = _row(clinical_diag="")

    judgements = judge_prescription(row, [front_diag_rule], batch_no="BATCH-TEST")

    assert len(judgements) == 1
    judgement = judgements[0]
    assert judgement.conclusion is Conclusion.VIOLATION
    assert "clinical_diag" in judgement.missing_fields
    assert "临床诊断" in judgement.reason


# ---------------------------------------------------------------------------
# 断言 2：缺项与空值区分
# ---------------------------------------------------------------------------


def test_absent_column_is_undeterminable_while_empty_value_is_violation() -> None:
    """整列未落地 → 不可判定（接口缺口）；栏位在但内容为空 → 违规（处方不合格）。"""
    rule = _rule()

    base = _row(clinical_diag="").__dict__
    absent = PrescriptionRow(
        **{**base, "present_columns": frozenset({"prescription_no", "dept_code", "event_time"})}
    )
    empty = PrescriptionRow(
        **{
            **base,
            "present_columns": frozenset(
                {"prescription_no", "clinical_diag", "dept_code", "event_time"}
            ),
        }
    )

    absent_result = evaluate_normativity(rule, absent)
    empty_result = evaluate_normativity(rule, empty)

    assert absent_result.status is NormativityStatus.UNDETERMINABLE
    assert absent_result.conclusion is Conclusion.UNDETERMINABLE
    assert absent_result.missing_fields == ("clinical_diag",)
    assert absent_result.empty_fields == ()
    assert "整列未落地" in absent_result.detail
    assert "接口" in absent_result.detail

    assert empty_result.status is NormativityStatus.VIOLATION
    assert empty_result.conclusion is Conclusion.VIOLATION
    assert empty_result.empty_fields == ("clinical_diag",)
    assert empty_result.missing_fields == ()
    assert "内容为空" in empty_result.detail

    # 两条结论互不相同，且都点名同一个字段——处置完全不同
    assert absent_result.conclusion is not empty_result.conclusion
    assert absent_result.named_fields == empty_result.named_fields == ("clinical_diag",)


def test_unregistered_present_columns_falls_back_to_empty_value_semantics() -> None:
    """未登记列集合时视为全部列存在（保持旧行为）。"""
    rule = _rule()

    row = _row(clinical_diag="")
    assert row.present_columns == frozenset()
    assert row.has_column("clinical_diag") is True

    evaluation = evaluate_normativity(rule, row)
    assert evaluation.status is NormativityStatus.VIOLATION


def test_absent_column_reaches_the_judgement_chain_as_undeterminable() -> None:
    """判定链上：整列未落地 → 不可判定并点名字段（进字段对照台账的原料）。"""
    rule = _rule()
    row = PrescriptionRow(
        **{
            **_row(clinical_diag="").__dict__,
            "present_columns": frozenset({"prescription_no", "dept_code", "event_time"}),
        }
    )

    judgements = judge_prescription(row, [rule], batch_no="BATCH-TEST")

    assert len(judgements) == 1
    judgement = judgements[0]
    assert judgement.conclusion is Conclusion.UNDETERMINABLE
    assert "clinical_diag" in judgement.missing_fields
    assert "clinical_diag" in judgement.reason
    assert "整列未落地" in judgement.reason


def test_csv_without_the_column_flows_into_undeterminable(tmp_path: Path) -> None:
    """端到端：落地文件整列没回传 → 判定层落不可判定并点名字段。

    走真实入站路径（CSV → 处方行 → 判定），证明「接口缺列」与「医师漏填」
    在台账上是两种不同的行。
    """
    source = tmp_path / "orders_no_diag.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "batch_no",
                "prescription_no",
                "visit_no",
                "patient_ref",
                "dept_code",
                "dept_name",
                "prescriber_id",
                "drug_code",
                "drug_name",
                "event_time",
            ]
        )
        writer.writerow(
            [
                "BATCH-TEST",
                "RX-TEST-0001",
                "VISIT-TEST",
                "PT-TEST",
                "D101",
                "呼吸内科门诊",
                "PR-001",
                "DC-001",
                "阿莫西林胶囊",
                "2026-10-28 09:15:00",
            ]
        )

    rows = read_prescription_rows(source)
    assert len(rows) == 1
    assert rows[0].has_column("clinical_diag") is False, "该列整列未落地"
    assert rows[0].has_column("dept_code") is True

    judgements = judge_prescription(rows[0], [_rule()], batch_no="BATCH-TEST")
    assert len(judgements) == 1
    assert judgements[0].conclusion is Conclusion.UNDETERMINABLE
    assert "clinical_diag" in judgements[0].missing_fields


def test_csv_with_the_column_but_empty_value_is_violation(tmp_path: Path) -> None:
    """端到端：栏位在但没填 → 违规（退回医师补写）。"""
    source = tmp_path / "orders_empty_diag.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "batch_no",
                "prescription_no",
                "visit_no",
                "patient_ref",
                "dept_code",
                "dept_name",
                "prescriber_id",
                "drug_code",
                "drug_name",
                "clinical_diag",
                "event_time",
            ]
        )
        writer.writerow(
            [
                "BATCH-TEST",
                "RX-TEST-0001",
                "VISIT-TEST",
                "PT-TEST",
                "D101",
                "呼吸内科门诊",
                "PR-001",
                "DC-001",
                "阿莫西林胶囊",
                "",
                "2026-10-28 09:15:00",
            ]
        )

    rows = read_prescription_rows(source)
    assert rows[0].has_column("clinical_diag") is True
    assert rows[0].is_missing("clinical_diag") is True

    judgements = judge_prescription(rows[0], [_rule()], batch_no="BATCH-TEST")
    assert len(judgements) == 1
    assert judgements[0].conclusion is Conclusion.VIOLATION
    assert "clinical_diag" in judgements[0].missing_fields


# ---------------------------------------------------------------------------
# 断言 3：后记缺签章单独成条
# ---------------------------------------------------------------------------


def test_missing_signature_is_violation_naming_the_signature_field(
    back_signature_rule,
) -> None:
    """后记缺签章 → 违规，并点名缺的是后记签章。"""
    row = _row(prescriber_id="")

    evaluation = evaluate_normativity(back_signature_rule, row)

    assert evaluation.status is NormativityStatus.VIOLATION
    assert evaluation.empty_fields == ("prescriber_id",)
    assert [item.name for item in evaluation.named_items] == ["签章"]

    reason = render_normativity_reason(evaluation, back_signature_rule, row)
    assert "prescriber_id" in reason
    assert "后记" in reason
    assert "签章" in reason


def test_front_and_back_matter_are_separate_judgement_rows(
    front_diag_rule, back_signature_rule
) -> None:
    """前记缺项与后记缺签章各占一条判定行，各点名各自的字段。"""
    row = _row(clinical_diag="", prescriber_id="")

    judgements = judge_prescription(
        row, [front_diag_rule, back_signature_rule], batch_no="BATCH-TEST"
    )

    assert len(judgements) == 2, "前记与后记必须单独成条"
    by_rule = {judgement.rule_id: judgement for judgement in judgements}
    assert set(by_rule) == {
        "RX-NORM-FRONT-DIAG",
        "RX-NORM-BACK-SIGNATURE",
    }

    front = by_rule["RX-NORM-FRONT-DIAG"]
    back = by_rule["RX-NORM-BACK-SIGNATURE"]
    assert front.conclusion is Conclusion.VIOLATION
    assert front.missing_fields == ("clinical_diag",)
    assert back.conclusion is Conclusion.VIOLATION
    assert back.missing_fields == ("prescriber_id",)
    assert "前记" in front.reason
    assert "后记" in back.reason


def test_front_and_back_must_not_mix_in_one_rule() -> None:
    """前记字段与后记字段混在一条规则里 → 显式报配置错误。"""
    mixed = _rule(
        rule_id="RX-NORM-TEST-MIXED",
        required_fields=("clinical_diag", "prescriber_id"),
    )

    issues = validate_normativity_rule(mixed)
    assert any("单独成条" in issue for issue in issues)

    with pytest.raises(NormativityConfigurationError):
        evaluate_normativity(mixed, _row())


# ---------------------------------------------------------------------------
# 配置错误不降级
# ---------------------------------------------------------------------------


def test_configuration_errors_are_reported_explicitly() -> None:
    """缺依赖 / 字段不在口径表内 / 前记与后记混用 → 各自显式报错。"""
    no_fields = _rule(required_fields=())
    unknown_field = _rule(required_fields=("no_such_field",))
    mixed = _rule(required_fields=("clinical_diag", "prescriber_id"))

    assert any("required_fields" in issue for issue in validate_normativity_rule(no_fields))
    assert any("口径表" in issue for issue in validate_normativity_rule(unknown_field))
    assert any("单独成条" in issue for issue in validate_normativity_rule(mixed))

    for rule in (no_fields, unknown_field, mixed):
        with pytest.raises(NormativityConfigurationError):
            evaluate_normativity(rule, _row())


def test_judge_does_not_downgrade_configuration_errors() -> None:
    """配置错误在判定链上显式报错，不得降级成「不可判定」。"""
    mixed = _rule(
        rule_id="RX-NORM-TEST-MIXED",
        required_fields=("clinical_diag", "prescriber_id"),
    )

    with pytest.raises(ExpressionError):
        judge_prescription(_row(), [mixed], batch_no="BATCH-TEST")


def test_rule_not_applicable_when_applies_when_does_not_match() -> None:
    """适用条件不命中 → 不产生判定行（不进台账）。"""
    rule = _rule(applies_when={"dept_code": "D999"})

    evaluation = evaluate_normativity(rule, _row(dept_code="D101"))
    assert evaluation.status is NormativityStatus.NOT_APPLICABLE

    judgements = judge_prescription(_row(dept_code="D101"), [rule], batch_no="BATCH-TEST")
    assert judgements == []


# ---------------------------------------------------------------------------
# 主干回归：样例批次
# ---------------------------------------------------------------------------


def test_sample_batch_front_matter_diagnosis_is_violation(rules) -> None:
    """样例批次里缺临床诊断的那张处方 → 违规并点名（与主干口径一致）。"""
    rows = read_prescription_rows(BATCH_FILE)
    judgement = judge_prescription(
        next(row for row in rows if row.prescription_no == "RX-0003"),
        rules,
        batch_no="DEMO-BATCH-001",
    )
    by_rule = {item.rule_id: item for item in judgement}

    front = by_rule["RX-NORM-FRONT-DIAG"]
    assert front.conclusion is Conclusion.VIOLATION
    assert "clinical_diag" in front.missing_fields
    assert "临床诊断" in front.reason


def test_sample_batch_back_matter_signature_is_compliant(rules) -> None:
    """样例批次的开立医师签章标识齐备 → 后记签章规则落合规。"""
    rows = read_prescription_rows(BATCH_FILE)
    judgement = judge_prescription(rows[0], rules, batch_no="DEMO-BATCH-001")
    by_rule = {item.rule_id: item for item in judgement}

    back = by_rule["RX-NORM-BACK-SIGNATURE"]
    assert back.conclusion is Conclusion.COMPLIANT
    assert back.missing_fields == ()
    assert "后记" in back.reason


def test_sample_batch_front_matter_other_items_are_compliant(rules) -> None:
    """样例批次前记其余四项齐备 → 前记抬头规则落合规。"""
    rows = read_prescription_rows(BATCH_FILE)
    judgement = judge_prescription(rows[0], rules, batch_no="DEMO-BATCH-001")
    by_rule = {item.rule_id: item for item in judgement}

    front = by_rule["RX-NORM-FRONT-HEADER"]
    assert front.conclusion is Conclusion.COMPLIANT
    assert front.missing_fields == ()


def test_normativity_reasons_carry_no_patient_identifier(rules) -> None:
    """归因文本不得带出患者可识别明细（只允许脱敏引用号）。"""
    rows = read_prescription_rows(BATCH_FILE)
    identifiers = {row.patient_ref for row in rows}

    for row in rows:
        for judgement in judge_prescription(row, rules, batch_no="DEMO-BATCH-001"):
            if judgement.rule_id.startswith("RX-NORM-"):
                for identifier in identifiers:
                    assert identifier not in judgement.reason


def test_items_for_rule_follows_the_declaration_order(front_diag_rule) -> None:
    """规则声明的依赖字段 → 栏位口径（按声明顺序）。"""
    items = items_for_rule(front_diag_rule)
    assert [item.field for item in items] == ["clinical_diag"]
    assert items[0].describe() == "前记「临床诊断」"
