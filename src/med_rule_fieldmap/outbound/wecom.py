"""企业微信批次卡片推送：把一批审方结果递到临床药师与信息科已经在用的通道。

**为什么推卡片而不是再造一个通知中心**：药学部与信息科日常都在企业微信里，
审方结果再往那儿递一趟，药师点开就是「这一批判了多少、哪些判不了、缺哪些字段、
先补哪个最划算」；再造一个站内通知中心，等于让临床多装一个系统、少看一个群。

**接口形态**（spec.md §2.4）::

    POST $WECOM_WEBHOOK_URL
    Content-Type: application/json

    {"msgtype": "markdown", "markdown": {"content": "<批次摘要 + 不可判定清单摘要 + 字段补齐顺序摘要>"}}

    响应：{"errcode": 0, "errmsg": "ok"}

**三条硬边界**：

1. **凭据从环境变量读，地址缺失或非法即显式报错** —— ``WECOM_WEBHOOK_URL``
   未配置 / 协议不是 http(s) / 主机名不合法 → **请求一个字节都不发**，
   并产生一条告警。webhook 地址里的 ``key`` 是凭据，**告警与日志一律隐去**
   （只留「协议://主机/路径」），不落明文。
2. **推送失败只告警，不阻断主流程** —— 推送是「递个话」，不是「改业务状态」：
   推送挂了，判定台账照样落盘、药师照样能回写 HIS。所以对外有两个入口：
   :meth:`WeComNotifier.push` **显式抛错**（给运维与测试用），
   :meth:`WeComNotifier.notify` **兜住异常、只留告警**（给守护进程用）。
   **失败不静默改数据**：推送失败绝不动判定行状态、绝不改写回写状态。
3. **卡片内容有上限且可核对** —— 企业微信 markdown 内容有长度上限，
   超限时**截断并注明**（「完整清单见 out/undeterminable_ledger.csv 与
   out/field_gap_priority.csv」），不做「悄悄少发几条」；条目按影响面排序，
   同样的输入必得同样的卡片。

**源产品能力对应**（源仓库 ``backend/src/baserow/contrib/database/webhooks/models.py``）：

* ``TableWebhook``（``url`` 带 ``url_validator``、``headers``）—— 源产品把
  「往哪个地址发、带哪些头」做成**可登记的推送配置**并**先校验地址再发**。
  本轮**复用改写**为「企业微信 webhook 推送」：可登记的推送目标 = 环境变量
  ``WECOM_WEBHOOK_URL``；地址校验复用 :func:`med_rule_fieldmap.outbound.client.validate_outbound_url`
  （非空 / 协议 http·https / 主机名合法 / 端口合法 / 不得内嵌凭据），
  校验不过 → 显式抛错 + 告警，**请求不发出**。
* ``TableWebhookCall``（请求 / 响应副本留痕）—— 本轮的推送留痕并入
  :mod:`med_rule_fieldmap.outbound.audit`（T32）与本模块的 ``outcomes`` 列表。

**摘录说明**：``webhooks/models.py`` 不在本轮 ``github_ref/`` 的 12 个摘录文件内，
证据路径沿用源仓库原路径（与 spec.md §7 / §8 的既有说明一致）。

**不做**什么：本模块只管「把这一批的结果递出去」。判定在
:mod:`med_rule_fieldmap.engine.judge`，台账在 :mod:`med_rule_fieldmap.ledger`，
回写 HIS 在 :mod:`med_rule_fieldmap.outbound.his_review`——本模块**不**改判定、
**不**回写、**不**改任何业务状态。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from ..config import ENV_WECOM_WEBHOOK_URL, Config
from ..models import Conclusion
from .audit import STATUS_PUSH_FAILED, STATUS_SENT, AuditRecorder
from .client import (
    DeadlineBudget,
    OutboundAddressError,
    OutboundClient,
    OutboundError,
    OutboundProtocolError,
    OutboundRequestError,
    OutboundResponse,
    validate_outbound_url,
)

if TYPE_CHECKING:  # pragma: no cover - 仅供类型标注，运行时不引入 ledger 包
    from ..ledger.gap import GapEntry
    from ..ledger.priority import FieldPriority
    from ..models import JudgementRow

logger = logging.getLogger("med_rule_fieldmap.outbound.wecom")

# ---------------------------------------------------------------------------
# 口径常量（改这里就能改全套推送行为；不在业务层散落硬编码）
# ---------------------------------------------------------------------------

#: 卡片消息类型（企业微信机器人只认这几种 msgtype）
WECOM_MSGTYPE = "markdown"

#: 请求体顶层键
WECOM_MARKDOWN_KEY = "markdown"

#: markdown 段里的正文键
WECOM_CONTENT_KEY = "content"

#: 企业微信业务码：0 表示推送成功
WECOM_SUCCESS_ERRCODE = 0

#: 卡片标题前缀
CARD_TITLE = "合理用药审方批次"

#: 不可判定条目默认最多列几条（超出部分在卡片里点名去台账看）
DEFAULT_MAX_GAP_ITEMS = 5

#: 字段补齐顺序默认最多列几条
DEFAULT_MAX_PRIORITY_ITEMS = 5

#: 卡片正文长度上限（企业微信 markdown 正文有上限，超了会被拒）
MAX_CONTENT_CHARS = 4096

#: 截断时提示读者去看的两份台账
UNDETERMINABLE_LEDGER_NAME = "out/undeterminable_ledger.csv"
PRIORITY_LEDGER_NAME = "out/field_gap_priority.csv"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class WeComError(OutboundError):
    """企业微信推送路径的基类异常。

    每一种失败都**显式抛出**并带一条可核对的告警文本——绝不静默吞掉。
    """


class WeComAddressError(WeComError):
    """推送地址缺失或非法：**请求一个字节都不发**。

    与「发出去但失败」区分开——地址不合法属配置错误，重试没有意义。
    """


class WeComResponseError(WeComError):
    """推送没被接受：HTTP 非 2xx / 业务 ``errcode != 0`` / 响应体不合法。

    :attr:`status_code` 是响应码（连接类失败时为 ``0``）；
    :attr:`attempts` 是实际发出的请求次数。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


