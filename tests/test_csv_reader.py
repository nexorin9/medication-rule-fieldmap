"""入站 CSV 解析器的回归（任务书 T9）。

对应任务书 T9 的四类用例：

* **列序打乱** —— HIS 导出文件的列序不固定，解析必须按表头名定位；
* **GBK 文件可解码** —— 老 HIS 导出的落地文件是 GBK，必须兜底解出来；
* **表头缺列时报错并列出缺哪几列** —— 整文件挂起，不做部分判定；
* **数值列单位后缀** —— ``3日量`` 要读成数值 3 并保留单位「日」。

另外覆盖：表头别名归一（中文表头）、``batch_no`` 回落到文件名、
日期列 ISO8601 与本地写法解析到同一时刻、空值 ≠ 显式否、
★ 常缺字段（``rx_type`` / ``days_qty`` …）缺列**不**触发整文件挂起。
"""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from med_rule_fieldmap.ingest.csv_reader import (
    COLUMN_ALIASES,
    REQUIRED_COLUMNS,
    CsvReadError,
    build_column_map,
    normalize_header_name,
    read_prescription_rows,
    read_typed_cell,
    read_typed_cells,
)
from med_rule_fieldmap.models import PRESCRIPTION_FIELDS, PrescriptionRow

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"
BATCH_FILE = BATCH_DIR / "demo_batch_001.csv"

#: 一份最小可判定的医嘱行（必需列齐全）。
MINIMAL_HEADER = [
    "prescription_no",
    "visit_no",
    "drug_code",
    "event_time",
    "days_qty",
    "rx_type",
    "drug_category",
    "skin_test_result",
    "dosage_value",
    "dosage_unit",
    "age_value",
    "age_unit",
]


def _write_csv(path: Path, header: list[str], records: list[list[str]], *, encoding: str = "utf-8") -> Path:
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(records)
    return path


def _write_bytes_csv(path: Path, header: list[str], records: list[list[str]]) -> Path:
    """按 GBK 写文件（老 HIS 导出的常见编码）。"""
    lines = [",".join(header)] + [",".join(record) for record in records]
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("gbk"))
    return path


# ---------------------------------------------------------------------------
# 表头映射：列序无关 + 别名归一
# ---------------------------------------------------------------------------


def test_column_order_is_irrelevant(tmp_path: Path) -> None:
    """列序打乱不影响解析：按表头名定位，不是按列序定位。"""
    source = read_prescription_rows(BATCH_FILE)

    with BATCH_FILE.open(encoding="utf-8", newline="") as handle:
        records = list(csv.reader(handle))
    header, first = records[0], records[1]
    shuffled = tmp_path / "shuffled.csv"
    with shuffled.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(reversed(header)))
        writer.writerow(list(reversed(first)))

    rows = read_prescription_rows(shuffled)
    assert len(rows) == 1
    assert rows[0].prescription_no == source[0].prescription_no
    assert rows[0].drug_code == source[0].drug_code
    assert rows[0].drug_name == source[0].drug_name
    assert rows[0].days_qty == source[0].days_qty
    assert rows[0].event_time == source[0].event_time


def test_chinese_header_aliases_map_to_canonical_fields(tmp_path: Path) -> None:
    """HIS 导出常见中文表头归一到口径字段名（值落在口径字段上）。"""
    header = [
        "处方号",
        "就诊号",
        "药品编码",
        "事件时间",
        "处方类型",
        "药品类别",
        "日量",
        "皮试结果",
    ]
    path = _write_csv(
        tmp_path / "alias.csv",
        header,
        [["RX-9001", "VISIT-9001", "DC-9001", "2026-10-28 09:00:00", "住院", "第一类精神药品", "3日量", "阴性"]],
    )

    rows = read_prescription_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row.prescription_no == "RX-9001"
    assert row.visit_no == "VISIT-9001"
    assert row.drug_code == "DC-9001"
    assert row.event_time == "2026-10-28 09:00:00"
    assert row.rx_type == "住院"
    assert row.drug_category == "第一类精神药品"
    assert row.days_qty == "3日量"
    assert row.skin_test_result == "阴性"


