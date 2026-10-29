"""审计台账导出：把判定事件与出站调用事件合成一张可导出的审计流水。

**对应源产品能力**：源仓库 ``contrib/database/webhooks/models.py`` 的
``TableWebhookCall``（每次触发留一条可复核记录，可导出复核）与
``webhooks/handler.py`` 的 ``make_request()``（发出去的那一次单独留痕）。

**改写**：源产品的留痕只有一股水——「一次出站调用」；本轮合成**两股水**：

* **判定事件**（``audit`` 表）——「审了什么」：一条判定一行，
  带规则号 / 规则版本 / 依据出处 / 结论 / 事件文件指纹 / 回写状态 / 重试次数；
* **出站调用事件**（``outbound_calls`` 表）——「发出去什么、发了几次、成没成」：
  回写 / 撤回 / 推送 / 重放各一行，失败的调用同样在册。

合成后按「判定事件在前、出站调用事件在后」稳定排序，同输入两次导出逐字相同
——飞检现场两份审计件对得上，才算自证成立。

**行数口径**（飞检与内审现场必问）：

* **判定事件行数 == 判定行数**——一批医嘱判一次，就有一条判定事件；
* **重复投递只补审计不新增判定**——判定行数不变，判定事件行数按判定行数增加；
* **重判另起一版**——判定行数不变（判定表只留当前那一版），判定事件行数增加
  （历史结论只追加、不覆盖）；
* **出站调用事件另计**——靠 ``action`` 列与判定事件分开数，
  「审了多少条」与「发出去多少次」是两个数，不能混。

**``basis`` 列必须非空**：一条结论说不出依据出处，审计台账就等于没有台账。
导出时逐行核对，缺依据直接报错（:class:`AuditExportError`），
不导出一份看着正常、实则漏了依据的表。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import AuditAction

#: 审计台账列（顺序固定）
AUDIT_LEDGER_COLUMNS: tuple[str, ...] = (
    "audit_id",
    "action",
    "batch_no",
    "prescription_no",
    "rule_id",
    "rule_version",
    "conclusion",
    "basis",
    "event_fingerprint",
    "writeback_state",
    "retry_count",
    "channel",
    "error_code",
    "created_at",
)

#: 审计台账文件名
AUDIT_LEDGER_FILENAME = "audit_ledger.csv"


class AuditExportError(RuntimeError):
    """审计台账导出错误（缺依据出处等硬伤，必须显式报出来）。"""


@dataclass(frozen=True)
class AuditRecord:
    """审计流水的一行（判定事件与出站调用事件共用同一张表）。"""

    audit_id: str
    action: str
    batch_no: str = ""
    prescription_no: str = ""
    rule_id: str = ""
    rule_version: str = ""
    conclusion: str = ""
    basis: str = ""
    event_fingerprint: str = ""
    writeback_state: str = ""
    retry_count: int = 0
    channel: str = ""
    error_code: str = ""
    created_at: str = ""

    @property
    def is_judgement(self) -> bool:
        """是不是判定事件（出站调用事件不是）。"""
        return self.action == AuditAction.JUDGEMENT.value

    def to_record(self) -> dict[str, str]:
        """转成台账行（字符串口径）。"""
        return {
            "audit_id": self.audit_id,
            "action": self.action,
            "batch_no": self.batch_no,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "conclusion": self.conclusion,
            "basis": self.basis,
            "event_fingerprint": self.event_fingerprint,
            "writeback_state": self.writeback_state,
            "retry_count": str(self.retry_count),
            "channel": self.channel,
            "error_code": self.error_code,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class AuditCheck:
    """审计台账自检结论。"""

    judgement_events: int = 0
    outbound_events: int = 0
    expected_judgements: int = 0
    basis_missing: int = 0

    @property
    def total(self) -> int:
        return self.judgement_events + self.outbound_events

    @property
    def judgement_rows_match(self) -> bool:
        """判定事件行数是否等于判定行数。

        未给定期望判定行数时（``expected_judgements < 0``）不做判定——
        「对不上」本身不是失败（重复投递与重判都会让审计行多于判定行）。
        """
        if self.expected_judgements < 0:
            return True
        return self.judgement_events == self.expected_judgements

    @property
    def basis_ok(self) -> bool:
        """每条**挂到具体规则上**的事件是否都说得出依据出处。

        批次级的卡片推送没有单条规则依据（不挂 ``rule_id``），不计入缺依据。
        """
        return self.basis_missing == 0

    @property
    def ok(self) -> bool:
        return self.basis_ok

    def to_dict(self) -> dict[str, object]:
        return {
            "judgement_events": self.judgement_events,
            "outbound_events": self.outbound_events,
            "expected_judgements": self.expected_judgements,
            "basis_missing": self.basis_missing,
            "judgement_rows_match": self.judgement_rows_match,
            "basis_ok": self.basis_ok,
            "ok": self.ok,
        }


def _audit_rows_to_records(rows: Iterable[Any]) -> list[AuditRecord]:
    """``store.list_audit()`` 的行 → 审计流水行。"""
    records: list[AuditRecord] = []
    for row in rows:
        records.append(
            AuditRecord(
                audit_id=f"判定-{getattr(row, 'batch_no', '')}-"
                f"{getattr(row, 'prescription_no', '')}-"
                f"{getattr(row, 'rule_id', '')}-"
                f"{getattr(row, 'rule_version', '')}-"
                f"{getattr(row, 'created_at', '')}",
                action=str(getattr(row, "action", "") or AuditAction.JUDGEMENT.value),
                batch_no=str(getattr(row, "batch_no", "")),
                prescription_no=str(getattr(row, "prescription_no", "")),
                rule_id=str(getattr(row, "rule_id", "")),
                rule_version=str(getattr(row, "rule_version", "")),
                conclusion=_conclusion_text(getattr(row, "conclusion", "")),
                basis=str(getattr(row, "basis", "")),
                event_fingerprint=str(getattr(row, "event_fingerprint", "")),
                writeback_state=str(getattr(row, "writeback_state", "")),
                retry_count=int(getattr(row, "retry_count", 0) or 0),
                created_at=str(getattr(row, "created_at", "")),
            )
        )
    return records


def _outbound_rows_to_records(rows: Iterable[Any]) -> list[AuditRecord]:
    """``store.list_outbound_calls()`` 的行 → 审计流水行。"""
    records: list[AuditRecord] = []
    for row in rows:
        records.append(
            AuditRecord(
                audit_id=str(getattr(row, "call_key", "")),
                action=str(getattr(row, "action", "") or AuditAction.WRITEBACK.value),
                batch_no=str(getattr(row, "batch_no", "")),
                prescription_no=str(getattr(row, "prescription_no", "")),
                rule_id=str(getattr(row, "rule_id", "")),
                rule_version=str(getattr(row, "rule_version", "")),
                conclusion=_conclusion_text(getattr(row, "conclusion", "")),
                basis=str(getattr(row, "basis", "")),
                event_fingerprint=str(getattr(row, "event_fingerprint", "")),
                writeback_state=str(getattr(row, "status", "")),
                retry_count=int(getattr(row, "retry_count", 0) or 0),
                channel=str(getattr(row, "channel", "")),
                error_code=str(getattr(row, "error_code", "")),
                created_at=str(getattr(row, "created_at", "")),
            )
        )
    return records


def _conclusion_text(value: object) -> str:
    """结论 → 文本（枚举取中文名，字符串原样）。"""
    return str(getattr(value, "value", value) or "")


def build_audit_records(
    ledger: Any, batch_no: str | None = None
) -> list[AuditRecord]:
    """从状态库读出判定事件与出站调用事件，合成审计流水。

    排序：**判定事件在前（按写入顺序）→ 出站调用事件在后（按写入顺序）**——
    同输入两次导出逐字相同，飞检现场两份审计件对得上。
    """
    judgement_records = _audit_rows_to_records(ledger.list_audit(batch_no))
    outbound_records = _outbound_rows_to_records(
        ledger.list_outbound_calls(batch_no)
    )
    return judgement_records + outbound_records


def check_audit_invariant(
    records: Sequence[AuditRecord], expected_judgements: int | None = None
) -> AuditCheck:
    """审计台账自检：判定事件行数、依据出处是否齐、判定行数对不对得上。

    「缺依据」只数**挂到具体规则上**的事件（``rule_id`` 非空）——批次级的
    企业微信卡片推送没有单条规则依据，不算缺依据。
    """
    judgement_events = sum(1 for record in records if record.is_judgement)
    outbound_events = sum(1 for record in records if not record.is_judgement)
    basis_missing = sum(
        1
        for record in records
        if str(record.rule_id).strip() and not str(record.basis).strip()
    )
    return AuditCheck(
        judgement_events=judgement_events,
        outbound_events=outbound_events,
        expected_judgements=(
            int(expected_judgements) if expected_judgements is not None else -1
        ),
        basis_missing=basis_missing,
    )


def assert_audit_invariant(
    records: Sequence[AuditRecord], expected_judgements: int | None = None
) -> AuditCheck:
    """自检并在**缺依据出处**时显式报错。

    判定事件行数与判定行数对不上**不报错**——重复投递与重判都会让审计行
    多于判定行，那正是「只补审计不新增判定」的证据；行数对不上时由
    :attr:`AuditCheck.judgement_rows_match` 报告，不当成失败。
    """
    check = check_audit_invariant(records, expected_judgements)
    if not check.basis_ok:
        missing = [
            record.audit_id
            for record in records
            if str(record.rule_id).strip() and not str(record.basis).strip()
        ]
        raise AuditExportError(
            f"审计台账有 {check.basis_missing} 行缺依据出处（basis 为空）："
            f"{'、'.join(missing[:5])} —— 一条结论说不出依据，审计台账等于没有台账，"
            "不导出一份看着正常、实则漏了依据的表。"
        )
    return check


def write_audit_ledger(
    records: Sequence[AuditRecord],
    out_dir: str | Path,
    *,
    filename: str = AUDIT_LEDGER_FILENAME,
) -> Path:
    """把审计流水写成审计台账 CSV，返回落盘路径。

    编码用 ``utf-8-sig``：Excel 直接双击打开不乱码。
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(AUDIT_LEDGER_COLUMNS))
        writer.writeheader()
        for record in records:
            writer.writerow(record.to_record())
    return target


