"""字段缺口台账 1/3：按缺失字段聚合不可判定项（本轮 10x 楔子）。

对应任务书 T23。本轮相对 Excel 与二元结论的核心增量：把「审不了」变成
**按字段聚合的可交付清单**——每组说清缺哪个字段、卡在哪条规则、牵涉多少
张处方与多少个科室。

覆盖六组硬断言（任务书点名「按缺失字段正确分组且各处方数正确」）：

1. **分组与计数**：20 条处方分别缺 ``rx_type`` / ``drug_category`` /
   ``days_qty`` → 聚合表按缺失字段分成三组，各组处方数正确；
2. **只收不可判定项**：结论为「违规」的行即使点名了缺失字段，也不进台账
   （那是处方本身不合格，与接口缺口是两回事）；
3. **两种「没有值」分开**：整列未落地（接口未回传）与栏位在但内容为空
   （医嘱未填写）分成两组，不混进同一张改造清单；
4. **科室数去重**：同一科室多张处方只算一个科室；
5. **落盘可读**：``out/undeterminable_ledger.csv`` 列固定、读回一致；
6. **同输入同输出**：两次聚合结果逐条相同（科室敢逐批对账）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.judge import judge_batch
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.ledger.gap import (
    GAP_COLUMNS,
    UNDETERMINABLE_FILENAME,
    GapCause,
    build_gap_entries,
    index_prescriptions,
    load_undeterminable_ledger,
    summarize_by_field,
    write_undeterminable_ledger,
)
from med_rule_fieldmap.models import Conclusion, JudgementRow, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_FILE = PROJECT_ROOT / "tests" / "data" / "batch" / "demo_batch_001.csv"
HL7_FILE = PROJECT_ROOT / "tests" / "data" / "hl7_batch" / "demo_batch_hl7_001.hl7"

AS_OF = date(2026, 10, 28)

BASIS = "处方审核规则体系 §3 特殊药品处方量限"

#: 三条只依赖一个字段的规则：缺哪个字段就在哪个字段上落不可判定。
#:
#: 表达式只引用**始终有值**的 ``prescriber_id``（且已在 ``required_fields``
#: 里声明），避免表达式本身再引出第二个缺口——本模块要验的是「缺口按字段
#: 分组」，不是表达式求值。
RX_TYPE_RULE = RuleRow(
    rule_id="RX-GAP-RX-TYPE",
    layer="合法性",
    basis=BASIS,
    version="2026.10",
    applies_when={},
    required_fields=("rx_type", "prescriber_id"),
    expression="prescriber_id == 'DOC-99'",
    severity="严重不合理用药",
)

DRUG_CATEGORY_RULE = RuleRow(
    rule_id="RX-GAP-DRUG-CATEGORY",
    layer="合法性",
    basis=BASIS,
    version="2026.10",
    applies_when={},
    required_fields=("drug_category", "prescriber_id"),
    expression="prescriber_id == 'DOC-99'",
    severity="严重不合理用药",
)

DAYS_QTY_RULE = RuleRow(
    rule_id="RX-GAP-DAYS-QTY",
    layer="合法性",
    basis=BASIS,
    version="2026.10",
    applies_when={},
    required_fields=("days_qty", "prescriber_id"),
    expression="prescriber_id == 'DOC-99'",
    severity="严重不合理用药",
)

GAP_RULES = (RX_TYPE_RULE, DRUG_CATEGORY_RULE, DAYS_QTY_RULE)

#: 20 条处方的缺口分布：7 条缺 rx_type、7 条缺 drug_category、6 条缺 days_qty。
RX_TYPE_GAP_COUNT = 7
DRUG_CATEGORY_GAP_COUNT = 7
DAYS_QTY_GAP_COUNT = 6
TOTAL_PRESCRIPTIONS = 20


def _row(
    prescription_no: str,
    *,
    dept_name: str = "心血管内科门诊",
    rx_type: str = "门急诊一般",
    drug_category: str = "麻醉药品注射剂",
    days_qty: str = "1",
    present_columns: frozenset[str] | None = None,
) -> PrescriptionRow:
    """构造一条医嘱行（字段口径与 spec.md §2.1 一致）。"""
    return PrescriptionRow(
        batch_no="BATCH-GAP-001",
        prescription_no=prescription_no,
        visit_no=f"V-{prescription_no}",
        patient_ref=f"P-{prescription_no}",
        dept_code="D-01",
        dept_name=dept_name,
        prescriber_id="DOC-01",
        drug_code="DRUG-01",
        drug_name="盐酸吗啡注射液",
        rx_type=rx_type,
        drug_category=drug_category,
        days_qty=days_qty,
        clinical_diag="高血压",
        event_time="2026-10-28T08:30:00+08:00",
        present_columns=present_columns if present_columns is not None else frozenset(),
    )


@pytest.fixture(scope="module")
def gap_prescriptions() -> list[PrescriptionRow]:
    """20 条处方：分别缺 rx_type（7）、drug_category（7）、days_qty（6）。

    前两组是「整列未落地」（接口没回传这一列），第三组是「栏位在但内容为空」
    （医师没填）——两种成因在断言里分开验。
    """
    from med_rule_fieldmap.models import PRESCRIPTION_FIELDS

    all_columns = frozenset(PRESCRIPTION_FIELDS)
    rows: list[PrescriptionRow] = []
    for index in range(RX_TYPE_GAP_COUNT):
        rows.append(
            _row(
                f"RX-G{index + 1:02d}",
                rx_type="",
                present_columns=all_columns - {"rx_type"},
            )
        )
    for index in range(DRUG_CATEGORY_GAP_COUNT):
        rows.append(
            _row(
                f"RX-G{RX_TYPE_GAP_COUNT + index + 1:02d}",
                drug_category="",
                present_columns=all_columns - {"drug_category"},
            )
        )
    for index in range(DAYS_QTY_GAP_COUNT):
        rows.append(
            _row(
                f"RX-G{RX_TYPE_GAP_COUNT + DRUG_CATEGORY_GAP_COUNT + index + 1:02d}",
                days_qty="",
                present_columns=all_columns,
            )
        )
    assert len(rows) == TOTAL_PRESCRIPTIONS
    return rows


@pytest.fixture(scope="module")
def gap_entries(gap_prescriptions):
    """对 20 条处方跑判定并聚合字段缺口。"""
    judgement = judge_batch(gap_prescriptions, GAP_RULES, batch_no="BATCH-GAP-001")
    index = index_prescriptions(gap_prescriptions)
    return build_gap_entries(judgement.rows, prescriptions=index)


# ---------------------------------------------------------------------------
# 1. 分组与计数：按缺失字段正确分组，各组处方数正确
# ---------------------------------------------------------------------------


def test_entries_group_by_missing_field(gap_entries) -> None:
    """三组缺口分别对应三个缺失字段，字段名互不混。"""
    fields = {entry.missing_field for entry in gap_entries}
    assert fields == {"rx_type", "drug_category", "days_qty"}


def test_affected_prescription_counts_are_correct(gap_entries) -> None:
    """各组的受影响处方数与构造的缺口分布一致。"""
    counts = {entry.missing_field: entry.affected_prescriptions for entry in gap_entries}
    assert counts["rx_type"] == RX_TYPE_GAP_COUNT
    assert counts["drug_category"] == DRUG_CATEGORY_GAP_COUNT
    assert counts["days_qty"] == DAYS_QTY_GAP_COUNT
    assert sum(counts.values()) == TOTAL_PRESCRIPTIONS


def test_each_entry_points_at_the_blocked_rule(gap_entries) -> None:
    """每组点名卡住的规则号。"""
    pairs = {(entry.missing_field, entry.rule_id) for entry in gap_entries}
    assert pairs == {
        ("rx_type", "RX-GAP-RX-TYPE"),
        ("drug_category", "RX-GAP-DRUG-CATEGORY"),
        ("days_qty", "RX-GAP-DAYS-QTY"),
    }


def test_prescription_numbers_are_listed(gap_entries) -> None:
    """聚合条目带出具体处方号（药师据此逐批复核）。"""
    by_field = {entry.missing_field: entry for entry in gap_entries}
    assert by_field["rx_type"].prescription_nos == tuple(
        f"RX-G{index:02d}" for index in range(1, RX_TYPE_GAP_COUNT + 1)
    )
    assert by_field["days_qty"].prescription_nos == tuple(
        f"RX-G{index:02d}"
        for index in range(
            RX_TYPE_GAP_COUNT + DRUG_CATEGORY_GAP_COUNT + 1,
            TOTAL_PRESCRIPTIONS + 1,
        )
    )


def test_dept_count_is_deduplicated(gap_entries) -> None:
    """同一科室的多张处方只算一个科室。"""
    for entry in gap_entries:
        assert entry.affected_depts == 1
        assert entry.dept_names == ("心血管内科门诊",)


def test_multiple_depts_are_counted_separately(gap_prescriptions) -> None:
    """跨科室的同一字段缺口 → 科室数按去重后的科室算。"""
    from med_rule_fieldmap.models import PRESCRIPTION_FIELDS

    all_columns = frozenset(PRESCRIPTION_FIELDS)
    rows = [
        _row(
            "RX-D01",
            dept_name="心血管内科门诊",
            rx_type="",
            present_columns=all_columns - {"rx_type"},
        ),
        _row(
            "RX-D02",
            dept_name="心血管内科门诊",
            rx_type="",
            present_columns=all_columns - {"rx_type"},
        ),
        _row(
            "RX-D03",
            dept_name="呼吸内科门诊",
            rx_type="",
            present_columns=all_columns - {"rx_type"},
        ),
    ]
    judgement = judge_batch(rows, GAP_RULES, batch_no="BATCH-DEPT")
    entries = build_gap_entries(
        judgement.rows, prescriptions=index_prescriptions(rows)
    )
    entry = next(item for item in entries if item.missing_field == "rx_type")
    assert entry.affected_prescriptions == 3
    assert entry.affected_depts == 2
    assert entry.dept_names == ("呼吸内科门诊", "心血管内科门诊")


# ---------------------------------------------------------------------------
# 2. 只收不可判定项：违规行不进字段缺口台账
# ---------------------------------------------------------------------------


def test_violation_rows_are_excluded() -> None:
    """结论为「违规」的行即便点名了缺失字段，也不进字段缺口台账。

    规范性缺项类规则把「前记临床诊断缺项」判成违规——那是处方本身不合格、
    退回医师补写，与「接口没回传字段」是两回事，不能混进同一张清单。
    """
    violation = JudgementRow(
        batch_no="B-1",
        prescription_no="RX-V-01",
        rule_id="RX-NORM-FRONT-DIAG",
        rule_version="2026.10",
        conclusion=Conclusion.VIOLATION,
        severity="不规范处方",
        reason="前记「临床诊断」缺项",
        basis=BASIS,
        missing_fields=("clinical_diag",),
    )
    undeterminable = JudgementRow(
        batch_no="B-1",
        prescription_no="RX-U-01",
        rule_id="RX-GAP-RX-TYPE",
        rule_version="2026.10",
        conclusion=Conclusion.UNDETERMINABLE,
        severity="严重不合理用药",
        reason="缺少判定所需字段 rx_type",
        basis=BASIS,
        missing_fields=("rx_type",),
    )
    entries = build_gap_entries([violation, undeterminable])
    assert [entry.missing_field for entry in entries] == ["rx_type"]
    assert entries[0].prescription_nos == ("RX-U-01",)


def test_every_gap_entry_backs_to_an_undeterminable_row(gap_prescriptions) -> None:
    """聚合出的每条缺口都能追回到一条不可判定判定行。"""
    judgement = judge_batch(gap_prescriptions, GAP_RULES, batch_no="BATCH-GAP-001")
    entries = build_gap_entries(
        judgement.rows, prescriptions=index_prescriptions(gap_prescriptions)
    )
    assert entries, "样例处方里应有不可判定的字段缺口"
    undeterminable = {
        (row.prescription_no, row.rule_id)
        for row in judgement.rows
        if row.conclusion is Conclusion.UNDETERMINABLE
    }
    for entry in entries:
        for prescription_no in entry.prescription_nos:
            assert (prescription_no, entry.rule_id) in undeterminable


# ---------------------------------------------------------------------------
# 3. 两种「没有值」必须分开
# ---------------------------------------------------------------------------


def test_not_landed_column_is_interface_gap(gap_entries) -> None:
    """整列未落地 → 成因是「接口未回传」，要动接口。"""
    by_field = {entry.missing_field: entry for entry in gap_entries}
    assert by_field["rx_type"].cause is GapCause.NOT_LANDED
    assert by_field["rx_type"].cause.needs_interface_change is True
    assert by_field["drug_category"].cause is GapCause.NOT_LANDED


def test_blank_value_is_physician_filled_gap(gap_entries) -> None:
    """栏位在但内容为空 → 成因是「医嘱未填写」，退回医师补写而不是改接口。"""
    by_field = {entry.missing_field: entry for entry in gap_entries}
    assert by_field["days_qty"].cause is GapCause.UNFILLED
    assert by_field["days_qty"].cause.needs_interface_change is False


def test_cause_is_unknown_without_prescription_index(gap_prescriptions) -> None:
    """没给处方索引 → 成因未判定，**不**当成接口缺口。"""
    judgement = judge_batch(gap_prescriptions, GAP_RULES, batch_no="BATCH-GAP-001")
    entries = build_gap_entries(judgement.rows)
    assert entries
    for entry in entries:
        assert entry.cause is GapCause.UNKNOWN
        assert entry.cause.needs_interface_change is False
        assert entry.affected_depts == 0


def test_hl7_channel_gaps_are_interface_gaps(sample_rules) -> None:
    """HL7 通道不携带的列 → 成因是「接口未回传」，这正是信息科要的那一档。

    HL7 落地口径没有 ``prescription_no``，只有医嘱号——判定行与处方索引的
    身份键必须一致（``prescription_no or order_no``），否则反查回不到原行，
    成因会误判成「成因未判定」。
    """
    from med_rule_fieldmap.ingest.hl7_orm import read_order_rows

    batch = read_order_rows(HL7_FILE)
    rows = list(batch.rows)
    assert rows
    judgement = judge_batch(rows, sample_rules, batch_no="HL7-BATCH-001")
    entries = build_gap_entries(
        judgement.rows, prescriptions=index_prescriptions(rows)
    )
    assert entries
    clinical = [entry for entry in entries if entry.missing_field == "clinical_diag"]
    assert clinical, "HL7 通道不携带临床诊断，应有该字段缺口"
    for entry in clinical:
        assert entry.cause is GapCause.NOT_LANDED
        assert entry.cause.needs_interface_change is True
        assert entry.affected_prescriptions == len(rows)


# ---------------------------------------------------------------------------
# 4. 落盘可读
# ---------------------------------------------------------------------------


def test_ledger_file_is_written_and_readable(gap_entries, tmp_path: Path) -> None:
    """不可判定台账列固定、读回一致。"""
    path = write_undeterminable_ledger(gap_entries, tmp_path / "out")
    assert path.name == UNDETERMINABLE_FILENAME
    records = load_undeterminable_ledger(path)
    assert len(records) == len(gap_entries)
    assert list(records[0].keys()) == list(GAP_COLUMNS)
    first = records[0]
    assert first["missing_field"] in {"rx_type", "drug_category", "days_qty"}
    assert int(first["affected_prescriptions"]) > 0
    assert first["cause"] in {
        cause.value for cause in GapCause
    }


def test_ledger_file_has_header_even_without_gaps(tmp_path: Path) -> None:
    """没有缺口时也落一个只有表头的文件（下游脚本不必判存在性）。"""
    path = write_undeterminable_ledger([], tmp_path / "out")
    assert path.exists()
    assert load_undeterminable_ledger(path) == []
    assert path.read_text(encoding="utf-8-sig").splitlines()[0].split(",") == list(
        GAP_COLUMNS
    )


# ---------------------------------------------------------------------------
# 5. 同输入同输出
# ---------------------------------------------------------------------------


def test_same_input_gives_same_output(gap_prescriptions) -> None:
    """同一份输入跑两次，聚合结果逐条相同（科室敢逐批对账）。"""
    index = index_prescriptions(gap_prescriptions)
    first = build_gap_entries(
        judge_batch(gap_prescriptions, GAP_RULES, batch_no="BATCH-GAP-001").rows,
        prescriptions=index,
    )
    second = build_gap_entries(
        judge_batch(gap_prescriptions, GAP_RULES, batch_no="BATCH-GAP-001").rows,
        prescriptions=index,
    )
    assert first == second


def test_entries_sorted_by_affected_prescriptions_desc(gap_entries) -> None:
    """排序稳定：受影响处方数降序，同数按字段名兜底。"""
    counts = [entry.affected_prescriptions for entry in gap_entries]
    assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# 6. 按字段汇总（字段补齐顺序表的原料）
# ---------------------------------------------------------------------------


def test_summarize_by_field_aggregates_rules(gap_entries) -> None:
    """一个字段一行：牵涉多少张处方、多少条规则、多少个科室。"""
    summary = summarize_by_field(gap_entries)
    assert len(summary) == 3
    by_field = {item.missing_field: item for item in summary}
    assert by_field["rx_type"].affected_prescriptions == RX_TYPE_GAP_COUNT
    assert by_field["rx_type"].affected_rules == 1
    assert by_field["rx_type"].affected_depts == 1
    assert by_field["rx_type"].causes == (GapCause.NOT_LANDED.value,)


# ---------------------------------------------------------------------------
# 7. 端到端：样例规则集 + 样例批次
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sample_rules():
    repository = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repository.load()
    assert repository.errors == (), "样例规则集不应有载入错误"
    return repository.effective_rules


def test_sample_batch_gap_entries(sample_rules) -> None:
    """样例批次的字段缺口：缺 rx_type / drug_category / days_qty 的处方被点名。"""
    rows = read_prescription_rows(BATCH_FILE)
    judgement = judge_batch(rows, sample_rules, batch_no="DEMO-BATCH-001")
    entries = build_gap_entries(
        judgement.rows, prescriptions=index_prescriptions(rows)
    )

    def by_field(name: str) -> list:
        return [entry for entry in entries if entry.missing_field == name]

    # RX-0007 缺 rx_type → 命中三条量限规则（门急诊一般 / 癌症疼痛 / 住院档）
    rx_type_entries = by_field("rx_type")
    assert len(rx_type_entries) == 3
    assert {entry.rule_id for entry in rx_type_entries} == {
        "RX-LEGAL-MA-OUT-DAYS",
        "RX-LEGAL-MA-CANCER-OUT-DAYS",
        "RX-LEGAL-MA-INPATIENT-DAYS",
    }
    for entry in rx_type_entries:
        assert entry.prescription_nos == ("RX-0007",)
        assert entry.affected_prescriptions == 1
        assert entry.affected_depts == 1
        assert entry.dept_names == ("呼吸内科门诊",)
        # CSV 落地文件的列全在 → 成因是「医嘱未填写」而不是接口没接
        assert entry.cause is GapCause.UNFILLED

    # RX-0006 缺 days_qty、RX-0024 缺 drug_category、RX-0020 缺皮试结果
    assert {entry.prescription_nos for entry in by_field("days_qty")} == {("RX-0006",)}
    assert {entry.prescription_nos for entry in by_field("drug_category")} == {
        ("RX-0024",)
    }
    assert {entry.prescription_nos for entry in by_field("skin_test_result")} == {
        ("RX-0020",)
    }


def test_sample_batch_violation_with_missing_field_is_not_a_gap(sample_rules) -> None:
    """RX-0003 缺临床诊断 → 落违规（退回医师补写），不进字段缺口台账。

    这条是「医嘱未填写」与「接口未回传」分流在台账层的落点：违规项不占
    信息科的接口改造清单。
    """
    rows = read_prescription_rows(BATCH_FILE)
    judgement = judge_batch(rows, sample_rules, batch_no="DEMO-BATCH-001")
    violations = [
        row
        for row in judgement.rows
        if row.conclusion is Conclusion.VIOLATION and "clinical_diag" in row.missing_fields
    ]
    assert violations, "RX-0003 缺临床诊断应落违规并点名该字段"
    entries = build_gap_entries(
        judgement.rows, prescriptions=index_prescriptions(rows)
    )
    assert "clinical_diag" not in {entry.missing_field for entry in entries}
    assert "RX-0003" not in {
        prescription_no
        for entry in entries
        for prescription_no in entry.prescription_nos
    }
