"""规则集体检：僵尸规则 / 恒不可判定 / 覆盖率。

对应任务书 T25。药事审核岗每月改版规则后要回答三问：哪条规则从来没命中过
（僵尸规则，写错了都不会有人发现）、哪条规则永远判不可判定（恒不可判定，
规则依赖的字段根本拿不到）、整体覆盖率多少。

覆盖六组硬断言（任务书点名「能识别 1 条僵尸规则与 1 条恒不可判定规则；
覆盖率分母为适用处方数；无命中规则时覆盖率分母为 0 而非除零报错」）：

1. **僵尸规则识别**：从未被命中的规则被标出，并区分「本批无适用处方」与
   「适用却一条结论都没产出」两种成因；
2. **恒不可判定识别**：判出来的全是不可判定的规则被标出，且归因点名
   拿不到的字段；
3. **覆盖率分母口径**：分母是**适用处方数**（这条规则该管的处方张数），
   不是批次里的全部处方数——量限规则只对麻醉药品处方适用，拿全院处方做
   分母会让覆盖率永远是个位数；
4. **除零不报错**：适用面为 0 时覆盖率记 0，不是抛错；
5. **排序稳定 + 落盘可读**：同输入两次运行逐条相同，CSV 列固定、读回一致；
6. **端到端**：样例批次 + 样例规则集的体检结果，分母落在适用面上。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.judge import judge_batch
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.ledger.rule_health import (
    HEALTH_ALWAYS_UNDETERMINABLE,
    HEALTH_NORMAL,
    HEALTH_PARTIAL,
    HEALTH_RETIRED,
    HEALTH_ZOMBIE,
    RULE_HEALTH_COLUMNS,
    RULE_HEALTH_FILENAME,
    build_rule_health,
    load_rule_health,
    summarize_rule_health,
    write_rule_health,
)
from med_rule_fieldmap.models import Conclusion, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_FILE = PROJECT_ROOT / "tests" / "data" / "batch" / "demo_batch_001.csv"

BASIS = "处方审核规则体系 §3 特殊药品处方量限"


def _rule(
    rule_id: str,
    *,
    expression: str = "dosage_value > 0",
    applies_when: dict[str, object] | None = None,
    required_fields: tuple[str, ...] = ("dosage_value",),
    layer: str = "合法性",
) -> RuleRow:
    """构造一条规则行（不经过 YAML 载入期校验，直接给判定层用）。"""
    return RuleRow(
        rule_id=rule_id,
        layer=layer,
        basis=BASIS,
        version="2026.10",
        applies_when=dict(applies_when or {}),
        required_fields=required_fields,
        expression=expression,
        severity="严重不合理用药",
    )


def _prescription(
    prescription_no: str,
    *,
    rx_type: str = "门急诊一般",
    drug_category: str = "麻醉药品注射剂",
    dosage_value: str = "5",
    days_qty: str = "3",
) -> PrescriptionRow:
    """构造一条医嘱行（只填体检用得到的字段）。"""
    return PrescriptionRow(
        batch_no="BATCH-TEST-001",
        order_no=f"ORD-{prescription_no}",
        prescription_no=prescription_no,
        visit_no=f"VISIT-{prescription_no}",
        patient_ref=f"PT-{prescription_no}",
        dept_code="D101",
        dept_name="呼吸内科门诊",
        prescriber_id="PR-001",
        drug_code="DC-001",
        drug_name="盐酸吗啡注射液",
        dosage_value=dosage_value,
        dosage_unit="mg",
        rx_type=rx_type,
        drug_category=drug_category,
        days_qty=days_qty,
        clinical_diag="高血压",
        event_time="2026-10-28 09:15:00",
    )


@pytest.fixture()
def fixture_rows() -> list[PrescriptionRow]:
    """三条医嘱：一条齐备、一条缺药品类别、一条住院档。"""
    return [
        _prescription("RX-001"),
        _prescription("RX-002", drug_category=""),
        _prescription("RX-003", rx_type="住院", drug_category="麻醉药品"),
    ]


@pytest.fixture()
def fixture_rules() -> list[RuleRow]:
    """四条规则：正常 / 僵尸 / 恒不可判定 / 部分不可判定。"""
    return [
        _rule("RX-TEST-NORMAL"),
        # 从未适用：没有任何一条医嘱是「门急诊癌症疼痛」档
        _rule(
            "RX-TEST-ZOMBIE",
            applies_when={"rx_type": "门急诊癌症疼痛"},
            required_fields=("rx_type", "drug_category", "days_qty"),
            expression="days_qty > 3",
        ),
        # 恒不可判定：依赖的 extend_reason 三条医嘱全都没填
        _rule(
            "RX-TEST-ALWAYS-UNDET",
            required_fields=("extend_reason",),
            expression="extend_reason != ''",
        ),
        # 部分不可判定：只对门急诊一般档适用（2 张），其中 1 张缺药品类别
        _rule(
            "RX-TEST-PARTIAL",
            applies_when={"rx_type": "门急诊一般"},
            required_fields=("drug_category",),
            expression="drug_category == '麻醉药品注射剂'",
        ),
    ]


def _health_of(items, rule_id: str):
    """按规则号取体检结论。"""
    for item in items:
        if item.rule_id == rule_id:
            return item
    raise AssertionError(f"体检表里没有规则 {rule_id}")


# ---------------------------------------------------------------------------
# 1. 僵尸规则
# ---------------------------------------------------------------------------


def test_僵尸规则_从未命中的规则被标出(fixture_rows, fixture_rules):
    """一条都没命中的规则必须被标成僵尸规则，而不是悄悄消失。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    items = build_rule_health(fixture_rules, judgements, fixture_rows)

    zombie = _health_of(items, "RX-TEST-ZOMBIE")
    assert zombie.health == HEALTH_ZOMBIE
    assert zombie.is_zombie is True
    assert zombie.always_undeterminable is False
    # 本批确实一条结论都没产出
    assert not [row for row in judgements if row.rule_id == "RX-TEST-ZOMBIE"]
    # 归因要说清是「从未适用」而不是「判漏了」
    assert "无适用处方" in zombie.reason
    assert zombie.applicable_prescriptions == 0
    assert zombie.hit_prescriptions == 0


