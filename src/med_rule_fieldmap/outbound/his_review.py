"""HIS 审方复核回写：本产品**唯一**改变真实业务状态的路径。

药师在判定台账上确认结论后，系统把结论回写到 HIS 的审方复核接口；
药师端因此看到「这条处方系统判了什么、依据是什么、谁确认的」，
这条复核记录进入 HIS 的处方点评流程，而不是停留在本地台账里。

**接口形态**（spec.md §2.3）::

    POST {HIS_BASE_URL}/api/v1/prescription/review
    Authorization: Bearer $HIS_API_TOKEN
    Content-Type: application/json

    {"batch_no": "...", "prescription_no": "...", "rule_id": "...",
     "rule_version": "...", "conclusion": "violation", "severity": "...",
     "reason": "...", "basis": "...", "missing_fields": [],
     "reviewed_by": "...", "reviewed_at": "..."}

    响应：{"code": 0, "data": {"prescription_no": "..."}}

**三条硬边界**（本模块的全部职责）：

1. **凭据从环境变量读，缺失即显式失败** —— ``HIS_BASE_URL`` 未配置 → 显式失败并退出
   （守护进程不静默跳过）；``HIS_API_TOKEN`` 未配置 → 拒绝发出请求并告警。
   请求体与日志里**永远不出现**令牌明文。
2. **实体绑定歧义不落库** —— 响应里的 ``prescription_no`` 与请求不一致，说明
   HIS 认到的处方不是我们以为的那一条（主索引不一致 / 串号 / 冒名顶替）。
   此时**进挂起队列并告警**，判定行**不动**（不写「已回写」、不补审计），
   交给信息科人工核对主索引——系统绝不能替 HIS 猜一条。
3. **失败不静默改数据** —— HTTP 非 2xx、业务 ``code != 0``、响应体不是 JSON、
   绑定歧义，四种情况各自抛显式异常并留告警，绝不静默吞掉。

**源产品能力对应**（`github_ref/baserow/backend/src/advocate/connection.py`）：

* ``validating_create_connection()`` —— 发连接**先校验目标**，校验不过显式抛错
  （``UnacceptableAddressException``），不带着问题往下走。本轮**复用改写**为
  「回写目标地址校验」：socket 层的主机黑名单换成 HTTP 层的三项硬约束
  （协议 http/https、主机名合法、不得内嵌凭据），见
  :func:`med_rule_fieldmap.outbound.client.validate_outbound_url`；
  凭据一律走环境变量，不进 URL。
* ``attempt_timeout(timeout, deadline)`` —— 单次连接超时压到「剩余 deadline」，
  deadline 过了直接抛 ``SocketTimeout``。本轮**复用改写**为
  :class:`med_rule_fieldmap.outbound.client.DeadlineBudget`：批量回写共享一份预算。
* 源产品里 socket 层的错误是**抛出来**的，不静默吞掉——本轮保留这条纪律：
  任何一次回写失败都必须以显式异常 + 告警的形式露出来。

**不做**什么：本模块只管「把一条已确认的结论按字段规范发出去、并核对 HIS
认到的是不是同一条处方」。回写幂等由
:mod:`med_rule_fieldmap.outbound.idempotency` 负责，退避重试与挂起队列由
:mod:`med_rule_fieldmap.outbound.retry_queue` 负责，药师确认门禁由
:mod:`med_rule_fieldmap.engine.confirm` 负责。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import (
    ENV_HIS_API_TOKEN,
    ENV_HIS_BASE_URL,
    HIS_REVIEW_PATH,
    HIS_REVIEW_WITHDRAW_PATH,
    Config,
)
from ..models import BUSINESS_KEY_SEP, Conclusion
from .client import (
    DeadlineBudget,
    OutboundClient,
    OutboundError,
    OutboundProtocolError,
    OutboundRequestError,
    validate_outbound_url,
)
from .idempotency import WritebackIdempotencyGuard

logger = logging.getLogger("med_rule_fieldmap.outbound.his_review")

# ---------------------------------------------------------------------------
# 口径常量（改这里就能改全套回写行为；不在业务层散落硬编码）
# ---------------------------------------------------------------------------

#: 请求体字段全集（spec.md §2.3）——**顺序即 JSON 里的书写顺序**，也是断言口径
REVIEW_BODY_FIELDS: tuple[str, ...] = (
    "batch_no",
    "prescription_no",
    "rule_id",
    "rule_version",
    "conclusion",
    "severity",
    "reason",
    "basis",
    "missing_fields",
    "reviewed_by",
    "reviewed_at",
)

#: 业务键必需字段（缺一 → 请求发不出去）
REQUIRED_REQUEST_FIELDS: tuple[str, ...] = (
    "batch_no",
    "prescription_no",
    "rule_id",
    "rule_version",
)

#: 实体绑定歧义挂起台账文件名
BINDING_AMBIGUITY_FILENAME = "binding_ambiguity_ledger.csv"

#: 撤回请求体字段全集（顺序即 JSON 里的书写顺序，也是断言口径）
#:
#: 撤回请求与复核请求是**两件事**：复核请求说「这条结论判成什么」，
#: 撤回请求说「刚才那条复核记录不算数」。因此撤回请求里带的是
#: **谁撤的、什么时候撤的、为什么撤**，而不是结论本身。
WITHDRAW_BODY_FIELDS: tuple[str, ...] = (
    "batch_no",
    "prescription_no",
    "rule_id",
    "rule_version",
    "conclusion",
    "basis",
    "reviewed_by",
    "withdrawn_by",
    "withdrawn_at",
    "reason",
)

#: 审计行的回写状态：复核记录已成功回写
WRITEBACK_STATE_SENT = "已回写"
#: 审计行的回写状态：复核记录已撤回并同步到 HIS
WRITEBACK_STATE_WITHDRAWN = "已撤回"

#: 实体绑定歧义挂起台账列（顺序即文件里的列序）
BINDING_AMBIGUITY_COLUMNS: tuple[str, ...] = (
    "batch_no",
    "rule_id",
    "rule_version",
    "requested_prescription_no",
    "returned_prescription_no",
    "reason",
    "response_code",
    "detected_at",
    "status",
    "detail",
)

#: 挂起台账状态：待人工核对
STATUS_PENDING_REVIEW = "待人工核对"

#: HIS 业务响应里表示成功的 code
HIS_SUCCESS_CODE = 0


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class HisReviewError(OutboundError):
    """HIS 回写路径的基类异常。

    每一种失败都**显式抛出**，并带一条可核对的告警文本——绝不静默吞掉。
    """

    def __init__(self, message: str, *, alert: str = "") -> None:
        super().__init__(message)
        self.alert = alert or message


class HisCredentialError(HisReviewError):
    """凭据位缺失：请求**一个字节都不发**。

    * ``HIS_BASE_URL`` 缺失 → 显式失败并退出（守护进程不静默跳过）；
    * ``HIS_API_TOKEN`` 缺失 → 拒绝发出请求并告警。
    """


class HisResponseError(HisReviewError):
    """响应不合法：HTTP 非 2xx、业务 ``code != 0``、响应体不是 JSON。"""


class HisBindingAmbiguityError(HisReviewError):
    """**实体绑定歧义**：HIS 认到的处方与我们请求的不是同一条。

    这是本模块最要紧的一条：主索引不一致 / 串号 / 冒名顶替都会走到这里。
    处理方式固定为——**进挂起队列 + 告警 + 不落库**（判定行不写「已回写」、
    不补审计行），交给信息科人工核对主索引。
    """

    def __init__(self, message: str, *, record: "BindingAmbiguityRecord | None" = None) -> None:
        super().__init__(message)
        self.record = record


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewRequest:
    """一条待回写的审方复核结论（字段口径 = spec.md §2.3）。

    :param event_fingerprint: 事件文件指纹。**不进请求体**（spec.md §2.3 无此字段），
        只用于本地审计留痕，让飞检能从回写记录追回原始落地文件。
    """

    batch_no: str
    prescription_no: str
    rule_id: str
    rule_version: str
    conclusion: Conclusion
    severity: str = ""
    reason: str = ""
    basis: str = ""
    missing_fields: tuple[str, ...] = ()
    reviewed_by: str = ""
    reviewed_at: str = ""
    event_fingerprint: str = ""

    # -- 派生 ---------------------------------------------------------------

    @property
    def business_key(self) -> str:
        """判定业务键（明文）：``batch_no｜prescription_no｜rule_id｜rule_version``。"""
        return BUSINESS_KEY_SEP.join(
            (self.batch_no, self.prescription_no, self.rule_id, self.rule_version)
        )

    @property
    def judgement_key(self) -> str:
        """判定幂等键 = sha256(业务键)（与 :attr:`models.JudgementRow.idempotency_key` 同口径）。"""
        return hashlib.sha256(self.business_key.encode("utf-8")).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        """转成请求体。

        **字段全集严格等于** :data:`REVIEW_BODY_FIELDS`——不多不少：
        多一个字段 HIS 可能拒收，少一个字段药师端就看不到依据。
        """
        conclusion = self.conclusion
        if not isinstance(conclusion, Conclusion):
            conclusion = Conclusion.from_value(conclusion)
        return {
            "batch_no": self.batch_no,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "conclusion": conclusion.wire_value,
            "severity": self.severity,
            "reason": self.reason,
            "basis": self.basis,
            "missing_fields": list(self.missing_fields),
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
        }


def now_iso() -> str:
    """当前时间（UTC，秒级 ISO8601）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# 撤回请求体
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WithdrawRequest:
    """一条待同步到 HIS 的**撤回**通知。

    药师撤回确认后有两种落点：

    * **还没发出去** —— 本地直接把排队中的回写标成「已撤回」，不发任何请求；
    * **已经发出去了** —— 必须让 HIS 端也知道这条复核记录不算数，
      这才由本请求体承载（走 ``/api/v1/prescription/review/withdraw``）。

    字段口径见 :data:`WITHDRAW_BODY_FIELDS`。
    """

    batch_no: str
    prescription_no: str
    rule_id: str
    rule_version: str
    conclusion: Conclusion
    basis: str = ""
    reviewed_by: str = ""
    withdrawn_by: str = ""
    withdrawn_at: str = ""
    reason: str = ""
    event_fingerprint: str = ""

    @property
    def business_key(self) -> str:
        """判定业务键（明文）：与复核请求同口径。"""
        return BUSINESS_KEY_SEP.join(
            (self.batch_no, self.prescription_no, self.rule_id, self.rule_version)
        )

    @property
    def judgement_key(self) -> str:
        """判定幂等键 = sha256(业务键)。"""
        return hashlib.sha256(self.business_key.encode("utf-8")).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        """转成请求体（字段全集严格等于 :data:`WITHDRAW_BODY_FIELDS`）。"""
        conclusion = self.conclusion
        if not isinstance(conclusion, Conclusion):
            conclusion = Conclusion.from_value(conclusion)
        return {
            "batch_no": self.batch_no,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "conclusion": conclusion.wire_value,
            "basis": self.basis,
            "reviewed_by": self.reviewed_by,
            "withdrawn_by": self.withdrawn_by,
            "withdrawn_at": self.withdrawn_at,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# 实体绑定歧义挂起队列
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindingAmbiguityRecord:
    """一条实体绑定歧义记录（追加式，不改写历史）。"""

    batch_no: str
    rule_id: str
    rule_version: str
    requested_prescription_no: str
    returned_prescription_no: str
    reason: str = ""
    response_code: str = ""
    detected_at: str = field(default_factory=now_iso)
    status: str = STATUS_PENDING_REVIEW
    detail: str = ""

    def to_record(self) -> dict[str, str]:
        """转成 CSV 行（列序与 :data:`BINDING_AMBIGUITY_COLUMNS` 一致）。"""
        return {
            "batch_no": self.batch_no,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "requested_prescription_no": self.requested_prescription_no,
            "returned_prescription_no": self.returned_prescription_no,
            "reason": self.reason,
            "response_code": self.response_code,
            "detected_at": self.detected_at,
            "status": self.status,
            "detail": self.detail,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, str]) -> "BindingAmbiguityRecord":
        """从 CSV 行还原。"""
        return cls(
            batch_no=record.get("batch_no", ""),
            rule_id=record.get("rule_id", ""),
            rule_version=record.get("rule_version", ""),
            requested_prescription_no=record.get("requested_prescription_no", ""),
            returned_prescription_no=record.get("returned_prescription_no", ""),
            reason=record.get("reason", ""),
            response_code=record.get("response_code", ""),
            detected_at=record.get("detected_at", ""),
            status=record.get("status", STATUS_PENDING_REVIEW) or STATUS_PENDING_REVIEW,
            detail=record.get("detail", ""),
        )


