"""判定证据链回放：每条结论可追到规则版本与依据。

**对应源产品能力**：源仓库 ``contrib/database/webhooks/models.py`` 的
``TableWebhookCall``（请求 / 响应副本留痕，可导出复核）与
``core/workflow_actions/handler.py`` 的 ``create_workflow_action()``
（动作实例按序创建、每条留一条记录）。

**改写**：源产品留痕的对象是「一次出站调用」，本轮改写成「一条判定结论的
证据链」——批次 → 处方号 → 规则号 → 规则版本 → 依据出处 → 归因文本 →
事件文件指纹 → 当前状态 → 写入时间。医务与质控抽查、飞检自证要的是
「这条结论凭什么这么判」的**完整链路**，而不是让人去翻日志、猜当时的规则版本。

**文件口径**

* 列固定为 :data:`REPLAY_COLUMNS`，顺序不变（下游按列名取值）。
* 编码用 ``utf-8-sig``（与判定台账一致，Excel 双击打开不乱码）。
* 文件名 ``replay_<batch_no>.csv``，落产物目录（默认 ``out/``）。
* 过滤：按**规则号**、按**结论类型**（中文结论名或英文 wire 值均可）；
  重判留痕（历史版本）留在状态库的 ``judgement_versions`` 表，回放件取
  **当前那一版**结论（判定表刷新的口径）。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..models import Conclusion, JudgementRow

#: 回放件列（顺序固定）
REPLAY_COLUMNS: tuple[str, ...] = (
    "batch_no",
    "prescription_no",
    "rule_id",
    "rule_version",
    "conclusion",
    "severity",
    "severity_source",
    "grade",
    "reason",
    "basis",
    "missing_fields",
    "event_fingerprint",
    "state",
    "written_at",
)

#: 多字段名分隔符（与判定台账一致，避免与 CSV 分隔符混淆）
MISSING_FIELDS_SEPARATOR = "|"

#: 回放件的文件名前缀
REPLAY_FILENAME_PREFIX = "replay_"


class ReplayError(RuntimeError):
    """回放件构造或导出失败。"""


@dataclass(frozen=True)
class ReplayEntry:
    """一条判定结论的证据链。

    每个字段对应链上的一环：``batch_no``（哪一批）→ ``prescription_no``
    （哪张处方）→ ``rule_id`` / ``rule_version``（哪条规则的哪一版）→
    ``conclusion`` / ``severity`` / ``severity_source`` / ``grade``
    （判了什么、多严重、严重度哪来的、属哪一类点评）→ ``reason`` / ``basis`` /
    ``missing_fields``（凭什么、缺什么）→ ``event_fingerprint``（依据哪份落地
    文件）→ ``state``（现在走到哪一步）→ ``written_at``（什么时候写的）。

    ``severity_source`` 与 ``grade`` 由**导出时提供的规则集**补出：没提供规则集
    时留空（空 = 未提供，不代表结论本身缺这两项）。
    """

    batch_no: str
    prescription_no: str
    rule_id: str
    rule_version: str
    conclusion: str
    severity: str
    severity_source: str
    grade: str
    reason: str
    basis: str
    missing_fields: tuple[str, ...]
    event_fingerprint: str
    state: str
    written_at: str

    @property
    def chain(self) -> str:
        """一行可读证据链（给人看的那一版，审查时直接念）。"""
        return (
            f"批次 {self.batch_no} · 处方 {self.prescription_no} · "
            f"规则 {self.rule_id}（版本 {self.rule_version}）· "
            f"结论 {self.conclusion} · 依据 {self.basis} · "
            f"状态 {self.state} · 写入 {self.written_at}"
        )

    def to_record(self) -> dict[str, str]:
        """回放行 → CSV 行（字符串口径）。"""
        return {
            "batch_no": self.batch_no,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "conclusion": self.conclusion,
            "severity": self.severity,
            "severity_source": self.severity_source,
            "grade": self.grade,
            "reason": self.reason,
            "basis": self.basis,
            "missing_fields": MISSING_FIELDS_SEPARATOR.join(self.missing_fields),
            "event_fingerprint": self.event_fingerprint,
            "state": self.state,
            "written_at": self.written_at,
        }


@dataclass(frozen=True)
class EvidenceIssue:
    """证据链断点：这条结论**追不下去**，或还没走完流程。

    ``kind`` 固定取值：

    * ``缺依据出处`` —— 规则行没带依据出处，结论无法自证；
    * ``缺规则版本`` —— 规则版本号为空，无法确定当时用的是哪一版规则；
    * ``缺归因文本`` —— 结论没有归因说明；
    * ``缺事件文件指纹`` —— 追不回落地文件是哪一份；
    * ``未走完流程`` —— 状态还停在「待判定」，结论尚未经药师复核。
    """

    kind: str
    detail: str
    prescription_no: str
    rule_id: str

    def __str__(self) -> str:
        scope = f"[{self.prescription_no} / {self.rule_id}]"
        return f"{scope} {self.kind}：{self.detail}"


def _safe_batch_token(batch_no: str) -> str:
    """批次号 → 文件名安全片段（只留字母数字与 ``_``-``-``）。"""
    token = "".join(
        char if (char.isalnum() or char in "_-") else "_" for char in batch_no.strip()
    )
    token = token.strip("_")
    return token or "batch"


def replay_filename(batch_no: str) -> str:
    """回放件文件名：``replay_<batch_no>.csv``。"""
    return f"{REPLAY_FILENAME_PREFIX}{_safe_batch_token(batch_no)}.csv"


def _normalize_conclusion_filter(
    conclusions: Iterable[object] | None,
) -> frozenset[str] | None:
    """结论过滤条件归一（接受中文结论名与英文 wire 值）。

    返回 ``None`` 表示不过滤；非法取值直接抛错（不静默当成"不过滤"）。
    """
    if conclusions is None:
        return None
    values = {Conclusion.from_value(item).value for item in conclusions}
    return frozenset(values) if values else None


def _normalize_rule_filter(rule_ids: Iterable[str] | None) -> frozenset[str] | None:
    """规则号过滤条件归一。"""
    if rule_ids is None:
        return None
    values = {str(item).strip() for item in rule_ids if str(item).strip()}
    return frozenset(values) if values else None


def build_replay_entries(
    rows: Iterable[JudgementRow],
    *,
    batch_no: str | None = None,
    rule_ids: Iterable[str] | None = None,
    conclusions: Iterable[object] | None = None,
    severity_sources: Mapping[str, str] | None = None,
    grades: Mapping[str, str] | None = None,
) -> list[ReplayEntry]:
    """把判定行组装成证据链回放条目。

    过滤条件（均可为空表示不过滤）：

    * ``batch_no`` —— 只回放该批次；
    * ``rule_ids`` —— 只回放这些规则号；
    * ``conclusions`` —— 只回放这些结论类型（合规 / 违规 / 不可判定）。

    补列（可选，按规则号索引）：

    * ``severity_sources`` —— 规则号 → 严重度来源（规则行 / 层次回落）；
    * ``grades`` —— 规则号 → 点评分级（不适宜处方 / 不规范处方 / 超常处方）。

    排序固定为 ``(写入时间, 处方号, 规则号, 规则版本)``——**同输入同输出**，
    同一批次反复导出得到逐字相同的回放件，科室之间对得上。
    """
    rule_filter = _normalize_rule_filter(rule_ids)
    conclusion_filter = _normalize_conclusion_filter(conclusions)

    entries: list[ReplayEntry] = []
    for row in rows:
        if batch_no is not None and row.batch_no != batch_no:
            continue
        if rule_filter is not None and row.rule_id not in rule_filter:
            continue
        if conclusion_filter is not None and row.conclusion.value not in conclusion_filter:
            continue
        entries.append(
            ReplayEntry(
                batch_no=row.batch_no,
                prescription_no=row.prescription_no,
                rule_id=row.rule_id,
                rule_version=row.rule_version,
                conclusion=row.conclusion.value,
                severity=row.severity or "",
                severity_source=(severity_sources or {}).get(row.rule_id, ""),
                grade=(grades or {}).get(row.rule_id, ""),
                reason=row.reason,
                basis=row.basis,
                missing_fields=tuple(row.missing_fields),
                event_fingerprint=row.event_fingerprint,
                state=row.state.value,
                written_at=row.created_at,
            )
        )

    entries.sort(
        key=lambda item: (
            item.written_at,
            item.prescription_no,
            item.rule_id,
            item.rule_version,
        )
    )
    return entries


def replay_from_store(
    store: object,
    batch_no: str,
    *,
    rule_ids: Iterable[str] | None = None,
    conclusions: Iterable[object] | None = None,
    severity_sources: Mapping[str, str] | None = None,
    grades: Mapping[str, str] | None = None,
) -> list[ReplayEntry]:
    """从状态库回放某批次的全部判定行。

    ``store`` 传 :class:`med_rule_fieldmap.store.Store` 实例；本模块只依赖
    它的 ``list_judgements(batch_no)`` 接口，不直接碰 SQLite。
    """
    lister = getattr(store, "list_judgements", None)
    if not callable(lister):
        raise ReplayError("回放源对象没有 list_judgements(batch_no) 接口")
    rows = lister(batch_no)
    return build_replay_entries(
        rows,
        batch_no=batch_no,
        rule_ids=rule_ids,
        conclusions=conclusions,
        severity_sources=severity_sources,
        grades=grades,
    )


def write_replay_csv(
    entries: Iterable[ReplayEntry],
    out_dir: str | Path,
    *,
    batch_no: str | None = None,
    filename: str | None = None,
) -> Path:
    """把回放条目写成 ``out/replay_<batch_no>.csv``，返回落盘路径。

    ``out_dir`` 不存在时自动创建；同名文件按批次覆盖写（同输入同输出）。
    """
    items = list(entries)
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if filename is None:
        resolved_batch = batch_no or (items[0].batch_no if items else "")
        filename = replay_filename(resolved_batch)
    target = directory / filename
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(REPLAY_COLUMNS))
        writer.writeheader()
        for entry in items:
            writer.writerow(entry.to_record())
    return target


def load_replay_csv(path: str | Path) -> list[dict[str, str]]:
    """读回回放件（供回归与下游核对）。"""
    target = Path(path)
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(record) for record in csv.DictReader(handle)]


def summarize_replay(entries: Iterable[ReplayEntry]) -> dict[str, int]:
    """回放结论分布（三态齐全，缺失计 0）。"""
    counts = {member.value: 0 for member in Conclusion}
    for entry in entries:
        counts[entry.conclusion] = counts.get(entry.conclusion, 0) + 1
    return counts


def check_evidence_chain(entries: Iterable[ReplayEntry]) -> list[EvidenceIssue]:
    """证据链完整性自检：挑出追不下去的结论。

    医务与质控抽查时最怕「结论在那儿，但说不清凭什么」——本函数把这类缺口
    逐条点名（缺依据出处 / 缺规则版本 / 缺归因文本 / 缺事件文件指纹 /
    状态还停在待判定），而不是让人逐行肉眼看。
    """
    issues: list[EvidenceIssue] = []
    for entry in entries:
        if not entry.basis.strip():
            issues.append(
                EvidenceIssue(
                    kind="缺依据出处",
                    detail="规则行未带依据出处，结论无法自证",
                    prescription_no=entry.prescription_no,
                    rule_id=entry.rule_id,
                )
            )
        if not entry.rule_version.strip():
            issues.append(
                EvidenceIssue(
                    kind="缺规则版本",
                    detail="规则版本号为空，无法确定当时用的是哪一版规则",
                    prescription_no=entry.prescription_no,
                    rule_id=entry.rule_id,
                )
            )
        if not entry.reason.strip():
            issues.append(
                EvidenceIssue(
                    kind="缺归因文本",
                    detail="结论没有归因说明",
                    prescription_no=entry.prescription_no,
                    rule_id=entry.rule_id,
                )
            )
        if not entry.event_fingerprint.strip():
            issues.append(
                EvidenceIssue(
                    kind="缺事件文件指纹",
                    detail="追不回依据哪一份落地文件",
                    prescription_no=entry.prescription_no,
                    rule_id=entry.rule_id,
                )
            )
        if entry.state == "待判定":
            issues.append(
                EvidenceIssue(
                    kind="未走完流程",
                    detail="结论状态还停在「待判定」，未经药师复核",
                    prescription_no=entry.prescription_no,
                    rule_id=entry.rule_id,
                )
            )
    return issues


def chain_health(entries: Iterable[ReplayEntry]) -> dict[str, int]:
    """证据链体检计数：总条数 / 链完整条数 / 断链条数。"""
    items = list(entries)
    # 断链条数按"有断点的条目"计（一条结论可能同时断好几环）
    issue_rows = {
        (issue.prescription_no, issue.rule_id) for issue in check_evidence_chain(items)
    }
    return {
        "总条数": len(items),
        "断链条数": len(issue_rows),
        "链完整条数": len(items) - len(issue_rows),
    }


__all__ = [
    "MISSING_FIELDS_SEPARATOR",
    "REPLAY_COLUMNS",
    "REPLAY_FILENAME_PREFIX",
    "EvidenceIssue",
    "ReplayEntry",
    "ReplayError",
    "build_replay_entries",
    "chain_health",
    "check_evidence_chain",
    "load_replay_csv",
    "replay_filename",
    "replay_from_store",
    "summarize_replay",
    "write_replay_csv",
]
