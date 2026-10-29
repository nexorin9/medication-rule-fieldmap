"""退避重试超次后的待重放回写队列（``pending_writeback``）。

**为什么要有这张队列**：HIS 抖动或超时不能让一条**已确认**的结论凭空消失，
更不能把它悄悄标成「已回写」——药师端会以为复核已经记录在案，而 HIS 里其实没有。
本模块给出的处置是一条固定的三段式：

1. **退避重试**（:mod:`med_rule_fieldmap.outbound.client`）——失败按指数退避重试，
   **重试次数上限 3 次**（最多发出 4 次请求：首次 + 3 次重试）；
2. **超次落队列**——重试耗尽仍失败 → 落 ``pending_writeback`` 表 + **产生告警**，
   判定行状态**不动**（仍是「药师已确认」，绝不改成「已回写」）；
3. **可重放**——人工或调度器把队列里的请求**按原样**再发一次；
   重放同样受**回写幂等键**约束：早已发出的结论不会被重复发出。

**失败不静默改数据**：入队这件事本身不修改任何业务状态，
只新增一行「待重放」记录 + 一条告警文本；判定行、审计行都不动。
只有重放真正成功（或被幂等守卫判为「早已发出」）时，才把队列记录标成「已重放」。

**源产品能力对应**（`github_ref/baserow/backend/src/baserow/core/workflow_actions/handler.py`）：

* ``WorkflowActionHandler.create_workflow_action()`` —— 动作实例**先落库**再执行，
  实例本身就是一条**持久记录**：执行失败时这条记录仍在库里，由调度器稍后重新触发，
  不会因为一次失败就凭空消失。本轮**复用改写**为「待重放回写队列」：
  源对象是工作流动作实例，本轮对象是一条已确认待回写的审方复核结论；
  源产品靠「实例落库 + 调度器按序触发」实现持久化重试，
  本轮靠「``pending_writeback`` 表 + 重放通道」实现**同一件事**，
  并且额外补上源产品没有的**回写幂等键**——重放不会让药师端出现两条复核记录。
* 失败的处理方式沿用源产品「**错误抛出来、不静默吞掉**」的纪律
  （见 `github_ref/baserow/backend/src/advocate/connection.py` 的
  ``attempt_timeout()``：deadline 过了直接抛 ``SocketTimeout``）——
  本轮把「抛出来」之后的落点显式化：**落队列 + 告警**，而不是记一条日志就完事。

**不做**什么：本模块只管「退避超次之后这条回写去哪了、怎么重放」。
请求体字段口径在 :mod:`med_rule_fieldmap.outbound.his_review`，
幂等键在 :mod:`med_rule_fieldmap.outbound.idempotency`，
退避与超时预算在 :mod:`med_rule_fieldmap.outbound.client`。
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import (
    AuditAction,
    Conclusion,
    OutboundCallRecord,
    PendingWriteback,
    PendingWritebackStatus,
    WritebackStatus,
)
from .audit import (
    CHANNEL_HIS,
    STATUS_FAILED,
    STATUS_REPLAYED,
    STATUS_SUPPRESSED,
    build_call_key,
)
from .client import OutboundError, OutboundRequestError
from .idempotency import canonical_request_body, hash_request_body, writeback_idempotency_key

logger = logging.getLogger("med_rule_fieldmap.outbound.retry_queue")

# ---------------------------------------------------------------------------
# 口径常量
# ---------------------------------------------------------------------------

#: 待重放队列的导出文件名（落在 out/ 目录，与判定台账同目录）
PENDING_WRITEBACK_FILENAME = "pending_writeback.csv"

#: 导出 CSV 的列（顺序即文件里的列序）
PENDING_WRITEBACK_COLUMNS: tuple[str, ...] = (
    "batch_no",
    "prescription_no",
    "rule_id",
    "rule_version",
    "writeback_key",
    "judgement_key",
    "request_hash",
    "retry_count",
    "last_error_code",
    "last_error",
    "status",
    "queued_at",
    "updated_at",
    "request_body",
)

#: 重放结果：已发出
REPLAY_SENT = "已重放"
#: 重放结果：被幂等守卫抑制（同一份结论早已发出，不重复发）
REPLAY_SUPPRESSED = "已抑制"
#: 重放结果：仍失败（继续留在队列里等下次重放）
REPLAY_FAILED = "仍失败"


class RetryQueueError(RuntimeError):
    """待重放队列用不了（没配回写通道、请求体无法还原等）。"""


# ---------------------------------------------------------------------------
# 重放结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayOutcome:
    """一条待重放记录的重放结果。"""

    writeback_key: str
    prescription_no: str
    rule_id: str
    status: str
    detail: str = ""
    attempts: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "writeback_key": self.writeback_key,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "status": self.status,
            "detail": self.detail,
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class ReplayReport:
    """一次重放的汇总。"""

    attempted: int = 0
    sent: int = 0
    suppressed: int = 0
    failed: int = 0
    outcomes: list[ReplayOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "sent": self.sent,
            "suppressed": self.suppressed,
            "failed": self.failed,
            "outcomes": [item.to_dict() for item in self.outcomes],
        }


# ---------------------------------------------------------------------------
# 请求体还原
# ---------------------------------------------------------------------------


def request_from_record(record: PendingWriteback) -> Any:
    """把队列里的一行还原成可重发的 :class:`ReviewRequest`。

    **按原样重发**：请求体从库里读回来直接用，不重新拼装——
    重新拼装出来的请求可能与原请求不是同一份（字段顺序、依据文本都可能变），
    幂等键也会跟着变，抑制就失效了。

    :raises RetryQueueError: 请求体不是合法 JSON 或字段不全。
    """
    from .his_review import ReviewRequest  # 局部导入：避免模块级循环依赖

    try:
        payload = json.loads(record.request_body)
    except (TypeError, ValueError) as exc:
        raise RetryQueueError(
            f"待重放记录 {record.writeback_key} 的请求体无法还原：{exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise RetryQueueError(
            f"待重放记录 {record.writeback_key} 的请求体不是 JSON 对象"
        )

    missing = [
        name
        for name in ("batch_no", "prescription_no", "rule_id", "rule_version")
        if not str(payload.get(name) or "").strip()
    ]
    if missing:
        raise RetryQueueError(
            f"待重放记录 {record.writeback_key} 的请求体缺业务键字段：{'、'.join(missing)}"
        )

    try:
        conclusion = Conclusion.from_value(payload.get("conclusion", ""))
    except ValueError as exc:
        raise RetryQueueError(
            f"待重放记录 {record.writeback_key} 的结论取值非法：{exc}"
        ) from exc

    missing_fields = payload.get("missing_fields") or ()
    if not isinstance(missing_fields, (list, tuple)):
        missing_fields = ()

    return ReviewRequest(
        batch_no=str(payload.get("batch_no") or ""),
        prescription_no=str(payload.get("prescription_no") or ""),
        rule_id=str(payload.get("rule_id") or ""),
        rule_version=str(payload.get("rule_version") or ""),
        conclusion=conclusion,
        severity=str(payload.get("severity") or ""),
        reason=str(payload.get("reason") or ""),
        basis=str(payload.get("basis") or ""),
        missing_fields=tuple(str(item) for item in missing_fields),
        reviewed_by=str(payload.get("reviewed_by") or ""),
        reviewed_at=str(payload.get("reviewed_at") or ""),
        event_fingerprint=record.event_fingerprint,
    )


def record_from_request(
    request: Any,
    error: BaseException,
    *,
    retry_count: int | None = None,
    error_code: str = "",
) -> PendingWriteback:
    """把一次失败的回写折成一条待重放记录（**尚未落库**）。

    :param request: 待回写的 :class:`ReviewRequest`。
    :param error: 失败原因（``OutboundRequestError`` 会被读出尝试次数与状态码）。
    :param retry_count: 覆盖重试次数；不给则按「发出次数 − 1」推算
        （首次请求不算重试）。
    :param error_code: 覆盖错误码；不给则从异常里推（HTTP 状态码或异常类型名）。
    """
    payload = request.to_payload()
    body = canonical_request_body(payload)
    request_hash = hash_request_body(payload)

    attempts = 0
    code = error_code
    if isinstance(error, OutboundRequestError):
        attempts = max(int(error.attempts), 0)
        if not code:
            code = f"HTTP {error.last_status}" if error.last_status else type(error).__name__
    if not code:
        code = type(error).__name__

    retries = (attempts - 1) if retry_count is None else int(retry_count)
    if retries < 0:
        retries = 0

    return PendingWriteback(
        writeback_key=writeback_idempotency_key(request.judgement_key, payload),
        judgement_key=request.judgement_key,
        batch_no=request.batch_no,
        prescription_no=request.prescription_no,
        rule_id=request.rule_id,
        rule_version=request.rule_version,
        request_body=body,
        request_hash=request_hash,
        event_fingerprint=request.event_fingerprint,
        retry_count=retries,
        last_error_code=code,
        last_error=str(error),
        status=PendingWritebackStatus.PENDING,
    )


# ---------------------------------------------------------------------------
# 待重放队列
# ---------------------------------------------------------------------------


class PendingWritebackQueue:
    """退避超次后的待重放回写队列（落 ``pending_writeback`` 表）。

    :param ledger: 状态库（需实现 ``enqueue_pending_writeback`` /
        ``list_pending_writeback`` / ``mark_pending_replayed`` /
        ``get_pending_writeback`` / ``list_writeback_requests``）。
    :param writer: 回写通道（``outbound/his_review.py`` 的
        :class:`HisReviewWriter`），重放时用；不给则只能入队、不能重放。
    :param csv_path: 导出 CSV 的路径；不给则不导出。
    :param logger_: 日志器（默认用本模块的 logger）。
    """

    def __init__(
        self,
        ledger: Any,
        *,
        writer: Any | None = None,
        csv_path: str | Path | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._ledger = ledger
        self._writer = writer
        self._csv_path = Path(csv_path) if csv_path else None
        self._logger = logger_ or logger
        self._alerts: list[str] = []

    # -- 告警 ---------------------------------------------------------------

    @property
    def alerts(self) -> list[str]:
        """本队列产生的告警文本（按发生顺序）。"""
        return list(self._alerts)

    def alert(self, reason: str) -> str:
        """记一条告警（写日志 + 留存文本，供审计导出）。"""
        self._alerts.append(reason)
        self._logger.warning("[retry_queue] %s", reason)
        return reason

    # -- 入队 ---------------------------------------------------------------

    def send(self, request: Any, *, writer: Any | None = None, budget: Any | None = None) -> Any:
        """发一次回写；**退避重试耗尽 → 落待重放队列 + 告警**，然后照原样抛错。

        这是回写路径的推荐入口：调用方不必记得在捕获异常后手工入队——
        「超次必落队列」是队列自己的纪律，漏一次就等于让一条已确认的结论
        凭空消失（这正是本模块要杜绝的事）。

        :raises OutboundRequestError: 退避重试耗尽（**已落队列**，异常照原样抛出，
            不静默吞掉）。
        """
        target = writer if writer is not None else self._writer
        if target is None:
            raise RetryQueueError("未配置回写通道，无法发出回写")
        try:
            if budget is None:
                return target.write(request)
            return target.write(request, budget=budget)
        except OutboundRequestError as exc:
            # 退避重试已耗尽 → 先落队列（持久化），再把异常抛给调用方
            self.enqueue(request, exc)
            raise

    def enqueue(
        self,
        request: Any,
        error: BaseException,
        *,
        retry_count: int | None = None,
        error_code: str = "",
    ) -> PendingWriteback:
        """把一条退避超次的回写落进队列 + **产生告警**。

        **不改判定行状态**：入队只是登记「这条回写还没发出去」，
        判定行仍是「药师已确认」——绝不改成「已回写」。
        """
        record = record_from_request(
            request, error, retry_count=retry_count, error_code=error_code
        )
        stored = self._ledger.enqueue_pending_writeback(record)
        self._record_call(
            stored,
            action=AuditAction.WRITEBACK.value,
            status=STATUS_FAILED,
            error_code=stored.last_error_code or error_code,
            detail=f"退避重试耗尽，已落待重放队列：{error}",
        )
        self.alert(
            f"回写退避重试已耗尽，已落待重放队列：批次 {stored.batch_no} · "
            f"处方 {stored.prescription_no} · 规则 {stored.rule_id} · "
            f"累计重试 {stored.retry_count} 次 · 最后错误 {stored.last_error_code}"
        )
        return stored

    def _record_call(
        self,
        record: PendingWriteback,
        *,
        action: str,
        status: str,
        error_code: str = "",
        detail: str = "",
    ) -> None:
        """把一次出站调用记进出站调用留痕（追加式）。

        状态库不支持出站留痕时**降级为告警**：留痕缺失不该让主流程失败，
        但必须留下一条痕迹，不能静默。
        """
        append = getattr(self._ledger, "append_outbound_call", None)
        if append is None:
            logger.warning(
                "状态库不支持出站调用留痕（缺 append_outbound_call）："
                "批次 %s · 处方 %s · 状态 %s —— 该次出站未留痕",
                record.batch_no,
                record.prescription_no,
                status,
            )
            return

        # 依据出处与结论从判定行补齐——出站留痕也要说得出「这条结论凭什么」。
        basis = ""
        conclusion = Conclusion.COMPLIANT
        getter = getattr(self._ledger, "get_judgement", None)
        if getter is not None and record.judgement_key:
            judgement = getter(record.judgement_key)
            if judgement is not None:
                basis = str(getattr(judgement, "basis", "") or "")
                conclusion = getattr(judgement, "conclusion", Conclusion.COMPLIANT)

        append(
            OutboundCallRecord(
                call_key=build_call_key(
                    CHANNEL_HIS, action, record.writeback_key, record.request_hash
                ),
                channel=CHANNEL_HIS,
                action=action,
                judgement_key=record.judgement_key,
                batch_no=record.batch_no,
                prescription_no=record.prescription_no,
                rule_id=record.rule_id,
                rule_version=record.rule_version,
                conclusion=conclusion,
                basis=basis,
                event_fingerprint=record.event_fingerprint,
                status=status,
                retry_count=record.retry_count,
                error_code=error_code,
                detail=detail,
            )
        )

    # -- 查询 ---------------------------------------------------------------

    def pending(self) -> list[PendingWriteback]:
        """仍待重放的记录（按入队时间排序）。"""
        return self._ledger.list_pending_writeback(PendingWritebackStatus.PENDING.value)

    def records(self) -> list[PendingWriteback]:
        """全部记录（含已重放的，供审计导出）。"""
        return self._ledger.list_pending_writeback()

    def count(self) -> int:
        """待重放条数。"""
        return self._ledger.count_pending_writeback(PendingWritebackStatus.PENDING.value)

    # -- 重放 ---------------------------------------------------------------

    def replay(self, *, writer: Any | None = None, limit: int | None = None) -> ReplayReport:
        """把队列里的请求按原样重发一遍。

        **重放同样受幂等键约束**：已经发出过的结论（``writeback_requests`` 里状态为
        「已回写」）在这里被**直接抑制**——一个字节都不发，队列记录标成「已重放」。
        因此「重放一遍」不会让药师端出现两条复核记录。

        :param writer: 回写通道；不给则用构造时传入的那个。
        :param limit: 最多重放几条（不给则全部）。
        :raises RetryQueueError: 没有可用的回写通道。
        """
        target = writer if writer is not None else self._writer
        if target is None:
            raise RetryQueueError("未配置回写通道，无法重放待重放队列")

        records = self.pending()
        if limit is not None and limit >= 0:
            records = records[:limit]

        already_sent = self._sent_keys()
        outcomes: list[ReplayOutcome] = []
        sent = suppressed = failed = 0

        for record in records:
            # -- 幂等：早已发出的结论不再发一遍 --------------------------------
            if record.writeback_key in already_sent:
                self._ledger.mark_pending_replayed(record.writeback_key)
                suppressed += 1
                outcomes.append(
                    ReplayOutcome(
                        writeback_key=record.writeback_key,
                        prescription_no=record.prescription_no,
                        rule_id=record.rule_id,
                        status=REPLAY_SUPPRESSED,
                        detail="同一结论早已回写成功，幂等键抑制，本次未发出任何请求",
                    )
                )
                continue

            # -- 还原请求体 ----------------------------------------------------
            try:
                request = request_from_record(record)
            except RetryQueueError as exc:
                failed += 1
                outcomes.append(
                    ReplayOutcome(
                        writeback_key=record.writeback_key,
                        prescription_no=record.prescription_no,
                        rule_id=record.rule_id,
                        status=REPLAY_FAILED,
                        detail=str(exc),
                    )
                )
                self.alert(f"待重放记录无法重放：{exc}")
                continue

            # -- 重发 ------------------------------------------------------------
            try:
                outcome = target.write(request)
            except OutboundRequestError as exc:
                # 退避再次耗尽 → 继续留在队列里（累加重试次数），不标已重放
                self.enqueue(request, exc)
                failed += 1
                outcomes.append(
                    ReplayOutcome(
                        writeback_key=record.writeback_key,
                        prescription_no=record.prescription_no,
                        rule_id=record.rule_id,
                        status=REPLAY_FAILED,
                        detail=f"重放仍失败：{exc}",
                        attempts=int(getattr(exc, "attempts", 0)),
                    )
                )
                continue
            except OutboundError as exc:
                # 其余出站失败（绑定歧义 / 凭据缺失 / 业务失败）：留在队列里等人工
                self.enqueue(request, exc, error_code=type(exc).__name__)
                failed += 1
                outcomes.append(
                    ReplayOutcome(
                        writeback_key=record.writeback_key,
                        prescription_no=record.prescription_no,
                        rule_id=record.rule_id,
                        status=REPLAY_FAILED,
                        detail=f"重放失败：{exc}",
                    )
                )
                continue

            if getattr(outcome, "sent", False):
                self._ledger.mark_pending_replayed(record.writeback_key)
                already_sent.add(record.writeback_key)
                sent += 1
                self._record_call(
                    record,
                    action=AuditAction.REPLAY.value,
                    status=STATUS_REPLAYED,
                    detail="重放已发出并被 HIS 认领",
                )
                outcomes.append(
                    ReplayOutcome(
                        writeback_key=record.writeback_key,
                        prescription_no=record.prescription_no,
                        rule_id=record.rule_id,
                        status=REPLAY_SENT,
                        detail="重放已发出并被 HIS 认领",
                        attempts=int(getattr(outcome, "attempts", 0)),
                    )
                )
                continue

            if getattr(outcome, "suppressed", False):
                # 幂等守卫判为「同一份早已发出」→ 不重复发，视为已重放
                self._ledger.mark_pending_replayed(record.writeback_key)
                already_sent.add(record.writeback_key)
                suppressed += 1
                self._record_call(
                    record,
                    action=AuditAction.REPLAY.value,
                    status=STATUS_SUPPRESSED,
                    detail="幂等守卫抑制，重放未发出任何请求",
                )
                outcomes.append(
                    ReplayOutcome(
                        writeback_key=record.writeback_key,
                        prescription_no=record.prescription_no,
                        rule_id=record.rule_id,
                        status=REPLAY_SUPPRESSED,
                        detail=str(getattr(outcome, "reason", "") or "幂等守卫抑制，未发出请求"),
                    )
                )
                continue

            failed += 1
            outcomes.append(
                ReplayOutcome(
                    writeback_key=record.writeback_key,
                    prescription_no=record.prescription_no,
                    rule_id=record.rule_id,
                    status=REPLAY_FAILED,
                    detail=str(getattr(outcome, "reason", "") or "重放未成功"),
                )
            )

        return ReplayReport(
            attempted=len(records),
            sent=sent,
            suppressed=suppressed,
            failed=failed,
            outcomes=outcomes,
        )

    # -- 导出 ---------------------------------------------------------------

    def export_csv(self, path: str | Path | None = None) -> Path:
        """把队列导出成 CSV（运维与质控可读）。

        默认写「全部记录」（含已重放的），便于回看「哪些回写过、哪些一直没发出去」。
        """
        target = Path(path) if path is not None else self._csv_path
        if target is None:
            raise RetryQueueError("未指定导出路径，无法导出待重放队列")

        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(PENDING_WRITEBACK_COLUMNS))
            writer.writeheader()
            for record in self.records():
                row = record.to_dict()
                writer.writerow({name: row.get(name, "") for name in PENDING_WRITEBACK_COLUMNS})
        return target

    # -- 内部 ---------------------------------------------------------------

    def _sent_keys(self) -> set[str]:
        """已经回写成功的回写幂等键集合（重放前的幂等闸门）。"""
        getter = getattr(self._ledger, "list_writeback_requests", None)
        if getter is None:
            return set()
        try:
            attempts = getter()
        except Exception:  # pragma: no cover - 状态库异常不应让重放整体崩掉
            return set()
        return {
            attempt.writeback_key
            for attempt in attempts
            if attempt.status is WritebackStatus.SENT
        }


def summarize_queue(records: Sequence[PendingWriteback]) -> dict[str, int]:
    """队列概览（按状态计数 + 累计重试次数）。"""
    counts = {status.value: 0 for status in PendingWritebackStatus}
    retries = 0
    for record in records:
        counts[record.status.value] = counts.get(record.status.value, 0) + 1
        retries += int(record.retry_count)
    counts["累计重试次数"] = retries
    return counts


__all__ = [
    "PENDING_WRITEBACK_COLUMNS",
    "PENDING_WRITEBACK_FILENAME",
    "REPLAY_FAILED",
    "REPLAY_SENT",
    "REPLAY_SUPPRESSED",
    "PendingWritebackQueue",
    "ReplayOutcome",
    "ReplayReport",
    "RetryQueueError",
    "record_from_request",
    "request_from_record",
    "summarize_queue",
]
