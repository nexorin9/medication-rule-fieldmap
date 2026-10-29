"""归一层（normalize）的回归测试。

覆盖任务书要求的四组主线 + 两条边界断言：

1. **日量 / 剂量文本归一**：「3日量」→ 数值 3 + 单位「日」；「5mg」→ 数值 5 + 单位「mg」；
2. **单位别名**：日 / 次 / 片 / 支 / 袋等写法归一到规范单位；
3. **频次枚举**：qd / bid / tid 及中文写法归一到频次枚举；
4. **枚举别名**：处方类型 / 药品类别 / 皮试结果的别名归一。

边界断言：

* **「未注明」≠ 空值**：字段留空是缺失（不可判定），字段写「未注明」是显式否（违规）；
* **量值转不动不猜**：「适量」不是 0；「3mg」作为日量时口径不可比（``comparable`` 置否）。

另附两条与既有资产的一致性断言：

* 样例规则行 ``applies_when`` 里声明的枚举值必须是归一后的规范值（否则规则匹配不上）；
* 官方案例集里的「3日量」归一后为 3 日 / 「门诊一般患者」归一后为「门急诊一般」，
  与《处方审核规则体系》§3 的量限规则对得上。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from med_rule_fieldmap.expression import (
    FieldState,
    Frequency,
    NormalizedValue,
    ValueKind,
    normalize_context,
    normalize_days_quantity,
    normalize_enum,
    normalize_field,
    normalize_frequency,
    normalize_quantity,
    normalize_row,
    normalize_timestamp,
    normalize_unit,
    normalize_values,
)
from med_rule_fieldmap.models import PRESCRIPTION_FIELDS, PrescriptionRow

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
DOMAIN_CASES = (
    PROJECT_ROOT.parents[2] / "domain" / "cases" / "prescription_review_cases.json"
)


# ---------------------------------------------------------------------------
# 1. 日量 / 剂量文本归一
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "number", "unit", "comparable"),
    [
        ("3日量", "3", "日", True),
        ("3日常量", "3", "日", True),
        ("3日常用量", "3", "日", True),
        ("3 日量", "3", "日", True),
        ("1日", "1", "日", True),
        ("7日常用量", "7", "日", True),
        ("14日常量", "14", "日", True),
        ("15日量", "15", "日", True),
        ("1次常用量", "1", "次", True),
        ("1次量", "1", "次", True),
        ("3", "3", "", True),
        ("5mg", "5", "mg", False),
        ("5 mg", "5", "mg", False),
        ("5毫克", "5", "mg", False),
        ("0.5g", "0.5", "g", False),
        ("10片", "10", "片", False),
        ("1支", "1", "支", False),
        ("2袋", "2", "袋", False),
        ("3支装", "3", "支", False),
    ],
)
def test_quantity_text_normalized_to_number_and_unit(
    raw: str, number: str, unit: str, comparable: bool
) -> None:
    """量值文本 → 数值 + 规范单位。

    日量口径只认「日 / 次 / 无单位」：写成 mg / 片 / 支 时数值能取到，
    但口径不可比（``comparable`` 置否），由判定内核落不可判定。
    """
    value = normalize_days_quantity(raw)

    assert value.kind is ValueKind.NUMBER
    assert value.text == number
    assert value.number == Decimal(number)
    assert value.unit == unit
    assert value.state is FieldState.VALUE
    assert value.comparable is comparable


def test_days_quantity_with_wrong_unit_is_not_comparable() -> None:
    """日量写成「3mg」：数值是 3，但口径不可比，必须显式标出而不是静默当 3。"""
    value = normalize_days_quantity("3mg")

    assert value.number == Decimal("3")
    assert value.unit == "mg"
    assert value.comparable is False


def test_non_numeric_text_is_kept_as_text_not_zero() -> None:
    """「适量」不是 0：转不动就保留原文，数值留空。"""
    value = normalize_days_quantity("适量")

    assert value.kind is ValueKind.TEXT
    assert value.text == "适量"
    assert value.number is None
    assert value.state is FieldState.VALUE


def test_quantity_with_explicit_no_marker_keeps_marker_state() -> None:
    """量值字段写「未注明」→ 显式否，不是缺失，也不是 0。"""
    value = normalize_quantity("未注明", field="dosage_value")

    assert value.state is FieldState.EXPLICIT_NO
    assert value.text == "未注明"
    assert value.number is None


# ---------------------------------------------------------------------------
# 2. 单位别名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("日", "日"),
        ("天", "日"),
        ("日常量", "日"),
        ("次", "次"),
        ("回", "次"),
        ("片", "片"),
        ("支", "支"),
        ("袋", "袋"),
        ("包", "袋"),
        ("mg", "mg"),
        ("毫克", "mg"),
        ("MG", "mg"),
        ("g", "g"),
        ("克", "g"),
        ("kg", "kg"),
        ("千克", "kg"),
        ("ml", "ml"),
        ("毫升", "ml"),
        ("iu", "iu"),
        ("单位", "iu"),
        ("岁", "岁"),
        ("周岁", "岁"),
        ("月", "月"),
        ("月龄", "月"),
    ],
)
def test_unit_alias_normalized(raw: str, canonical: str) -> None:
    """单位写法归一。"""
    assert normalize_unit(raw) == canonical


def test_unknown_unit_is_preserved() -> None:
    """未知单位不猜、不丢，原样保留。"""
    assert normalize_unit("xyz") == "xyz"


def test_unit_only_field_normalization() -> None:
    """只放单位的字段（dosage_unit / age_unit）走单位归一。"""
    value = normalize_field("dosage_unit", "毫克")

    assert value.kind is ValueKind.UNIT
    assert value.text == "mg"
    assert value.unit == "mg"


# ---------------------------------------------------------------------------
# 3. 频次枚举
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("qd", Frequency.QD),
        ("QD", Frequency.QD),
        ("Bid", Frequency.BID),
        ("tid", Frequency.TID),
        ("qid", Frequency.QID),
        ("q8h", Frequency.Q8H),
        ("prn", Frequency.PRN),
        ("po", Frequency.PO),
        ("iv", Frequency.IV),
        ("每日一次", Frequency.QD),
        ("一日三次", Frequency.TID),
        ("一天两次", Frequency.BID),
        ("必要时", Frequency.PRN),
        ("口服", Frequency.PO),
        ("静脉滴注", Frequency.IV),
        ("肌注", Frequency.IM),
    ],
)
def test_frequency_normalized_to_enum(raw: str, expected: Frequency) -> None:
    """频次写法 → 频次枚举（规范值为代码，中文名只用于展示）。"""
    value = normalize_frequency(raw)

    assert value.kind is ValueKind.FREQUENCY
    assert value.text == expected.value
    assert value.state is FieldState.VALUE
    assert value.label == expected.label


def test_unknown_frequency_kept_as_text() -> None:
    """无法归一的频次保留原文，不硬塞进枚举。"""
    value = normalize_frequency("隔日一次")

    assert value.kind is ValueKind.TEXT
    assert value.text == "隔日一次"


# ---------------------------------------------------------------------------
# 4. 枚举别名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("门急诊一般", "门急诊一般"),
        ("门诊一般", "门急诊一般"),
        ("门诊一般患者", "门急诊一般"),
        ("门（急）诊一般患者", "门急诊一般"),
        ("门诊癌症疼痛患者", "门急诊癌症疼痛"),
        ("门急诊癌症疼痛", "门急诊癌症疼痛"),
        ("住院", "住院"),
        ("住院患者", "住院"),
    ],
)
def test_rx_type_alias_normalized(raw: str, canonical: str) -> None:
    """处方类型别名归一（《处方审核规则体系》§3 的三档口径）。"""
    value = normalize_enum("rx_type", raw)

    assert value.kind is ValueKind.ENUM
    assert value.text == canonical
    assert value.state is FieldState.VALUE


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("麻醉药品", "麻醉药品"),
        ("麻醉注射剂", "麻醉药品注射剂"),
        ("麻醉药品注射剂", "麻醉药品注射剂"),
        ("麻醉药品控缓释制剂", "麻醉药品控缓释制剂"),
        ("麻药控缓释", "麻醉药品控缓释制剂"),
        ("精一", "第一类精神药品"),
        ("第一类精神药品", "第一类精神药品"),
        ("精一注射剂", "第一类精神药品注射剂"),
        ("精二", "第二类精神药品"),
        ("第二类精神药品", "第二类精神药品"),
    ],
)
def test_drug_category_alias_normalized(raw: str, canonical: str) -> None:
    """药品类别别名归一（§3 术语 + 精一 / 精二 简称）。"""
    value = normalize_enum("drug_category", raw)

    assert value.kind is ValueKind.ENUM
    assert value.text == canonical


@pytest.mark.parametrize(
    ("raw", "canonical", "explicit_no"),
    [
        ("阴性", "阴性", False),
        ("阴", "阴性", False),
        ("positive", "阳性", False),
        ("阳性", "阳性", False),
        ("未注明", "未注明", True),
        ("未做", "未注明", True),
        ("未知", "未注明", True),
    ],
)
def test_skin_test_alias_normalized(
    raw: str, canonical: str, explicit_no: bool
) -> None:
    """皮试结果别名归一（「未做」与「未注明」同一分支，均为显式否）。"""
    value = normalize_enum("skin_test_result", raw)

    assert value.text == canonical
    if explicit_no:
        assert value.state is FieldState.EXPLICIT_NO
    else:
        assert value.state is FieldState.VALUE


# ---------------------------------------------------------------------------
# 5. 空值 ≠ 显式否
# ---------------------------------------------------------------------------


def test_empty_field_is_missing_not_explicit_no() -> None:
    """字段留空 → 缺失（不可判定），绝不能归一成「未注明」（违规）。"""
    value = normalize_enum("skin_test_result", "")

    assert value.kind is ValueKind.EMPTY
    assert value.state is FieldState.MISSING
    assert value.text == ""
    assert not value.is_explicit_no


def test_whitespace_only_field_is_missing() -> None:
    """纯空白等同于空值。"""
    value = normalize_enum("rx_type", "   ")

    assert value.state is FieldState.MISSING
    assert value.text == ""


def test_explicit_marker_field_is_not_missing() -> None:
    """「未注明」是有值的显式否，不是缺失。"""
    value = normalize_enum("skin_test_result", "未注明")

    assert value.state is FieldState.EXPLICIT_NO
    assert value.is_explicit_no
    assert not value.is_missing
    assert value.text == "未注明"


def test_missing_and_explicit_no_produce_different_context_values() -> None:
    """归一后的上下文里，空值与「未注明」必须是两个不同的值。"""
    empty = normalize_values({"skin_test_result": ""})
    marked = normalize_values({"skin_test_result": "未注明"})

    assert empty["skin_test_result"] == ""
    assert marked["skin_test_result"] == "未注明"
    assert empty["skin_test_result"] != marked["skin_test_result"]


# ---------------------------------------------------------------------------
# 6. 日期归一
# ---------------------------------------------------------------------------


def test_two_date_writings_share_one_timestamp() -> None:
    """ISO8601 与「2026-10-28 08:30」两种写法归到同一时刻。"""
    iso = normalize_timestamp("2026-10-28T08:30:00")
    local = normalize_timestamp("2026-10-28 08:30")

    assert iso.timestamp is not None
    assert local.timestamp is not None
    assert iso.timestamp == local.timestamp
    assert iso.text == local.text == "2026-10-28T00:30:00Z"


def test_date_with_offset_equals_local_writing() -> None:
    """带 +08:00 偏移的 ISO8601 与本地写法同刻。"""
    offset = normalize_timestamp("2026-10-28T08:30:00+08:00")
    local = normalize_timestamp("2026-10-28 08:30")

    assert offset.timestamp == local.timestamp


def test_date_only_is_midnight_local_time() -> None:
    """只有日期时按本地零点处理。"""
    value = normalize_timestamp("2026-10-28")

    assert value.kind is ValueKind.TIMESTAMP
    assert value.timestamp is not None
    assert value.text == "2026-10-27T16:00:00Z"


def test_unparsable_date_kept_as_text() -> None:
    """解析不出的时间戳保留原文，交判定层按不可比处理。"""
    value = normalize_timestamp("不是时间")

    assert value.kind is ValueKind.TEXT
    assert value.timestamp is None
    assert value.text == "不是时间"


# ---------------------------------------------------------------------------
# 7. 整行归一
# ---------------------------------------------------------------------------


def test_row_normalization_produces_evaluator_ready_context() -> None:
    """整行归一后的上下文可直接喂给求值器（量值变数值、枚举变规范值）。"""
    row = PrescriptionRow(
        batch_no="B1",
        prescription_no="RX-1",
        rx_type="门诊一般患者",
        drug_category="麻醉药品注射剂",
        days_qty="3日量",
        dosage_value="5mg",
        dosage_unit="毫克",
        freq="qd",
        skin_test_result="未做",
        age_value="65",
        age_unit="岁",
        event_time="2026-10-28T08:30:00",
        clinical_diag="高血压",
    )

    context = normalize_context(row)

    assert context["rx_type"] == "门急诊一般"
    assert context["drug_category"] == "麻醉药品注射剂"
    assert context["days_qty"] == "3"
    assert context["dosage_value"] == "5"
    assert context["dosage_unit"] == "mg"
    assert context["freq"] == "qd"
    assert context["skin_test_result"] == "未注明"
    assert context["age_value"] == "65"
    assert context["age_unit"] == "岁"
    assert context["event_time"] == "2026-10-28T00:30:00Z"
    assert context["clinical_diag"] == "高血压"


def test_normalize_row_exposes_structured_values() -> None:
    """结构化归一结果带单位与可比标记，供判定内核做口径检查。"""
    row = PrescriptionRow(days_qty="3mg", dosage_value="5毫克")

    values = normalize_row(row)

    assert isinstance(values["days_qty"], NormalizedValue)
    assert values["days_qty"].number == Decimal("3")
    assert values["days_qty"].unit == "mg"
    assert values["days_qty"].comparable is False
    assert values["dosage_value"].unit == "mg"
    assert values["dosage_value"].comparable is True


def test_normalize_values_covers_all_prescription_columns() -> None:
    """归一上下文覆盖医嘱列全集，缺列按缺失处理。"""
    context = normalize_values({"days_qty": "2日量"})

    assert len(context) == len(PRESCRIPTION_FIELDS)
    assert context["days_qty"] == "2"
    assert context["rx_type"] == ""
    assert context["skin_test_result"] == ""


# ---------------------------------------------------------------------------
# 8. 与既有资产的一致性
# ---------------------------------------------------------------------------


def _sample_rules() -> list[dict]:
    rules: list[dict] = []
    for path in sorted(SAMPLE_RULES.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if document:
                rules.append(document)
    return rules


def test_sample_rule_enum_values_are_canonical() -> None:
    """样例规则 applies_when 里声明的枚举值必须是归一后的规范值。

    否则 HIS 落地文件里的别名写法（门诊一般患者）归一后匹配不上规则行，
    量限规则会静默不适用。
    """
    checked = 0
    for rule in _sample_rules():
        for field, value in (rule.get("applies_when") or {}).items():
            if field not in ("rx_type", "drug_category"):
                continue
            normalized = normalize_enum(field, str(value)).text
            assert normalized == str(value), (
                f"规则 {rule.get('rule_id')} 的 {field}={value!r} 不是规范值"
                f"（归一后为 {normalized!r}）"
            )
            checked += 1
    assert checked >= 8


def test_official_case_values_normalize_to_rule_scope() -> None:
    """官方案例 rx-001 的写法归一后与 §3 量限规则对得上。"""
    if not DOMAIN_CASES.exists():
        pytest.skip("官方案例集不可用")
    payload = json.loads(DOMAIN_CASES.read_text(encoding="utf-8"))
    cases = {case["case_id"]: case for case in payload["cases"]}

    rx_type = normalize_enum("rx_type", cases["rx-001"]["input"]["处方类型"])
    category = normalize_enum(
        "drug_category", cases["rx-001"]["input"]["药品类别"]
    )
    days = normalize_days_quantity(cases["rx-001"]["input"]["开具量"])

    assert rx_type.text == "门急诊一般"
    assert category.text == "麻醉药品注射剂"
    assert days.number == Decimal("3")
    assert days.unit == "日"
    assert days.comparable is True