def test_僵尸规则_适用却无结论的成因单独归因(fixture_rows, fixture_rules):
    """适用面不为 0 却一条结论都没产出——成因是规则写错，措辞要区分开。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    # 把某条规则的结论从台账里抽掉（模拟判定链漏判 / 台账被清了一部分），
    # 但处方行照旧——这条规则在数据上明明是适用的
    kept = [row for row in judgements if row.rule_id != "RX-TEST-PARTIAL"]
    items = build_rule_health(fixture_rules, kept, fixture_rows)

    zombie = _health_of(items, "RX-TEST-PARTIAL")
    assert zombie.health == HEALTH_ZOMBIE
    assert zombie.is_zombie is True
    # 适用面仍是 2 张，但一条结论都没产出——与「从未适用」是两种成因
    assert zombie.applicable_prescriptions == 2
    assert zombie.judged_prescriptions == 0
    assert "却一条判定行都没产出" in zombie.reason
    assert "无适用处方" not in zombie.reason


def test_僵尸规则_生效集里的新规则也进体检表(fixture_rows, fixture_rules):
    """一条新加的、还没产生任何结论的规则也要出现在体检表里。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    new_rule = _rule(
        "RX-TEST-BRAND-NEW",
        applies_when={"rx_type": "门急诊一般"},
        required_fields=("days_qty",),
        expression="days_qty > 30",
    )
    items = build_rule_health([*fixture_rules, new_rule], judgements, fixture_rows)

    fresh = _health_of(items, "RX-TEST-BRAND-NEW")
    assert fresh.health == HEALTH_ZOMBIE
    assert fresh.is_zombie is True
    assert fresh.in_effective_rules is True


