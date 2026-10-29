"""字段缺口台账：把「判不了」按缺失字段聚合成可交付的清单。

**对应源产品能力**：源仓库 ``contrib/database/webhooks/models.py`` 的
``TableWebhookCall``（请求 / 响应副本留痕，可导出复核）与
``contrib/database/field_rules/registries.py`` 的 ``before_row_created`` /
``before_row_updated``（逐行逐规则求值并挂 ``error_text`` 归因）。

**改写**：源产品只有**通用行级聚合**（按行、按列做透视），没有「按缺失字段
分组」的语义。本模块把「审不了」从一句归因说明变成一张**按缺失字段聚合的
正式清单**——每组说清三件事：**缺哪个字段**、**卡在哪条规则**、
**牵涉多少张处方 / 多少个科室**。这是本轮相对 Excel 台账与二元审方结论的
核心增量，也是信息科排接口改造顺序的原料。

**两种「没有值」必须分开（spec.md §2.1）**

落地记录里「这个字段没值」有两种完全不同的成因，处置方也不同：

==================  ==================================  ====================
成因                判据                              处置
==================  ==================================  ====================
接口未回传          **整列未落地**（该列压根没有）      信息科补接口字段
医嘱未填写          **栏位在但内容为空**               退回医师补写
==================  ==================================  ====================

判据落在 ``PrescriptionRow.has_column()``——入站层按表头名（CSV）或通道覆盖
字段（HL7）登记了「这份落地记录里真实存在哪些列」，``present_columns``
非空时就能把两者分开（未登记时一律按「栏位在」处理）。

不分开的后果很实在：字段补齐顺序表会把「医师漏填」当成「接口没接」排到
信息科的改造清单上，科室照着改了一圈接口，字段还是没值。

**口径不可比不算接口缺口**：日量写成 ``3mg`` 时结论同样是「不可判定」，
但字段是**落地了**的（值写错了），该找的是开单医师而不是信息科——本模块
按「栏位在」归到「医嘱未填写」，不混进接口缺口。

**只收不可判定项**：结论是「违规」的行即使 ``missing_fields`` 里有字段名
（如规范性缺项类规则把「前记临床诊断缺项」判成违规），也不进本台账——
那是「处方本身不合格」，药师退回医师补写，与接口缺口是两回事。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..models import Conclusion, JudgementRow, PrescriptionRow

#: 不可判定台账列（顺序固定）
GAP_COLUMNS: tuple[str, ...] = (
    "missing_field",
    "rule_id",
    "cause",
    "affected_prescriptions",
    "affected_depts",
)

#: 不可判定台账文件名
UNDETERMINABLE_FILENAME = "undeterminable_ledger.csv"


class GapCause(str, Enum):
    """字段缺口的成因（spec.md §2.1 的两种「没有值」）。"""

    #: 整列未落地——接口没回传这一列
    NOT_LANDED = "接口未回传"
    #: 栏位在但内容为空——医师没填
    UNFILLED = "医嘱未填写"
    #: 未提供处方索引，成因无从判定（不静默当成接口缺口）
    UNKNOWN = "成因未判定"

    @property
    def needs_interface_change(self) -> bool:
        """这条缺口是否要动接口。

        只有「接口未回传」才算——「医嘱未填写」是退回医师补写，
        「成因未判定」时**不得**断言要改接口。
        """
        return self is GapCause.NOT_LANDED


@dataclass(frozen=True)
class GapEntry:
    """一条「缺失字段 × 规则」的聚合结果。

    ``affected_prescriptions`` 与 ``affected_depts`` 是**去重后的**计数：
    同一张处方在同一条规则下命中多次只算一次，同一科室多张处方只算一个科室。
    """

    missing_field: str
    rule_id: str
    cause: GapCause
    affected_prescriptions: int
    affected_depts: int
    prescription_nos: tuple[str, ...] = ()
    dept_names: tuple[str, ...] = ()

    def to_record(self) -> dict[str, str]:
        """台账行（字符串口径）。"""
        return {
            "missing_field": self.missing_field,
            "rule_id": self.rule_id,
            "cause": self.cause.value,
            "affected_prescriptions": str(self.affected_prescriptions),
            "affected_depts": str(self.affected_depts),
        }


@dataclass(frozen=True)
class FieldGap:
    """按**缺失字段**（跨规则）的汇总——字段补齐顺序表的原料。"""

    missing_field: str
    affected_prescriptions: int
    affected_rules: int
    affected_depts: int
    causes: tuple[str, ...] = ()

    def to_record(self) -> dict[str, str]:
        return {
            "missing_field": self.missing_field,
            "affected_prescriptions": str(self.affected_prescriptions),
            "affected_rules": str(self.affected_rules),
            "affected_depts": str(self.affected_depts),
            "causes": "|".join(self.causes),
        }


def row_identity(row: PrescriptionRow) -> str:
    """一条医嘱行的身份键。

    与判定行口径一致：``prescription_no`` 为空时退回 ``order_no``
    （HL7 通道不携带处方号，只有医嘱号）。两边口径必须一致，
    否则判定行反查回不到原行，字段缺口的成因会误判成「成因未判定」。
    """
    return row.prescription_no or row.order_no


def index_prescriptions(rows: Iterable[PrescriptionRow]) -> dict[str, PrescriptionRow]:
    """按身份键建索引（判定行 → 处方行的反查表）。

    同一身份键出现多次时**后写覆盖**（后读到的文件为准）——同一批次内
    身份键本应唯一，重复出现属落地异常，聚合时以最后一次为准。
    """
    index: dict[str, PrescriptionRow] = {}
    for row in rows:
        key = row_identity(row)
        if key:
            index[key] = row
    return index


def _cause_of(name: str, prescription: PrescriptionRow | None) -> GapCause:
    """判定一条字段缺口的成因。

    没有处方索引 → 成因未判定（**不**当成接口缺口，避免把医师漏填
    排到信息科的改造清单上）；索引里有但该列不存在 → 接口未回传；
    该列存在（只是值为空）→ 医嘱未填写。
    """
    if prescription is None:
        return GapCause.UNKNOWN
    try:
        landed = prescription.has_column(name)
    except KeyError:  # pragma: no cover - 字段名不在医嘱列全集内
        return GapCause.UNKNOWN
    return GapCause.UNFILLED if landed else GapCause.NOT_LANDED


def build_gap_entries(
    rows: Iterable[JudgementRow],
    *,
    prescriptions: Mapping[str, PrescriptionRow] | None = None,
) -> list[GapEntry]:
    """把不可判定项按「缺失字段 × 规则 × 成因」聚合。

    ``prescriptions`` 是处方索引（``index_prescriptions()`` 的产物），
    给了才能分辨「接口未回传」与「医嘱未填写」、才能统计受影响科室；
    不给则成因记「成因未判定」、科室数记 0（未知）。

    **排序稳定**：受影响处方数降序 → 缺失字段名升序 → 规则号升序 →
    成因名升序。同一输入必得同一输出——科室要能逐批对得上。
    """
    buckets: dict[tuple[str, str, GapCause], dict[str, set[str]]] = {}
    for row in rows:
        if row.conclusion is not Conclusion.UNDETERMINABLE:
            # 违规 / 合规的行不进字段缺口台账：那是处方本身不合格，
            # 与「接口没回传字段」是两回事。
            continue
        if not row.missing_fields:
            continue
        prescription = (
            prescriptions.get(row.prescription_no) if prescriptions is not None else None
        )
        dept_name = prescription.dept_name if prescription is not None else ""
        for name in row.missing_fields:
            cause = _cause_of(name, prescription)
            bucket = buckets.setdefault(
                (name, row.rule_id, cause), {"prescriptions": set(), "depts": set()}
            )
            bucket["prescriptions"].add(row.prescription_no)
            if dept_name:
                bucket["depts"].add(dept_name)

    entries = [
        GapEntry(
            missing_field=missing_field,
            rule_id=rule_id,
            cause=cause,
            affected_prescriptions=len(bucket["prescriptions"]),
            affected_depts=len(bucket["depts"]),
            prescription_nos=tuple(sorted(bucket["prescriptions"])),
            dept_names=tuple(sorted(bucket["depts"])),
        )
        for (missing_field, rule_id, cause), bucket in buckets.items()
    ]
    entries.sort(
        key=lambda entry: (
            -entry.affected_prescriptions,
            entry.missing_field,
            entry.rule_id,
            entry.cause.value,
        )
    )
    return entries


def summarize_by_field(entries: Sequence[GapEntry]) -> list[FieldGap]:
    """把「缺失字段 × 规则」的条目再聚合成「一个字段一行」。

    这是字段补齐顺序表（``ledger/priority.py``）的原料：一个字段牵涉
    多少张处方、多少条规则、多少个科室——补一个字段能解锁多少东西。

    条目若未带处方号清单（手工构造），按 ``affected_prescriptions``
    折算成占位计数，聚合结果不会凭空少算。
    """
    buckets: dict[str, dict[str, set[str]]] = {}
    for entry in entries:
        bucket = buckets.setdefault(
            entry.missing_field,
            {"prescriptions": set(), "rules": set(), "depts": set(), "causes": set()},
        )
        if entry.prescription_nos:
            bucket["prescriptions"].update(entry.prescription_nos)
        else:
            for index in range(entry.affected_prescriptions):
                bucket["prescriptions"].add(f"{entry.rule_id}#{index}")
        bucket["rules"].add(entry.rule_id)
        bucket["depts"].update(entry.dept_names)
        bucket["causes"].add(entry.cause.value)

    summary = [
        FieldGap(
            missing_field=missing_field,
            affected_prescriptions=len(bucket["prescriptions"]),
            affected_rules=len(bucket["rules"]),
            affected_depts=len(bucket["depts"]),
            causes=tuple(sorted(bucket["causes"])),
        )
        for missing_field, bucket in buckets.items()
    ]
    summary.sort(key=lambda item: (-item.affected_prescriptions, item.missing_field))
    return summary


def write_undeterminable_ledger(
    entries: Iterable[GapEntry],
    out_dir: str | Path,
    *,
    filename: str = UNDETERMINABLE_FILENAME,
) -> Path:
    """把字段缺口条目写成不可判定台账 CSV，返回落盘路径。

    ``out_dir`` 不存在时自动创建；编码用 ``utf-8-sig``（Excel 直开不乱码）。
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(GAP_COLUMNS))
        writer.writeheader()
        for entry in entries:
            writer.writerow(entry.to_record())
    return target


def load_undeterminable_ledger(path: str | Path) -> list[dict[str, str]]:
    """读回不可判定台账（供回归与字段补齐顺序表消费）。"""
    target = Path(path)
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(record) for record in csv.DictReader(handle)]


__all__ = [
    "GAP_COLUMNS",
    "GapCause",
    "GapEntry",
    "FieldGap",
    "UNDETERMINABLE_FILENAME",
    "build_gap_entries",
    "index_prescriptions",
    "load_undeterminable_ledger",
    "summarize_by_field",
    "write_undeterminable_ledger",
]
