"""判定内核 7/10：重复给药（§1 适宜性审核第 5 项）+ 超常处方（§3 尾段）。

对应任务书 T19。本轮的两条规则都是**跨行判定**——单看一行永远判不出来：

* **重复给药**：判定键 = 同一就诊 ``visit_no`` · 同一药品 ``drug_code`` ·
  同一日 ``event_time`` 的日期部分。三样一样才算重复。
* **超常处方**：同一开方医师 ``prescriber_id`` 在本批内「超 §3 量限且未注明
  正当理由」的处方张数，按开立先后累计；累计次数超过 3 次（第 4 次起）→
  出提示并标记应限制处方权。

覆盖六组硬断言（任务书点名 + 本轮边界）：

1. ``domain/cases/prescription_review_cases.json`` 的 **rx-005**（重复给药）
   与 **rx-009**（超常处方）逐条结论一致（拦截 / 不拦截）；
2. **同日不同药不误判**、**跨日不误判**；
3. **恰好 3 次不提示**（第 4 次才提示）；
4. 键字段缺失 → **不可判定**并点名该字段（接口缺口，不是「不重复」）；
5. **只出提示不自动限权**——系统标记但不推进药事委员会的处置动作；
6. 载入期校验：键字段没声明全 / 前提没写进 ``expression`` 的规则行被拒，
   且不影响其余规则生效。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.context import BatchContext
from med_rule_fieldmap.engine.duplicate import (
    DUPLICATE_KEY_FIELDS,
    DuplicateConfigurationError,
    duplicate_key,
    evaluate_duplicate,
    event_date_part,
    is_duplicate_rule,
    validate_duplicate_rule,
)
from med_rule_fieldmap.engine.judge import judge_batch, judge_prescription
from med_rule_fieldmap.engine.supernormal import (
    SUPERNNORMAL_THRESHOLD,
    SupernormalConfigurationError,
    SupernormalStatus,
    detect_occurrences,
    evaluate_supernormal,
    is_supernormal_rule,
    validate_supernormal_rule,
)
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.models import Conclusion, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository
from med_rule_fieldmap.rules.schema import RuleValidationError, validate_rule

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"

#: 端到端样例批次（重复给药 + 超常处方各一处可核对的违规）。
DUPLICATE_BATCH_FILE = (
    PROJECT_ROOT / "tests" / "data" / "duplicate_batch" / "duplicate_batch_001.csv"
)

#: 官方案例集（``domain/cases/prescription_review_cases.json``）。
CASES_FILE = PROJECT_ROOT.parents[2] / "domain" / "cases" / "prescription_review_cases.json"

DUPLICATE_RULE_ID = "RX-APPR-DUPLICATE-ADMINISTRATION"
SUPERNNORMAL_RULE_ID = "RX-APPR-SUPERNORMAL-PRESCRIBING-RIGHT"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _row(**overrides: str) -> PrescriptionRow:
    """构造一条医嘱行（未给的字段按落地文件的常规写法补齐）。"""
    values: dict[str, str] = {
        "batch_no": "DUP-BATCH-001",
        "order_no": "ORD-0001",
        "prescription_no": "RX-0001",
        "visit_no": "VISIT-0001",
        "patient_ref": "PAT-0001",
        "dept_code": "D-01",
        "dept_name": "心血管内科门诊",
        "prescriber_id": "PR-001",
        "prescriber_name": "医师甲",
        "drug_code": "DC-001",
        "drug_name": "氨氯地平片",
        "spec": "5mg",
        "dosage_form": "片剂",
        "dosage_text": "5mg qd",
        "dosage_value": "5",
        "dosage_unit": "mg",
        "route": "po",
        "freq": "qd",
        "quantity": "7",
        "rx_type": "门急诊一般",
        "drug_category": "普通药品",
        "days_qty": "1",
        "extend_reason": "",
        "skin_test_result": "",
        "clinical_diag": "高血压",
        "age_value": "65",
        "age_unit": "岁",
        "event_time": "2026-10-28 09:00:00",
    }
    values.update(overrides)
    return PrescriptionRow(**values)


def _rule(repository: RuleRepository, rule_id: str) -> RuleRow:
    for rule in repository.effective_rules:
        if rule.rule_id == rule_id:
            return rule
    raise AssertionError(f"样例规则集里没有规则 {rule_id}")


@pytest.fixture(scope="module")
def repository() -> RuleRepository:
    repo = RuleRepository(SAMPLE_RULES)
    repo.load()
    assert not repo.errors, f"样例规则集载入报错：{repo.errors}"
    return repo


@pytest.fixture(scope="module")
def duplicate_rule(repository: RuleRepository) -> RuleRow:
    return _rule(repository, DUPLICATE_RULE_ID)


@pytest.fixture(scope="module")
def supernormal_rule(repository: RuleRepository) -> RuleRow:
    return _rule(repository, SUPERNNORMAL_RULE_ID)


def _official_cases() -> dict[str, dict]:
    assert CASES_FILE.exists(), f"官方案例集缺失：{CASES_FILE}"
    payload = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    return {case["case_id"]: case for case in payload["cases"]}


def _judgements(rows, rule: RuleRow) -> list:
    """按一条规则判定一批医嘱，返回该规则的判定行。"""
    outcome = judge_batch(rows, [rule], batch_no="TEST-BATCH")
    return [row for row in outcome.rows if row.rule_id == rule.rule_id]


# ---------------------------------------------------------------------------
# 1. 官方案例 rx-005（重复给药）与 rx-009（超常处方）
# ---------------------------------------------------------------------------


def test_official_case_rx_005_duplicate_administration(
    duplicate_rule: RuleRow,
) -> None:
    """官方案例 rx-005：同一患者同日两张氨氯地平 → 重复给药应拦截。

    案例原文：``处方1 = 氨氯地平 5mg qd（今日）``、``处方2 = 氨氯地平 5mg qd
    （同日另开）``、``同一患者 = true``，``expect.拦截 = true``。
    """
    case = _official_cases()["rx-005"]
    assert case["expect"]["拦截"] is True

    rows = [
        _row(prescription_no="RX-005-A", order_no="ORD-005-A", event_time="2026-10-28 09:00:00"),
        _row(prescription_no="RX-005-B", order_no="ORD-005-B", event_time="2026-10-28 10:30:00"),
    ]
    judgements = _judgements(rows, duplicate_rule)
    assert len(judgements) == 2

    first, second = judgements
    assert first.prescription_no == "RX-005-A"
    assert first.conclusion is Conclusion.COMPLIANT, "组内首张是正常开立，不算重复"
    assert second.prescription_no == "RX-005-B"
    assert second.conclusion is Conclusion.VIOLATION, "同日另开的第二张应判重复给药"
    assert "RX-005-A" in second.reason, "归因文本要点名同组更早的处方号"
    assert duplicate_rule.rule_id in second.reason
    assert duplicate_rule.basis in second.reason


def test_official_case_rx_009_supernormal_prescribing_right(
    supernormal_rule: RuleRow,
) -> None:
    """官方案例 rx-009：医师超常处方 4 次且无正当理由 → 应拦截并提示限权。

    案例原文：``医师 = 张三``、``超常处方次数 = 4``、``正当理由 = 无``，
    ``expect.拦截 = true``。
    """
    case = _official_cases()["rx-009"]
    assert case["expect"]["拦截"] is True

    rows = [
        _row(
            prescription_no=f"RX-009-{index}",
            order_no=f"ORD-009-{index}",
            prescriber_id="PR-009",
            rx_type="门急诊一般",
            drug_category="麻醉药品注射剂",
            days_qty="3",
            extend_reason="",
            event_time=f"2026-10-28 0{index}:00:00",
        )
        for index in range(1, 5)
    ]
    judgements = _judgements(rows, supernormal_rule)
    assert len(judgements) == 4

    # 前三次未达阈值，第四次越线
    assert [row.conclusion for row in judgements[:3]] == [Conclusion.COMPLIANT] * 3
    assert judgements[3].conclusion is Conclusion.VIOLATION, "第 4 次应触发超常处方提示"
    assert "限制处方权" in judgements[3].reason
    assert supernormal_rule.rule_id in judgements[3].reason
    assert supernormal_rule.basis in judgements[3].reason


def test_official_case_expectations_are_covered_by_this_round() -> None:
    """本轮覆盖的官方案例号固定为 rx-005 / rx-009，其余案例不由本轮负责。"""
    cases = _official_cases()
    assert "rx-005" in cases and "rx-009" in cases
    assert cases["rx-005"]["basis"].startswith("适宜性审核七项")
    assert "超常处方" in cases["rx-009"]["expect"]["规则"]


# ---------------------------------------------------------------------------
# 2. 同日不同药不误判 / 跨日不误判
# ---------------------------------------------------------------------------


def test_same_day_different_drug_is_not_duplicate(duplicate_rule: RuleRow) -> None:
    """同日不同药不是重复给药：键里的 drug_code 必须一样才算重复。"""
    rows = [
        _row(prescription_no="RX-A", drug_code="DC-001", drug_name="氨氯地平片"),
        _row(prescription_no="RX-B", drug_code="DC-002", drug_name="硝苯地平片"),
    ]
    judgements = _judgements(rows, duplicate_rule)
    assert [row.conclusion for row in judgements] == [Conclusion.COMPLIANT] * 2


def test_cross_day_same_drug_is_not_duplicate(duplicate_rule: RuleRow) -> None:
    """跨日的同一药品不是重复给药：键里的日期必须一样才算重复。"""
    rows = [
        _row(prescription_no="RX-A", event_time="2026-10-28 09:00:00"),
        _row(prescription_no="RX-B", event_time="2026-10-29 09:00:00"),
    ]
    judgements = _judgements(rows, duplicate_rule)
    assert [row.conclusion for row in judgements] == [Conclusion.COMPLIANT] * 2


def test_different_visit_is_not_duplicate(duplicate_rule: RuleRow) -> None:
    """不同就诊的同一药品不是重复给药：键里的 visit_no 必须一样才算重复。"""
    rows = [
        _row(prescription_no="RX-A", visit_no="VISIT-0001"),
        _row(prescription_no="RX-B", visit_no="VISIT-0002"),
    ]
    judgements = _judgements(rows, duplicate_rule)
    assert [row.conclusion for row in judgements] == [Conclusion.COMPLIANT] * 2


def test_three_prescriptions_same_key_flag_second_and_third(
    duplicate_rule: RuleRow,
) -> None:
    """同键三张：首张合规，后两张都落违规。"""
    rows = [
        _row(prescription_no="RX-A", event_time="2026-10-28 08:00:00"),
        _row(prescription_no="RX-B", event_time="2026-10-28 09:00:00"),
        _row(prescription_no="RX-C", event_time="2026-10-28 10:00:00"),
    ]
    judgements = _judgements(rows, duplicate_rule)
    assert [row.conclusion for row in judgements] == [
        Conclusion.COMPLIANT,
        Conclusion.VIOLATION,
        Conclusion.VIOLATION,
    ]
    assert "RX-A" in judgements[1].reason
    assert "RX-A" in judgements[2].reason and "RX-B" in judgements[2].reason


def test_duplicate_detection_is_order_independent(
    duplicate_rule: RuleRow,
) -> None:
    """判定结果与落地文件的行序无关：按事件时间定先后，同批同结果。"""
    forward = [
        _row(prescription_no="RX-A", event_time="2026-10-28 08:00:00"),
        _row(prescription_no="RX-B", event_time="2026-10-28 09:00:00"),
    ]
    backward = list(reversed(forward))
    by_prescription = {
        row.prescription_no: row.conclusion
        for row in _judgements(forward, duplicate_rule)
    }
    for row in _judgements(backward, duplicate_rule):
        assert by_prescription[row.prescription_no] is row.conclusion


# ---------------------------------------------------------------------------
# 3. 超常处方的次数边界
# ---------------------------------------------------------------------------


def _supernormal_rows(count: int, prescriber_id: str = "PR-009") -> list[PrescriptionRow]:
    """同一医师连开 ``count`` 张超量处方（门急诊一般 + 麻醉药品注射剂 + 3 日量）。"""
    return [
        _row(
            prescription_no=f"RX-SN-{index:02d}",
            order_no=f"ORD-SN-{index:02d}",
            prescriber_id=prescriber_id,
            rx_type="门急诊一般",
            drug_category="麻醉药品注射剂",
            days_qty="3",
            extend_reason="",
            event_time=f"2026-10-28 0{index}:00:00",
        )
        for index in range(1, count + 1)
    ]


@pytest.mark.parametrize("count", [1, 2, 3])
def test_exactly_three_times_does_not_prompt(
    count: int, supernormal_rule: RuleRow
) -> None:
    """恰好 3 次不提示：阈值口径是「超过 3 次」（第 4 次起）。"""
    assert SUPERNNORMAL_THRESHOLD == 3
    judgements = _judgements(_supernormal_rows(count), supernormal_rule)
    assert len(judgements) == count
    assert all(row.conclusion is Conclusion.COMPLIANT for row in judgements)
    assert all("未达超常处方提示线" in row.reason for row in judgements)


def test_fourth_time_prompts_and_flags_restriction(
    supernormal_rule: RuleRow,
) -> None:
    """第 4 次越线：落违规 + 标记应限制处方权 + 归因文本点明只提示不自动限权。"""
    rows = _supernormal_rows(4)
    judgements = _judgements(rows, supernormal_rule)
    assert judgements[3].conclusion is Conclusion.VIOLATION
    reason = judgements[3].reason
    assert "第 4 次" in reason
    assert "限制处方权" in reason
    assert "系统不自动执行" in reason, "提示与限权处置必须分开写清"


def test_supernormal_only_prompts_and_never_restricts_rights(
    supernormal_rule: RuleRow,
) -> None:
    """只出提示不自动限权：判定结论里带标记，但不改任何权限状态。"""
    rows = _supernormal_rows(4)
    context = BatchContext.of(rows)
    evaluations = [
        evaluate_supernormal(supernormal_rule, row, context=context) for row in rows
    ]
    assert evaluations[3].status is SupernormalStatus.VIOLATION
    assert evaluations[3].should_restrict_prescribing_right is True
    assert evaluations[3].exceeds_threshold is True
    # 标记只挂在判定结果上：判定行里没有「已限权」之类的状态推进
    judgements = _judgements(rows, supernormal_rule)
    assert judgements[3].conclusion is Conclusion.VIOLATION
    assert judgements[3].state.value == "待判定"


def test_supernormal_counts_per_prescriber(supernormal_rule: RuleRow) -> None:
    """次数按开方医师分别累计：A 医师 3 次 + B 医师 3 次都不提示。"""
    rows = _supernormal_rows(3, prescriber_id="PR-A") + _supernormal_rows(
        3, prescriber_id="PR-B"
    )
    judgements = _judgements(rows, supernormal_rule)
    assert len(judgements) == 6
    assert all(row.conclusion is Conclusion.COMPLIANT for row in judgements)


def test_supernormal_with_stated_reason_is_not_counted(
    supernormal_rule: RuleRow,
) -> None:
    """已注明正当理由的超量处方不计入「无正当理由」的次数（§3 延长须注明理由）。"""
    rows = _supernormal_rows(3)
    rows.append(
        _row(
            prescription_no="RX-SN-04",
            order_no="ORD-SN-04",
            prescriber_id="PR-009",
            rx_type="门急诊一般",
            drug_category="麻醉药品注射剂",
            days_qty="3",
            extend_reason="慢性疼痛需持续用药",
            event_time="2026-10-28 04:00:00",
        )
    )
    judgements = _judgements(rows, supernormal_rule)
    # 前三张无理由 → 合规（未达阈值）；第四张已注明理由 → 不适用，不进台账
    assert [row.conclusion for row in judgements] == [Conclusion.COMPLIANT] * 3
    assert len(judgements) == 3


def test_supernormal_occurrence_detection_matches_limit_matrix() -> None:
    """超常发生清单 = 超 §3 量限且未注明正当理由的处方。"""
    rows = _supernormal_rows(2)
    rows.append(
        _row(
            prescription_no="RX-OK",
            order_no="ORD-OK",
            rx_type="门急诊一般",
            drug_category="麻醉药品注射剂",
            days_qty="1",
            event_time="2026-10-28 05:00:00",
        )
    )
    occurrences = detect_occurrences(rows)
    assert [item.identity for item in occurrences] == ["RX-SN-01", "RX-SN-02"]
    assert occurrences[0].limit_ref == "门急诊一般/麻醉药品注射剂"


# ---------------------------------------------------------------------------
# 4. 缺字段 → 不可判定并点名（接口缺口，不是「不重复」）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", DUPLICATE_KEY_FIELDS)
def test_duplicate_missing_key_field_is_undeterminable(
    field: str, duplicate_rule: RuleRow
) -> None:
    """键字段缺失 → 不可判定并点名该字段，不静默判成「不重复」。"""
    rows = [_row(prescription_no="RX-A", **{field: ""})]
    judgements = _judgements(rows, duplicate_rule)
    assert len(judgements) == 1
    row = judgements[0]
    assert row.conclusion is Conclusion.UNDETERMINABLE
    assert field in row.missing_fields
    assert field in row.reason
    assert "系统判不了" in row.reason


def test_duplicate_unreadable_event_time_is_undeterminable(
    duplicate_rule: RuleRow,
) -> None:
    """事件时间不是时间文本 → 不可判定并点名 event_time（口径不可比）。"""
    rows = [_row(prescription_no="RX-A", event_time="不是时间")]
    judgements = _judgements(rows, duplicate_rule)
    assert len(judgements) == 1
    assert judgements[0].conclusion is Conclusion.UNDETERMINABLE
    assert judgements[0].missing_fields == ("event_time",)
    assert "口径不可比" in judgements[0].reason


def test_supernormal_missing_reason_column_is_undeterminable(
    supernormal_rule: RuleRow,
) -> None:
    """理由字段整列未落地 → 不可判定并点名 extend_reason。"""
    row = _row(
        prescription_no="RX-SN-01",
        order_no="ORD-SN-01",
        prescriber_id="PR-009",
        rx_type="门急诊一般",
        drug_category="麻醉药品注射剂",
        days_qty="3",
        event_time="2026-10-28 01:00:00",
    )
    # 整列未落地（接口没回传这一列），与「栏位在但内容为空」是两回事
    row.present_columns = frozenset(
        name for name in row.__dict__ if name != "present_columns"
    ) - {"extend_reason"}
    judgements = _judgements([row], supernormal_rule)
    assert len(judgements) == 1
    assert judgements[0].conclusion is Conclusion.UNDETERMINABLE
    assert judgements[0].missing_fields == ("extend_reason",)
    assert "extend_reason" in judgements[0].reason


def test_supernormal_missing_prescriber_is_undeterminable(
    supernormal_rule: RuleRow,
) -> None:
    """开方医师字段为空 → 不可判定并点名 prescriber_id（分组键缺了没法计数）。"""
    row = _row(
        prescription_no="RX-SN-01",
        order_no="ORD-SN-01",
        prescriber_id="",
        rx_type="门急诊一般",
        drug_category="麻醉药品注射剂",
        days_qty="3",
        event_time="2026-10-28 01:00:00",
    )
    judgements = _judgements([row], supernormal_rule)
    assert len(judgements) == 1
    assert judgements[0].conclusion is Conclusion.UNDETERMINABLE
    assert judgements[0].missing_fields == ("prescriber_id",)


# ---------------------------------------------------------------------------
# 5. 规则行识别与载入期校验
# ---------------------------------------------------------------------------


def test_rule_family_identification(duplicate_rule: RuleRow, supernormal_rule: RuleRow) -> None:
    """规则族按**显式依赖**识别，不按规则号前缀猜。"""
    assert is_duplicate_rule(duplicate_rule) is True
    assert is_supernormal_rule(supernormal_rule) is True
    assert is_duplicate_rule(supernormal_rule) is False
    assert is_supernormal_rule(duplicate_rule) is False


def test_validators_accept_the_sample_rules(
    duplicate_rule: RuleRow, supernormal_rule: RuleRow
) -> None:
    """样例规则行通过各自的配置校验。"""
    assert validate_duplicate_rule(duplicate_rule) == ()
    assert validate_supernormal_rule(supernormal_rule) == ()


def test_duplicate_rule_missing_key_field_is_rejected_at_load_time() -> None:
    """键字段没声明全 → 载入期拒（错误点名缺哪个），不影响其余规则。"""
    raw = {
        "rule_id": "RX-APPR-DUP-BROKEN",
        "layer": "适宜性",
        "basis": "处方审核规则体系 §1 适宜性审核第 5 项（是否有重复给药现象）",
        "version": "2026.10",
        "applies_when": {},
        "required_fields": ["visit_no", "drug_code"],
        "expression": "visit_no != ''",
        "severity": "不合理用药",
    }
    with pytest.raises(RuleValidationError) as excinfo:
        validate_rule(raw)
    message = str(excinfo.value)
    assert "event_time" in message
    assert "expression" in message or "键字段" in message


def test_supernormal_rule_missing_dependency_is_rejected_at_load_time() -> None:
    """超常规则没声明 extend_reason → 载入期拒。"""
    raw = {
        "rule_id": "RX-APPR-SUPER-BROKEN",
        "layer": "适宜性",
        "basis": "处方审核规则体系 §3 尾段（超常处方 3 次以上且无正当理由）",
        "version": "2026.10",
        "applies_when": {},
        "required_fields": ["prescriber_id"],
        "expression": "prescriber_id != ''",
        "severity": "超常处方",
    }
    with pytest.raises(RuleValidationError) as excinfo:
        validate_rule(raw)
    assert "extend_reason" in str(excinfo.value)


def test_broken_rules_do_not_block_the_other_rules(
    repository: RuleRepository,
) -> None:
    """一条写坏的规则行被拒，其余规则照常生效。"""
    broken = SAMPLE_RULES / "broken_probe.yaml"
    broken.write_text(
        "rule_id: RX-APPR-DUP-BROKEN\n"
        "layer: 适宜性\n"
        "basis: 处方审核规则体系 §1 适宜性审核第 5 项（是否有重复给药现象）\n"
        'version: "2026.10"\n'
        "applies_when: {}\n"
        "required_fields: [visit_no]\n"
        "expression: \"visit_no != ''\"\n"
        "severity: 不合理用药\n",
        encoding="utf-8",
    )
    try:
        repo = RuleRepository(SAMPLE_RULES)
        repo.load()
        assert repo.errors, "写坏的规则行应当被拒"
        assert "RX-APPR-DUP-BROKEN" in str(repo.errors[0])
        assert all(
            rule.rule_id != "RX-APPR-DUP-BROKEN" for rule in repo.effective_rules
        )
        assert any(
            rule.rule_id == DUPLICATE_RULE_ID for rule in repo.effective_rules
        )
    finally:
        broken.unlink()


def test_configuration_error_is_raised_not_downgraded(
    duplicate_rule: RuleRow, supernormal_rule: RuleRow
) -> None:
    """配置错误显式抛错，不降级成「不可判定」。"""
    broken = RuleRow(
        rule_id=duplicate_rule.rule_id,
        layer=duplicate_rule.layer,
        basis=duplicate_rule.basis,
        version=duplicate_rule.version,
        required_fields=("visit_no",),
        expression=duplicate_rule.expression,
        severity=duplicate_rule.severity,
    )
    with pytest.raises(DuplicateConfigurationError) as excinfo:
        evaluate_duplicate(broken, _row())
    assert "drug_code" in str(excinfo.value)

    broken_super = RuleRow(
        rule_id=supernormal_rule.rule_id,
        layer=supernormal_rule.layer,
        basis=supernormal_rule.basis,
        version=supernormal_rule.version,
        required_fields=("prescriber_id",),
        expression=supernormal_rule.expression,
        severity=supernormal_rule.severity,
    )
    with pytest.raises(SupernormalConfigurationError) as excinfo:
        evaluate_supernormal(broken_super, _row())
    assert "extend_reason" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 6. 口径细节
# ---------------------------------------------------------------------------


def test_event_date_part_uses_local_calendar_day() -> None:
    """事件日期取本地日历日：早于 08:00 的开单不会被挪到前一天。"""
    early = _row(event_time="2026-10-28 07:30:00")
    assert event_date_part(early) == "2026-10-28"
    late = _row(event_time="2026-10-28 23:30:00")
    assert event_date_part(late) == "2026-10-28"
    assert event_date_part(_row(event_time="2026-10-29 00:30:00")) == "2026-10-29"


def test_duplicate_key_requires_all_three_fields() -> None:
    """键不全 → 键算不出来（不进任何分组）。"""
    assert duplicate_key(_row()) is not None
    assert duplicate_key(_row(visit_no="")) is None
    assert duplicate_key(_row(drug_code="")) is None
    assert duplicate_key(_row(event_time="")) is None


def test_single_row_judgement_sees_no_duplicate(duplicate_rule: RuleRow) -> None:
    """单行判定（不看同批其他行）时不得凭空判出重复。"""
    row = _row(prescription_no="RX-A")
    judgements = judge_prescription(row, [duplicate_rule])
    assert len(judgements) == 1
    assert judgements[0].conclusion is Conclusion.COMPLIANT
    assert "本批内未发现" in judgements[0].reason


def test_judgement_rows_keep_privacy_constraint(
    duplicate_rule: RuleRow, supernormal_rule: RuleRow
) -> None:
    """归因文本不带患者可识别明细（只允许 patient_ref）。"""
    rows = [
        _row(
            prescription_no="RX-A",
            patient_ref="PAT-0001",
            prescriber_name="医师甲",
            event_time="2026-10-28 09:00:00",
        ),
        _row(
            prescription_no="RX-B",
            patient_ref="PAT-0001",
            prescriber_name="医师甲",
            event_time="2026-10-28 10:00:00",
        ),
    ]
    judgements = _judgements(rows, duplicate_rule)
    for row in judgements:
        assert "PAT-0001" not in row.reason
        assert "医师甲" not in row.reason


# ---------------------------------------------------------------------------
# 7. 端到端：样例批次跑出可核对的两条违规
# ---------------------------------------------------------------------------


def test_demo_batch_end_to_end_produces_two_checkable_violations(
    repository: RuleRepository,
) -> None:
    """端到端：落地批次文件 → 判定台账 → 两条可核对的违规。

    样例批次 ``tests/data/duplicate_batch/duplicate_batch_001.csv`` 里：
    ``RX-DUP-002`` 是同次就诊同日同药的第二张（重复给药），``RX-SUP-004`` 是
    同一医师第 4 次无理由超量（超常处方提示）。
    """
    rows = read_prescription_rows(DUPLICATE_BATCH_FILE)
    assert len(rows) == 8

    outcome = judge_batch(rows, repository.effective_rules, batch_no="DUP-BATCH-001")
    by_key = {(row.prescription_no, row.rule_id): row for row in outcome.rows}

    # 重复给药：只有同次就诊 · 同日 · 同药的第二张落违规
    assert by_key[("RX-DUP-001", DUPLICATE_RULE_ID)].conclusion is Conclusion.COMPLIANT
    assert by_key[("RX-DUP-002", DUPLICATE_RULE_ID)].conclusion is Conclusion.VIOLATION
    assert by_key[("RX-DUP-003", DUPLICATE_RULE_ID)].conclusion is Conclusion.COMPLIANT
    assert by_key[("RX-DUP-004", DUPLICATE_RULE_ID)].conclusion is Conclusion.COMPLIANT

    # 超常处方：前三次未达阈值，第四次越线
    for index in (1, 2, 3):
        row = by_key[(f"RX-SUP-00{index}", SUPERNNORMAL_RULE_ID)]
        assert row.conclusion is Conclusion.COMPLIANT
    flagged = by_key[("RX-SUP-004", SUPERNNORMAL_RULE_ID)]
    assert flagged.conclusion is Conclusion.VIOLATION
    assert "第 4 次" in flagged.reason
    assert "限制处方权" in flagged.reason

    # 两条违规各归其位：重复给药与超常处方不串味
    assert "重复给药" not in flagged.reason
    assert "重复给药" in by_key[("RX-DUP-002", DUPLICATE_RULE_ID)].reason
    assert "超常处方" not in by_key[("RX-DUP-002", DUPLICATE_RULE_ID)].reason