def test_alias_table_covers_canonical_names_and_has_no_conflicts() -> None:
    """别名表自身要自洽：每个别名归一到真实存在的口径字段，且不与口径字段名打架。"""
    for alias, canonical in COLUMN_ALIASES.items():
        assert canonical in PRESCRIPTION_FIELDS, f"别名 {alias!r} 指向了口径外字段 {canonical!r}"
        assert normalize_header_name(alias) == canonical

    # 口径字段名本身一定能被认出
    for field in PRESCRIPTION_FIELDS:
        assert normalize_header_name(field) == field

    # 驼峰写法与下划线写法等价
    assert normalize_header_name("prescriptionNo") == "prescription_no"
    assert normalize_header_name("  PRESCRIPTION_NO ") == "prescription_no"


def test_unknown_columns_are_ignored(tmp_path: Path) -> None:
    """口径外的列（HIS 落地文件常有额外列）忽略，不影响解析。"""
    header = MINIMAL_HEADER + ["his_internal_flag", "备注"]
    path = _write_csv(
        tmp_path / "extra.csv",
        header,
        [["RX-9002", "VISIT-9002", "DC-9002", "2026-10-28 09:00:00", "", "", "", "", "", "", "", ""]],
    )

    rows = read_prescription_rows(path)
    assert len(rows) == 1
    assert rows[0].prescription_no == "RX-9002"
    columns = build_column_map(header)
    assert "his_internal_flag" not in columns


# ---------------------------------------------------------------------------
# 编码兜底
# ---------------------------------------------------------------------------


def test_gbk_file_is_decoded(tmp_path: Path) -> None:
    """GBK 落地文件必须能解码（老 HIS 导出常见编码）。"""
    path = _write_bytes_csv(
        tmp_path / "gbk.csv",
        MINIMAL_HEADER,
        [["RX-9003", "VISIT-9003", "DC-9003", "2026-10-28 09:00:00", "3日量", "住院", "第一类精神药品", "阴性", "5", "mg", "45", "岁"]],
    )

    rows = read_prescription_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row.prescription_no == "RX-9003"
    assert row.days_qty == "3日量"
    assert row.skin_test_result == "阴性"


def test_gbk_with_chinese_department_name_round_trips(tmp_path: Path) -> None:
    """GBK 文件里的中文科室名要原样读出来。"""
    header = MINIMAL_HEADER + ["dept_name"]
    path = _write_bytes_csv(
        tmp_path / "gbk_dept.csv",
        header,
        [["RX-9004", "VISIT-9004", "DC-9004", "2026-10-28 09:00:00", "", "", "", "", "", "", "", "", "心血管内科门诊"]],
    )

    rows = read_prescription_rows(path)
    assert rows[0].dept_name == "心血管内科门诊"


def test_undecodable_file_raises_whole_file_error(tmp_path: Path) -> None:
    """既不是 UTF-8 也不是 GBK 的字节流 → 整文件错误，不做部分读取。"""
    path = tmp_path / "broken.csv"
    path.write_bytes(b"\xff\xfe\x00\x81\x8d\x8f\x90\x9d\xff\xfe")

    with pytest.raises(CsvReadError) as excinfo:
        read_prescription_rows(path)
    assert "整文件挂起" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 必需列完整性
# ---------------------------------------------------------------------------


def test_missing_required_column_raises_and_lists_missing_columns(tmp_path: Path) -> None:
    """表头缺必需列 → 报错并列出缺哪几列。"""
    header = [name for name in MINIMAL_HEADER if name != "drug_code"]
    path = _write_csv(tmp_path / "missing.csv", header, [])

    with pytest.raises(CsvReadError) as excinfo:
        read_prescription_rows(path)
    error = excinfo.value
    assert error.missing_columns == ("drug_code",)
    assert "drug_code" in str(error)
    assert "整文件挂起" in str(error)
    assert "不做部分判定" in str(error)


