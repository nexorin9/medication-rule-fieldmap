"""入站 HL7 v2 ``ORM^O01`` 解析器的回归（任务书 T10）。

对应任务书 T10 的三类用例：

* **CSV 与 HL7 字段等价** —— 同一份医嘱用两种格式落地，解析出的口径字段必须逐项相同
  （等价范围 = HL7 落地口径覆盖到的字段，见 ``HL7_COVERED_FIELDS``）；
* **非 ORM 消息跳过** —— 结果 / 检验等消息一律显式跳过并告警，不静默丢弃；
* **字段缺项置空** —— HL7 侧字段缺项与 CSV 等价：置空而非报错，由判定层归入不可判定。

另外覆盖：HL7 ``TS`` 时间戳归一、``MSH-2`` 自带编码字符、重复符 / 组件分隔、
一条消息多条 ``RXE`` 明细、GBK 落地文件、HL7 行在判定层落「不可判定」并点名缺失字段。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.judge import judge_batch
from med_rule_fieldmap.expression.normalize import normalize_field
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.ingest.hl7_orm import (
    HL7_COVERED_FIELDS,
    HL7_FIELD_MAP,
    HL7_SUFFIXES,
    Hl7ParseError,
    is_order_message,
    list_hl7_files,
    parse_hl7_timestamp,
    parse_message,
    parse_order_messages,
    read_order_rows,
)
from med_rule_fieldmap.models import PRESCRIPTION_FIELDS, PrescriptionRow
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"
BATCH_FILE = BATCH_DIR / "demo_batch_001.csv"
HL7_DIR = PROJECT_ROOT / "tests" / "data" / "hl7_batch"
HL7_FILE = HL7_DIR / "demo_batch_hl7_001.hl7"
RULES_DIR = PROJECT_ROOT / "rules" / "sample"


# ---------------------------------------------------------------------------
# 构造器：把一条 CSV 医嘱行还原成一条 HL7 ORM^O01 消息
# ---------------------------------------------------------------------------


def build_hl7_message(row: PrescriptionRow, *, control_id: str = "") -> str:
    """按 spec.md §2.1 B 的字段口径生成一条 ``ORM^O01`` 消息。

    段内字段位严格按本项目口径摆放（``PV1-1`` = 就诊号、``ORC-2`` = 医嘱号、
    ``RXO-4`` = 剂量、``RXO-11`` = 单位、``RXE-4`` = 药品名称、``RXE-10/11`` =
    给药剂量与单位），不是 HL7 标准的原始段义。
    """
    stamp = row.event_time.replace("-", "").replace(":", "").replace(" ", "")
    prescriber = f"{row.prescriber_id}^{row.prescriber_name}^PR^D101^主任"
    return "\r".join(
        [
            f"MSH|^~\\&|HIS|HOSP|REVIEW|HIS|{stamp}||ORM^O01|{control_id or row.batch_no}|P|2.5",
            f"PID|1||{row.patient_ref}^^^HIS^MR|||19810101|M",
            f"PV1|{row.visit_no}||||||{prescriber}|||||V{stamp}",
            f"ORC|NW|{row.order_no}|||||{stamp}||{prescriber}",
            f"OMG|1|{row.order_no}||{row.dosage_text}",
            f"RXO|1|{row.route}||{row.dosage_value}|||||||{row.dosage_unit}",
            f"RXE|1|||{row.drug_name}^DC-001^HIS||||||{row.dosage_value}|{row.dosage_unit}",
        ]
    )


def _csv_row(prescription_no: str) -> PrescriptionRow:
    rows = read_prescription_rows(BATCH_FILE)
    by_no = {row.prescription_no: row for row in rows}
    return by_no[prescription_no]


# ---------------------------------------------------------------------------
# 段字段 → 口径字段映射
# ---------------------------------------------------------------------------


def test_mapping_targets_are_all_real_prescription_fields() -> None:
    """映射表自身要自洽：每条规则的落点都在医嘱口径字段全集内。"""
    for rule in HL7_FIELD_MAP:
        assert rule.target in PRESCRIPTION_FIELDS, (
            f"映射 {rule.segment}-{rule.number} 指向了口径外字段 {rule.target!r}"
        )
        assert re.fullmatch(r"[A-Z][A-Z0-9]{2}", rule.segment), (
            f"段名 {rule.segment!r} 不是合法的 HL7 段名"
        )
        assert rule.number >= 1


def test_covered_fields_are_deduplicated_and_sorted_by_first_appearance() -> None:
    """等价性断言的字段范围来自映射表本身，不另写一份清单。"""
    assert len(HL7_COVERED_FIELDS) == len(set(HL7_COVERED_FIELDS))
    assert "batch_no" in HL7_COVERED_FIELDS
    assert "drug_name" in HL7_COVERED_FIELDS
    assert "event_time" in HL7_COVERED_FIELDS


# ---------------------------------------------------------------------------
# CSV ↔ HL7 字段等价
# ---------------------------------------------------------------------------


def test_hl7_and_csv_produce_equivalent_fields() -> None:
    """同一份医嘱用两种格式落地，覆盖到的口径字段逐项相同。"""
    source = _csv_row("RX-0001")
    batch = parse_order_messages(build_hl7_message(source), source="eq.hl7")

    assert batch.skipped == ()
    assert len(batch.rows) == 1
    row = batch.rows[0]

    for name in HL7_COVERED_FIELDS:
        assert row.value(name) == source.value(name), f"字段 {name} 在两种格式下不一致"


def test_typed_values_are_equivalent_not_just_raw_strings() -> None:
    """类型化口径也要一致（``event_time`` 的写法不同但落在同一时刻）。"""
    source = _csv_row("RX-0001")
    batch = parse_order_messages(build_hl7_message(source), source="eq.hl7")
    row = batch.rows[0]

    csv_time = normalize_field("event_time", source.event_time).timestamp
    hl7_time = normalize_field("event_time", row.event_time).timestamp
    assert csv_time is not None and hl7_time is not None
    assert csv_time == hl7_time


def test_hl7_profile_gaps_are_blank_not_invented() -> None:
    """本轮 HL7 落地口径不携带的字段一律置空——不在入站层编造数据。"""
    # 选一条 CSV 侧字段最全的样例行（RX-0021 皮试结果为「未注明」，非空）。
    source = _csv_row("RX-0021")
    batch = parse_order_messages(build_hl7_message(source), source="gap.hl7")
    row = batch.rows[0]

    gaps = (
        "prescription_no",
        "drug_code",
        "rx_type",
        "drug_category",
        "days_qty",
        "skin_test_result",
        "dept_code",
        "dept_name",
        "clinical_diag",
        "age_value",
        "age_unit",
        "freq",
        "spec",
        "dosage_form",
    )
    for name in gaps:
        assert name not in HL7_COVERED_FIELDS, f"{name} 不应在本轮的字段缺口清单里"
        assert row.value(name) == "", f"字段 {name} 应置空，实际 {row.value(name)!r}"
        assert source.value(name) != "", f"样例 CSV 行 {name} 不该为空（测试前提失效）"


# ---------------------------------------------------------------------------
# 缺项处理
# ---------------------------------------------------------------------------


def test_missing_fields_are_blank_and_never_raise() -> None:
    """HL7 侧字段缺项 → 置空而非报错（判定层再归入不可判定）。"""
    message = "\r".join(
        [
            "MSH|^~\\&|HIS|HOSP|REVIEW|HIS|20261028091500||ORM^O01|HL7-MIN|P|2.5",
            "RXE|1|||",
        ]
    )
    batch = parse_order_messages(message, source="min.hl7")

    assert batch.skipped == ()
    assert len(batch.rows) == 1
    row = batch.rows[0]
    assert row.batch_no == "HL7-MIN"
    assert row.drug_name == ""
    assert row.event_time == ""
    assert row.prescriber_name == ""


def test_missing_patient_and_visit_segments_are_tolerated() -> None:
    """没有 ``PID`` / ``PV1`` 段的消息照样落行，患者与就诊字段置空。"""
    message = "\r".join(
        [
            "MSH|^~\\&|HIS|HOSP|REVIEW|HIS|20261028091500||ORM^O01|HL7-NOPV|P|2.5",
            "ORC|NW|ORD-9001|||||20261028091500||PR-001^医师甲",
            "RXE|1|||阿莫西林胶囊",
        ]
    )
    batch = parse_order_messages(message, source="nopv.hl7")

    assert len(batch.rows) == 1
    row = batch.rows[0]
    assert row.patient_ref == ""
    assert row.visit_no == ""
    assert row.order_no == "ORD-9001"
    assert row.prescriber_id == "PR-001"
    assert row.prescriber_name == "医师甲"
    assert row.drug_name == "阿莫西林胶囊"


# ---------------------------------------------------------------------------
# 非医嘱消息跳过
# ---------------------------------------------------------------------------


def test_non_orm_message_is_skipped_with_warning() -> None:
    """非 ``ORM^O01`` 消息显式跳过并告警，不静默丢弃。"""
    message = "\r".join(
        [
            "MSH|^~\\&|LIS|HOSP|REVIEW|HIS|20261028091500||ORU^R01|MSG-777|P|2.5",
            "PID|1||PT-0001",
            "OBR|1|ORD-777",
        ]
    )
    batch = parse_order_messages(message, source="oru.hl7")

    assert batch.rows == ()
    assert len(batch.skipped) == 1
    assert batch.skipped[0].message_type == "ORU^R01"
    assert "跳过" in batch.skipped[0].reason
    assert batch.warnings
    assert "ORU^R01" in batch.warnings[0]


def test_message_without_msh_segment_is_skipped() -> None:
    """没有 ``MSH`` 段的结构损坏消息 → 整消息跳过并告警。"""
    batch = parse_order_messages("PID|1||PT-0001\rOBR|1|ORD-777", source="broken.hl7")

    assert batch.rows == ()
    assert len(batch.skipped) == 1
    assert "MSH" in batch.skipped[0].reason


def test_order_message_without_rxe_is_skipped() -> None:
    """医嘱消息但没有 ``RXE`` 明细 → 显式跳过并告警（不落空行）。"""
    message = "\r".join(
        [
            "MSH|^~\\&|HIS|HOSP|REVIEW|HIS|20261028091500||ORM^O01|HL7-NORXE|P|2.5",
            "ORC|NW|ORD-9002|||||20261028091500||PR-001^医师甲",
        ]
    )
    batch = parse_order_messages(message, source="norxe.hl7")

    assert batch.rows == ()
    assert len(batch.skipped) == 1
    assert "RXE" in batch.skipped[0].reason


def test_orm_message_is_recognised() -> None:
    """``ORM^O01`` 判定为医嘱消息。"""
    message = parse_message(
        "MSH|^~\\&|HIS|HOSP|REVIEW|HIS|20261028091500||ORM^O01|HL7-1|P|2.5"
    )
    assert is_order_message(message) is True
    assert message.control_id == "HL7-1"


# ---------------------------------------------------------------------------
# 一条消息多条明细
# ---------------------------------------------------------------------------


def test_multiple_rxe_segments_produce_one_row_each() -> None:
    """一个医嘱组里的多条 ``RXE`` 明细 → 一条明细一行。"""
    message = "\r".join(
        [
            "MSH|^~\\&|HIS|HOSP|REVIEW|HIS|20261028091500||ORM^O01|HL7-MULTI|P|2.5",
            "ORC|NW|ORD-9003|||||20261028091500||PR-001^医师甲",
            "RXE|1|||盐酸吗啡注射液",
            "RXE|2|||硫酸吗啡缓释片",
            "RXE|3|||地西泮片",
        ]
    )
    batch = parse_order_messages(message, source="multi.hl7")

    assert len(batch.rows) == 3
    assert [row.drug_name for row in batch.rows] == [
        "盐酸吗啡注射液",
        "硫酸吗啡缓释片",
        "地西泮片",
    ]
    assert {row.order_no for row in batch.rows} == {"ORD-9003"}


def test_second_orc_group_starts_a_new_order() -> None:
    """新的 ``ORC`` 开启新的医嘱组，医嘱号不被上一组带过去。"""
    message = "\r".join(
        [
            "MSH|^~\\&|HIS|HOSP|REVIEW|HIS|20261028091500||ORM^O01|HL7-TWO|P|2.5",
            "ORC|NW|ORD-9004|||||20261028091500||PR-001^医师甲",
            "RXE|1|||青霉素钠注射剂",
            "ORC|NW|ORD-9005|||||20261028091500||PR-002^医师乙",
            "RXE|2|||阿莫西林克拉维酸钾片",
        ]
    )
    batch = parse_order_messages(message, source="two.hl7")

    assert [row.order_no for row in batch.rows] == ["ORD-9004", "ORD-9005"]
    assert [row.prescriber_name for row in batch.rows] == ["医师甲", "医师乙"]


# ---------------------------------------------------------------------------
# 分隔符与编码字符
# ---------------------------------------------------------------------------


def test_encoding_characters_come_from_msh_2() -> None:
    """编码字符以消息自带的 ``MSH-2`` 为准（默认 ``^~\\&``）。"""
    message = parse_message(
        "MSH|*!\\&|HIS|HOSP|REVIEW|HIS|20261028091500||ORM*O01|HL7-ENC|P|2.5"
    )
    assert message.encoding.component == "*"
    assert message.encoding.repetition == "!"
    assert message.message_code == "ORM"
    assert message.trigger_event == "O01"


def test_repetition_separator_takes_first_repetition() -> None:
    """重复字段只取第一个重复值（患者标识多机构场景的常规口径）。"""
    message = "\r".join(
        [
            "MSH|^~\\&|HIS|HOSP|REVIEW|HIS|20261028091500||ORM^O01|HL7-REP|P|2.5",
            "PID|1||PT-0001^^^HIS^MR~PT-0002^^^OTHER^MR",
            "RXE|1|||青霉素钠注射剂",
        ]
    )
    batch = parse_order_messages(message, source="rep.hl7")
    assert batch.rows[0].patient_ref == "PT-0001"


def test_component_separator_splits_ce_and_cwe() -> None:
    """组件分隔符把 ``CE`` / ``CWE`` 拆开，只取第 1 个组件。"""
    message = "\r".join(
        [
            "MSH|^~\\&|HIS|HOSP|REVIEW|HIS|20261028091500||ORM^O01|HL7-COMP|P|2.5",
            "RXO|1|NV^静脉滴注^HL70100||5.5^mg^UCUM|||||||mg^毫克",
            "RXE|1|||盐酸吗啡注射液^DC-001^HIS",
        ]
    )
    batch = parse_order_messages(message, source="comp.hl7")
    row = batch.rows[0]
    assert row.route == "NV"
    assert row.dosage_value == "5.5"
    assert row.dosage_unit == "mg"
    assert row.drug_name == "盐酸吗啡注射液"


# ---------------------------------------------------------------------------
# 时间戳归一
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20261028091500", "2026-10-28 09:15:00"),
        ("202610280915", "2026-10-28 09:15"),
        ("20261028", "2026-10-28"),
        ("20261028091500+0800", "2026-10-28 09:15:00+08:00"),
        ("20261028091500.5", "2026-10-28 09:15:00.5"),
        ("", ""),
        ("不是时间", "不是时间"),
    ],
)
def test_hl7_timestamp_normalisation(raw: str, expected: str) -> None:
    """HL7 ``TS`` 归一到口径时间写法；认不出来保留原文。"""
    assert parse_hl7_timestamp(raw) == expected


def test_hl7_timestamp_lands_on_same_moment_as_csv() -> None:
    """归一后与 CSV 的本地写法落在同一时刻。"""
    source = _csv_row("RX-0001")
    batch = parse_order_messages(build_hl7_message(source), source="eq.hl7")
    row = batch.rows[0]
    assert row.event_time == source.event_time


# ---------------------------------------------------------------------------
# 文件读取
# ---------------------------------------------------------------------------


def test_read_order_rows_from_utf8_file() -> None:
    """UTF-8 落地文件读得出处方行。"""
    batch = read_order_rows(HL7_FILE)
    assert batch.rows
    assert {row.batch_no for row in batch.rows} == {"HL7-BATCH-001"}
    assert "盐酸吗啡注射液" in {row.drug_name for row in batch.rows}


def test_read_order_rows_from_gbk_file(tmp_path: Path) -> None:
    """GBK 落地文件也能读（老 HIS 接口常见编码）。"""
    message = build_hl7_message(_csv_row("RX-0002"))
    path = tmp_path / "gbk.hl7"
    path.write_bytes(message.replace("\r", "\r\n").encode("gbk"))

    batch = read_order_rows(path)
    assert len(batch.rows) == 1
    assert batch.rows[0].drug_name == "盐酸吗啡注射液"
    assert batch.rows[0].dept_name == ""  # 口径未覆盖


def test_undecodable_file_raises_whole_file_error(tmp_path: Path) -> None:
    """既不是 UTF-8 也不是 GBK 的字节流 → 整文件错误，不做部分解析。"""
    path = tmp_path / "broken.hl7"
    path.write_bytes(b"\xff\xfe\x00\x81\x8d\x8f\x90\x9d\xff\xfe")

    with pytest.raises(Hl7ParseError) as excinfo:
        read_order_rows(path)
    assert "整文件" in str(excinfo.value)


def test_list_hl7_files_finds_only_hl7(tmp_path: Path) -> None:
    """落地目录里的 HL7 文件按文件名排序列出（CSV 不混进来）。"""
    (tmp_path / "b.hl7").write_text("MSH|^~\\&||||||||ORM^O01|B|P|2.5", encoding="utf-8")
    (tmp_path / "a.hl7").write_text("MSH|^~\\&||||||||ORM^O01|A|P|2.5", encoding="utf-8")
    (tmp_path / "c.csv").write_text("prescription_no\n", encoding="utf-8")

    files = list_hl7_files(tmp_path)
    assert [path.name for path in files] == ["a.hl7", "b.hl7"]
    assert HL7_SUFFIXES == (".hl7",)


# ---------------------------------------------------------------------------
# 判定层衔接：HL7 行落「不可判定」并点名缺失字段
# ---------------------------------------------------------------------------


def test_hl7_rows_fall_to_undeterminable_naming_missing_fields() -> None:
    """HL7 行缺 ``rx_type`` / ``drug_category`` / ``days_qty`` → 不可判定并点名。

    这正是本产品的落点：不是把行丢掉，而是产出一条**带字段归因**的正式结论，
    交给字段缺口台账排接口补齐顺序。
    """
    source = _csv_row("RX-0001")
    batch = parse_order_messages(build_hl7_message(source), source="eq.hl7")
    repository = RuleRepository(RULES_DIR)
    repository.load()
    rules = repository.effective_rules

    judgement = judge_batch(batch.rows, rules, batch_no="HL7-BATCH-001")
    undeterminable = [
        row for row in judgement.rows if row.conclusion.value == "不可判定"
    ]
    assert undeterminable, "HL7 行应当落出不可判定结论"

    named = {name for row in undeterminable for name in row.missing_fields}
    assert "rx_type" in named
    assert "drug_category" in named
    assert "days_qty" in named

    # 归因文本必须点名规则号与依据，且不含患者可识别明细
    reasons = " ".join(row.reason for row in undeterminable)
    assert "RX-LEGAL-MA-OUT-DAYS" in reasons
    assert "处方审核规则体系" in reasons
    assert source.patient_ref not in reasons