# ---------------------------------------------------------------------------
# 卡片内容的数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchCardSummary:
    """一个批次的判定摘要（卡片第一段）。

    口径与 :class:`med_rule_fieldmap.run_summary.RunSummary` 一致：
    「收到 N 行」是**医嘱行数**，「判定 M 条」是**逐处方 × 逐规则**的结论条数。
    """

    batch_no: str = ""
    received_rows: int = 0
    judgement_rows: int = 0
    compliant: int = 0
    violation: int = 0
    undeterminable: int = 0

    @property
    def conclusion_counts(self) -> dict[str, int]:
        """三态计数（键是中文结论名）。"""
        return {
            Conclusion.COMPLIANT.value: self.compliant,
            Conclusion.VIOLATION.value: self.violation,
            Conclusion.UNDETERMINABLE.value: self.undeterminable,
        }


@dataclass(frozen=True)
class UndeterminableItem:
    """卡片第二段的一条不可判定条目（缺失字段 × 卡住的规则）。

    口径与 :class:`med_rule_fieldmap.ledger.gap.GapEntry` 一致，
    但**不引入 ledger 包**（ledger → engine → outbound 会形成循环依赖）。
    """

    missing_field: str
    rule_id: str
    cause: str = ""
    affected_prescriptions: int = 0


@dataclass(frozen=True)
class PriorityItem:
    """卡片第三段的一条字段补齐顺序。

    口径与 :class:`med_rule_fieldmap.ledger.priority.FieldPriority` 一致。
    """

    priority: int
    missing_field: str
    affected_prescriptions: int = 0
    affected_rules: int = 0
    route_to: str = ""