# ---------------------------------------------------------------------------
# 2. 恒不可判定规则
# ---------------------------------------------------------------------------


def test_恒不可判定规则_全部落不可判定被标出(fixture_rows, fixture_rules):
    """判出来的全是不可判定——规则依赖的字段根本拿不到。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    items = build_rule_health(fixture_rules, judgements, fixture_rows)

    always = _health_of(items, "RX-TEST-ALWAYS-UNDET")
    assert always.health == HEALTH_ALWAYS_UNDETERMINABLE
    assert always.always_undeterminable is True
    assert always.is_zombie is False
    assert always.hit_prescriptions == 0
    assert always.undeterminable_prescriptions == 3
    # 归因点名拿不到的字段
    assert always.blocking_fields == ("extend_reason",)
    assert "extend_reason" in always.reason


def test_部分不可判定规则_覆盖率未满被标出(fixture_rows, fixture_rules):
    """判出一部分、判不了另一部分——覆盖率未满 100%。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    items = build_rule_health(fixture_rules, judgements, fixture_rows)

    partial = _health_of(items, "RX-TEST-PARTIAL")
    assert partial.health == HEALTH_PARTIAL
    assert partial.applicable_prescriptions == 2
    assert partial.hit_prescriptions == 1
    assert partial.undeterminable_prescriptions == 1
    assert partial.coverage == pytest.approx(0.5)
    assert "drug_category" in partial.blocking_fields


# ---------------------------------------------------------------------------
# 3. 覆盖率分母口径
# ---------------------------------------------------------------------------


def test_覆盖率分母是适用处方数而非全部处方数(fixture_rows, fixture_rules):
    """分母 = 这条规则该管的处方张数，不是批次里的全部处方数。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    items = build_rule_health(fixture_rules, judgements, fixture_rows)

    partial = _health_of(items, "RX-TEST-PARTIAL")
    total_rows = len(fixture_rows)
    assert total_rows == 3
    # 适用面只有 2 张（住院那条不适用）
    assert partial.applicable_prescriptions == 2
    assert partial.applicable_prescriptions != total_rows
    # 覆盖率按适用面算 = 1/2；若误用全部处方数做分母会算成 1/3
    assert partial.coverage == pytest.approx(0.5)
    assert partial.coverage != pytest.approx(1 / 3)
    assert partial.coverage_pct == 50.0


def test_覆盖率分子只算判出结论的处方(fixture_rows, fixture_rules):
    """不可判定不算「审过了」——分子排除不可判定。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    items = build_rule_health(fixture_rules, judgements, fixture_rows)

    normal = _health_of(items, "RX-TEST-NORMAL")
    assert normal.applicable_prescriptions == 3
    assert normal.hit_prescriptions == 3
    assert normal.undeterminable_prescriptions == 0
    assert normal.coverage == pytest.approx(1.0)
    assert normal.health == HEALTH_NORMAL


# ---------------------------------------------------------------------------
# 4. 除零
# ---------------------------------------------------------------------------


def test_无适用处方时覆盖率为0且不抛除零错(fixture_rows, fixture_rules):
    """适用面为 0 是常态（新规则 / 本批没这场景），记 0 而不是抛错。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    items = build_rule_health(fixture_rules, judgements, fixture_rows)

    zombie = _health_of(items, "RX-TEST-ZOMBIE")
    assert zombie.applicable_prescriptions == 0
    assert zombie.coverage == 0.0
    assert zombie.coverage_pct == 0.0
    assert zombie.to_record()["coverage_pct"] == "0.0"

    # 一条规则都没判过、也没有任何处方行时同样不报错
    empty = build_rule_health(fixture_rules, [], [])
    assert _health_of(empty, "RX-TEST-NORMAL").coverage == 0.0
    assert all(item.coverage == 0.0 for item in empty)


# ---------------------------------------------------------------------------
# 5. 排序稳定 + 落盘
# ---------------------------------------------------------------------------


def test_体检表排序稳定且同输入同输出(fixture_rows, fixture_rules):
    """同输入两次运行逐条相同；僵尸规则排在最前面。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    first = build_rule_health(fixture_rules, judgements, fixture_rows)
    second = build_rule_health(fixture_rules, judgements, fixture_rows)

    assert [item.rule_id for item in first] == [item.rule_id for item in second]
    assert [item.to_record() for item in first] == [item.to_record() for item in second]
    assert first[0].rule_id == "RX-TEST-ZOMBIE"
    assert first[0].health == HEALTH_ZOMBIE
    # 同一档内按规则号升序
    for health in {item.health for item in first}:
        same_level = [item.rule_id for item in first if item.health == health]
        assert same_level == sorted(same_level)