def test_missing_columns_are_listed_in_canonical_order(tmp_path: Path) -> None:
    """缺多列时清单按口径顺序给出，便于台账对齐。"""
    header = [name for name in MINIMAL_HEADER if name not in ("visit_no", "event_time")]
    path = _write_csv(tmp_path / "missing_two.csv", header, [])

    with pytest.raises(CsvReadError) as excinfo:
        read_prescription_rows(path)
    assert excinfo.value.missing_columns == ("visit_no", "event_time")


def test_starred_optional_columns_are_not_required(tmp_path: Path) -> None:
    """★ 常缺字段（rx_type / drug_category / days_qty / skin_test_result）缺列不挂起。

    它们缺了是「判不了」的来源，由判定层落第三态，不是整文件问题。
    """
    header = ["prescription_no", "visit_no", "drug_code", "event_time"]
    path = _write_csv(
        tmp_path / "starred_missing.csv",
        header,
        [["RX-9005", "VISIT-9005", "DC-9005", "2026-10-28 09:00:00"]],
    )

    rows = read_prescription_rows(path)
    assert len(rows) == 1
    assert rows[0].rx_type == ""
    assert rows[0].days_qty == ""
    assert rows[0].skin_test_result == ""
    assert "rx_type" not in REQUIRED_COLUMNS
    assert "days_qty" not in REQUIRED_COLUMNS


def test_missing_header_raises(tmp_path: Path) -> None:
    """空文件（没有表头）→ 整文件错误。"""
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises(CsvReadError) as excinfo:
        read_prescription_rows(path)
    assert "没有表头" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 类型化读数：数值列单位后缀 + 日期列
# ---------------------------------------------------------------------------


def test_numeric_column_tolerates_unit_suffix() -> None:
    """``3日量`` 读成数值 3 并保留单位「日」。"""
    cell = read_typed_cell("days_qty", "3日量")
    assert cell.number == Decimal("3")
    assert cell.unit == "日"
    assert cell.kind == "数值"
    assert cell.comparable is True
    assert cell.raw == "3日量"


def test_numeric_column_keeps_unknown_unit_verbatim() -> None:
    """未知单位不猜：数值取出来，单位原样保留。"""
    cell = read_typed_cell("dosage_value", "5mg")
    assert cell.number == Decimal("5")
    assert cell.unit == "mg"


def test_days_quantity_with_wrong_unit_is_not_comparable() -> None:
    """日量写成 ``3mg``：数值是 3，但口径不可比 → 交判定层落不可判定。"""
    cell = read_typed_cell("days_qty", "3mg")
    assert cell.number == Decimal("3")
    assert cell.comparable is False


def test_non_numeric_text_is_kept_verbatim_and_never_zero() -> None:
    """``适量`` 保留原文，绝不当成 0。"""
    cell = read_typed_cell("dosage_value", "适量")
    assert cell.number is None
    assert cell.text == "适量"
    assert cell.kind == "文本"
    assert cell.note


def test_empty_numeric_cell_is_missing_not_zero() -> None:
    """字段留空是缺失，不是 0。"""
    cell = read_typed_cell("days_qty", "")
    assert cell.number is None
    assert cell.state == "缺失"
    assert cell.kind == "空"


def test_explicit_no_is_not_missing() -> None:
    """``未注明`` 是显式否（有值），与字段留空（缺失）不是一回事。"""
    explicit = read_typed_cell("skin_test_result", "未注明")
    empty = read_typed_cell("skin_test_result", "")

    assert explicit.state == "显式否"
    assert explicit.raw == "未注明"
    assert empty.state == "缺失"