class BindingAmbiguityLedger:
    """实体绑定歧义挂起队列（追加式 CSV）。

    落盘位置 ``out/binding_ambiguity_ledger.csv``（与判定台账同目录）。

    典型用法::

        ledger = BindingAmbiguityLedger(out_dir / BINDING_AMBIGUITY_FILENAME)
        ledger.append(BindingAmbiguityRecord(...))   # 追加，不改写历史
        ledger.pending()                              # → 待人工核对的记录
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, record: BindingAmbiguityRecord) -> BindingAmbiguityRecord:
        """追加一条记录（文件不存在时先写表头）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.path.exists()
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(BINDING_AMBIGUITY_COLUMNS))
            if not exists:
                writer.writeheader()
            writer.writerow(record.to_record())
        return record

    def records(self) -> list[BindingAmbiguityRecord]:
        """读回全部记录（按落盘顺序）。"""
        if not self.path.is_file():
            return []
        out: list[BindingAmbiguityRecord] = []
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                out.append(BindingAmbiguityRecord.from_record(row))
        return out

    def pending(self) -> list[BindingAmbiguityRecord]:
        """仍待人工核对的记录。"""
        return [row for row in self.records() if row.status == STATUS_PENDING_REVIEW]


# ---------------------------------------------------------------------------
# 回写结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewOutcome:
    """一次回写的结果。"""

    prescription_no: str
    request_hash: str = ""
    writeback_key: str = ""
    sent: bool = False
    suppressed: bool = False
    status_code: int = 0
    attempts: int = 0
    reason: str = ""
    response_body: Any = None