def test_体检表落盘与读回一致(tmp_path, fixture_rows, fixture_rules):
    """CSV 列固定、读回一致；空表也落一个只有表头的文件。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    items = build_rule_health(fixture_rules, judgements, fixture_rows)

    target = write_rule_health(items, tmp_path / "out")
    assert target.name == RULE_HEALTH_FILENAME
    assert target.exists()

    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        body = list(reader)
    assert tuple(header) == RULE_HEALTH_COLUMNS
    assert len(body) == len(items)

    loaded = load_rule_health(target)
    assert len(loaded) == len(items)
    first = loaded[0]
    assert first["rule_id"] == "RX-TEST-ZOMBIE"
    assert first["health"] == HEALTH_ZOMBIE
    assert first["is_zombie"] == "是"
    assert first["coverage_pct"] == "0.0"

    empty_target = write_rule_health([], tmp_path / "empty")
    assert empty_target.exists()
    assert load_rule_health(empty_target) == []


def test_体检小结统计(fixture_rows, fixture_rules):
    """小结：僵尸 / 恒不可判定条数与整体覆盖率。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    items = build_rule_health(fixture_rules, judgements, fixture_rows)
    summary = summarize_rule_health(items)

    assert summary["规则总数"] == len(items)
    assert summary["僵尸规则数"] == 1
    assert summary["恒不可判定规则数"] == 1
    assert summary["部分不可判定规则数"] == 1
    assert summary["不在生效集规则数"] == 0
    assert summary["适用处方数合计"] == sum(
        item.applicable_prescriptions for item in items
    )
    assert summary["命中处方数合计"] == sum(item.hit_prescriptions for item in items)
    assert 0.0 <= summary["整体覆盖率"] <= 100.0


# ---------------------------------------------------------------------------
# 6. 端到端：样例批次 + 样例规则集
# ---------------------------------------------------------------------------


def test_端到端_样例批次的规则体检分母落在适用面上():
    """样例批次 + 样例规则集：覆盖率分母必须是适用面，不是全部处方数。"""
    repository = RuleRepository(SAMPLE_RULES)
    repository.load()
    rules = repository.effective_rules
    rows = read_prescription_rows(BATCH_FILE)
    judgements = judge_batch(rows, rules, batch_no="DEMO-BATCH-001").rows

    items = build_rule_health(rules, judgements, rows)
    by_id = {item.rule_id: item for item in items}

    # 量限规则只管麻醉药品注射剂那一档
    limit = by_id["RX-LEGAL-MA-OUT-DAYS"]
    assert limit.applicable_prescriptions < len(rows)
    assert limit.applicable_prescriptions > 0
    # 分母是适用面，不是 26 张全量
    assert limit.coverage == pytest.approx(
        limit.hit_prescriptions / limit.applicable_prescriptions
    )
    assert limit.coverage != pytest.approx(limit.hit_prescriptions / len(rows))
    # 缺 rx_type 的那条进了不可判定，其余判出结论
    assert limit.undeterminable_prescriptions >= 1
    assert limit.hit_prescriptions >= 1

    # 前记缺项规则：样例批次里 clinical_diag 齐全 → 判出结论
    front = by_id["RX-NORM-FRONT-DIAG"]
    assert front.applicable_prescriptions == len(rows)
    assert front.hit_prescriptions == len(rows)

    # 生效集里的每条规则都在体检表里
    assert len(items) == len(rules)


