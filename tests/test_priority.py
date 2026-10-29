"""字段缺口台账 2/3：补齐优先级排序 + 字段补齐顺序表。

对应任务书 T24。信息科要的不是一张缺口清单，而是一张**「先补哪个字段最
划算」**的顺序表：补一个字段能救多少条处方、解锁多少条规则。排序必须
**稳定可复现**，否则每次生成的表对不上，科室不敢用。

覆盖六组硬断言（任务书点名「同输入两次运行排序结果完全一致；优先级排序
符合三条规则；受影响处方数相同时按规则数与字段名兜底」）：

1. **三级排序**：受影响处方数降序 → 受影响规则数降序 → 缺失字段名升序；
2. **同输入同输出**：两次运行逐条相同，且与入参顺序无关；
3. **名次连续**：``priority`` 是 1..N 的连续名次（排期用的，不是打分的）；
4. **两种成因分流**：接口未回传 / 医嘱未填写在顺序表里仍然分开，
   ``route_to`` 点明下一棒交给谁；
5. **落盘可读**：``out/field_gap_priority.csv`` 列固定、读回一致；
6. **端到端**：CLI ``judge`` 跑完产出顺序表，且与不可判定台账的处方数对得上。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from med_rule_fieldmap.cli import main
from med_rule_fieldmap.engine.judge import judge_batch
from med_rule_fieldmap.ledger.gap import (
    GapCause,
    GapEntry,
    build_gap_entries,
    index_prescriptions,
    summarize_by_field,
)
from med_rule_fieldmap.ledger.priority import (
    PRIORITY_COLUMNS,
    PRIORITY_FILENAME,
    ROUTE_BOTH,
    ROUTE_INTERFACE,
    ROUTE_PHYSICIAN,
    ROUTE_UNKNOWN,
    build_priority_table,
    load_priority_table,
    select_interface_gaps,
    write_priority_table,
)
from med_rule_fieldmap.models import PRESCRIPTION_FIELDS, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_FILE = PROJECT_ROOT / "tests" / "data" / "batch" / "demo_batch_001.csv"

AS_OF = date(2026, 10, 28)

BASIS = "处方审核规则体系 §3 特殊药品处方量限"


def _entry(
    missing_field: str,
    rule_id: str,
    *,
    cause: GapCause = GapCause.NOT_LANDED,
    prescriptions: tuple[str, ...] = (),
    depts: tuple[str, ...] = ("心血管内科门诊",),
) -> GapEntry:
    """构造一条字段缺口条目（口径与 ``ledger/gap.py`` 一致）。"""
    return GapEntry(
        missing_field=missing_field,
        rule_id=rule_id,
        cause=cause,
        affected_prescriptions=len(prescriptions),
        affected_depts=len(depts),
        prescription_nos=prescriptions,
        dept_names=depts,
    )


# ---------------------------------------------------------------------------
# 1. 三级排序规则
# ---------------------------------------------------------------------------


def test_sorted_by_affected_prescriptions_desc() -> None:
    """第一级：受影响处方数降序——补这个字段能多救几张处方。"""
    entries = [
        _entry("rx_type", "RX-A", prescriptions=("RX-01", "RX-02", "RX-03")),
        _entry("days_qty", "RX-B", prescriptions=("RX-04",)),
        _entry("drug_category", "RX-C", prescriptions=("RX-05", "RX-06")),
    ]
    table = build_priority_table(entries)
    assert [item.missing_field for item in table] == [
        "rx_type",
        "drug_category",
        "days_qty",
    ]
    assert [item.affected_prescriptions for item in table] == [3, 2, 1]


def test_tie_broken_by_affected_rules_desc() -> None:
    """第二级：处方数相同时，受影响规则数多的排前面。"""
    entries = [
        # alpha：2 张处方、1 条规则
        _entry("alpha", "RX-A1", prescriptions=("RX-01", "RX-02")),
        # beta：2 张处方、3 条规则（同一批处方在三条规则上卡住）
        _entry("beta", "RX-B1", prescriptions=("RX-03", "RX-04")),
        _entry("beta", "RX-B2", prescriptions=("RX-03", "RX-04")),
        _entry("beta", "RX-B3", prescriptions=("RX-03", "RX-04")),
    ]
    table = build_priority_table(entries)
    assert [item.missing_field for item in table] == ["beta", "alpha"]
    by_field = {item.missing_field: item for item in table}
    assert by_field["beta"].affected_rules == 3
    assert by_field["alpha"].affected_rules == 1


def test_tie_broken_by_field_name_asc() -> None:
    """第三级：处方数与规则数都相同时，按缺失字段名升序兜底（唯一序）。"""
    entries = [
        _entry("gamma", "RX-G", prescriptions=("RX-01",)),
        _entry("alpha", "RX-A", prescriptions=("RX-02",)),
        _entry("beta", "RX-B", prescriptions=("RX-03",)),
    ]
    table = build_priority_table(entries)
    assert [item.missing_field for item in table] == ["alpha", "beta", "gamma"]


def test_priority_is_dense_ordinal() -> None:
    """``priority`` 是 1..N 的连续名次（排期用，不是评分用）。"""
    entries = [
        _entry("rx_type", "RX-A", prescriptions=("RX-01", "RX-02")),
        _entry("days_qty", "RX-B", prescriptions=("RX-03",)),
        _entry("drug_category", "RX-C", prescriptions=("RX-04",)),
    ]
    table = build_priority_table(entries)
    assert [item.priority for item in table] == [1, 2, 3]


def test_same_field_aggregates_across_rules() -> None:
    """同一字段在多条规则上卡住 → 合成一行（处方数去重、规则数累加）。"""
    entries = [
        _entry("rx_type", "RX-A1", prescriptions=("RX-01", "RX-02")),
        _entry("rx_type", "RX-A2", prescriptions=("RX-02", "RX-03")),
    ]
    table = build_priority_table(entries)
    assert len(table) == 1
    item = table[0]
    assert item.missing_field == "rx_type"
    assert item.affected_prescriptions == 3
    assert item.affected_rules == 2


def test_dept_count_is_deduplicated() -> None:
    """受影响科室数按去重后的科室算。"""
    entries = [
        _entry(
            "rx_type",
            "RX-A",
            prescriptions=("RX-01", "RX-02"),
            depts=("心血管内科门诊", "呼吸内科门诊"),
        ),
        _entry(
            "rx_type",
            "RX-B",
            prescriptions=("RX-03",),
            depts=("心血管内科门诊",),
        ),
    ]
    table = build_priority_table(entries)
    assert table[0].affected_depts == 2


# ---------------------------------------------------------------------------
# 2. 同输入同输出
# ---------------------------------------------------------------------------


def _sample_entries() -> list[GapEntry]:
    """一份稳定的缺口条目样本（三字段、三成因、两科室）。"""
    return [
        _entry("rx_type", "RX-A1", prescriptions=("RX-01", "RX-02", "RX-03")),
        _entry("rx_type", "RX-A2", prescriptions=("RX-03", "RX-04")),
        _entry(
            "drug_category",
            "RX-B1",
            cause=GapCause.UNFILLED,
            prescriptions=("RX-05", "RX-06"),
            depts=("呼吸内科门诊",),
        ),
        _entry(
            "days_qty",
            "RX-C1",
            cause=GapCause.UNFILLED,
            prescriptions=("RX-07",),
            depts=("呼吸内科门诊",),
        ),
    ]


def test_same_input_gives_same_output() -> None:
    """同一份输入跑两次，顺序表逐条相同（科室敢逐批对账）。"""
    first = build_priority_table(_sample_entries())
    second = build_priority_table(_sample_entries())
    assert first == second
    assert [item.to_record() for item in first] == [
        item.to_record() for item in second
    ]


def test_output_is_independent_of_input_order() -> None:
    """入参顺序打乱，输出顺序不变（三级键合起来是全序）。"""
    entries = _sample_entries()
    forward = build_priority_table(entries)
    backward = build_priority_table(list(reversed(entries)))
    assert [item.to_record() for item in forward] == [
        item.to_record() for item in backward
    ]


# ---------------------------------------------------------------------------
# 3. 两种成因分流（接口缺口 vs 医师漏填）
# ---------------------------------------------------------------------------


def test_interface_gap_count_is_separated_from_total() -> None:
    """接口缺口数只算「接口未回传」那部分，医师漏填的不算进去。"""
    entries = [
        _entry(
            "rx_type",
            "RX-A1",
            cause=GapCause.NOT_LANDED,
            prescriptions=("RX-01", "RX-02"),
        ),
        _entry(
            "rx_type",
            "RX-A2",
            cause=GapCause.UNFILLED,
            prescriptions=("RX-03",),
        ),
    ]
    item = build_priority_table(entries)[0]
    assert item.affected_prescriptions == 3
    assert item.interface_gap_prescriptions == 2
    assert item.needs_interface_change is True
    assert set(item.causes) == {GapCause.NOT_LANDED.value, GapCause.UNFILLED.value}
    assert item.route_to == ROUTE_BOTH


def test_route_to_points_at_interface_team_for_landed_gaps() -> None:
    """整列未落地 → 下一棒是信息科接口岗。"""
    item = build_priority_table(
        [_entry("rx_type", "RX-A", cause=GapCause.NOT_LANDED, prescriptions=("RX-01",))]
    )[0]
    assert item.route_to == ROUTE_INTERFACE


def test_route_to_points_at_physician_for_blank_values() -> None:
    """栏位在但内容为空 → 下一棒是退回医师补写（不是改接口）。"""
    item = build_priority_table(
        [_entry("days_qty", "RX-B", cause=GapCause.UNFILLED, prescriptions=("RX-01",))]
    )[0]
    assert item.route_to == ROUTE_PHYSICIAN
    assert item.interface_gap_prescriptions == 0
    assert item.needs_interface_change is False


def test_route_to_is_unknown_when_cause_unknown() -> None:
    """成因未判定 → 不派单给任何人，先核实。"""
    item = build_priority_table(
        [_entry("event_time", "RX-C", cause=GapCause.UNKNOWN, prescriptions=("RX-01",))]
    )[0]
    assert item.route_to == ROUTE_UNKNOWN
    assert item.needs_interface_change is False


def test_interface_only_filter_keeps_interface_gaps() -> None:
    """只给信息科的那一叠：只留含「接口未回传」成因的字段。"""
    entries = [
        _entry("rx_type", "RX-A", cause=GapCause.NOT_LANDED, prescriptions=("RX-01",)),
        _entry(
            "days_qty",
            "RX-B",
            cause=GapCause.UNFILLED,
            prescriptions=("RX-02", "RX-03"),
        ),
    ]
    table = build_priority_table(entries)
    interface = select_interface_gaps(table)
    assert [item.missing_field for item in interface] == ["rx_type"]
    # 过滤**不改名次**：这张字段在总表里排第几就是第几（同一张表里两叠人
    # 各看各的，名字还能对上）
    original_rank = next(
        item.priority for item in table if item.missing_field == "rx_type"
    )
    assert interface[0].priority == original_rank

    # 单独出一张只含接口缺口的表时，名次在这张表内重新连续编号
    only_interface = build_priority_table(entries, interface_only=True)
    assert [item.missing_field for item in only_interface] == ["rx_type"]
    assert only_interface[0].priority == 1


# ---------------------------------------------------------------------------
# 4. 落盘可读
# ---------------------------------------------------------------------------


def test_priority_file_is_written_and_readable(tmp_path: Path) -> None:
    """顺序表列固定、读回一致。"""
    table = build_priority_table(_sample_entries())
    path = write_priority_table(table, tmp_path / "out")
    assert path.name == PRIORITY_FILENAME
    records = load_priority_table(path)
    assert len(records) == len(table)
    assert list(records[0].keys()) == list(PRIORITY_COLUMNS)
    assert [record["priority"] for record in records] == ["1", "2", "3"]
    assert records[0]["missing_field"] == "rx_type"
    assert int(records[0]["affected_prescriptions"]) > 0
    assert records[0]["route_to"] in {
        ROUTE_INTERFACE,
        ROUTE_PHYSICIAN,
        ROUTE_BOTH,
        ROUTE_UNKNOWN,
    }


def test_priority_file_has_header_even_without_gaps(tmp_path: Path) -> None:
    """没有缺口时也落一个只有表头的文件（下游脚本不必判存在性）。"""
    path = write_priority_table([], tmp_path / "out")
    assert path.exists()
    assert load_priority_table(path) == []
    assert path.read_text(encoding="utf-8-sig").splitlines()[0].split(",") == list(
        PRIORITY_COLUMNS
    )


def test_priority_file_written_twice_is_byte_identical(tmp_path: Path) -> None:
    """同一输入两次落盘，逐字相同（两份表对得上）。"""
    out_dir = tmp_path / "out"
    first = write_priority_table(build_priority_table(_sample_entries()), out_dir)
    first_bytes = first.read_bytes()
    second = write_priority_table(build_priority_table(_sample_entries()), out_dir)
    assert second.read_bytes() == first_bytes


# ---------------------------------------------------------------------------
# 5. 端到端：判定链 → 字段缺口 → 顺序表
# ---------------------------------------------------------------------------


RX_TYPE_RULE = RuleRow(
    rule_id="RX-PRIO-RX-TYPE",
    layer="合法性",
    basis=BASIS,
    version="2026.10",
    applies_when={},
    required_fields=("rx_type", "prescriber_id"),
    expression="prescriber_id == 'DOC-99'",
    severity="严重不合理用药",
)

DAYS_QTY_RULE = RuleRow(
    rule_id="RX-PRIO-DAYS-QTY",
    layer="合法性",
    basis=BASIS,
    version="2026.10",
    applies_when={},
    required_fields=("days_qty", "prescriber_id"),
    expression="prescriber_id == 'DOC-99'",
    severity="严重不合理用药",
)


def _row(
    prescription_no: str,
    *,
    rx_type: str = "门急诊一般",
    days_qty: str = "1",
    present_columns: frozenset[str] | None = None,
) -> PrescriptionRow:
    return PrescriptionRow(
        batch_no="BATCH-PRIO-001",
        prescription_no=prescription_no,
        visit_no=f"V-{prescription_no}",
        patient_ref=f"P-{prescription_no}",
        dept_code="D-01",
        dept_name="心血管内科门诊",
        prescriber_id="DOC-01",
        drug_code="DRUG-01",
        drug_name="盐酸吗啡注射液",
        rx_type=rx_type,
        days_qty=days_qty,
        clinical_diag="高血压",
        event_time="2026-10-28T08:30:00+08:00",
        present_columns=present_columns if present_columns is not None else frozenset(),
    )


def test_priority_table_from_judgement_path() -> None:
    """判定链产出不可判定项 → 顺序表按「能救多少张处方」排序。"""
    all_columns = frozenset(PRESCRIPTION_FIELDS)
    rows = [
        # 3 张处方缺 rx_type，且整列未落地（接口缺口）
        _row(f"RX-P{index:02d}", rx_type="", present_columns=all_columns - {"rx_type"})
        for index in range(1, 4)
    ] + [
        # 2 张处方缺 days_qty，但栏位在（医师漏填）
        _row(f"RX-Q{index:02d}", days_qty="", present_columns=all_columns)
        for index in range(1, 3)
    ]
    judgement = judge_batch(rows, (RX_TYPE_RULE, DAYS_QTY_RULE), batch_no="BATCH-PRIO-001")
    entries = build_gap_entries(
        judgement.rows, prescriptions=index_prescriptions(rows)
    )
    table = build_priority_table(entries)

    assert [item.missing_field for item in table] == ["rx_type", "days_qty"]
    by_field = {item.missing_field: item for item in table}
    assert by_field["rx_type"].affected_prescriptions == 3
    assert by_field["rx_type"].interface_gap_prescriptions == 3
    assert by_field["rx_type"].route_to == ROUTE_INTERFACE
    assert by_field["days_qty"].affected_prescriptions == 2
    assert by_field["days_qty"].interface_gap_prescriptions == 0
    assert by_field["days_qty"].route_to == ROUTE_PHYSICIAN


def test_priority_counts_match_gap_summary() -> None:
    """顺序表的处方数与不可判定台账的汇总口径一致（两张表对得上）。"""
    all_columns = frozenset(PRESCRIPTION_FIELDS)
    rows = [
        _row(f"RX-P{index:02d}", rx_type="", present_columns=all_columns - {"rx_type"})
        for index in range(1, 4)
    ]
    judgement = judge_batch(rows, (RX_TYPE_RULE,), batch_no="BATCH-PRIO-001")
    entries = build_gap_entries(
        judgement.rows, prescriptions=index_prescriptions(rows)
    )
    summary = {item.missing_field: item for item in summarize_by_field(entries)}
    table = {item.missing_field: item for item in build_priority_table(entries)}
    assert table["rx_type"].affected_prescriptions == summary["rx_type"].affected_prescriptions
    assert table["rx_type"].affected_rules == summary["rx_type"].affected_rules
    assert table["rx_type"].affected_depts == summary["rx_type"].affected_depts


def test_cli_judge_writes_priority_table(tmp_path: Path, capsys) -> None:
    """CLI ``judge`` 跑完产出字段补齐顺序表，并打印排期摘要。"""
    exit_code = main(
        [
            "judge",
            "--inbox",
            str(BATCH_FILE.parent),
            "--out",
            str(tmp_path / "out"),
            "--rules",
            str(SAMPLE_RULES),
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "字段补齐顺序表 →" in captured

    path = tmp_path / "out" / PRIORITY_FILENAME
    assert path.exists()
    records = load_priority_table(path)
    assert records, "样例批次有不可判定项，应产出字段补齐顺序表"
    assert list(records[0].keys()) == list(PRIORITY_COLUMNS)
    # 名次连续且按处方数降序
    assert [record["priority"] for record in records] == [
        str(index) for index in range(1, len(records) + 1)
    ]
    counts = [int(record["affected_prescriptions"]) for record in records]
    assert counts == sorted(counts, reverse=True)
    assert any(record["missing_field"] == "rx_type" for record in records)


def test_cli_judge_json_payload_includes_priority_table(tmp_path: Path, capsys) -> None:
    """``--json`` 输出里带上顺序表路径与内容（运维脚本可直接消费）。"""
    exit_code = main(
        [
            "judge",
            "--inbox",
            str(BATCH_FILE.parent),
            "--out",
            str(tmp_path / "out"),
            "--rules",
            str(SAMPLE_RULES),
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["field_gap_priority_path"].endswith(PRIORITY_FILENAME)
    assert payload["field_gap_priority"], "JSON 载荷应带字段补齐顺序表内容"
    assert payload["field_gap_priority"][0]["priority"] == "1"
    assert "interface_gaps" in payload


def test_priority_table_empty_when_batch_has_no_gaps(tmp_path: Path) -> None:
    """没有字段缺口时顺序表只有表头（下游不必判存在性）。"""
    all_columns = frozenset(PRESCRIPTION_FIELDS)
    rows = [_row("RX-OK-01", present_columns=all_columns)]
    judgement = judge_batch(rows, (RX_TYPE_RULE,), batch_no="BATCH-PRIO-001")
    entries = build_gap_entries(
        judgement.rows, prescriptions=index_prescriptions(rows)
    )
    assert entries == []
    table = build_priority_table(entries)
    assert table == []
    path = write_priority_table(table, tmp_path / "out")
    assert load_priority_table(path) == []


def test_sample_rules_load_without_errors() -> None:
    """样例规则集可载入（端到端用例的前提）。"""
    repository = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repository.load()
    assert repository.errors == ()
    assert repository.effective_rules