def test_event_time_accepts_iso8601_and_local_format(tmp_path: Path) -> None:
    """ISO8601 与本地写法解析到同一时刻。"""
    iso = read_typed_cell("event_time", "2026-10-28T08:30:00")
    local = read_typed_cell("event_time", "2026-10-28 08:30")
    with_seconds = read_typed_cell("event_time", "2026-10-28 08:30:00")

    assert iso.timestamp is not None
    assert iso.timestamp == local.timestamp
    assert iso.timestamp == with_seconds.timestamp
    assert iso.kind == "时间"


def test_unparsable_event_time_keeps_verbatim(tmp_path: Path) -> None:
    """时间解析不出来时保留原文，不静默当成某个时刻。"""
    cell = read_typed_cell("event_time", "昨天上午")
    assert cell.timestamp is None
    assert cell.raw == "昨天上午"
    assert cell.note


def test_typed_cells_cover_every_prescription_field() -> None:
    """类型化读数覆盖医嘱字段全集（每列都有读数，不漏列）。"""
    row = PrescriptionRow(
        prescription_no="RX-9006",
        visit_no="VISIT-9006",
        drug_code="DC-9006",
        event_time="2026-10-28 08:30",
        days_qty="3日量",
        skin_test_result="未注明",
    )
    cells = read_typed_cells(row)

    assert set(cells) == set(PRESCRIPTION_FIELDS)
    assert cells["days_qty"].number == Decimal("3")
    assert cells["days_qty"].unit == "日"
    assert cells["skin_test_result"].state == "显式否"
    assert cells["event_time"].timestamp is not None
    assert cells["prescription_no"].raw == "RX-9006"


# ---------------------------------------------------------------------------
# 批次号回落与其他读取细节
# ---------------------------------------------------------------------------


def test_batch_no_falls_back_to_file_name(tmp_path: Path) -> None:
    """文件里没写 batch_no 时回落到文件名（spec.md §2.1）。"""
    path = _write_csv(
        tmp_path / "BATCH-2026-1028-A.csv",
        ["prescription_no", "visit_no", "drug_code", "event_time"],
        [["RX-9007", "VISIT-9007", "DC-9007", "2026-10-28 09:00:00"]],
    )

    rows = read_prescription_rows(path)
    assert rows[0].batch_no == "BATCH-2026-1028-A"


def test_batch_no_in_file_wins_over_file_name(tmp_path: Path) -> None:
    """文件内的批次号优先于文件名。"""
    path = _write_csv(
        tmp_path / "file-name.csv",
        ["batch_no", "prescription_no", "visit_no", "drug_code", "event_time"],
        [["BATCH-IN-FILE", "RX-9008", "VISIT-9008", "DC-9008", "2026-10-28 09:00:00"]],
    )

    rows = read_prescription_rows(path)
    assert rows[0].batch_no == "BATCH-IN-FILE"


def test_blank_rows_are_skipped_and_short_rows_are_padded(tmp_path: Path) -> None:
    """全空行跳过；列数不足的行按缺列补空，不报错。"""
    path = _write_csv(
        tmp_path / "ragged.csv",
        MINIMAL_HEADER,
        [
            [],
            ["RX-9009"],
            ["RX-9010", "VISIT-9010", "DC-9010", "2026-10-28 09:00:00"],
        ],
    )

    rows = read_prescription_rows(path)
    assert [row.prescription_no for row in rows] == ["RX-9009", "RX-9010"]
    assert rows[0].visit_no == ""
    assert rows[1].days_qty == ""


def test_sample_batch_still_reads_to_the_same_rows() -> None:
    """样例批次回归：行数与关键字段不受本轮改动影响。"""
    rows = read_prescription_rows(BATCH_FILE)
    assert len(rows) >= 20
    by_no = {row.prescription_no: row for row in rows}
    assert by_no["RX-0001"].drug_name == "盐酸吗啡注射液"
    assert by_no["RX-0007"].rx_type == ""
    assert by_no["RX-0020"].skin_test_result == ""
