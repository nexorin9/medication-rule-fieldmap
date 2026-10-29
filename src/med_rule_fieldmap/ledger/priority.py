"""字段补齐顺序表：把「审不了」排成一张能直接照着排期的顺序表。

**对应源产品能力**：源仓库 ``contrib/database/webhooks/models.py`` 的
``TableWebhookCall``（请求 / 响应副本留痕，可导出复核）与
``contrib/database/field_rules/registries.py`` 的 ``before_row_created`` /
``before_row_updated``（逐行逐规则求值并挂 ``error_text`` 归因）。

**改写**：源产品只有**通用行级聚合**（按行、按列做透视），没有「先补哪个
字段最划算」这一层。``ledger/gap.py`` 解决了「缺哪个字段」，本模块解决
「**先补哪个**」——按「补一个字段能救多少张处方、解锁多少条规则」排序，
产出 ``out/field_gap_priority.csv``，直接给信息科接口岗排接口改造顺序。

**排序规则（任务书口径 · 三级 · 全序）**

1. 受影响处方数**降序**（补这个字段能多救几张处方）；
2. 受影响规则数**降序**（补这个字段能解锁几条规则）；
3. 缺失字段名**升序**（同分兜底，保证唯一序）。

三级键合起来是**全序**（字段名唯一），因此**同输入必得同输出**——信息科
与药学部逐批对账时，两份表对得上；每次重跑不会因为字典序或插入顺序变化而
换一张表。

**两种「没有值」在顺序表里仍然分开**：``ledger/gap.py`` 已把成因分成
「接口未回传」（整列未落地 → 要动接口）与「医嘱未填写」（栏位在但为空 →
退回医师补写）。顺序表**两类都收**，但把承接方点出来：

* ``route_to`` —— 这条缺口的**下一棒**交给谁（信息科接口岗 / 退回医师补写 /
  两者都有 / 成因待核实）；
* ``interface_gap_prescriptions`` —— 这个字段的缺口里，**有多少张处方真的是
  「接口没回传」**（医师漏填的那部分不算）。

不点出来的后果很实在：医师漏填的字段排在表的第一行，信息科照着改了接口，
字段照样没值——接口改造排期被一条根本不该动的字段占掉了。

**口径一致性**：处方计数沿用 ``ledger/gap.py`` 的占位约定（条目未带处方号
清单时按 ``规则号#序号`` 折算），因此**顺序表与不可判定台账的处方数对得上**；
真实判定链上条目一定带处方号清单，占位分支只在手工构造的条目上出现。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .gap import GapCause, GapEntry

#: 字段补齐顺序表列（顺序固定）
PRIORITY_COLUMNS: tuple[str, ...] = (
    "priority",
    "missing_field",
    "affected_prescriptions",
    "affected_rules",
    "affected_depts",
    "interface_gap_prescriptions",
    "causes",
    "route_to",
)

#: 字段补齐顺序表文件名
PRIORITY_FILENAME = "field_gap_priority.csv"

#: 多成因分隔符（与判定台账的 missing_fields 分隔符一致）
CAUSE_SEPARATOR = "|"

#: 承接方 —— 要动接口（整列未落地）
ROUTE_INTERFACE = "信息科接口岗"
#: 承接方 —— 退回医师补写（栏位在但内容为空）
ROUTE_PHYSICIAN = "退回医师补写"
#: 承接方 —— 两头都有
ROUTE_BOTH = "信息科接口岗 + 退回医师补写"
#: 承接方 —— 成因未判定，先核实再派单
ROUTE_UNKNOWN = "成因待核实"


@dataclass(frozen=True)
class FieldPriority:
    """一个缺失字段的补齐优先级（顺序表的一行）。

    ``priority`` 是**名次**（1 = 最先补），不是分值——顺序表是排期用的，
    不是打分用的。
    """

    priority: int
    missing_field: str
    affected_prescriptions: int
    affected_rules: int
    affected_depts: int
    interface_gap_prescriptions: int
    causes: tuple[str, ...]
    route_to: str

    @property
    def needs_interface_change(self) -> bool:
        """这个字段的缺口里，是否有「接口未回传」那部分要动接口。"""
        return self.interface_gap_prescriptions > 0

    def to_record(self) -> dict[str, str]:
        """顺序表行（字符串口径）。"""
        return {
            "priority": str(self.priority),
            "missing_field": self.missing_field,
            "affected_prescriptions": str(self.affected_prescriptions),
            "affected_rules": str(self.affected_rules),
            "affected_depts": str(self.affected_depts),
            "interface_gap_prescriptions": str(self.interface_gap_prescriptions),
            "causes": CAUSE_SEPARATOR.join(self.causes),
            "route_to": self.route_to,
        }


def _identity_tokens(entry: GapEntry) -> list[str]:
    """条目的处方身份键。

    条目带处方号清单时用真号；不带时按 ``规则号#序号`` 折算——与
    ``ledger/gap.py`` 的 ``summarize_by_field()`` 同一套占位约定，
    两张表的处方数才对得上。
    """
    if entry.prescription_nos:
        return list(entry.prescription_nos)
    return [f"{entry.rule_id}#{index}" for index in range(entry.affected_prescriptions)]


def _route_to(causes: frozenset[GapCause]) -> str:
    """按成因集合定承接方。"""
    has_interface = GapCause.NOT_LANDED in causes
    has_physician = GapCause.UNFILLED in causes
    if has_interface and has_physician:
        return ROUTE_BOTH
    if has_interface:
        return ROUTE_INTERFACE
    if has_physician:
        return ROUTE_PHYSICIAN
    return ROUTE_UNKNOWN


def build_priority_table(
    entries: Iterable[GapEntry],
    *,
    interface_only: bool = False,
) -> list[FieldPriority]:
    """把字段缺口条目排成字段补齐顺序表。

    ``interface_only=True`` 时只保留**含「接口未回传」成因**的字段——
    信息科只要自己该动接口的那部分，名字一过滤就干净了。

    **排序稳定**：受影响处方数降序 → 受影响规则数降序 → 缺失字段名升序。
    三级键合起来是全序，同输入必得同输出。
    """
    prescriptions: dict[str, set[str]] = {}
    rules: dict[str, set[str]] = {}
    depts: dict[str, set[str]] = {}
    causes: dict[str, set[GapCause]] = {}
    interface_prescriptions: dict[str, set[str]] = {}

    for entry in entries:
        field = entry.missing_field
        tokens = _identity_tokens(entry)
        prescriptions.setdefault(field, set()).update(tokens)
        rules.setdefault(field, set()).add(entry.rule_id)
        depts.setdefault(field, set()).update(entry.dept_names)
        causes.setdefault(field, set()).add(entry.cause)
        if entry.cause is GapCause.NOT_LANDED:
            interface_prescriptions.setdefault(field, set()).update(tokens)

    buckets: list[tuple[str, int, int, int, tuple[str, ...], str]] = []
    for field in prescriptions:
        field_causes = frozenset(causes[field])
        if interface_only and GapCause.NOT_LANDED not in field_causes:
            continue
        buckets.append(
            (
                field,
                len(prescriptions[field]),
                len(rules[field]),
                len(depts[field]),
                tuple(sorted(cause.value for cause in field_causes)),
                _route_to(field_causes),
            )
        )

    buckets.sort(key=lambda item: (-item[1], -item[2], item[0]))

    return [
        FieldPriority(
            priority=rank,
            missing_field=field,
            affected_prescriptions=affected_prescriptions,
            affected_rules=affected_rules,
            affected_depts=affected_depts,
            interface_gap_prescriptions=len(interface_prescriptions.get(field, ())),
            causes=cause_values,
            route_to=route_to,
        )
        for rank, (
            field,
            affected_prescriptions,
            affected_rules,
            affected_depts,
            cause_values,
            route_to,
        ) in enumerate(buckets, start=1)
    ]


def select_interface_gaps(
    items: Sequence[FieldPriority],
) -> list[FieldPriority]:
    """挑出**含接口缺口**的字段（信息科那一叠）。

    过滤后保留原名次（不改写 priority），这样同一张表里两叠人各看各的，
    名字还能对上。
    """
    return [item for item in items if item.needs_interface_change]


def write_priority_table(
    items: Iterable[FieldPriority],
    out_dir: str | Path,
    *,
    filename: str = PRIORITY_FILENAME,
) -> Path:
    """把字段补齐顺序表写成 CSV，返回落盘路径。

    ``out_dir`` 不存在时自动创建；编码用 ``utf-8-sig``（Excel 直开不乱码）。
    没有缺口时也落一个只有表头的文件（下游脚本不必判存在性）。
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PRIORITY_COLUMNS))
        writer.writeheader()
        for item in items:
            writer.writerow(item.to_record())
    return target


def load_priority_table(path: str | Path) -> list[dict[str, str]]:
    """读回字段补齐顺序表（供回归与下游消费）。"""
    target = Path(path)
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(record) for record in csv.DictReader(handle)]


__all__ = [
    "CAUSE_SEPARATOR",
    "PRIORITY_COLUMNS",
    "PRIORITY_FILENAME",
    "ROUTE_BOTH",
    "ROUTE_INTERFACE",
    "ROUTE_PHYSICIAN",
    "ROUTE_UNKNOWN",
    "FieldPriority",
    "build_priority_table",
    "load_priority_table",
    "select_interface_gaps",
    "write_priority_table",
]