# ---------------------------------------------------------------------------
# 凭据位
# ---------------------------------------------------------------------------


def require_writeback_credentials(config: Config) -> tuple[str, str]:
    """解析回写凭据位；缺一即**显式失败**（并告警）。

    :returns: ``(base_url, api_token)``。
    :raises HisCredentialError: ``HIS_BASE_URL`` 或 ``HIS_API_TOKEN`` 未配置。

    两种缺失的区别在**处置**上：

    * ``HIS_BASE_URL`` 缺失 → 回写根本没有目标，属部署未完成，
      **显式失败并退出**（守护进程不静默跳过，也不退化成只读旁路）；
    * ``HIS_API_TOKEN`` 缺失 → 目标明确但没有凭据，**拒绝发出请求并告警**
      （发了也一定被 HIS 拒，还会在链路上留下无凭据尝试）。
    """
    base_url = (config.his_base_url or "").strip()
    token = (config.his_api_token or "").strip()
    if not base_url:
        message = (
            f"{ENV_HIS_BASE_URL} 未配置：HIS 审方复核回写是本产品唯一改变业务状态的路径，"
            "缺失时必须显式失败并退出，不得静默跳过。"
        )
        logger.error("[his_review] %s", message)
        raise HisCredentialError(message)
    if not token:
        message = (
            f"{ENV_HIS_API_TOKEN} 未配置：拒绝发出 HIS 回写请求"
            "（缺少凭据位的请求会被 HIS 拒绝）。"
        )
        logger.warning("[his_review] %s", message)
        raise HisCredentialError(message)
    return base_url, token