@dataclass(frozen=True)
class PushOutcome:
    """一次推送的结果。"""

    batch_no: str = ""
    sent: bool = False
    status_code: int = 0
    errcode: int | None = None
    errmsg: str = ""
    attempts: int = 0
    reason: str = ""
    content_chars: int = 0
    truncated: bool = False


# ---------------------------------------------------------------------------
# 地址脱敏（webhook 地址里的 key 是凭据，不落明文）
# ---------------------------------------------------------------------------


def mask_webhook_url(url: str | None) -> str:
    """把 webhook 地址脱敏成「协议://主机/路径」。

    企业微信机器人的地址形如
    ``https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<凭据>``，
    **query 段里的 ``key`` 就是凭据**——告警与日志里一律不显示它。
    地址本身不合法时返回 ``（地址不合法）``，不把原文吐出去。
    """
    text = (url or "").strip()
    if not text:
        return "（地址未配置）"
    try:
        parts = urlsplit(text)
    except ValueError:  # pragma: no cover - urlsplit 极少抛错
        return "（地址不合法）"
    if not parts.scheme or not parts.netloc:
        return "（地址不合法）"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


#: 文本里可能出现的 URL（query 段里可能藏着凭据）
_URL_IN_TEXT = re.compile(
    r"(?P<base>[A-Za-z][A-Za-z0-9+.\-]*://[^\s?，。；、（）()\"'<>]+)"
    r"(?:\?[^\s，。；、（）()\"'<>]*)?"
)


def redact_urls(text: str) -> str:
    """把文本里所有 URL 的 query 段抹掉。

    出站客户端的异常消息里带着**完整目标地址**（如
    「…目标 http://host/cgi-bin/webhook/send?key=<凭据>」），
    告警与日志直接引用它就会把凭据位落进日志。引用异常文本前一律先过这一道。
    """
    return _URL_IN_TEXT.sub(lambda match: match.group("base"), text)


# ---------------------------------------------------------------------------
# 卡片正文组装
# ---------------------------------------------------------------------------


def _count_conclusions(rows: Iterable["JudgementRow"]) -> tuple[int, dict[str, int]]:
    """数三态结论与结论条数。"""
    counts: dict[str, int] = {}
    total = 0
    for row in rows:
        conclusion = getattr(row, "conclusion", "")
        key = getattr(conclusion, "value", None) or str(conclusion)
        counts[key] = counts.get(key, 0) + 1
        total += 1
    return total, counts


def summarize_judgements(
    rows: Iterable["JudgementRow"],
    *,
    batch_no: str = "",
    received_rows: int | None = None,
) -> BatchCardSummary:
    """由判定行汇总出卡片第一段的批次摘要。

    :param received_rows: 医嘱行数；不给时按**去重后的处方号**数推导
        （「收到 N 行」说的是医嘱行，不是结论条数）。
    """
    materialised = list(rows)
    total, counts = _count_conclusions(materialised)
    if received_rows is None:
        received_rows = len(
            {
                str(getattr(row, "prescription_no", "") or "")
                for row in materialised
                if str(getattr(row, "prescription_no", "") or "")
            }
        )
    return BatchCardSummary(
        batch_no=batch_no or str(getattr(materialised[0], "batch_no", "") or "") if materialised else "",
        received_rows=int(received_rows),
        judgement_rows=total,
        compliant=int(counts.get(Conclusion.COMPLIANT.value, 0)),
        violation=int(counts.get(Conclusion.VIOLATION.value, 0)),
        undeterminable=int(counts.get(Conclusion.UNDETERMINABLE.value, 0)),
    )


