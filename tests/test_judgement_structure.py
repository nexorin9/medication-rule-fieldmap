"""判定内核 1/10：判定结论结构 + 归因文本（error_text 归因位）。

对应任务书 T13。本轮把「结论怎么落」「归因文本怎么写」「归因文本不许带什么」
三件事定死，后面 9 条判定内核任务（规则调度、量限矩阵、皮试、状态机……）都
在这套结构上扩展，不再各写各的。

覆盖三组断言：

1. **归因文本含规则号、规则版本、依据出处与缺失字段名**——药师看一句话就能复算；
2. **三种结论各有固定句式**——合规 / 违规 / 不可判定的归因文本互不相同，
   句式固定在 ``engine/evidence.py`` 的模板里，回归可逐字比对；
3. **归因文本不含患者可识别明细**——只允许出现 ``patient_ref`` 这一脱敏引用号，
   姓名 / 身份证号 / 手机号 / 住址 / 联系人 / 开方医师姓名一律不得出现。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.evidence import (
    ALLOWED_PII_FIELDS,
    CONCLUSION_TEMPLATES,
    MASK_TEXT,
    EvidencePrivacyError,
    assert_privacy_safe,
    build_evidence,
    build_reason,
    detect_pii_leaks,
    forbidden_values,
    mask_forbidden_values,
    render_reason,
)
from med_rule_fieldmap.engine.judge import (
    JUDGEMENT_FIELDS,
    evaluate_expression,
    judge_batch,
)
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.models import Conclusion, JudgementRow, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"
BATCH_FILE = BATCH_DIR / "demo_batch_001.csv"

AS_OF = date(2026, 10, 28)

RULE = RuleRow(
    rule_id="RX-LEGAL-MA-OUT-DAYS",
    layer="合法性",
    basis="处方审核规则体系 §3 特殊药品处方量限",
    version="2026.10",
    applies_when={},
    required_fields=("rx_type", "drug_category", "days_qty"),
    expression="days_qty > 1",
    severity="严重不合理用药",
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


# ---------------------------------------------------------------------------
# 结论结构
# ---------------------------------------------------------------------------


def test_conclusion_has_exactly_three_states() -> None:
    assert [member.value for member in Conclusion] == ["合规", "违规", "不可判定"]
    assert Conclusion.COMPLIANT is not Conclusion.VIOLATION
    assert Conclusion("不可判定") is Conclusion.UNDETERMINABLE


def test_judgement_row_carries_the_documented_ten_fields() -> None:
    assert JUDGEMENT_FIELDS == (
        "batch_no",
        "prescription_no",
        "rule_id",
        "rule_version",
        "conclusion",
        "severity",
        "reason",
        "basis",
        "missing_fields",
        "event_fingerprint",
    )

    row = JudgementRow(
        batch_no="B-001",
        prescription_no="RX-0001",
        rule_id=RULE.rule_id,
        rule_version=RULE.version,
        conclusion=Conclusion.VIOLATION,
        severity=RULE.severity,
        reason="规则 …判定：违规。",
        basis=RULE.basis,
        missing_fields=("days_qty",),
        event_fingerprint="f" * 16,
    )
    record = row.to_dict()
    assert set(JUDGEMENT_FIELDS).issubset(record)
    assert record["conclusion"] == "违规"
    assert record["missing_fields"] == ["days_qty"]
    assert record["event_fingerprint"] == "f" * 16


# ---------------------------------------------------------------------------
# 归因文本：规则号 / 版本 / 依据 / 缺失字段名
# ---------------------------------------------------------------------------


def test_reason_carries_rule_id_version_and_basis() -> None:
    text = build_reason(Conclusion.VIOLATION, RULE, detail="判定条件「days_qty > 1」命中。")
    assert RULE.rule_id in text
    assert RULE.version in text
    assert RULE.basis in text


def test_undeterminable_reason_names_the_missing_fields() -> None:
    text = build_reason(
        Conclusion.UNDETERMINABLE,
        RULE,
        detail="字段 rx_type 缺失，无法完成判定",
        missing_fields=("rx_type", "drug_category"),
    )
    assert "rx_type" in text
    assert "drug_category" in text
    assert RULE.rule_id in text
    assert RULE.basis in text


def test_violation_reason_for_missing_content_also_names_the_field() -> None:
    """规范性前记缺项类规则：结论定为违规，但缺项仍要点名。"""
    text = build_reason(
        Conclusion.VIOLATION,
        RULE,
        detail="缺少必填内容 clinical_diag。",
        missing_fields=("clinical_diag",),
    )
    assert "clinical_diag" in text
    assert "判定：违规" in text


# ---------------------------------------------------------------------------
# 归因文本：三种结论句式固定且互不相同
# ---------------------------------------------------------------------------


def test_three_conclusions_use_distinct_fixed_sentence_patterns() -> None:
    texts = {
        Conclusion.COMPLIANT: build_reason(
            Conclusion.COMPLIANT, RULE, detail="", missing_fields=()
        ),
        Conclusion.VIOLATION: build_reason(
            Conclusion.VIOLATION, RULE, detail="判定条件「days_qty > 1」命中。"
        ),
        Conclusion.UNDETERMINABLE: build_reason(
            Conclusion.UNDETERMINABLE,
            RULE,
            detail="字段 rx_type 缺失，无法完成判定",
            missing_fields=("rx_type",),
        ),
    }

    assert len(set(texts.values())) == 3, "三种结论的归因文本必须互不相同"
    assert "判定：合规。" in texts[Conclusion.COMPLIANT]
    assert "判定：违规。" in texts[Conclusion.VIOLATION]
    assert "判定：不可判定。" in texts[Conclusion.UNDETERMINABLE]

    # 句式固定在模板里：改模板即改口径，回归可逐字比对
    assert set(CONCLUSION_TEMPLATES) == set(Conclusion)
    for conclusion, template in CONCLUSION_TEMPLATES.items():
        evidence = build_evidence(
            conclusion,
            RULE,
            detail="命中详情。",
            missing_fields=("rx_type",),
        )
        assert evidence.template == template
        assert evidence.render() == template.format(
            rule_id=RULE.rule_id,
            rule_version=RULE.version,
            expression=RULE.expression,
            detail="命中详情。",
            basis=RULE.basis,
            missing_fields="rx_type",
        )


# ---------------------------------------------------------------------------
# 隐私约束
# ---------------------------------------------------------------------------


def test_forbidden_fields_cover_patient_identifiers_and_allow_patient_ref() -> None:
    assert "patient_name" in forbidden_values({"patient_name": "张三"})
    assert "id_card" in forbidden_values({"id_card": "110101199001011234"})
    assert "prescriber_name" in forbidden_values({"prescriber_name": "医师甲"})
    # patient_ref 是唯一允许出现的一支
    assert "patient_ref" not in forbidden_values({"patient_ref": "PT-0001"})
    assert ALLOWED_PII_FIELDS == ("patient_ref",)


def test_reason_never_carries_patient_identifiers(rules, prescription_rows) -> None:
    """样例批次全量判定后，归因文本不得带出任何患者可识别明细。"""
    identifiers = {row.patient_ref for row in prescription_rows}
    judgement = judge_batch(
        prescription_rows, rules, batch_no="DEMO-BATCH-001", event_fingerprint="fp-1"
    )
    assert judgement.rows, "样例批次应产生判定行"

    for row in judgement.rows:
        assert detect_pii_leaks(row.reason, {}) == []
        for identifier in identifiers:
            assert identifier not in row.reason, "归因文本不得带出患者标识"
        assert row.basis in row.reason
        assert row.rule_id in row.reason


def test_detail_carrying_a_patient_name_is_masked() -> None:
    fields = {
        "patient_ref": "PT-0001",
        "patient_name": "张三",
        "id_card": "110101199001011234",
    }
    text = build_reason(
        Conclusion.VIOLATION,
        RULE,
        detail="患者张三 的处方用量超标。",
        fields=fields,
    )
    assert "张三" not in text
    assert "110101199001011234" not in text
    assert MASK_TEXT in text
    assert detect_pii_leaks(text, fields) == []


def test_detect_pii_leaks_points_at_the_leaking_field() -> None:
    fields = {"patient_name": "张三", "patient_ref": "PT-0001"}
    assert detect_pii_leaks("患者张三 的处方", fields) == ["patient_name"]
    assert detect_pii_leaks("患者 PT-0001 的处方", fields) == []


def test_assert_privacy_safe_raises_on_leak() -> None:
    fields = {"patient_name": "张三"}
    with pytest.raises(EvidencePrivacyError) as excinfo:
        assert_privacy_safe("患者张三 的处方", fields)
    assert "patient_name" in str(excinfo.value)


def test_mask_forbidden_values_leaves_allowed_fields_untouched() -> None:
    fields = {"patient_name": "张三", "patient_ref": "PT-0001"}
    masked = mask_forbidden_values("处方 PT-0001 由张三开具", fields)
    assert "PT-0001" in masked
    assert "张三" not in masked


def test_render_reason_without_fields_skips_masking() -> None:
    evidence = build_evidence(Conclusion.COMPLIANT, RULE)
    assert render_reason(evidence) == evidence.render()
    assert render_reason(evidence, {"patient_name": "张三"}) == evidence.render()


# ---------------------------------------------------------------------------
# 与判定主路径的衔接
# ---------------------------------------------------------------------------


def test_judged_rows_expose_the_evidence_elements(rules, prescription_rows) -> None:
    judgement = judge_batch(
        prescription_rows, rules, batch_no="DEMO-BATCH-001", event_fingerprint="fp-1"
    )
    assert judgement.rows

    by_conclusion = {row.conclusion for row in judgement.rows}
    assert Conclusion.COMPLIANT in by_conclusion
    assert Conclusion.UNDETERMINABLE in by_conclusion

    for row in judgement.rows:
        assert row.rule_id in row.reason
        assert row.rule_version in row.reason
        assert row.basis in row.reason
        assert row.event_fingerprint == "fp-1"

    undeterminable = [row for row in judgement.rows if row.conclusion is Conclusion.UNDETERMINABLE]
    assert undeterminable
    for row in undeterminable:
        assert row.missing_fields, "不可判定行必须点名缺失字段"
        for name in row.missing_fields:
            assert name in row.reason


def test_evaluation_uses_the_same_three_states() -> None:
    row = PrescriptionRow(days_qty="")
    evaluation = evaluate_expression("days_qty > 1", row)
    assert evaluation.truth.value == "unknown"
    assert evaluation.missing == ("days_qty",)