def build_review_url(base_url: str) -> str:
    """拼出回写完整地址（``{HIS_BASE_URL}/api/v1/prescription/review``）。"""
    return base_url.rstrip("/") + HIS_REVIEW_PATH


# ---------------------------------------------------------------------------
# 回写客户端
# ---------------------------------------------------------------------------


class HisReviewWriter:
    """HIS 审方复核回写客户端。

    :param client: 出站客户端（地址校验 + deadline 预算 + 退避重试）。
    :param base_url: HIS 基址（``HIS_BASE_URL``）。
    :param api_token: 回写用 Bearer 令牌（``HIS_API_TOKEN``）。
    :param queue: 实体绑定歧义挂起队列；不给则不落盘（仅告警）。
    :param guard: 回写幂等守卫；给了则同一条结论只发一次。
    :param ledger: 状态库（实现 ``append_audit``）；给了则成功后补一条审计行。
    """

    def __init__(
        self,
        client: OutboundClient,
        *,
        base_url: str,
        api_token: str,
        queue: BindingAmbiguityLedger | None = None,
        guard: WritebackIdempotencyGuard | None = None,
        ledger: Any | None = None,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._queue = queue
        self._guard = guard
        self._ledger = ledger
        self._alerts: list[str] = []

    # -- 构造 ---------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        client: OutboundClient | None = None,
        queue: BindingAmbiguityLedger | None = None,
        guard: WritebackIdempotencyGuard | None = None,
        ledger: Any | None = None,
    ) -> "HisReviewWriter":
        """按配置面构造（凭据缺失 → 显式失败）。"""
        base_url, token = require_writeback_credentials(config)
        return cls(
            client if client is not None else OutboundClient(),
            base_url=base_url,
            api_token=token,
            queue=queue,
            guard=guard,
            ledger=ledger,
        )

    # -- 属性 ---------------------------------------------------------------

    @property
    def review_url(self) -> str:
        """回写完整地址。"""
        return build_review_url(self._base_url)

    @property
    def withdraw_url(self) -> str:
        """撤回通知完整地址（``{HIS_BASE_URL}/api/v1/prescription/review/withdraw``）。"""
        return self._base_url.rstrip("/") + HIS_REVIEW_WITHDRAW_PATH

    @property
    def base_url(self) -> str:
        """HIS 基址。"""
        return self._base_url

    @property
    def alerts(self) -> list[str]:
        """本客户端产生的告警文本（按发生顺序）。"""
        return list(self._alerts)

    def alert(self, reason: str) -> str:
        """记一条告警（写日志 + 留存文本，供审计导出）。"""
        self._alerts.append(reason)
        logger.warning("[his_review] %s", reason)
        return reason

    # -- 发送 ---------------------------------------------------------------

    def write(
        self,
        request: ReviewRequest,
        *,
        budget: DeadlineBudget | None = None,
    ) -> ReviewOutcome:
        """把一条已确认的结论回写到 HIS。

        :raises HisCredentialError: 凭据缺失（构造阶段即已挡住）。
        :raises HisResponseError: HTTP 非 2xx / 业务 code != 0 / 响应体不合法。
        :raises HisBindingAmbiguityError: 响应 ``prescription_no`` 与请求不一致
            （**已进挂起队列并告警，判定行不落库**）。
        :raises OutboundRequestError: 重试耗尽仍未成功（退避队列据此接手）。
        """
        self._validate_request(request)
        payload = request.to_payload()
        return self._send(
            self.review_url,
            payload,
            request,
            audit_state=WRITEBACK_STATE_SENT,
            success_reason="回写已发出并被 HIS 认领",
            budget=budget,
        )

    def withdraw(
        self,
        request: WithdrawRequest,
        *,
        budget: DeadlineBudget | None = None,
    ) -> ReviewOutcome:
        """把一条**撤回**通知同步到 HIS（药师撤回后已经发出过的那条复核记录）。

        语义与 :meth:`write` 一致（同样的幂等守卫、同样的实体绑定歧义处理），
        只是地址与请求体不同。
        """
        self._validate_withdraw_request(request)
        payload = request.to_payload()
        return self._send(
            self.withdraw_url,
            payload,
            request,
            audit_state=WRITEBACK_STATE_WITHDRAWN,
            success_reason="撤回已同步到 HIS",
            budget=budget,
        )

    # -- 内部 ---------------------------------------------------------------

    def _send(
        self,
        url: str,
        payload: Mapping[str, Any],
        request: Any,
        *,
        audit_state: str,
        success_reason: str,
        budget: DeadlineBudget | None = None,
    ) -> ReviewOutcome:
        """复核与撤回共用的发送路径（幂等 → 发出 → 核对 → 落痕）。

        两条路径共用同一套防线，是因为它们的风险完全一样：把请求发错对象、
        把失败当成功、把同一条复核发两遍。
        """
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }

        # -- 幂等：同一条结论只发一次 ---------------------------------------
        decision = None
        writeback_key = ""
        if self._guard is not None:
            decision = self._guard.claim(request.judgement_key, payload)
            writeback_key = decision.writeback_key
            if not decision.allowed:
                return ReviewOutcome(
                    prescription_no=request.prescription_no,
                    request_hash=decision.request_hash,
                    writeback_key=writeback_key,
                    sent=False,
                    suppressed=True,
                    reason=decision.reason,
                )

        # -- 发出 ------------------------------------------------------------
        try:
            response = self._client.post_json(url, payload, headers=headers, budget=budget)
        except OutboundRequestError:
            self._mark_failed(writeback_key)
            raise
        except OutboundProtocolError:
            self._mark_failed(writeback_key)
            raise

        if not response.ok:
            self._mark_failed(writeback_key)
            message = (
                f"HIS 审方复核接口返回 HTTP {response.status_code}："
                f"回写未生效（目标 {url}）"
            )
            self.alert(message)
            raise HisResponseError(message)

        # -- 核对业务响应 ----------------------------------------------------
        body = response.body
        if not isinstance(body, dict):
            self._mark_failed(writeback_key)
            message = (
                "HIS 审方复核响应体不是 JSON 对象：回写未生效"
                f"（HTTP {response.status_code}，目标 {url}）"
            )
            self.alert(message)
            raise HisResponseError(message)

        code = body.get("code")
        if code != HIS_SUCCESS_CODE and str(code) != str(HIS_SUCCESS_CODE):
            self._mark_failed(writeback_key)
            message = (
                f"HIS 审方复核返回业务失败 code={code!r}：回写未生效（目标 {url}）"
            )
            self.alert(message)
            raise HisResponseError(message)

        data = body.get("data")
        returned_rx = ""
        if isinstance(data, Mapping):
            returned_rx = str(data.get("prescription_no") or "").strip()

        # -- 实体绑定歧义：HIS 认到的不是我们请求的那条处方 ------------------
        if returned_rx != request.prescription_no:
            self._mark_failed(writeback_key)
            record = BindingAmbiguityRecord(
                batch_no=request.batch_no,
                rule_id=request.rule_id,
                rule_version=request.rule_version,
                requested_prescription_no=request.prescription_no,
                returned_prescription_no=returned_rx,
                reason="响应 prescription_no 与请求不一致，判为实体绑定歧义",
                response_code=str(code),
                detail=(
                    f"HIS 认到的处方号 {returned_rx or '（空）'} 与请求的 "
                    f"{request.prescription_no} 不一致；判定行不落库，"
                    "须由信息科核对 HIS 主索引后人工重投。"
                ),
            )
            if self._queue is not None:
                self._queue.append(record)
            message = (
                f"实体绑定歧义：请求处方号 {request.prescription_no}，"
                f"HIS 响应处方号 {returned_rx or '（空）'}（规则 {request.rule_id}）——"
                "已进挂起队列并告警，判定行不落库。"
            )
            self.alert(message)
            raise HisBindingAmbiguityError(message, record=record)

        # -- 成功 ------------------------------------------------------------
        self._mark_sent(writeback_key)
        self._append_audit(request, audit_state)
        return ReviewOutcome(
            prescription_no=request.prescription_no,
            request_hash=decision.request_hash if decision is not None else "",
            writeback_key=writeback_key,
            sent=True,
            suppressed=False,
            status_code=response.status_code,
            attempts=response.attempts,
            reason=success_reason,
            response_body=body,
        )

    def _validate_request(self, request: ReviewRequest) -> None:
        """请求体自检：业务键四段齐全 + 药师标识齐全 + 依据非空。"""
        missing = [
            name
            for name in REQUIRED_REQUEST_FIELDS
            if not str(getattr(request, name, "") or "").strip()
        ]
        if missing:
            raise HisResponseError(
                f"回写请求缺少业务键字段：{'、'.join(missing)}（无法定位到唯一一条判定）"
            )
        if not str(request.reviewed_by or "").strip():
            raise HisResponseError(
                "回写请求缺少 reviewed_by（药师标识）：审方复核的第一责任人是药师，"
                "系统不代签。"
            )

    def _validate_withdraw_request(self, request: WithdrawRequest) -> None:
        """撤回请求体自检：业务键四段齐全 + 撤回人标识齐全。"""
        missing = [
            name
            for name in REQUIRED_REQUEST_FIELDS
            if not str(getattr(request, name, "") or "").strip()
        ]
        if missing:
            raise HisResponseError(
                f"撤回请求缺少业务键字段：{'、'.join(missing)}（无法定位到唯一一条判定）"
            )
        if not str(request.withdrawn_by or "").strip():
            raise HisResponseError(
                "撤回请求缺少 withdrawn_by（药师标识）：撤回同样要留人，系统不代撤。"
            )

    def _mark_sent(self, writeback_key: str) -> None:
        if self._guard is not None and writeback_key:
            self._guard.mark_sent(writeback_key)

    def _mark_failed(self, writeback_key: str) -> None:
        if self._guard is not None and writeback_key:
            self._guard.mark_failed(writeback_key)

    def _append_audit(self, request: Any, audit_state: str) -> None:
        """成功后补一条审计行（追加式，不覆盖历史）。

        ``action`` 记「回写」或「撤回」——审计表是**事件流水**，导出时靠这一列
        把「审了多少条」（判定事件）与「发出去多少次」（出站调用事件）分开数。
        """
        if self._ledger is None:
            return
        from ..models import AuditAction, AuditRow  # 局部导入：避免 models → outbound 的循环依赖

        action = (
            AuditAction.WITHDRAW.value
            if audit_state == WRITEBACK_STATE_WITHDRAWN
            else AuditAction.WRITEBACK.value
        )
        self._ledger.append_audit(
            [
                AuditRow(
                    batch_no=request.batch_no,
                    prescription_no=request.prescription_no,
                    rule_id=request.rule_id,
                    rule_version=request.rule_version,
                    basis=str(getattr(request, "basis", "") or ""),
                    conclusion=request.conclusion,
                    event_fingerprint=str(getattr(request, "event_fingerprint", "") or ""),
                    writeback_state=audit_state,
                    retry_count=0,
                    action=action,
                )
            ]
        )