def items_from_gap_entries(entries: Sequence["GapEntry"]) -> list[UndeterminableItem]:
    """把不可判定台账条目转成卡片条目。

    **排序口径**：按「受影响处方数降序 → 缺失字段名升序 → 规则号升序」排——
    药师先看到卡得最多的字段，规则号只作同影响面下的稳定兜底，
    同样的输入必得同样的卡片。
    """
    items: list[UndeterminableItem] = []
    for entry in entries:
        cause = getattr(entry, "cause", "")
        items.append(
            UndeterminableItem(
                missing_field=str(getattr(entry, "missing_field", "") or ""),
                rule_id=str(getattr(entry, "rule_id", "") or ""),
                cause=str(getattr(cause, "value", None) or cause or ""),
                affected_prescriptions=int(getattr(entry, "affected_prescriptions", 0) or 0),
            )
        )
    items.sort(key=lambda item: (-item.affected_prescriptions, item.missing_field, item.rule_id))
    return items


def items_from_priority_table(items: Sequence["FieldPriority"]) -> list[PriorityItem]:
    """把字段补齐顺序表转成卡片条目（按名次升序）。"""
    converted = [
        PriorityItem(
            priority=int(getattr(item, "priority", 0) or 0),
            missing_field=str(getattr(item, "missing_field", "") or ""),
            affected_prescriptions=int(getattr(item, "affected_prescriptions", 0) or 0),
            affected_rules=int(getattr(item, "affected_rules", 0) or 0),
            route_to=str(getattr(item, "route_to", "") or ""),
        )
        for item in items
    ]
    converted.sort(key=lambda item: (item.priority, item.missing_field))
    return converted


def build_card_content(
    summary: BatchCardSummary,
    gap_items: Sequence[UndeterminableItem] = (),
    priority_items: Sequence[PriorityItem] = (),
    *,
    max_gap_items: int = DEFAULT_MAX_GAP_ITEMS,
    max_priority_items: int = DEFAULT_MAX_PRIORITY_ITEMS,
) -> str:
    """组装卡片正文（企业微信 markdown）。

    三段固定：**批次摘要** → **不可判定条目摘要** → **字段补齐顺序摘要**。
    超出上限的部分**在卡片里点名**去台账看，不做「悄悄少发几条」。
    """
    lines: list[str] = []

    title = f"**{CARD_TITLE} {summary.batch_no}**".rstrip()
    lines.append(title)
    lines.append(
        f"> 收到 {summary.received_rows} 行医嘱 · 判定 {summary.judgement_rows} 条"
    )
    lines.append(
        f"> 合规 {summary.compliant} · 违规 {summary.violation} · "
        f"不可判定 {summary.undeterminable}"
    )
    lines.append("")

    # -- 第二段：不可判定条目 -------------------------------------------------
    lines.append(f"**不可判定条目（{len(gap_items)} 条）**")
    if not gap_items:
        lines.append("> 无")
    else:
        shown = list(gap_items[: max(0, int(max_gap_items))])
        for index, item in enumerate(shown, start=1):
            cause = f" · 成因：{item.cause}" if item.cause else ""
            lines.append(
                f"> {index}. `{item.missing_field}` · {item.rule_id} · "
                f"影响 {item.affected_prescriptions} 张处方{cause}"
            )
        hidden = len(gap_items) - len(shown)
        if hidden > 0:
            lines.append(f"> 另有 {hidden} 条，详见 {UNDETERMINABLE_LEDGER_NAME}")
    lines.append("")

    # -- 第三段：字段补齐顺序 -------------------------------------------------
    lines.append(f"**字段补齐顺序（{len(priority_items)} 项）**")
    if not priority_items:
        lines.append("> 无")
    else:
        shown = list(priority_items[: max(0, int(max_priority_items))])
        for item in shown:
            route = f" · 承接方：{item.route_to}" if item.route_to else ""
            lines.append(
                f"> {item.priority}. `{item.missing_field}` · "
                f"影响 {item.affected_prescriptions} 张处方 · "
                f"{item.affected_rules} 条规则{route}"
            )
        hidden = len(priority_items) - len(shown)
        if hidden > 0:
            lines.append(f"> 另有 {hidden} 项，详见 {PRIORITY_LEDGER_NAME}")

    return "\n".join(lines)


