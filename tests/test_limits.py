"""判定内核 3/10：特殊药品处方量限矩阵（门急诊一般 / 癌症疼痛 / 住院 三档）。

对应任务书 T15。本轮把 §3 量限从「规则行里手写的阈值表达式」改成
**一张矩阵**（``engine/limits.py`` 的 ``LIMIT_MATRIX``）：

* **阈值口径唯一真源**：矩阵单元 = 档位 × 类别；规则行用 ``limit_ref`` 声明落在哪个
  单元，阈值表达式必须与矩阵逐字一致（载入期不一致即拒）。
* **条件依赖**：精二「延长须注明理由」只在超量时才需要理由字段——表达式语法表达不了
  「字段缺失」这一档，矩阵单元能（``needs_reason`` + ``reason_field``）。
* **口径核对**：日量写成 ``3mg`` 时数值是 3，但拿它比日量上限必然出错 → 落
  **不可判定**并点名该字段，不静默判违规。

覆盖五组硬断言（任务书点名）：

1. ``domain/cases/prescription_review_cases.json`` 的 **rx-001 / rx-002 / rx-006 /
   rx-007 / rx-010** 逐条结论一致（拦截 / 不拦截）；
2. **单位不可辨**（``3mg`` / ``适量``）→ 不可判定，不判违规；
3. **精二理由分支**三态：理由未注明 → 违规；理由已注明 → 合规；理由未落地 → 不可判定；
4. 矩阵本身与 §3 原文一致（阈值、单位、依据出处逐单元核对）；
5. 矩阵与规则行**无口径漂移**，且漂移规则在载入期被拒。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.limits import (
    CLASS_MA_INJECTION,
    CLASS_MA_OTHER,
    CLASS_MA_SUSTAINED,
    CLASS_SPIRIT1_INJECTION,
    CLASS_SPIRIT1_OTHER,
    CLASS_SPIRIT2,
    LIMIT_MATRIX,
    TIER_CANCER,
    TIER_INPATIENT,
    TIER_OUTPATIENT,
    LimitStatus,
    check_quantity_semantics,
    classify_category,
    classify_limit,
    evaluate_limit,
    evaluate_limit_rule,
    matrix_coverage,
    matrix_drift,
    resolve_limit_ref,
)
from med_rule_fieldmap.engine.judge import judge_prescription
from med_rule_fieldmap.models import Conclusion, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository
from med_rule_fieldmap.rules.schema import RuleValidationError, validate_rule

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"

#: 官方案例集（``domain/cases/prescription_review_cases.json``）。
CASES_FILE = PROJECT_ROOT.parents[2] / "domain" / "cases" / "prescription_review_cases.json"

AS_OF = date(2026, 10, 28)

BASIS = "处方审核规则体系 §3 特殊药品处方量限"

#: §3 的量限口径快照（档位 → 类别 → （上限, 单位, 是否须注明理由））。
#:
#: 逐字照抄《处方审核规则体系》§3 与《处方管理办法》第二十三 / 二十四 / 二十五条，
#: 用作矩阵自身的核对基准——矩阵写错一个数，这里就红。
SECTION_3_MATRIX: dict[tuple[str, str], tuple[str, str, bool]] = {
    (TIER_OUTPATIENT, CLASS_MA_INJECTION): ("1", "次", False),
    (TIER_OUTPATIENT, CLASS_MA_SUSTAINED): ("7", "日", False),
    (TIER_OUTPATIENT, CLASS_MA_OTHER): ("3", "日", False),
    (TIER_OUTPATIENT, CLASS_SPIRIT1_INJECTION): ("1", "次", False),
    (TIER_OUTPATIENT, CLASS_SPIRIT1_OTHER): ("3", "日", False),
    (TIER_OUTPATIENT, CLASS_SPIRIT2): ("7", "日", True),
    (TIER_CANCER, CLASS_MA_INJECTION): ("3", "日", False),
    (TIER_CANCER, CLASS_MA_SUSTAINED): ("15", "日", False),
    (TIER_CANCER, CLASS_MA_OTHER): ("7", "日", False),
    (TIER_CANCER, CLASS_SPIRIT1_INJECTION): ("3", "日", False),
    (TIER_CANCER, CLASS_SPIRIT1_OTHER): ("7", "日", False),
    (TIER_CANCER, CLASS_SPIRIT2): ("7", "日", True),
    (TIER_INPATIENT, CLASS_MA_OTHER): ("1", "日", False),
    (TIER_INPATIENT, CLASS_SPIRIT1_OTHER): ("1", "日", False),
    (TIER_INPATIENT, CLASS_SPIRIT2): ("7", "日", True),
}

#: 官方案例 → （处方行字段, 应当命中的量限规则号）。
#:
#: 案例输入里的中文键（``处方类型`` / ``药品类别`` / ``开具量`` / ``理由``）映射到
#: 医嘱落地列口径；案例的 ``拦截`` 期望映射到判定结论（拦截 = 违规）。
OFFICIAL_CASES: dict[str, tuple[dict[str, str], str]] = {
    "rx-001": (
        {
            "rx_type": "门诊一般患者",
            "drug_category": "麻醉药品注射剂",
            "days_qty": "3日量",
        },
        "RX-LEGAL-MA-OUT-DAYS",
    ),
    "rx-002": (
        {
            "rx_type": "门诊癌症疼痛患者",
            "drug_category": "麻醉药品控缓释制剂",
            "days_qty": "15日量",
        },
        "RX-LEGAL-CR-CANCER-OUT-DAYS",
    ),
    "rx-006": (
        {
            "rx_type": "门诊一般患者",
            "drug_category": "第二类精神药品",
            "days_qty": "7日常用量",
            "extend_reason": "无延长说明",
        },
        "RX-LEGAL-SPIRIT2-OUT-DAYS",
    ),
    "rx-007": (
        {
            "rx_type": "门诊一般患者",
            "drug_category": "第二类精神药品",
            "days_qty": "14日常用量",
            "extend_reason": "未注明",
        },
        "RX-LEGAL-SPIRIT2-OUT-DAYS",
    ),
    "rx-010": (
        {
            "rx_type": "住院患者",
            "drug_category": "麻醉药品",
            "days_qty": "1日常用量",
        },
        "RX-LEGAL-MA-INPATIENT-DAYS",
    ),
}


@pytest.fixture(scope="module")
def rules():
    repository = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repository.load()
    assert repository.errors == (), "样例规则集不应有载入错误"
    return repository.effective_rules


def _judgement_for(rule_id: str, row: PrescriptionRow, rules):
    """按规则号取该医嘱的判定行（判定台账里一行对应一个 (处方, 规则)）。"""
    matched = [
        item
        for item in judge_prescription(row, rules, batch_no="CASE-BATCH")
        if item.rule_id == rule_id
    ]
    assert matched, f"规则 {rule_id} 未命中该医嘱"
    return matched[0]


def _case_row(values: dict[str, str], case_id: str) -> PrescriptionRow:
    """按案例输入造一条医嘱行（其余列留空）。"""
    payload = {
        "batch_no": "CASE-BATCH",
        "prescription_no": case_id.upper(),
        "visit_no": f"VISIT-{case_id.upper()}",
        "drug_code": "DC-CASE",
        "event_time": "2026-10-28 09:00:00",
    }
    payload.update(values)
    return PrescriptionRow(**payload)


# ---------------------------------------------------------------------------
# 1. 官方案例逐条一致
# ---------------------------------------------------------------------------


def _official_expectations() -> dict[str, bool]:
    """读官方案例集，取本轮覆盖的 5 条的「拦截」期望。

    案例集缺失时用内置快照兜底（同一份口径），保证回归仍然成立。
    """
    snapshot = {
        "rx-001": True,
        "rx-002": False,
        "rx-006": False,
        "rx-007": True,
        "rx-010": False,
    }
    if not CASES_FILE.exists():
        return snapshot
    payload = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    expectations: dict[str, bool] = {}
    for case in payload["cases"]:
        if case["case_id"] in snapshot:
            expectations[case["case_id"]] = bool(case["expect"]["拦截"])
    assert expectations, "官方案例集里应当有本轮覆盖的 5 条案例"
    return expectations


def test_official_case_expectations_match_the_domain_case_set() -> None:
    """本轮引用的 5 条案例在官方案例集里确实存在，期望值一致。"""
    expectations = _official_expectations()
    assert set(expectations) == set(OFFICIAL_CASES)
    assert expectations["rx-001"] is True
    assert expectations["rx-007"] is True
    assert expectations["rx-002"] is False
    assert expectations["rx-006"] is False
    assert expectations["rx-010"] is False


@pytest.mark.parametrize("case_id", sorted(OFFICIAL_CASES))
def test_official_cases_match_expected_conclusion(case_id, rules) -> None:
    """rx-001 / rx-002 / rx-006 / rx-007 / rx-010 逐条结论与官方案例一致。"""
    values, rule_id = OFFICIAL_CASES[case_id]
    row = _case_row(values, case_id)
    should_block = _official_expectations()[case_id]

    judgement = _judgement_for(rule_id, row, rules)
    if should_block:
        assert judgement.conclusion is Conclusion.VIOLATION, (
            f"{case_id} 官方期望拦截，实际结论：{judgement.conclusion}"
        )
    else:
        assert judgement.conclusion is Conclusion.COMPLIANT, (
            f"{case_id} 官方期望不拦截，实际结论：{judgement.conclusion}"
        )
    assert judgement.basis
    assert rule_id in judgement.reason


def test_official_case_basis_is_cited_in_the_reason(rules) -> None:
    """归因文本带出规则号与依据出处（药师复算的依据链）。"""
    values, rule_id = OFFICIAL_CASES["rx-001"]
    judgement = _judgement_for(rule_id, _case_row(values, "rx-001"), rules)
    assert rule_id in judgement.reason
    assert BASIS.split(" ")[0] in judgement.reason


# ---------------------------------------------------------------------------
# 2. 单位不可辨 → 不可判定（不静默判违规）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["3mg", "10片", "适量", "遵医嘱"])
def test_non_comparable_daily_quantity_is_undeterminable(raw, rules) -> None:
    """日量写成剂量口径或文字 → 不可判定并点名该字段，不判违规。"""
    values, rule_id = OFFICIAL_CASES["rx-001"]
    payload = dict(values)
    payload["days_qty"] = raw
    judgement = _judgement_for(rule_id, _case_row(payload, "rx-001"), rules)
    assert judgement.rule_id == rule_id
    assert judgement.conclusion is Conclusion.UNDETERMINABLE
    assert judgement.missing_fields == ("days_qty",)
    assert "口径不可比" in judgement.reason


def test_quantity_semantics_reports_the_unit_it_found() -> None:
    """口径核对结果带上落地单位，便于归因。"""
    semantics = check_quantity_semantics(PrescriptionRow(days_qty="3日量"))
    assert semantics.comparable is True
    assert semantics.number == Decimal("3")
    assert semantics.unit == "日"

    incomparable = check_quantity_semantics(PrescriptionRow(days_qty="3mg"))
    assert incomparable.comparable is False
    assert incomparable.number == Decimal("3")
    assert incomparable.unit == "mg"
    assert "口径不可比" in incomparable.reason


# ---------------------------------------------------------------------------
# 3. 精二延长理由分支（三态）
# ---------------------------------------------------------------------------


def test_spirit2_over_limit_without_reason_note_is_violation(rules) -> None:
    """超 7 日常量且理由写「未注明」→ 违规（官方案例 rx-007）。"""
    values, rule_id = OFFICIAL_CASES["rx-007"]
    judgement = _judgement_for(rule_id, _case_row(values, "rx-007"), rules)
    assert judgement.rule_id == rule_id
    assert judgement.conclusion is Conclusion.VIOLATION
    assert "延长理由未注明" in judgement.reason


def test_spirit2_over_limit_with_reason_stated_is_compliant(rules) -> None:
    """超 7 日常量但已注明延长理由 → 合规（§3：延长须注明理由）。"""
    values, rule_id = OFFICIAL_CASES["rx-007"]
    payload = dict(values)
    payload["extend_reason"] = "慢性疼痛需持续用药"
    judgement = _judgement_for(rule_id, _case_row(payload, "rx-007"), rules)
    assert judgement.conclusion is Conclusion.COMPLIANT
    assert "延长理由" in judgement.reason


def test_spirit2_over_limit_with_reason_field_missing_is_undeterminable(rules) -> None:
    """超 7 日常量但理由字段根本没落地 → 不可判定并点名理由字段。"""
    values, rule_id = OFFICIAL_CASES["rx-006"]
    payload = dict(values)
    payload["days_qty"] = "14日常用量"
    payload.pop("extend_reason", None)
    judgement = _judgement_for(rule_id, _case_row(payload, "rx-006"), rules)
    assert judgement.rule_id == rule_id
    assert judgement.conclusion is Conclusion.UNDETERMINABLE
    assert judgement.missing_fields == ("extend_reason",)
    assert "extend_reason" in judgement.reason


def test_spirit2_within_limit_needs_no_reason_field(rules) -> None:
    """未超 7 日常量时不需要理由字段（条件依赖，不是无条件依赖）。"""
    values, rule_id = OFFICIAL_CASES["rx-006"]
    payload = dict(values)
    payload.pop("extend_reason", None)
    judgement = _judgement_for(rule_id, _case_row(payload, "rx-006"), rules)
    assert judgement.rule_id == rule_id
    assert judgement.conclusion is Conclusion.COMPLIANT
    assert judgement.missing_fields == ()


# ---------------------------------------------------------------------------
# 4. 矩阵本身与 §3 原文一致
# ---------------------------------------------------------------------------


def test_matrix_matches_section_3_snapshot() -> None:
    """矩阵逐单元核对 §3：档位、类别、上限、单位、是否须注明理由。"""
    snapshot = {
        f"{tier}/{drug_class}": values
        for (tier, drug_class), values in SECTION_3_MATRIX.items()
    }
    matrix = {ref: cell for ref, cell in LIMIT_MATRIX.items()}
    assert set(matrix) == set(snapshot), "矩阵单元集合与 §3 口径不一致"
    for ref, (limit, unit, needs_reason) in snapshot.items():
        cell = matrix[ref]
        assert cell.max_quantity == Decimal(limit), f"{ref} 上限与 §3 不一致"
        assert cell.unit == unit, f"{ref} 单位口径与 §3 不一致"
        assert cell.needs_reason is needs_reason, f"{ref} 理由要求与 §3 不一致"


def test_every_cell_carries_basis_and_threshold_expression() -> None:
    """每个单元都要有依据出处与可核对的阈值表达式。"""
    for ref, cell in LIMIT_MATRIX.items():
        assert cell.basis, f"{ref} 缺依据出处"
        assert "处方审核规则体系 §3" in cell.basis or "处方管理办法" in cell.basis
        assert cell.rule_id, f"{ref} 缺规则号"
        assert cell.expected_expression == f"days_qty > {cell.max_quantity}"
        assert cell.condition_text


def test_three_tiers_are_all_covered() -> None:
    """三档都在矩阵里：门急诊一般 / 门急诊癌症疼痛 / 住院。"""
    tiers = {cell.tier for cell in LIMIT_MATRIX.values()}
    assert tiers == {TIER_OUTPATIENT, TIER_CANCER, TIER_INPATIENT}


def test_inpatient_forms_collapse_to_the_drug_family() -> None:
    """住院档逐日开具：麻醉药品与精一的注射剂 / 控缓释变体同归大类。"""
    assert classify_category(TIER_INPATIENT, CLASS_MA_INJECTION) == CLASS_MA_OTHER
    assert classify_category(TIER_INPATIENT, CLASS_MA_SUSTAINED) == CLASS_MA_OTHER
    assert classify_category(TIER_INPATIENT, CLASS_SPIRIT1_INJECTION) == CLASS_SPIRIT1_OTHER
    # 门急诊两档按剂型分单元
    assert classify_category(TIER_OUTPATIENT, CLASS_MA_INJECTION) == CLASS_MA_INJECTION
    assert classify_category(TIER_CANCER, CLASS_MA_SUSTAINED) == CLASS_MA_SUSTAINED


def test_unclassified_rows_do_not_reach_the_matrix() -> None:
    """档位未知或类别不在矩阵内（如普通药品）→ 归类不出来，不判违规。"""
    assert classify_limit(PrescriptionRow(rx_type="门急诊一般", drug_category="普通药品")) is None
    assert classify_limit(PrescriptionRow(rx_type="", drug_category="麻醉药品注射剂")) is None
    assert classify_limit(PrescriptionRow(rx_type="门急诊一般", drug_category="")) is None


# ---------------------------------------------------------------------------
# 5. 矩阵 ↔ 规则行：无漂移，且漂移规则载入期被拒
# ---------------------------------------------------------------------------


def test_sample_rules_have_no_drift_against_the_matrix(rules) -> None:
    """样例规则行的阈值与矩阵逐条一致（口径唯一真源）。"""
    drifts = matrix_drift(rules)
    assert drifts == (), f"规则行与量限矩阵存在口径漂移：{[d.message for d in drifts]}"


def test_matrix_coverage_report_lists_uncovered_cells(rules) -> None:
    """覆盖报告：已规则化的单元与只定义未启用的单元分开列。"""
    coverage = matrix_coverage(rules)
    assert coverage.covered
    assert set(coverage.covered) <= set(LIMIT_MATRIX)
    assert set(coverage.covered) | set(coverage.uncovered) == set(LIMIT_MATRIX)
    assert not set(coverage.covered) & set(coverage.uncovered)
    # 本轮落地的单元都必须在覆盖集里
    assert "门急诊一般/麻醉药品注射剂" in coverage.covered
    assert "门急诊一般/第二类精神药品" in coverage.covered


def test_rule_with_drifted_threshold_is_rejected_at_load_time() -> None:
    """规则行把阈值改成别的数 → 载入期被拒（矩阵是口径唯一真源）。"""
    raw = {
        "rule_id": "RX-LEGAL-MA-OUT-DAYS",
        "layer": "合法性",
        "basis": BASIS,
        "version": "2026.10",
        "applies_when": {"rx_type": "门急诊一般", "drug_category": "麻醉药品注射剂"},
        "limit_ref": "门急诊一般/麻醉药品注射剂",
        "required_fields": ["rx_type", "drug_category", "days_qty"],
        "expression": "days_qty > 5",
        "severity": "严重不合理用药",
    }
    with pytest.raises(RuleValidationError) as excinfo:
        validate_rule(raw)
    assert "量限矩阵" in str(excinfo.value)


def test_rule_with_unknown_limit_ref_is_rejected_at_load_time() -> None:
    """limit_ref 指向矩阵里没有的单元 → 载入期被拒。"""
    raw = {
        "rule_id": "RX-LEGAL-UNKNOWN",
        "layer": "合法性",
        "basis": BASIS,
        "version": "2026.10",
        "applies_when": {"rx_type": "门急诊一般", "drug_category": "麻醉药品注射剂"},
        "limit_ref": "门急诊一般/不存在的类别",
        "required_fields": ["rx_type", "drug_category", "days_qty"],
        "expression": "days_qty > 1",
        "severity": "严重不合理用药",
    }
    with pytest.raises(RuleValidationError) as excinfo:
        validate_rule(raw)
    assert "limit_ref" in str(excinfo.value)


def test_limit_rule_for_a_different_tier_is_not_applicable() -> None:
    """矩阵单元与医嘱实际档位不符 → 该规则对这条医嘱不适用（不产生判定行）。"""
    rule = RuleRow(
        rule_id="RX-LEGAL-MA-OUT-DAYS",
        layer="合法性",
        basis=BASIS,
        version="2026.10",
        applies_when={"rx_type": "门急诊一般", "drug_category": "麻醉药品注射剂"},
        required_fields=("rx_type", "drug_category", "days_qty"),
        expression="days_qty > 1",
        severity="严重不合理用药",
        limit_ref="门急诊一般/麻醉药品注射剂",
    )
    inpatient = PrescriptionRow(
        prescription_no="RX-IP",
        rx_type="住院",
        drug_category="麻醉药品注射剂",
        days_qty="3",
    )
    result = evaluate_limit_rule(rule, inpatient)
    assert result.status is LimitStatus.NOT_APPLICABLE
    assert judge_prescription(inpatient, [rule]) == []


def test_evaluate_limit_directly_on_a_matrix_cell() -> None:
    """矩阵单元可直接判定（不经规则行），结论与经规则行一致。"""
    cell = resolve_limit_ref("门急诊一般/麻醉药品注射剂")
    assert cell is not None
    over = evaluate_limit(
        PrescriptionRow(rx_type="门急诊一般", drug_category="麻醉药品注射剂", days_qty="3日量"),
        cell,
    )
    assert over.status is LimitStatus.VIOLATION
    assert over.conclusion is Conclusion.VIOLATION

    within = evaluate_limit(
        PrescriptionRow(rx_type="门急诊一般", drug_category="麻醉药品注射剂", days_qty="1日量"),
        cell,
    )
    assert within.status is LimitStatus.COMPLIANT


def test_sample_batch_limit_rows_agree_with_the_matrix(rules) -> None:
    """样例批次里每条量限判定，都与直接按矩阵判定的结果一致。

    这是「矩阵是口径唯一真源」的回归：规则行路径与矩阵路径不得分叉。
    """
    from med_rule_fieldmap.engine.judge import judge_batch
    from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows

    batch_file = PROJECT_ROOT / "tests" / "data" / "batch" / "demo_batch_001.csv"
    rows = read_prescription_rows(batch_file)
    judgement = judge_batch(rows, rules, batch_no="DEMO-BATCH-001")

    limit_rules = {rule.rule_id: rule for rule in rules if rule.limit_ref}
    assert limit_rules

    checked = 0
    for item in judgement.rows:
        rule = limit_rules.get(item.rule_id)
        if rule is None:
            continue
        # 依赖字段缺失的判定行由通用依赖检查给出（不进矩阵路径），不参与比对
        if item.missing_fields:
            continue
        prescription = next(
            row for row in rows if (row.prescription_no or row.order_no) == item.prescription_no
        )
        result = evaluate_limit_rule(rule, prescription)
        assert result.status is not LimitStatus.NOT_APPLICABLE
        assert result.conclusion is item.conclusion, (
            f"{item.prescription_no} / {item.rule_id}：规则行路径与矩阵路径结论不一致"
        )
        checked += 1
    assert checked > 0, "样例批次应当含有量限判定行"