def load_audit_ledger(path: str | Path) -> list[dict[str, str]]:
    """读回审计台账（供回归与下游核对）。"""
    target = Path(path)
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(record) for record in csv.DictReader(handle)]


def export_audit_ledger(
    ledger: Any,
    out_dir: str | Path,
    *,
    batch_no: str | None = None,
    expected_judgements: int | None = None,
) -> tuple[Path, AuditCheck]:
    """导出审计台账，返回（落盘路径，自检结论）。

    自检发现**缺依据出处**时显式报错并**不落盘**——不导出一份看着正常、
    实则漏了依据的表。
    """
    records = build_audit_records(ledger, batch_no)
    check = assert_audit_invariant(records, expected_judgements)
    path = write_audit_ledger(records, out_dir)
    return path, check


def summarize_audit(records: Sequence[AuditRecord]) -> dict[str, int]:
    """按动作类型统计审计流水行数。"""
    counts: dict[str, int] = {}
    for record in records:
        counts[record.action] = counts.get(record.action, 0) + 1
    return counts


__all__ = [
    "AUDIT_LEDGER_COLUMNS",
    "AUDIT_LEDGER_FILENAME",
    "AuditCheck",
    "AuditExportError",
    "AuditRecord",
    "assert_audit_invariant",
    "build_audit_records",
    "check_audit_invariant",
    "export_audit_ledger",
    "load_audit_ledger",
    "summarize_audit",
    "write_audit_ledger",
]
