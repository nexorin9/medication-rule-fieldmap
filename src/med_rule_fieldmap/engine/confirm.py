"""药师确认 + 撤回 + **回写触发门禁**。

**为什么要有这道门禁**：《处方审核规则体系》§4 写得很直白——「药师是处方审核
第一责任人；信息系统可辅助，但系统筛选出的不合理处方及系统不能审核的部分
必须人工审核」。本产品是审方辅助系统，它能筛、能把依据摊开，但**它没有权力
替药师签字**。因此本模块把「人工确认」做成回写的**硬门禁**：

* 只有状态为 **药师已确认** 的判定行才允许进入任何回写请求体；
* 「已判定」「待补字段」两条状态**永远不会**出现在回写请求里；
* 药师可以**撤回**：还没发出去的排队回写标成「已撤回」不再发；
  已经发出去了的，必须同步一条撤回通知到 HIS，并留下撤回痕迹。

**一条硬红线**：本模块**不生成、不改写**审方结论。它只推进**状态**、
留**痕迹**、控制**出站**。结论归判定内核，签字归药师。

**源产品能力对应**（`github_ref/baserow/backend/src/baserow/core/workflow_actions/handler.py`）：

* ``WorkflowActionHandler.create_workflow_action()`` —— 动作实例**先落库、再执行**，
  实例本身就是一条持久记录：执行失败时记录仍在库里，由调度器稍后重新触发。
  本轮**复用改写**为「药师确认先落库（确认记录，追加式）、再允许出站回写」：
  源对象是工作流动作实例，本轮对象是一条审方复核结论。
* 源产品的动作实例一旦被触发就执行，**没有人工复核关口**。本轮**新增**的正是
  这一关：确认门禁 + 可撤回 + 撤回留痕——这是本产品与源产品最本质的差别，
  也是院内落地的关键（「系统不代签」）。

**状态与留痕的关系**：判定行状态回答「这条结论走到哪一步、能不能回写」；
确认留痕回答「谁在什么时候确认的、后来为什么撤回」。两者分开存，
「谁签的字」永远查得到、改不掉（见 :class:`med_rule_fieldmap.models.ConfirmationRecord`）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..models import (
    Conclusion,
    ConfirmationRecord,
    JudgementRow,
    JudgementState,
    now_iso,
)
from ..outbound.his_review import ReviewRequest, WithdrawRequest
from ..outbound.idempotency import hash_request_body
from .statemachine import JudgementStateMachine

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注；运行期导入会与 store 形成循环
    from ..store import Store

logger = logging.getLogger("med_rule_fieldmap.engine.confirm")

# ---------------------------------------------------------------------------
# 口径常量
# ---------------------------------------------------------------------------

#: 确认动作
ACTION_CONFIRM = "确认"
#: 撤回动作
ACTION_WITHDRAW = "撤回"

#: 确认后的回写状态：已确认、尚未回写
WRITEBACK_STATE_CONFIRMED = "已确认未回写"
#: 撤回后的回写状态：已撤回并同步到 HIS
WRITEBACK_STATE_WITHDRAWN = "已撤回"
#: 撤回后的回写状态：本地已撤回，但同步到 HIS 失败（待重放）
WRITEBACK_STATE_WITHDRAW_PENDING = "撤回待同步"

#: 可以发起确认的状态（其余状态由 :func:`_check_confirm_state` 明确拒绝）
CONFIRMABLE_STATES: frozenset[JudgementState] = frozenset({JudgementState.DECIDED})

#: 可以发起撤回的状态
WITHDRAWABLE_STATES: frozenset[JudgementState] = frozenset(
    {
        JudgementState.DECIDED,
        JudgementState.CONFIRMED,
        JudgementState.WRITTEN_BACK,
    }
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ConfirmError(RuntimeError):
    """确认 / 撤回路径的基类异常。"""


class PharmacistIdentityError(ConfirmError):
    """缺药师标识——系统不代签、不代撤。"""


class ConfirmationStateError(ConfirmError):
    """当前状态不允许确认 / 撤回。

    携带 :attr:`state` 与 :attr:`allowed`，让调用方一眼看出「本来该走哪一步」。
    """

    def __init__(
        self,
        message: str,
        *,
        state: JudgementState,
        allowed: Sequence[JudgementState] = (),
    ) -> None:
        super().__init__(message)
        self.state = state
        self.allowed = tuple(allowed)


class WritebackGateError(ConfirmError):
    """回写门禁不通过——请求体根本不该被构造出来。"""


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfirmOutcome:
    """一次确认或撤回的结果。"""

    judgement: JudgementRow
    record: ConfirmationRecord
    state_before: JudgementState
    state_after: JudgementState
    #: 本次是否触发了出站回写
    writeback_triggered: bool = False
    #: 回写是否真的发出并被 HIS 认领
    writeback_sent: bool = False
    #: 撤回是否已同步到 HIS
    withdrawn_notified: bool = False
    #: 因撤回而被标成「已撤回」的排队回写条数
    pending_withdrawn: int = 0
    #: 过程中的告警文本（按发生顺序）
    alerts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_no": self.judgement.batch_no,
            "prescription_no": self.judgement.prescription_no,
            "rule_id": self.judgement.rule_id,
            "rule_version": self.judgement.rule_version,
            "action": self.record.action,
            "pharmacist_id": self.record.pharmacist_id,
            "state_before": self.state_before.value,
            "state_after": self.state_after.value,
            "writeback_triggered": self.writeback_triggered,
            "writeback_sent": self.writeback_sent,
            "withdrawn_notified": self.withdrawn_notified,
            "pending_withdrawn": self.pending_withdrawn,
            "writeback_state": self.record.writeback_state,
            "alerts": list(self.alerts),
        }


@dataclass(frozen=True)
class WritebackCandidate:
    """一条**已确认、待回写**的判定行（含确认留痕）。"""

    judgement: JudgementRow
    confirmation: ConfirmationRecord

    @property
    def idempotency_key(self) -> str:
        return self.judgement.idempotency_key


@dataclass(frozen=True)
class WritebackOutcome:
    """一条候选回写的处理结果。"""

    idempotency_key: str
    prescription_no: str
    rule_id: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "idempotency_key": self.idempotency_key,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class WritebackReport:
    """一次回写触发的汇总。"""

    attempted: int = 0
    sent: int = 0
    suppressed: int = 0
    failed: int = 0
    skipped: int = 0
    outcomes: list[WritebackOutcome] = field(default_factory=list)
    alerts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "sent": self.sent,
            "suppressed": self.suppressed,
            "failed": self.failed,
            "skipped": self.skipped,
            "outcomes": [item.to_dict() for item in self.outcomes],
            "alerts": list(self.alerts),
        }


#: 回写结果：已发出并被 HIS 认领
WRITEBACK_SENT = "已回写"
#: 回写结果：被幂等守卫抑制（同一份早已发出）
WRITEBACK_SUPPRESSED = "已抑制"
#: 回写结果：仍失败（已落待重放队列）
WRITEBACK_FAILED = "仍失败"
#: 回写结果：跳过（缺确认留痕 / 状态不是「药师已确认」）
WRITEBACK_SKIPPED = "已跳过"


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class ConfirmService:
    """药师确认 / 撤回 + 回写触发门禁。

    :param store: 状态库（判定行、确认留痕、待重放队列都从这里读写）。
    :param machine: 判定行状态机；不给则按状态库自建一个。
    :param writer: 出站回写通道（``outbound/his_review.py`` 的
        :class:`~med_rule_fieldmap.outbound.his_review.HisReviewWriter`）；
        不给则本服务只能确认 / 撤回，**不能**触发回写。
    :param queue: 待重放队列（``outbound/retry_queue.py``）；给了则回写失败时
        由它落队列 + 告警。
    :param clock: 时间源（``() -> str``）；不给则用 :func:`models.now_iso`。
    """

    def __init__(
        self,
        store: "Store",
        *,
        machine: JudgementStateMachine | None = None,
        writer: Any | None = None,
        queue: Any | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.machine = machine if machine is not None else JudgementStateMachine(store)
        self._writer = writer
        self._queue = queue
        self._clock = clock or now_iso
        self._alerts: list[str] = []

    # -- 属性 ---------------------------------------------------------------

    @property
    def writer(self) -> Any:
        """出站回写通道（可能为 ``None``）。"""
        return self._writer

    @property
    def queue(self) -> Any:
        """待重放队列（可能为 ``None``）。"""
        return self._queue

    @property
    def alerts(self) -> list[str]:
        """本服务累计产生的告警文本（按发生顺序）。"""
        return list(self._alerts)

    # -- 查询 ---------------------------------------------------------------

    def resolve(self, key: str) -> JudgementRow:
        """按**判定幂等键**或**业务键**定位一条判定行。

        台账上药师看到的是业务键（``batch_no｜prescription_no｜rule_id｜rule_version``），
        运维脚本里常用的是幂等键（sha256）——两种都收，不必让调用方自己判断。
        """
        text = (key or "").strip()
        if not text:
            raise ConfirmError("判定标识为空：既没给判定幂等键，也没给业务键")
        row = self.store.get_judgement(text)
        if row is not None:
            return row
        row = self.store.find_judgement_by_business_key(text)
        if row is not None:
            return row
        raise ConfirmError(
            f"找不到判定行（判定标识 {text!r}）：既不匹配判定幂等键，也不匹配业务键"
        )

    def candidates(self, batch_no: str | None = None) -> list[WritebackCandidate]:
        """当前**可回写**的候选：状态为「药师已确认」且有确认留痕的判定行。

        这是回写门禁的核心：**未确认的结论不在这个列表里**，
        因此永远不可能出现在任何回写请求体中。
        """
        found: list[WritebackCandidate] = []
        for row in self.store.list_judgements(batch_no):
            if row.state is not JudgementState.CONFIRMED:
                continue
            confirmation = self.store.latest_confirmation(row.idempotency_key)
            if confirmation is None or confirmation.action != ACTION_CONFIRM:
                # 状态说是「已确认」但没有确认留痕 —— 不猜、不补签，跳过并告警
                self._alert(
                    f"判定行 {row.prescription_no} · {row.rule_id} 状态为「药师已确认」"
                    "但查不到确认留痕，本轮不回写（系统不代签）。"
                )
                continue
            found.append(WritebackCandidate(judgement=row, confirmation=confirmation))
        return found

    def confirmations(self, idempotency_key: str | None = None) -> list[ConfirmationRecord]:
        """确认 / 撤回留痕（可按判定幂等键过滤）。"""
        return self.store.list_confirmations(idempotency_key)

    # -- 确认 ---------------------------------------------------------------

    def confirm(
        self,
        key: str,
        *,
        pharmacist_id: str,
        note: str = "",
    ) -> ConfirmOutcome:
        """药师确认一条判定结论（**回写的前置条件**）。

        :raises PharmacistIdentityError: 没给药师标识。
        :raises ConfirmationStateError: 当前状态不允许确认（已确认 / 待补字段 / 已回写…）。
        """
        who = (pharmacist_id or "").strip()
        if not who:
            raise PharmacistIdentityError(
                "必须给出药师标识（reviewed_by）：审方复核的第一责任人是药师，系统不代签。"
            )

        row = self.resolve(key)
        state_before = row.state
        _check_confirm_state(row)

        updated = self.machine.advance_persisted(
            row, JudgementState.CONFIRMED, note=note or "药师已确认"
        )

        record = ConfirmationRecord(
            idempotency_key=row.idempotency_key,
            business_key=row.business_key,
            batch_no=row.batch_no,
            prescription_no=row.prescription_no,
            rule_id=row.rule_id,
            rule_version=row.rule_version,
            action=ACTION_CONFIRM,
            pharmacist_id=who,
            acted_at=self._clock(),
            note=note,
            writeback_state=WRITEBACK_STATE_CONFIRMED,
        )
        self.store.append_confirmation(record)

        return ConfirmOutcome(
            judgement=updated,
            record=record,
            state_before=state_before,
            state_after=updated.state,
        )

    # -- 撤回 ---------------------------------------------------------------

    def withdraw(
        self,
        key: str,
        *,
        pharmacist_id: str,
        note: str = "",
    ) -> ConfirmOutcome:
        """药师撤回一条判定结论。

        三种落点：

        #. **还没回写** → 排队中的回写标成「已撤回」，不再发出，一个字节都不发；
        #. **已经回写** → 同步一条撤回通知到 HIS，并留撤回痕迹；
        #. **同步失败** → 本地已撤回、HIS 端未同步，状态记「撤回待同步」+ 告警，
           并把撤回通知落进待重放队列（由 ``outbound/retry_queue.py`` 重放）。
        """
        who = (pharmacist_id or "").strip()
        if not who:
            raise PharmacistIdentityError(
                "必须给出药师标识（withdrawn_by）：撤回同样要留人，系统不代撤。"
            )

        row = self.resolve(key)
        state_before = row.state
        _check_withdraw_state(row)
        was_written_back = state_before is JudgementState.WRITTEN_BACK

        updated = self.machine.advance_persisted(
            row, JudgementState.WITHDRAWN, note=note or "药师撤回"
        )

        # 排队中的回写一律作废（不管之前发没发出去）
        pending_withdrawn = self.store.withdraw_pending_writebacks(row.idempotency_key)

        alerts: list[str] = []
        notified = False
        writeback_state = WRITEBACK_STATE_WITHDRAWN

        if was_written_back:
            notified, writeback_state, extra = self._notify_withdraw(
                row,
                pharmacist_id=who,
                note=note,
            )
            alerts.extend(extra)

        record = ConfirmationRecord(
            idempotency_key=row.idempotency_key,
            business_key=row.business_key,
            batch_no=row.batch_no,
            prescription_no=row.prescription_no,
            rule_id=row.rule_id,
            rule_version=row.rule_version,
            action=ACTION_WITHDRAW,
            pharmacist_id=who,
            acted_at=self._clock(),
            note=note,
            writeback_state=writeback_state,
        )
        self.store.append_confirmation(record)

        return ConfirmOutcome(
            judgement=updated,
            record=record,
            state_before=state_before,
            state_after=updated.state,
            withdrawn_notified=notified,
            pending_withdrawn=pending_withdrawn,
            alerts=tuple(alerts),
        )

    # -- 回写门禁 -----------------------------------------------------------

    def build_review_request(self, candidate: WritebackCandidate) -> ReviewRequest:
        """把一条候选折成回写请求体。

        **门禁在这一层再挡一次**：状态不是「药师已确认」就直接抛
        :class:`WritebackGateError`——即使调用方绕过 :meth:`candidates`
        自己拼了一条候选，也构造不出请求体。
        """
        row = candidate.judgement
        if row.state is not JudgementState.CONFIRMED:
            raise WritebackGateError(
                f"回写门禁拒绝：判定 {row.prescription_no} · {row.rule_id} 当前状态为"
                f"「{row.state.value}」，只有「药师已确认」才允许回写 HIS。"
            )
        confirmation = candidate.confirmation
        if not confirmation.pharmacist_id:
            raise WritebackGateError(
                f"回写门禁拒绝：判定 {row.prescription_no} · {row.rule_id} 的确认留痕"
                "里没有药师标识，系统不代签。"
            )
        return ReviewRequest(
            batch_no=row.batch_no,
            prescription_no=row.prescription_no,
            rule_id=row.rule_id,
            rule_version=row.rule_version,
            conclusion=row.conclusion,
            severity=row.severity or "",
            reason=row.reason,
            basis=row.basis,
            missing_fields=row.missing_fields,
            reviewed_by=confirmation.pharmacist_id,
            reviewed_at=confirmation.acted_at,
            event_fingerprint=row.event_fingerprint,
        )

    def trigger_writeback(
        self,
        *,
        batch_no: str | None = None,
        limit: int | None = None,
    ) -> WritebackReport:
        """触发回写：把**已确认**的结论发到 HIS。

        * 只有「药师已确认」的判定行会进请求体；
        * 同一条结论只发一次（幂等键 + 状态跃迁双重把关）；
        * 失败不静默改数据：退避重试耗尽 → 落待重放队列 + 告警，
          判定行**停在「药师已确认」**，绝不改成「已回写」。
        """
        if self._writer is None:
            raise WritebackGateError(
                "未配置回写通道（HIS 凭据 / 出站客户端）：回写是本产品唯一改变业务"
                "状态的路径，缺凭据时必须显式失败，不静默跳过。"
            )

        candidates = self.candidates(batch_no)
        if limit is not None and limit >= 0:
            candidates = candidates[:limit]

        outcomes: list[WritebackOutcome] = []
        alerts: list[str] = []
        sent = suppressed = failed = skipped = 0

        for candidate in candidates:
            row = candidate.judgement
            try:
                request = self.build_review_request(candidate)
            except WritebackGateError as exc:
                skipped += 1
                alerts.append(str(exc))
                outcomes.append(
                    WritebackOutcome(
                        idempotency_key=row.idempotency_key,
                        prescription_no=row.prescription_no,
                        rule_id=row.rule_id,
                        status=WRITEBACK_SKIPPED,
                        detail=str(exc),
                    )
                )
                continue

            try:
                if self._queue is not None:
                    outcome = self._queue.send(request)
                else:
                    outcome = self._writer.write(request)
            except Exception as exc:  # 出站失败：留痕 + 告警，不静默改数据
                failed += 1
                message = (
                    f"回写失败（未改判定状态，仍为「药师已确认」）：处方 "
                    f"{row.prescription_no} · 规则 {row.rule_id} —— {exc}"
                )
                alerts.append(message)
                outcomes.append(
                    WritebackOutcome(
                        idempotency_key=row.idempotency_key,
                        prescription_no=row.prescription_no,
                        rule_id=row.rule_id,
                        status=WRITEBACK_FAILED,
                        detail=str(exc),
                    )
                )
                continue

            if getattr(outcome, "sent", False):
                sent += 1
                self.machine.advance_persisted(
                    row, JudgementState.WRITTEN_BACK, note="回写已发出并被 HIS 认领"
                )
                outcomes.append(
                    WritebackOutcome(
                        idempotency_key=row.idempotency_key,
                        prescription_no=row.prescription_no,
                        rule_id=row.rule_id,
                        status=WRITEBACK_SENT,
                        detail=str(getattr(outcome, "reason", "") or "已回写"),
                    )
                )
                continue

            if getattr(outcome, "suppressed", False):
                suppressed += 1
                outcomes.append(
                    WritebackOutcome(
                        idempotency_key=row.idempotency_key,
                        prescription_no=row.prescription_no,
                        rule_id=row.rule_id,
                        status=WRITEBACK_SUPPRESSED,
                        detail=str(getattr(outcome, "reason", "") or "幂等守卫抑制，未发出请求"),
                    )
                )
                continue

            failed += 1
            outcomes.append(
                WritebackOutcome(
                    idempotency_key=row.idempotency_key,
                    prescription_no=row.prescription_no,
                    rule_id=row.rule_id,
                    status=WRITEBACK_FAILED,
                    detail=str(getattr(outcome, "reason", "") or "回写未成功"),
                )
            )

        for message in alerts:
            self._alert(message)

        return WritebackReport(
            attempted=len(candidates),
            sent=sent,
            suppressed=suppressed,
            failed=failed,
            skipped=skipped,
            outcomes=outcomes,
            alerts=tuple(alerts),
        )

    # -- 内部 ---------------------------------------------------------------

    def resync_withdrawals(self) -> WritebackReport:
        """重投**未同步成功**的撤回通知。

        撤回通知走的是**另一个地址、另一种请求体**，因此不进复核用的待重放队列
        （队列里的记录按复核请求体还原，重放时会打到复核接口上去）。
        「撤回待同步」的那些留痕由本方法重新发一次——发成功后追加一条
        「已撤回」留痕（追加式，历史不改写）。

        返回 :class:`WritebackReport`（复用同一套结果结构，便于 CLI 统一打印）。
        """
        if self._writer is None:
            raise WritebackGateError(
                "未配置回写通道（HIS 凭据 / 出站客户端）：撤回同步必须显式失败，"
                "不静默跳过。"
            )

        outcomes: list[WritebackOutcome] = []
        alerts: list[str] = []
        sent = failed = 0

        for record in self.store.list_confirmations():
            if record.action != ACTION_WITHDRAW:
                continue
            if record.writeback_state != WRITEBACK_STATE_WITHDRAW_PENDING:
                continue
            row = self.store.get_judgement(record.idempotency_key)
            if row is None:
                failed += 1
                outcomes.append(
                    WritebackOutcome(
                        idempotency_key=record.idempotency_key,
                        prescription_no=record.prescription_no,
                        rule_id=record.rule_id,
                        status=WRITEBACK_FAILED,
                        detail="判定行已不在库里，无法重投撤回",
                    )
                )
                continue

            request = WithdrawRequest(
                batch_no=row.batch_no,
                prescription_no=row.prescription_no,
                rule_id=row.rule_id,
                rule_version=row.rule_version,
                conclusion=row.conclusion,
                basis=row.basis,
                reviewed_by=record.pharmacist_id,
                withdrawn_by=record.pharmacist_id,
                withdrawn_at=self._clock(),
                reason=record.note or "药师撤回确认（重投）",
                event_fingerprint=row.event_fingerprint,
            )
            try:
                outcome = self._writer.withdraw(request)
            except Exception as exc:
                failed += 1
                message = (
                    f"撤回重投仍失败：处方 {row.prescription_no} · 规则 {row.rule_id} —— {exc}"
                )
                alerts.append(message)
                outcomes.append(
                    WritebackOutcome(
                        idempotency_key=row.idempotency_key,
                        prescription_no=row.prescription_no,
                        rule_id=row.rule_id,
                        status=WRITEBACK_FAILED,
                        detail=str(exc),
                    )
                )
                continue

            if getattr(outcome, "sent", False):
                sent += 1
                self.store.append_confirmation(
                    ConfirmationRecord(
                        idempotency_key=row.idempotency_key,
                        business_key=row.business_key,
                        batch_no=row.batch_no,
                        prescription_no=row.prescription_no,
                        rule_id=row.rule_id,
                        rule_version=row.rule_version,
                        action=ACTION_WITHDRAW,
                        pharmacist_id=record.pharmacist_id,
                        acted_at=self._clock(),
                        note=record.note,
                        writeback_state=WRITEBACK_STATE_WITHDRAWN,
                    )
                )
                outcomes.append(
                    WritebackOutcome(
                        idempotency_key=row.idempotency_key,
                        prescription_no=row.prescription_no,
                        rule_id=row.rule_id,
                        status=WRITEBACK_SENT,
                        detail="撤回已同步到 HIS",
                    )
                )
                continue

            failed += 1
            outcomes.append(
                WritebackOutcome(
                    idempotency_key=row.idempotency_key,
                    prescription_no=row.prescription_no,
                    rule_id=row.rule_id,
                    status=WRITEBACK_FAILED,
                    detail=str(getattr(outcome, "reason", "") or "撤回重投未成功"),
                )
            )

        for message in alerts:
            self._alert(message)

        return WritebackReport(
            attempted=len(outcomes),
            sent=sent,
            failed=failed,
            outcomes=outcomes,
            alerts=tuple(alerts),
        )

    def _notify_withdraw(
        self,
        row: JudgementRow,
        *,
        pharmacist_id: str,
        note: str,
    ) -> tuple[bool, str, list[str]]:
        """把撤回同步到 HIS。返回 ``(是否同步成功, 回写状态, 告警列表)``。"""
        alerts: list[str] = []
        if self._writer is None:
            message = (
                f"撤回未同步到 HIS（未配置回写通道）：处方 {row.prescription_no} · "
                f"规则 {row.rule_id} —— 该条复核记录在 HIS 端仍然存在，请人工核对。"
            )
            alerts.append(message)
            self._alert(message)
            return False, WRITEBACK_STATE_WITHDRAW_PENDING, alerts

        confirmation = self.store.latest_confirmation(row.idempotency_key)
        request = WithdrawRequest(
            batch_no=row.batch_no,
            prescription_no=row.prescription_no,
            rule_id=row.rule_id,
            rule_version=row.rule_version,
            conclusion=row.conclusion,
            basis=row.basis,
            reviewed_by=confirmation.pharmacist_id if confirmation is not None else "",
            withdrawn_by=pharmacist_id,
            withdrawn_at=self._clock(),
            reason=note or "药师撤回确认",
            event_fingerprint=row.event_fingerprint,
        )

        try:
            outcome = self._writer.withdraw(request)
        except Exception as exc:
            message = (
                f"撤回未同步到 HIS（本地已撤回，待人工重投）：处方 {row.prescription_no} · "
                f"规则 {row.rule_id} —— {exc}"
            )
            alerts.append(message)
            self._alert(message)
            return False, WRITEBACK_STATE_WITHDRAW_PENDING, alerts

        if getattr(outcome, "sent", False):
            return True, WRITEBACK_STATE_WITHDRAWN, alerts

        message = (
            f"撤回未同步到 HIS：处方 {row.prescription_no} · 规则 {row.rule_id} —— "
            f"{getattr(outcome, 'reason', '') or '撤回未成功'}"
        )
        alerts.append(message)
        self._alert(message)
        return False, WRITEBACK_STATE_WITHDRAW_PENDING, alerts

    def _alert(self, reason: str) -> str:
        """记一条告警（写日志 + 留存文本）。"""
        self._alerts.append(reason)
        logger.warning("[confirm] %s", reason)
        return reason


# ---------------------------------------------------------------------------
# 状态校验
# ---------------------------------------------------------------------------


def _state_hint(state: JudgementState) -> str:
    """给现场看的下一句建议（人能直接照着做）。"""
    if state is JudgementState.PENDING_FIELDS:
        return "请先补齐缺失字段并重判，再由药师确认。"
    if state is JudgementState.PENDING:
        return "该判定尚未出结论，请先完成判定。"
    if state is JudgementState.CONFIRMED:
        return "该判定已确认，无需重复确认。"
    if state is JudgementState.WRITTEN_BACK:
        return "该判定已回写 HIS，如需更正请走撤回。"
    if state is JudgementState.WITHDRAWN:
        return "该判定已撤回，须重新进入判定流程。"
    if state is JudgementState.SUSPENDED:
        return "该判定已挂起，请先人工解除挂起后重投。"
    return "请检查该判定行当前状态。"


def _check_confirm_state(row: JudgementRow) -> None:
    """确认前的状态校验（不合法 → 显式抛错，行不动）。"""
    if row.state in CONFIRMABLE_STATES:
        return
    missing = "、".join(row.missing_fields) if row.missing_fields else ""
    detail = f"（缺字段：{missing}）" if missing else ""
    raise ConfirmationStateError(
        f"当前状态「{row.state.value}」不允许确认{detail}：{_state_hint(row.state)}",
        state=row.state,
        allowed=tuple(sorted(CONFIRMABLE_STATES, key=lambda item: item.value)),
    )


def _check_withdraw_state(row: JudgementRow) -> None:
    """撤回前的状态校验（不合法 → 显式抛错，行不动）。"""
    if row.state in WITHDRAWABLE_STATES:
        return
    raise ConfirmationStateError(
        f"当前状态「{row.state.value}」不允许撤回：{_state_hint(row.state)}",
        state=row.state,
        allowed=tuple(sorted(WITHDRAWABLE_STATES, key=lambda item: item.value)),
    )


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


def request_hash_of(request: ReviewRequest) -> str:
    """回写请求体的规范化哈希（运维核对用：同一条结论的请求体哈希应当稳定）。"""
    return hash_request_body(request.to_payload())


def summarize_candidates(candidates: Sequence[WritebackCandidate]) -> dict[str, int]:
    """候选回写概览（按结论类型计数）。"""
    counts = {member.value: 0 for member in Conclusion}
    for candidate in candidates:
        counts[candidate.judgement.conclusion.value] += 1
    counts["合计"] = len(candidates)
    return counts


__all__ = [
    "ACTION_CONFIRM",
    "ACTION_WITHDRAW",
    "CONFIRMABLE_STATES",
    "WRITEBACK_FAILED",
    "WRITEBACK_SENT",
    "WRITEBACK_SKIPPED",
    "WRITEBACK_STATE_CONFIRMED",
    "WRITEBACK_STATE_WITHDRAWN",
    "WRITEBACK_STATE_WITHDRAW_PENDING",
    "WRITEBACK_SUPPRESSED",
    "WITHDRAWABLE_STATES",
    "ConfirmError",
    "ConfirmOutcome",
    "ConfirmService",
    "ConfirmationStateError",
    "PharmacistIdentityError",
    "WritebackCandidate",
    "WritebackGateError",
    "WritebackOutcome",
    "WritebackReport",
    "request_hash_of",
    "summarize_candidates",
]