# ---------------------------------------------------------------------------
# 预检（运维通道）
# ---------------------------------------------------------------------------


def preflight(config: Config) -> list[str]:
    """回写链路预检：返回可读的状态行。

    预检**不发任何请求**，只核对凭据位与目标地址是否齐备合法——
    让信息科在部署当天就能发现「环境变量没配」，而不是等到第一批回写失败。
    """
    lines: list[str] = []
    base_url = (config.his_base_url or "").strip()
    token = (config.his_api_token or "").strip()
    if not base_url:
        lines.append(f"[缺失] {ENV_HIS_BASE_URL} 未配置 → 回写不可用（必须显式失败）")
    else:
        lines.append(f"[已配置] {ENV_HIS_BASE_URL} = {base_url}")
        try:
            validated = validate_outbound_url(build_review_url(base_url))
        except OutboundError as exc:
            lines.append(f"[非法] 回写地址不合法：{exc}")
        else:
            lines.append(f"[合法] 回写地址 = {validated.url}")
    if not token:
        lines.append(f"[缺失] {ENV_HIS_API_TOKEN} 未配置 → 拒绝发出请求")
    else:
        lines.append(f"[已配置] {ENV_HIS_API_TOKEN}（{len(token)} 位，令牌明文不显示）")
    return lines


