"""入站 CSV 读取：把 HIS 医嘱落地文件读成 :class:`PrescriptionRow` 列表。

**字段口径**：spec.md §2.1 A 段 —— 医嘱落地目录里的 CSV，首行表头，
按**表头名映射**（列序不敏感），UTF-8 优先、GBK 兜底解码。

**对应源产品能力**：源仓库 ``integrations/zapier/index.js`` 的
``searches: { get-single-row, list-rows }`` —— 读侧是**按 key 注册的取数处理器**，
产物是行；**改写**：读侧从「行数据库」换成「HIS 医嘱落地目录的批次文件」，
且取数**按字段名定位而不是按列序定位**，因此 HIS 导出文件列序打乱、
列名带中文别名都不影响解析。

**本轮落地五件事**

1. **表头名映射 + 列序无关**：表头归一到口径字段名（spec.md §2.1）；
2. **表头别名归一**：HIS 导出常见中文名 / 驼峰写法（``处方号`` / ``prescriptionNo``）
   → 口径字段名；
3. **编码兜底**：UTF-8（含 BOM）优先，GBK/GB18030 兜底，仍失败 → 整文件挂起；
4. **必需列完整性校验**：缺必需列 → 抛 :class:`CsvReadError` 并带缺列清单，
   **绝不部分读取**（挂起台账与告警由入站完整性任务接管）；
5. **类型化读数**：数值列容忍单位后缀（``3日量`` → 数值 3 / 单位「日」）、
   时间列接受 ISO8601 与本地写法——归一口径复用判定前的归一层，
   入站不另起一套解析规则，避免两处口径对不上。

**入站不做判断**：落地文件里的原文一律原样保留（``3日量`` / ``5mg`` / ``qd``
都不在读入阶段被改写），类型化读数是**读数视图**，判定内核按归一层口径取用。
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..expression.normalize import normalize_field
from ..models import NUMERIC_FIELDS, PRESCRIPTION_FIELDS, PrescriptionRow

# ---------------------------------------------------------------------------
# 口径常量
# ---------------------------------------------------------------------------

#: 候选编码（按优先级尝试）：HIS 导出常见 UTF-8（含 BOM）与 GBK/GB18030。
CSV_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "gb18030")

#: 医嘱落地文件后缀
CSV_SUFFIXES: tuple[str, ...] = (".csv",)

#: 必需列：缺任意一列 → **整文件挂起**，不做部分判定。
#:
#: 判据是「缺了这列，这张处方就判不了」：
#:
#: * ``prescription_no`` —— 判定业务键的一环（批次号｜处方号｜规则号｜规则版本）；
#: * ``visit_no`` —— 跨行判定（重复给药、超常处方）以就诊号聚合；
#: * ``drug_code`` —— 药品标识，规则按药品 / 类别匹配；
#: * ``event_time`` —— 事件时间，「同一就诊同一药品同日」判定要用。
#:
#: 注意 spec.md §2.1 标注 ★ 常缺的 ``rx_type`` / ``drug_category`` / ``days_qty`` /
#: ``skin_test_result`` **不在必需列里**：它们缺了是「判不了」的来源，
#: 由判定层落第三态「不可判定」并点名字段，而不是把整份文件挂起。
REQUIRED_COLUMNS: tuple[str, ...] = (
    "prescription_no",
    "visit_no",
    "drug_code",
    "event_time",
)

#: 数值型列（读入时容忍单位后缀）：``3日量`` → 数值 3 / 单位「日」。
NUMERIC_COLUMNS: tuple[str, ...] = NUMERIC_FIELDS

#: 时间型列（接受 ISO8601 与本地写法）：``2026-10-28 08:30`` 与
#: ``2026-10-28T08:30:00`` 解析到同一时刻。
TIME_COLUMNS: tuple[str, ...] = ("event_time",)

#: 表头别名 → 口径字段名（HIS 导出的常见中文名；驼峰写法由字段名本身覆盖）。
COLUMN_ALIASES: dict[str, str] = {
    # 标识
    "批次号": "batch_no",
    "批次编号": "batch_no",
    "批次": "batch_no",
    "医嘱号": "order_no",
    "医嘱流水号": "order_no",
    "申请单号": "order_no",
    "处方号": "prescription_no",
    "处方编号": "prescription_no",
    "处方流水号": "prescription_no",
    "就诊号": "visit_no",
    "门诊号": "visit_no",
    "住院号": "visit_no",
    "就诊流水号": "visit_no",
    "患者标识": "patient_ref",
    "患者号": "patient_ref",
    "病案号": "patient_ref",
    "就诊卡号": "patient_ref",
    # 科室与医师
    "科室编码": "dept_code",
    "科室代码": "dept_code",
    "申请科室编码": "dept_code",
    "科室名称": "dept_name",
    "申请科室": "dept_name",
    "开单科室": "dept_name",
    "医师工号": "prescriber_id",
    "医生工号": "prescriber_id",
    "开立医师工号": "prescriber_id",
    "处方医师工号": "prescriber_id",
    "医师姓名": "prescriber_name",
    "医生姓名": "prescriber_name",
    "开立医师": "prescriber_name",
    "处方医师": "prescriber_name",
    # 药品
    "药品编码": "drug_code",
    "药品代码": "drug_code",
    "药品编号": "drug_code",
    "药品名称": "drug_name",
    "药品名": "drug_name",
    "规格": "spec",
    "规格型号": "spec",
    "剂型": "dosage_form",
    "药品剂型": "dosage_form",
    # 用法用量
    "用法用量": "dosage_text",
    "用法": "dosage_text",
    "剂量用法": "dosage_text",
    "剂量": "dosage_value",
    "剂量值": "dosage_value",
    "单次剂量": "dosage_value",
    "每次剂量": "dosage_value",
    "剂量单位": "dosage_unit",
    "给药途径": "route",
    "用药途径": "route",
    "途径": "route",
    "频次": "freq",
    "用药频次": "freq",
    "给药频次": "freq",
    "数量": "quantity",
    "发药数量": "quantity",
    "数量合计": "quantity",
    # ★ 常缺的三态来源字段
    "处方类型": "rx_type",
    "处方分类": "rx_type",
    "药品类别": "drug_category",
    "特殊管理类别": "drug_category",
    "药品分类": "drug_category",
    "日量": "days_qty",
    "处方日量": "days_qty",
    "开具日量": "days_qty",
    "疗程日量": "days_qty",
    # 延长理由（《处方管理办法》第二十三条：精二延长须注明理由）
    "延长理由": "extend_reason",
    "延长说明": "extend_reason",
    "超量理由": "extend_reason",
    "超量说明": "extend_reason",
    "处方理由": "extend_reason",
    "理由": "extend_reason",
    "备注": "extend_reason",
    "皮试结果": "skin_test_result",
    "皮肤试验结果": "skin_test_result",
    "过敏试验结果": "skin_test_result",
    "皮试": "skin_test_result",
    # 诊断与年龄
    "临床诊断": "clinical_diag",
    "诊断": "clinical_diag",
    "年龄": "age_value",
    "年龄值": "age_value",
    "年龄单位": "age_unit",
    # 时间
    "事件时间": "event_time",
    "医嘱时间": "event_time",
    "开立时间": "event_time",
    "报告时间": "event_time",
}


class CsvReadError(ValueError):
    """CSV 的**整文件级**问题：解码失败、无表头、必需列缺失。

    整文件级问题一律不做部分读取——半份数据会让药师拿到一份看起来正常、
    实则漏判的台账。``missing_columns`` 给出缺失列清单，供挂起台账与
    信息科的字段补齐排期使用。
    """

    def __init__(self, message: str, *, missing_columns: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.missing_columns: tuple[str, ...] = tuple(missing_columns)


@dataclass(frozen=True)
class TypedCell:
    """一列的类型化读数。

    ``raw`` 是落地文件里的原文（判定链仍以原文为准），
    ``number`` / ``unit`` / ``timestamp`` 是类型化解析出来的结构化值，
    ``state`` 区分**缺失**（字段空着）与**显式否**（字段被写成「未注明」）
    ——两者在判定结论上不是一回事。
    """

    field: str
    raw: str
    kind: str
    state: str = "有值"
    text: str = ""
    number: Decimal | None = None
    unit: str = ""
    timestamp: datetime | None = None
    comparable: bool = True
    label: str = ""
    note: str = ""


# ---------------------------------------------------------------------------
# 表头归一
# ---------------------------------------------------------------------------


def _header_key(name: str) -> str:
    """表头归一键：NFKC 归一（全角转半角）+ 去空白与下划线连字符 + 大小写折叠。"""
    text = unicodedata.normalize("NFKC", str(name)).strip().casefold()
    return re.sub(r"[\s_\-·()\[\]{}、]+", "", text)


def _build_column_index() -> dict[str, str]:
    """表头归一键 → 口径字段名。

    口径字段名本身优先（精确命中优先于别名），别名只补口径没覆盖的写法。
    """
    index: dict[str, str] = {}
    for field in PRESCRIPTION_FIELDS:
        index[_header_key(field)] = field
    for alias, field in COLUMN_ALIASES.items():
        index.setdefault(_header_key(alias), field)
    return index


#: 表头归一键 → 口径字段名。
COLUMN_INDEX: dict[str, str] = _build_column_index()


def normalize_header_name(name: str) -> str | None:
    """表头名归一到口径字段名；不在口径内的列返回 ``None``（该列忽略）。"""
    return COLUMN_INDEX.get(_header_key(name))


def build_column_map(header: Sequence[str]) -> dict[str, int]:
    """把表头映射成 ``{口径字段名: 列下标}``。

    * 精确命中口径字段名的列优先于别名列；
    * 同一口径字段被多列命中时取**第一次**出现的列（同输入同输出）；
    * 不在口径内的列忽略（HIS 落地文件常有额外列）。
    """
    exact: dict[str, int] = {}
    aliased: dict[str, int] = {}
    for index, name in enumerate(header):
        canonical = normalize_header_name(name)
        if canonical is None:
            continue
        if _header_key(name) == _header_key(canonical):
            exact.setdefault(canonical, index)
        else:
            aliased.setdefault(canonical, index)
    exact.update(aliased)
    return exact


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def decode_bytes(raw: bytes, *, source: str = "") -> str:
    """按候选编码依次尝试解码；全部失败即报整文件错误（不做部分解码）。"""
    for encoding in CSV_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvReadError(
        f"文件 {source or '<未知>'} 不是可识别的 UTF-8 / GBK 文本，整文件挂起，不做部分读取"
    )


def _clean_cell(value: object) -> str:
    """单元格转字符串：``None`` → 空串，其余去首尾空白。"""
    if value is None:
        return ""
    return str(value).strip()


def _missing_required_columns(columns: dict[str, int]) -> tuple[str, ...]:
    """必需列里缺了哪几列（按口径顺序返回，便于台账对齐）。"""
    return tuple(name for name in REQUIRED_COLUMNS if name not in columns)


def read_prescription_rows(path: str | Path) -> list[PrescriptionRow]:
    """读取一份医嘱落地 CSV，返回处方行列表。

    * 表头按名称映射，列序打乱不影响解析；
    * 表头别名（中文名 / 驼峰写法）归一到口径字段名；
    * 表头之外的列忽略（HIS 落地文件常有额外列）；
    * 必需列缺失 → 抛 :class:`CsvReadError`（带缺列清单），**不部分读取**；
    * ``batch_no`` 取自文件内字段，文件里没写时回落到文件名（spec.md §2.1）；
    * 完全空白的行跳过。
    """
    file_path = Path(path)
    text = decode_bytes(file_path.read_bytes(), source=str(file_path))
    reader = csv.reader(io.StringIO(text, newline=""))

    try:
        header = [str(name) for name in next(reader)]
    except StopIteration:
        raise CsvReadError(
            f"文件 {file_path} 没有表头，整文件挂起，不做部分读取"
        ) from None

    columns = build_column_map(header)
    missing = _missing_required_columns(columns)
    if missing:
        raise CsvReadError(
            f"文件 {file_path} 缺少必需列：{'、'.join(missing)}；"
            "整文件挂起，不做部分判定",
            missing_columns=missing,
        )

    rows: list[PrescriptionRow] = []
    for record in reader:
        values = {
            name: _clean_cell(record[index]) if index < len(record) else ""
            for name, index in columns.items()
        }
        if not any(values.get(name) for name in PRESCRIPTION_FIELDS):
            continue  # 全空行
        row_values = {name: values.get(name, "") for name in PRESCRIPTION_FIELDS}
        if not row_values["batch_no"]:
            row_values["batch_no"] = file_path.stem
        rows.append(
            PrescriptionRow(
                **row_values,
                # 登记本文件真实存在的列：判定层据此区分「整列未落地」
                # （接口没回传这一列）与「栏位在但内容为空」（医师没填）。
                present_columns=frozenset(columns),
            )
        )
    return rows


def list_csv_files(inbox_dir: str | Path) -> list[Path]:
    """列出落地目录下的 CSV 文件（按文件名排序，保证批次处理顺序稳定）。"""
    directory = Path(inbox_dir)
    if not directory.is_dir():
        return []
    files = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in CSV_SUFFIXES
    ]
    return sorted(files, key=lambda item: item.name)


# ---------------------------------------------------------------------------
# 类型化读数
# ---------------------------------------------------------------------------


def read_typed_cell(field: str, raw: str) -> TypedCell:
    """把一列读成类型化结果。

    * 数值列容忍单位后缀：``3日量`` → 数值 3 / 单位「日」；
      写成 ``3mg`` 的日量数值是 3 但口径不可比（``comparable=False``），
      由判定内核据此落「不可判定」而不是误判违规。
    * 时间列接受 ISO8601 与本地写法，解析到同一时刻。
    * 转不动的**保留原文**：``适量`` 不当成 0，``未注明`` 不当成缺失。
    """
    text = "" if raw is None else str(raw)
    value = normalize_field(field, text)

    if field in NUMERIC_COLUMNS:
        kind = "数值" if value.number is not None else (
            "空" if value.state.value == "缺失" else "文本"
        )
    elif field in TIME_COLUMNS:
        kind = "时间" if value.timestamp is not None else (
            "空" if value.state.value == "缺失" else "文本"
        )
    else:
        kind = "空" if value.state.value == "缺失" else "文本"

    note = ""
    if value.state.value == "有值" and value.number is None and field in NUMERIC_COLUMNS:
        note = "非数值文本，保留原文（不当成 0），由判定层按口径不可比处理"
    elif value.state.value == "有值" and value.timestamp is None and field in TIME_COLUMNS:
        note = "时间格式无法解析，保留原文，交判定层按不可比处理"

    return TypedCell(
        field=field,
        raw=text,
        kind=kind,
        state=value.state.value,
        text=value.text,
        number=value.number,
        unit=value.unit,
        timestamp=value.timestamp,
        comparable=value.comparable,
        label=value.label,
        note=note,
    )


def read_typed_cells(row: PrescriptionRow) -> dict[str, TypedCell]:
    """整行类型化读数：``列名 → TypedCell``（覆盖医嘱字段全集）。"""
    return {
        name: read_typed_cell(name, row.value(name)) for name in PRESCRIPTION_FIELDS
    }


__all__ = [
    "COLUMN_ALIASES",
    "COLUMN_INDEX",
    "CSV_ENCODINGS",
    "CSV_SUFFIXES",
    "NUMERIC_COLUMNS",
    "REQUIRED_COLUMNS",
    "TIME_COLUMNS",
    "CsvReadError",
    "TypedCell",
    "build_column_map",
    "decode_bytes",
    "list_csv_files",
    "normalize_header_name",
    "read_prescription_rows",
    "read_typed_cell",
    "read_typed_cells",
]