def build_payload(content: str) -> dict[str, Any]:
    """组装企业微信请求体（spec.md §2.4）。

    结构固定为 ``{"msgtype": "markdown", "markdown": {"content": ...}}``。
    """
    return {
        "msgtype": WECOM_MSGTYPE,
        WECOM_MARKDOWN_KEY: {WECOM_CONTENT_KEY: content},
    }


def _truncate(content: str) -> tuple[str, bool]:
    """正文超长时截断，并**注明**去哪儿看完整清单。

    截断不是静默丢数据：卡片尾部点名两份台账，药师 / 信息科按图索骥。
    """
    if len(content) <= MAX_CONTENT_CHARS:
        return content, False
    notice = (
        f"\n> （卡片正文过长，已截断；完整清单见 {UNDETERMINABLE_LEDGER_NAME} "
        f"与 {PRIORITY_LEDGER_NAME}）"
    )
    keep = MAX_CONTENT_CHARS - len(notice)
    return content[:keep] + notice, True


# ---------------------------------------------------------------------------
# 推送客户端
# ---------------------------------------------------------------------------


class WeComNotifier:
    """企业微信批次卡片推送客户端。

    :param client: 出站客户端（地址校验 + deadline 预算 + 退避重试）。
    :param webhook_url: 企业微信机器人 webhook 地址（``WECOM_WEBHOOK_URL``）。
    :param max_gap_items: 卡片第二段最多列几条不可判定条目。
    :param max_priority_items: 卡片第三段最多列几条字段补齐顺序。
    :param ledger: 状态库（给了则每次推送留一条出站调用痕迹，送达与失败都留）。
    """

    def __init__(
        self,
        client: OutboundClient,
        *,
        webhook_url: str | None = None,
        max_gap_items: int = DEFAULT_MAX_GAP_ITEMS,
        max_priority_items: int = DEFAULT_MAX_PRIORITY_ITEMS,
        ledger: Any | None = None,
    ) -> None:
        self._client = client
        self._webhook_url = (webhook_url or "").strip()
        self._max_gap_items = int(max_gap_items)
        self._max_priority_items = int(max_priority_items)
        self._ledger = ledger
        self._alerts: list[str] = []
        self._outcomes: list[PushOutcome] = []

    # -- 构造 ---------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        client: OutboundClient | None = None,
        max_gap_items: int = DEFAULT_MAX_GAP_ITEMS,
        max_priority_items: int = DEFAULT_MAX_PRIORITY_ITEMS,
        ledger: Any | None = None,
    ) -> "WeComNotifier":
        """按配置面构造（地址缺失**不在这里抛**，由 :meth:`push` 显式报错）。

        构造期不抛是为了让守护进程能先把「推送没配」这件事记成告警继续跑批次——
        推送是递话，不是改业务状态，缺个 webhook 不该让整个守护进程起不来。

        ``ledger``：给了状态库则每次推送留一条出站调用痕迹（送达与失败都留），
        守护进程据此判断「这张卡片发过了没」（断点续跑不重推）。
        """
        return cls(
            client if client is not None else OutboundClient(),
            webhook_url=config.wecom_webhook_url,
            max_gap_items=max_gap_items,
            max_priority_items=max_priority_items,
            ledger=ledger,
        )

    # -- 属性 ---------------------------------------------------------------

    @property
    def webhook_url(self) -> str:
        """推送地址原文（**不要直接打进日志**，用 :func:`mask_webhook_url`）。"""
        return self._webhook_url

    @property
    def masked_webhook_url(self) -> str:
        """推送地址的脱敏形态（凭据位已隐去）。"""
        return mask_webhook_url(self._webhook_url)

    @property
    def alerts(self) -> list[str]:
        """本推送器产生的告警文本（按发生顺序）。"""
        return list(self._alerts)

    @property
    def outcomes(self) -> list[PushOutcome]:
        """历次推送结果（追加式，可供 CLI / 守护进程读取）。"""
        return list(self._outcomes)

    def alert(self, reason: str) -> str:
        """记一条告警（写日志 + 留存文本，供审计导出）。"""
        self._alerts.append(reason)
        logger.warning("[wecom] %s", reason)
        return reason

    # -- 发送 ---------------------------------------------------------------

    def push(
        self,
        summary: BatchCardSummary,
        *,
        gap_items: Sequence[UndeterminableItem] = (),
        priority_items: Sequence[PriorityItem] = (),
        budget: DeadlineBudget | None = None,
    ) -> PushOutcome:
        """推一张批次卡片（**显式抛错**：地址非法 / 推送失败都抛）。

        :raises WeComAddressError: 地址缺失或非法（**请求不发出**）。
        :raises WeComResponseError: HTTP 非 2xx / 业务 ``errcode != 0`` / 响应体不合法。
        """
        url = self._require_url()
        content, truncated = _truncate(
            build_card_content(
                summary,
                gap_items,
                priority_items,
                max_gap_items=self._max_gap_items,
                max_priority_items=self._max_priority_items,
            )
        )
        payload = build_payload(content)

        try:
            response = self._client.post_json(
                url,
                payload,
                headers={"Content-Type": "application/json"},
                budget=budget,
            )
        except OutboundAddressError as exc:
            # 异常消息里带着完整目标地址（含凭据位）→ 引用前先脱敏，
            # 且用 ``from None`` 断掉异常链，免得 traceback 把凭据带出去。
            message = (
                f"企业微信推送地址非法，请求未发出：{redact_urls(str(exc))}"
                f"（地址：{self.masked_webhook_url}）"
            )
            self.alert(message)
            raise WeComAddressError(message) from None
        except (OutboundRequestError, OutboundProtocolError) as exc:
            message = f"企业微信推送失败：{redact_urls(str(exc))}"
            self.alert(message)
            self._record_push(
                summary,
                status=STATUS_PUSH_FAILED,
                retry_count=max(int(getattr(exc, "attempts", 0)) - 1, 0),
                error_code=type(exc).__name__,
                detail=message,
            )
            raise WeComResponseError(
                message,
                attempts=getattr(exc, "attempts", 0),
            ) from None
        except OutboundError as exc:
            message = f"企业微信推送失败：{redact_urls(str(exc))}"
            self.alert(message)
            self._record_push(
                summary,
                status=STATUS_PUSH_FAILED,
                retry_count=max(int(getattr(exc, "attempts", 0)) - 1, 0),
                error_code=type(exc).__name__,
                detail=message,
            )
            raise WeComResponseError(message) from None

        return self._accept(summary, response, content, truncated)

    def notify(
        self,
        summary: BatchCardSummary,
        *,
        gap_items: Sequence[UndeterminableItem] = (),
        priority_items: Sequence[PriorityItem] = (),
        budget: DeadlineBudget | None = None,
    ) -> PushOutcome:
        """推一张批次卡片，**失败只记录告警、不抛异常**（守护进程用这个入口）。

        推送挂了就挂了：判定台账照样落盘、药师照样能回写 HIS。
        但失败**一定留告警**（:attr:`alerts`），绝不静默。
        """
        try:
            return self.push(
                summary,
                gap_items=gap_items,
                priority_items=priority_items,
                budget=budget,
            )
        except WeComError as exc:
            outcome = PushOutcome(
                batch_no=summary.batch_no,
                sent=False,
                reason=str(exc),
            )
            self._outcomes.append(outcome)
            return outcome

    # -- 内部 ---------------------------------------------------------------

    def _require_url(self) -> str:
        """取推送地址；缺失或非法 → 告警 + 显式抛错（请求不发出）。"""
        url = (self._webhook_url or "").strip()
        if not url:
            message = (
                f"{ENV_WECOM_WEBHOOK_URL} 未配置：企业微信推送地址缺失，"
                "请求不发出（凭据位一律从环境变量读，不得静默跳过）。"
            )
            self.alert(message)
            raise WeComAddressError(message)
        try:
            validate_outbound_url(url)
        except OutboundAddressError as exc:
            message = (
                f"企业微信推送地址非法，请求未发出：{redact_urls(str(exc))}"
                f"（地址：{mask_webhook_url(url)}）"
            )
            self.alert(message)
            raise WeComAddressError(message) from None
        return url

    def _record_push(
        self,
        summary: BatchCardSummary,
        *,
        status: str,
        retry_count: int = 0,
        error_code: str = "",
        detail: str = "",
    ) -> None:
        """把一次推送记进出站调用留痕（送达与失败都留）。

        状态库没配时**什么都不做**（不留痕也不能让推送失败）；
        留痕缺失会降级为告警，不静默。
        """
        if self._ledger is None:
            return
        recorder = AuditRecorder(self._ledger)
        recorder.record_push(
            status=status,
            retry_count=retry_count,
            batch_no=summary.batch_no,
            error_code=error_code,
            detail=detail,
        )

    def _accept(
        self,
        summary: BatchCardSummary,
        response: OutboundResponse,
        content: str,
        truncated: bool,
    ) -> PushOutcome:
        """核对企业微信的应答：HTTP 2xx **且** 业务 ``errcode`` 为 0 才算送达。"""
        if not response.ok:
            message = (
                f"企业微信推送被拒：HTTP {response.status_code}"
                f"（目标 {self.masked_webhook_url}）"
            )
            self.alert(message)
            self._record_push(
                summary,
                status=STATUS_PUSH_FAILED,
                retry_count=max(int(getattr(response, "attempts", 1)) - 1, 0),
                error_code=f"HTTP {response.status_code}",
                detail=message,
            )
            raise WeComResponseError(
                message,
                status_code=response.status_code,
                attempts=response.attempts,
            )

        body = response.body
        if not isinstance(body, Mapping):
            message = (
                "企业微信推送响应体不是 JSON 对象：无法确认是否送达"
                f"（HTTP {response.status_code}）"
            )
            self.alert(message)
            self._record_push(
                summary,
                status=STATUS_PUSH_FAILED,
                retry_count=max(int(getattr(response, "attempts", 1)) - 1, 0),
                error_code="响应体不合法",
                detail=message,
            )
            raise WeComResponseError(
                message,
                status_code=response.status_code,
                attempts=response.attempts,
            )

        raw_errcode = body.get("errcode", body.get("code"))
        errcode: int | None
        if raw_errcode is None:
            # 响应里既无 errcode 也无 code：HTTP 2xx 即视为送达（不臆造失败）。
            errcode = None
        else:
            try:
                errcode = int(raw_errcode)
            except (TypeError, ValueError):
                errcode = None

        errmsg = str(body.get("errmsg") or body.get("msg") or "")
        if errcode is not None and errcode != WECOM_SUCCESS_ERRCODE:
            message = (
                f"企业微信推送被拒：errcode={errcode}"
                f"{'（' + errmsg + '）' if errmsg else ''}"
                f"（目标 {self.masked_webhook_url}）"
            )
            self.alert(message)
            self._record_push(
                summary,
                status=STATUS_PUSH_FAILED,
                retry_count=max(int(getattr(response, "attempts", 1)) - 1, 0),
                error_code=f"errcode={errcode}",
                detail=message,
            )
            raise WeComResponseError(
                message,
                status_code=response.status_code,
                attempts=response.attempts,
            )

        self._record_push(
            summary,
            status=STATUS_SENT,
            retry_count=max(int(getattr(response, "attempts", 1)) - 1, 0),
            detail="企业微信批次卡片已送达",
        )

        if truncated:
            self.alert(
                f"企业微信卡片正文超过 {MAX_CONTENT_CHARS} 字，已截断并注明台账位置"
            )

        outcome = PushOutcome(
            batch_no=summary.batch_no,
            sent=True,
            status_code=response.status_code,
            errcode=errcode,
            errmsg=errmsg,
            attempts=response.attempts,
            reason="企业微信推送已送达",
            content_chars=len(content),
            truncated=truncated,
        )
        self._outcomes.append(outcome)
        return outcome