def main(argv: list[str] | None = None) -> int:
    """预检入口：``python -m med_rule_fieldmap.outbound.his_review --check``。

    退出码：``0`` 齐备、``2`` 缺凭据或地址非法（**显式失败**，不静默跳过）。
    """
    parser = argparse.ArgumentParser(
        prog="python -m med_rule_fieldmap.outbound.his_review",
        description="HIS 审方复核回写链路预检（不发请求）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做凭据与地址预检，不发出任何回写请求",
    )
    args = parser.parse_args(argv)

    config = Config.from_env()
    lines = preflight(config)
    for line in lines:
        print(line)
    failed = any(line.startswith(("[缺失]", "[非法]")) for line in lines)
    if failed:
        print(
            "预检未通过：HIS 审方复核回写不可用（显式失败，不静默跳过）。",
            file=sys.stderr,
        )
        return 2
    if args.check:
        print("预检模式：本次未发出任何回写请求。")
    print("预检通过：HIS 审方复核回写链路可用。")
    return 0


if __name__ == "__main__":  # pragma: no cover - 预检入口
    raise SystemExit(main())


__all__ = [
    "BINDING_AMBIGUITY_COLUMNS",
    "BINDING_AMBIGUITY_FILENAME",
    "HIS_SUCCESS_CODE",
    "REQUIRED_REQUEST_FIELDS",
    "REVIEW_BODY_FIELDS",
    "STATUS_PENDING_REVIEW",
    "WITHDRAW_BODY_FIELDS",
    "WRITEBACK_STATE_SENT",
    "WRITEBACK_STATE_WITHDRAWN",
    "BindingAmbiguityLedger",
    "BindingAmbiguityRecord",
    "HisBindingAmbiguityError",
    "HisCredentialError",
    "HisResponseError",
    "HisReviewError",
    "HisReviewWriter",
    "ReviewOutcome",
    "ReviewRequest",
    "WithdrawRequest",
    "build_review_url",
    "main",
    "now_iso",
    "preflight",
    "require_writeback_credentials",
]
