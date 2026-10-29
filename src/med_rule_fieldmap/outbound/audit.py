"""审计写入面：每条判定与每次出站调用都留痕（追加式）。

**对应源产品能力**：源仓库 ``contrib/database/webhooks/models.py`` 的
``TableWebhookCall``（每次触发留一条可复核记录：请求 / 响应副本 + 重试次数）
与 ``webhooks/handler.py`` 的 ``make_request()``（发出去的那一次单独留痕）。

**改写**：源产品的留痕对象是「一次出站调用」，本轮把留痕拆成**两股水**：

* **判定事件**（:class:`~med_rule_fieldmap.models.AuditRow`）——「审了什么」：
  批次号 / 处方号 / 规则号 / 规则版本 / 依据出处 / 结论 / 事件文件指纹 /
  回写状态 / 重试次数，一条判定一行；
* **出站调用事件**（:class:`~med_rule_fieldmap.models.OutboundCallRecord`）——
  「发出去什么、发了几次、成没成」：HIS 审方复核回写、撤回通知、
  企业微信卡片推送、待重放队列重放各记一行，**失败的调用同样留痕**并带
  累计重试次数。

**为什么失败的调用也要留痕**：飞检现场必问的一句话是「发了几次都没成」。
失败的痕迹若只留在内存里的异常栈里，进程一重启就没了；落库之后，
「重试 3 次仍失败 → 进待重放队列」这件事才答得出来。

**为什么判定事件里没有失败**：判定本身没有「失败」——判不出来是**第三态**
「不可判定」，不是异常。判定事件的 ``writeback_state`` 记的是**判定当时**
的回写状态（未回写 / 重复投递 / 已回写），不是后来发生了什么；
「后来发生了什么」记在出站调用事件里。两股水在
:mod:`med_rule_fieldmap.ledger.audit_export` 合成一张审计流水。

**只追加，不覆盖**：本模块只暴露 ``record_*`` 写入口，没有任何改写 / 删除
接口；同一份请求重发会另起一行（新事件号），旧行原样留着。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from typing import Any

from ..models import (
    AuditAction,
    AuditRow,
    Conclusion,
    JudgementRow,
    OutboundCallRecord,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 通道与状态口径
# ---------------------------------------------------------------------------

#: 出站通道：HIS 审方复核
CHANNEL_HIS = "HIS"
#: 出站通道：企业微信群机器人
CHANNEL_WECOM = "企业微信"

#: 出站调用状态：已送达（HIS 回写成功 / 企业微信卡片送达）
STATUS_SENT = "已回写"
#: 出站调用状态：送达失败（已按退避重试过，仍失败）
STATUS_FAILED = "回写失败"
#: 出站调用状态：实体绑定歧义（响应处方号与请求不一致，判定行不落库）
STATUS_AMBIGUOUS = "绑定歧义"
#: 出站调用状态：推送失败
STATUS_PUSH_FAILED = "推送失败"
#: 出站调用状态：待重放队列里的重新发出已送达
STATUS_REPLAYED = "已重放"
#: 出站调用状态：重放被幂等守卫抑制（一个字节都没发）
STATUS_SUPPRESSED = "已抑制"

#: 判定事件的默认回写状态：已出结论但药师尚未确认，本产品不回写 HIS
WRITEBACK_PENDING = "未回写"
#: 判定事件的回写状态：该事件是重复投递，只补审计不新增判定
WRITEBACK_DUPLICATE = "重复投递"


class AuditError(RuntimeError):
    """审计写入错误（留痕本身失败必须显式报出来，不静默吞掉）。"""


def build_call_key(
    channel: str,
    action: str,
    judgement_key: str,
    request_hash: str = "",
    attempt: int = 1,
) -> str:
    """出站调用事件号。

    **同一次尝试重发不会覆盖旧行**：事件号里带尝试序号，
    「发第 1 次失败、发第 2 次失败」是两条独立的事件，不是同一条被改写。
    """
    raw = "｜".join(
        [
            str(channel or ""),
            str(action or ""),
            str(judgement_key or ""),
            str(request_hash or ""),
            str(int(attempt)),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _conclusion_value(conclusion: object) -> Conclusion:
    if isinstance(conclusion, Conclusion):
        return conclusion
    return Conclusion.from_value(conclusion)


class AuditRecorder:
    """审计写入面：判定事件与出站调用事件的统一入口。

    :param ledger: 状态库（需实现 ``append_audit`` 与 ``append_outbound_call``）；
        没有出站调用留痕能力的旧库也能用——出站事件会被**降级为告警**而不是
        让主流程失败（审计留痕缺失不该让一条已确认的结论回写不出去）。
    """

    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger

    # -- 判定事件 -----------------------------------------------------------

    def record_judgements(
        self,
        rows: Iterable[JudgementRow],
        *,
        event_fingerprint: str = "",
        writeback_state: str = WRITEBACK_PENDING,
    ) -> int:
        """把一批判定行落成判定事件（追加式），返回落库行数。

        ``writeback_state`` 记的是**判定当时**的回写状态（未回写 / 重复投递），
        不是后来发生了什么——后来发生了什么记在出站调用事件里。
        """
        payload = [
            AuditRow(
                batch_no=row.batch_no,
                prescription_no=row.prescription_no,
                rule_id=row.rule_id,
                rule_version=row.rule_version,
                basis=row.basis,
                conclusion=row.conclusion,
                event_fingerprint=event_fingerprint or row.event_fingerprint,
                writeback_state=writeback_state,
                retry_count=0,
                action=AuditAction.JUDGEMENT.value,
            )
            for row in rows
        ]
        if not payload:
            return 0
        return int(self._ledger.append_audit(payload))

    # -- 出站调用事件 -------------------------------------------------------

    def record_outbound_call(
        self,
        *,
        channel: str,
        action: str,
        status: str,
        judgement_key: str = "",
        batch_no: str = "",
        prescription_no: str = "",
        rule_id: str = "",
        rule_version: str = "",
        conclusion: object = Conclusion.COMPLIANT,
        basis: str = "",
        event_fingerprint: str = "",
        retry_count: int = 0,
        error_code: str = "",
        detail: str = "",
        attempt: int = 1,
        request_hash: str = "",
    ) -> OutboundCallRecord:
        """记一次出站调用（追加式），返回落库的留痕行。

        **失败同样留痕**：``status`` 取「回写失败 / 推送失败 / 绑定歧义」时
        ``retry_count`` 记累计重试次数、``error_code`` 记最后错误码——
        「发了几次都没成」是飞检现场要当场答得出的问题。

        状态库不支持出站留痕时**降级为告警**：留痕缺失不能让一条已确认的
        结论凭空回写不出去（主流程优先），但必须留下一条告警，不能静默。
        """
        record = OutboundCallRecord(
            call_key=build_call_key(
                channel, action, judgement_key, request_hash, attempt
            ),
            channel=channel,
            action=action,
            judgement_key=judgement_key,
            batch_no=batch_no,
            prescription_no=prescription_no,
            rule_id=rule_id,
            rule_version=rule_version,
            conclusion=_conclusion_value(conclusion),
            basis=basis,
            event_fingerprint=event_fingerprint,
            status=status,
            retry_count=int(retry_count),
            error_code=error_code,
            detail=detail,
        )
        append = getattr(self._ledger, "append_outbound_call", None)
        if append is None:
            logger.warning(
                "状态库不支持出站调用留痕（缺 append_outbound_call）："
                "通道 %s · 动作 %s · 状态 %s —— 该次出站未留痕",
                channel,
                action,
                status,
            )
            return record
        append(record)
        return record

    def record_writeback(
        self,
        *,
        status: str = STATUS_SENT,
        retry_count: int = 0,
        attempt: int = 1,
        **kwargs: Any,
    ) -> OutboundCallRecord:
        """记一次 HIS 审方复核回写（含撤回通知）。"""
        return self.record_outbound_call(
            channel=CHANNEL_HIS,
            action=kwargs.pop("action", AuditAction.WRITEBACK.value),
            status=status,
            retry_count=retry_count,
            attempt=attempt,
            **kwargs,
        )

    def record_push(
        self,
        *,
        status: str = STATUS_SENT,
        retry_count: int = 0,
        attempt: int = 1,
        **kwargs: Any,
    ) -> OutboundCallRecord:
        """记一次企业微信批次卡片推送。"""
        return self.record_outbound_call(
            channel=CHANNEL_WECOM,
            action=AuditAction.PUSH.value,
            status=status,
            retry_count=retry_count,
            attempt=attempt,
            **kwargs,
        )

    # -- 读回 ---------------------------------------------------------------

    def list_outbound_calls(
        self, batch_no: str | None = None
    ) -> list[OutboundCallRecord]:
        """读回出站调用留痕（按写入顺序）。"""
        lister = getattr(self._ledger, "list_outbound_calls", None)
        if lister is None:
            return []
        return list(lister(batch_no))

    def count_outbound_calls(self, batch_no: str | None = None) -> int:
        """出站调用留痕行数。"""
        counter = getattr(self._ledger, "count_outbound_calls", None)
        if counter is None:
            return 0
        return int(counter(batch_no))


__all__ = [
    "CHANNEL_HIS",
    "CHANNEL_WECOM",
    "STATUS_AMBIGUOUS",
    "STATUS_FAILED",
    "STATUS_PUSH_FAILED",
    "STATUS_REPLAYED",
    "STATUS_SENT",
    "STATUS_SUPPRESSED",
    "WRITEBACK_DUPLICATE",
    "WRITEBACK_PENDING",
    "AuditError",
    "AuditRecorder",
    "build_call_key",
]