def test_端到端_体检表落盘可读(tmp_path):
    """样例批次跑完的体检表能落盘并读回。"""
    repository = RuleRepository(SAMPLE_RULES)
    repository.load()
    rules = repository.effective_rules
    rows = read_prescription_rows(BATCH_FILE)
    judgements = judge_batch(rows, rules, batch_no="DEMO-BATCH-001").rows

    items = build_rule_health(rules, judgements, rows)
    target = write_rule_health(items, tmp_path / "out")
    loaded = load_rule_health(target)

    assert len(loaded) == len(items)
    assert {record["rule_id"] for record in loaded} == {item.rule_id for item in items}
    for record in loaded:
        assert record["rule_id"]
        assert record["reason"]
        assert float(record["coverage_pct"]) >= 0.0


def test_规则不在生效集_台账里的历史结论单列一档(fixture_rows, fixture_rules):
    """规则下线后历史结论还留在台账里——要单列一档，不能悄悄消失。"""
    judgements = judge_batch(fixture_rows, fixture_rules, batch_no="BATCH-TEST-001").rows
    # 生效集里去掉 RX-TEST-NORMAL，但台账里留着它的结论
    remaining = [rule for rule in fixture_rules if rule.rule_id != "RX-TEST-NORMAL"]
    items = build_rule_health(remaining, judgements, fixture_rows)

    retired = _health_of(items, "RX-TEST-NORMAL")
    assert retired.health == HEALTH_RETIRED
    assert retired.in_effective_rules is False
    assert retired.judged_prescriptions == 3
    assert "生效规则集" in retired.reason


def test_体检结论三态互不重叠():
    """僵尸 / 恒不可判定两个标记位不能同时为真。"""
    judgements = judge_batch(
        [
            _prescription("RX-001"),
            _prescription("RX-002", drug_category=""),
            _prescription("RX-003", rx_type="住院", drug_category="麻醉药品"),
        ],
        [
            _rule("RX-TEST-NORMAL"),
            _rule(
                "RX-TEST-ZOMBIE",
                applies_when={"rx_type": "门急诊癌症疼痛"},
                required_fields=("rx_type", "drug_category", "days_qty"),
                expression="days_qty > 3",
            ),
            _rule(
                "RX-TEST-ALWAYS-UNDET",
                required_fields=("extend_reason",),
                expression="extend_reason != ''",
            ),
        ],
        batch_no="BATCH-TEST-001",
    ).rows
    items = build_rule_health(
        [
            _rule("RX-TEST-NORMAL"),
            _rule(
                "RX-TEST-ZOMBIE",
                applies_when={"rx_type": "门急诊癌症疼痛"},
                required_fields=("rx_type", "drug_category", "days_qty"),
                expression="days_qty > 3",
            ),
            _rule(
                "RX-TEST-ALWAYS-UNDET",
                required_fields=("extend_reason",),
                expression="extend_reason != ''",
            ),
        ],
        judgements,
        [
            _prescription("RX-001"),
            _prescription("RX-002", drug_category=""),
            _prescription("RX-003", rx_type="住院", drug_category="麻醉药品"),
        ],
    )
    for item in items:
        assert not (item.is_zombie and item.always_undeterminable)
        if item.is_zombie:
            assert item.judged_prescriptions == 0
        if item.always_undeterminable:
            assert item.hit_prescriptions == 0
            assert item.judged_prescriptions > 0
    # 不可判定结论只出现在依赖字段缺失的那条规则上
    undeterminable_rows = [
        row for row in judgements if row.conclusion is Conclusion.UNDETERMINABLE
    ]
    assert undeterminable_rows
    assert {row.rule_id for row in undeterminable_rows} == {"RX-TEST-ALWAYS-UNDET"}
    # 判出结论的规则不受影响
    assert {
        row.rule_id for row in judgements if row.conclusion is not Conclusion.UNDETERMINABLE
    } == {"RX-TEST-NORMAL"}