# ---------------------------------------------------------------------------
# 预检（运维通道）
# ---------------------------------------------------------------------------


def preflight(config: Config) -> list[str]:
    """推送链路预检：返回可读的状态行。

    预检**不发任何请求**，只核对 webhook 地址是否配置且合法——
    让信息科在部署当天就能发现「环境变量没配」，而不是等第一批推送失败。
    地址里的凭据位**不显示**。
    """
    lines: list[str] = []
    url = (config.wecom_webhook_url or "").strip()
    if not url:
        lines.append(f"[缺失] {ENV_WECOM_WEBHOOK_URL} 未配置 → 推送不可用（判定与回写不受影响）")
        return lines
    lines.append(f"[已配置] {ENV_WECOM_WEBHOOK_URL} = {mask_webhook_url(url)}（凭据位已隐去）")
    try:
        validate_outbound_url(url)
    except OutboundError as exc:
        lines.append(f"[非法] 企业微信推送地址不合法：{exc}")
    else:
        lines.append("[合法] 企业微信推送地址校验通过")
    return lines


def main(argv: list[str] | None = None) -> int:
    """预检入口：``python -m med_rule_fieldmap.outbound --check-push``。

    退出码：``0`` 齐备、``2`` 地址缺失或非法（**显式失败**，不静默跳过）。
    （走包级入口而不是 ``-m med_rule_fieldmap.outbound.wecom``——后者会因包
    ``__init__`` 已导入该模块而触发 runpy 的重复导入告警。）
    """
    parser = argparse.ArgumentParser(
        prog="python -m med_rule_fieldmap.outbound.wecom",
        description="企业微信批次卡片推送链路预检（不发请求）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做 webhook 地址预检，不发出任何推送",
    )
    parser.parse_args(argv)

    config = Config.from_env()
    lines = preflight(config)
    for line in lines:
        print(line)
    failed = any(line.startswith(("[缺失]", "[非法]")) for line in lines)
    if failed:
        print(
            "预检未通过：企业微信推送不可用（判定与回写主流程不受影响）。",
            file=sys.stderr,
        )
        return 2
    print("预检通过：企业微信推送链路可用。")
    return 0


if __name__ == "__main__":  # pragma: no cover - 预检入口
    raise SystemExit(main())


__all__ = [
    "CARD_TITLE",
    "DEFAULT_MAX_GAP_ITEMS",
    "DEFAULT_MAX_PRIORITY_ITEMS",
    "MAX_CONTENT_CHARS",
    "PRIORITY_LEDGER_NAME",
    "UNDETERMINABLE_LEDGER_NAME",
    "WECOM_CONTENT_KEY",
    "WECOM_MARKDOWN_KEY",
    "WECOM_MSGTYPE",
    "WECOM_SUCCESS_ERRCODE",
    "BatchCardSummary",
    "PriorityItem",
    "PushOutcome",
    "UndeterminableItem",
    "WeComAddressError",
    "WeComError",
    "WeComNotifier",
    "WeComResponseError",
    "build_card_content",
    "build_payload",
    "items_from_gap_entries",
    "items_from_priority_table",
    "main",
    "mask_webhook_url",
    "preflight",
    "redact_urls",
    "summarize_judgements",
]
