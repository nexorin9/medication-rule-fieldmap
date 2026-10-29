"""出站客户端：目标地址校验 + deadline 超时预算 + 指数退避重试。

本模块是本产品**唯一**的出站通道底座（HIS 审方复核回写、企业微信推送都走这里），
因此自带三道防线，任何一道不过都**显式失败**、绝不带着问题往下走：

1. **地址校验**——发请求前先校验目标地址（非空、协议为 http / https、主机名合法、
   端口合法、不得内嵌凭据）。校验不过 → **请求一个字节都不发**，并产生告警。
2. **整体 deadline 预算**——一次批量回写共享一份预算，单次连接 / 读取超时按
   「剩余预算」分配；预算用尽 → 立刻停手，不再发出下一次请求。
3. **指数退避重试**——失败（超时 / 连接错误 / 5xx / 429）按指数退避重试，
   **重试次数上限 3 次**（即最多发出 4 次请求：首次 + 3 次重试）。

**源产品能力对应**（`github_ref/baserow/backend/src/advocate/connection.py`）：

* ``validating_create_connection()`` —— 发请求**先校验目标地址**，校验不过显式抛错，
  绝不带着不合法目标往下走。本轮**复用改写**为「目标 URL 校验」：把 socket 层的
  主机黑名单 / 地址可达性判断，换成出站地址的三项硬约束（非空 / 协议 / 主机名合法）
  + 「不得内嵌凭据」（凭据一律从环境变量读，见 :mod:`med_rule_fieldmap.config`）。
* ``attempt_timeout(timeout, deadline)`` —— 单次连接超时压到「剩余 deadline」，
  deadline 过了直接抛 ``SocketTimeout``。本轮**复用改写**为
  :class:`DeadlineBudget`：源是「一次连接尝试」的预算，本轮是「一批出站调用」的
  预算——批量回写时每一条请求都从同一份预算里取剩余时间，剩余时间不会因为
  前一条请求慢而被忽略。

**不做**什么：本模块只管「怎么把请求安全地发出去」，
不关心请求体字段口径（spec.md §2.3 由 ``outbound/his_review.py`` 负责），
也不关心幂等（:mod:`med_rule_fieldmap.outbound.idempotency` 负责）。
"""

from __future__ import annotations

import ipaddress
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("med_rule_fieldmap.outbound")

# ---------------------------------------------------------------------------
# 口径常量（改这里就能改全套出站行为；不在业务层散落硬编码）
# ---------------------------------------------------------------------------

#: 单次连接超时上限（秒）
DEFAULT_CONNECT_TIMEOUT = 5.0

#: 单次读取超时上限（秒）
DEFAULT_READ_TIMEOUT = 15.0

#: 一批出站调用的整体 deadline（秒）
DEFAULT_DEADLINE_SEC = 30.0

#: 重试次数上限（**不含**首次请求，即最多发出 4 次）
MAX_RETRY_ATTEMPTS = 3

#: 指数退避基数（秒）：第 1 次退避 = 基数，第 2 次 = 基数 × 2，以此类推
DEFAULT_BACKOFF_BASE = 0.2

#: 指数退避上限（秒）
MAX_BACKOFF_SEC = 5.0

#: 触发重试的响应码（5xx 与 429）
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: 允许的出站协议
ALLOWED_SCHEMES = ("http", "https")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class OutboundError(RuntimeError):
    """出站通道的基类异常。"""


class OutboundAddressError(OutboundError):
    """目标地址非法：**请求不发出**，并产生告警。

    与「发出去但失败」区分开——地址不合法属于配置错误，重试没有意义。
    """


class DeadlineExceeded(OutboundError):
    """整体 deadline 已用尽：不再发出下一次请求。"""


class OutboundRequestError(OutboundError):
    """重试耗尽仍未成功（超时 / 连接错误 / 5xx）。

    :attr:`attempts` 是实际发出的请求次数，:attr:`last_status` 是最后一次的响应码
    （连接类失败时为 ``None``）。
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        last_status: int | None = None,
        last_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_status = last_status
        self.last_error = last_error


class OutboundProtocolError(OutboundError):
    """响应体不符合预期协议（如声明要 JSON 却拿到非 JSON）。

    这类失败**不重试**——重发同一个坏请求只会再坏一次。
    """


# ---------------------------------------------------------------------------
# 目标地址校验
# ---------------------------------------------------------------------------

#: 主机名标签（允许下划线：院内主机名常见 ``his_review_svc`` 这类写法；
#: 但拒绝空格、控制字符、空标签、以 ``-`` 开头或结尾的标签）
_LABEL_RE = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?$")


def _is_valid_host(host: str) -> bool:
    """主机名是否合法（IPv4 / IPv6 字面量或域名标签序列）。"""
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if len(host) > 253:
        return False
    labels = host.rstrip(".").split(".")
    if not labels:
        return False
    return all(_LABEL_RE.match(label) for label in labels)


@dataclass(frozen=True)
class ValidatedUrl:
    """一份已通过校验的出站地址。"""

    url: str
    scheme: str
    host: str
    port: int | None
    path: str

    def __str__(self) -> str:  # pragma: no cover - 仅用于展示
        return self.url


def validate_outbound_url(url: str | None) -> ValidatedUrl:
    """校验出站目标地址。

    :param url: 目标地址（通常是 ``{HIS_BASE_URL}/api/v1/prescription/review``）。
    :raises OutboundAddressError: 地址为空 / 协议不是 http(s) / 主机名不合法 /
        端口越界 / 地址内嵌凭据。
    """
    if url is None or not str(url).strip():
        raise OutboundAddressError(
            "目标地址为空：出站请求不发出（地址与凭据一律从环境变量读，不得静默跳过）"
        )
    text = str(url).strip()
    try:
        parts = urlsplit(text)
    except ValueError as exc:  # pragma: no cover - urlsplit 极少抛错
        raise OutboundAddressError(f"目标地址无法解析：{text!r}（{exc}）") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise OutboundAddressError(
            f"目标地址协议只允许 {' / '.join(ALLOWED_SCHEMES)}，当前为 "
            f"{scheme or '（空）'}：{text}"
        )

    if parts.username or parts.password:
        raise OutboundAddressError(
            "目标地址不得内嵌凭据（凭据一律从环境变量读，避免落进日志与告警文本）"
        )

    host = parts.hostname or ""
    if not _is_valid_host(host):
        raise OutboundAddressError(f"目标地址主机名不合法：{host!r}")

    try:
        port = parts.port
    except ValueError as exc:
        raise OutboundAddressError(f"目标地址端口不合法：{text!r}（{exc}）") from exc
    if port is not None and not (1 <= port <= 65535):
        raise OutboundAddressError(f"目标地址端口越界（1–65535）：{port}")

    return ValidatedUrl(
        url=text,
        scheme=scheme,
        host=host,
        port=port,
        path=parts.path or "/",
    )


# ---------------------------------------------------------------------------
# deadline 预算
# ---------------------------------------------------------------------------


class DeadlineBudget:
    """一批出站调用共享的整体 deadline 预算。

    对应源产品 ``attempt_timeout(timeout, deadline)``：单次超时被**压到剩余预算**，
    预算耗尽即抛错不再等待。差别在于本轮是**批次级**预算——批量回写时每一条
    请求都从同一份预算取剩余时间，剩余时间不会因为前一条慢而被忽略。

    :param deadline_sec: 预算秒数；``None`` 表示不限（退化为固定超时）。
    :param clock: 单调时钟（测试可注入假时钟）。
    """

    def __init__(
        self,
        deadline_sec: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._deadline: float | None = None
        if deadline_sec is not None:
            self._deadline = clock() + float(deadline_sec)

    @property
    def deadline(self) -> float | None:
        """预算截止时刻（``time.monotonic()`` 口径）。"""
        return self._deadline

    @property
    def expired(self) -> bool:
        """预算是否已用尽。"""
        return self.remaining() <= 0

    def remaining(self) -> float:
        """剩余预算秒数；不限预算时为 ``math.inf``。"""
        if self._deadline is None:
            return math.inf
        return self._deadline - self._clock()

    def attempt_timeout(self, timeout: float) -> float:
        """把单次超时压到「剩余预算」。

        :raises DeadlineExceeded: 预算已用尽。
        """
        remaining = self.remaining()
        if remaining <= 0:
            raise DeadlineExceeded(
                f"出站 deadline 已用尽（剩余 {remaining:.3f}s）：不再发出本次请求"
            )
        return timeout if timeout < remaining else remaining


# ---------------------------------------------------------------------------
# 重试策略
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """指数退避重试策略。

    :param max_retries: 重试次数上限（**不含**首次请求）；``3`` 表示最多发 4 次。
    :param backoff_base: 退避基数（秒）。
    :param backoff_cap: 退避上限（秒）。
    :param retry_statuses: 触发重试的响应码。
    """

    max_retries: int = MAX_RETRY_ATTEMPTS
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_cap: float = MAX_BACKOFF_SEC
    retry_statuses: frozenset[int] = RETRYABLE_STATUS

    def backoff_delay(self, retry_index: int) -> float:
        """第 ``retry_index`` 次重试前的等待秒数（1 起算，指数增长并封顶）。"""
        if retry_index <= 0:
            return 0.0
        return min(self.backoff_base * (2 ** (retry_index - 1)), self.backoff_cap)


# ---------------------------------------------------------------------------
# 响应
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboundResponse:
    """一次成功发��的出站响应。"""

    url: str
    status_code: int
    body: Any
    text: str
    headers: dict[str, str]
    attempts: int
    elapsed_sec: float

    @property
    def ok(self) -> bool:
        """响应码是否 2xx。"""
        return 200 <= self.status_code < 300

    @property
    def retried(self) -> bool:
        """是否发生过重试。"""
        return self.attempts > 1


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class OutboundClient:
    """出站 httpx 客户端封装。

    * 发请求前**先校验目标地址**，不合法 → 拒绝发出 + 告警；
    * 单次连接 / 读取超时按**剩余 deadline** 分配；
    * 失败按**指数退避**重试，重试次数上限 **3 次**（最多发出 4 次请求）。

    :param connect_timeout: 单次连接超时上限（秒）。
    :param read_timeout: 单次读取超时上限（秒）。
    :param deadline_sec: 每次调用默认的整体预算（秒）。
    :param retry_policy: 重试策略。
    :param sleep: 退避等待函数（测试可注入假等待，避免真实 sleep）。
    :param clock: 单调时钟（测试可注入假时钟）。
    :param client: 已构造好的 ``httpx.Client``（给了就不再自建）。
    :param transport: ``httpx`` 传输层（测试替身用）。
    """

    def __init__(
        self,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        deadline_sec: float | None = DEFAULT_DEADLINE_SEC,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.deadline_sec = deadline_sec
        self.retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._client = client
        self._owns_client = client is None
        self._transport = transport
        self._logger = logger_ or logger
        self._alerts: list[str] = []

    # -- 基础属性 -----------------------------------------------------------

    @property
    def alerts(self) -> list[str]:
        """本客户端产生的告警文本（按发生顺序）。"""
        return list(self._alerts)

    @property
    def http(self) -> httpx.Client:
        """底层 ``httpx.Client``（懒创建）。"""
        if self._client is None:
            self._client = httpx.Client(transport=self._transport)
            self._owns_client = True
        return self._client

    def new_budget(self, deadline_sec: float | None = None) -> DeadlineBudget:
        """开一份新的 deadline 预算（批量回写时共享同一份）。"""
        seconds = self.deadline_sec if deadline_sec is None else deadline_sec
        return DeadlineBudget(seconds, clock=self._clock)

    def close(self) -> None:
        """关闭底层连接池（自建时才关）。"""
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "OutboundClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 告警 ---------------------------------------------------------------

    def alert(self, reason: str) -> str:
        """记一条告警（写日志 + 留存文本，供审计导出）。"""
        self._alerts.append(reason)
        self._logger.warning("[outbound] %s", reason)
        return reason

    # -- 发送 ---------------------------------------------------------------

    def post_json(
        self,
        url: str | None,
        payload: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        budget: DeadlineBudget | None = None,
        expect_json: bool = True,
    ) -> OutboundResponse:
        """发一次 JSON POST。

        :raises OutboundAddressError: 地址非法（**请求不发出**）。
        :raises DeadlineExceeded: 预算已用尽。
        :raises OutboundRequestError: 重试耗尽仍未成功。
        :raises OutboundProtocolError: 响应体不符合约定协议。
        """
        return self.request(
            "POST",
            url,
            json=dict(payload or {}),
            headers=headers,
            budget=budget,
            expect_json=expect_json,
        )

    def request(
        self,
        method: str,
        url: str | None,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        budget: DeadlineBudget | None = None,
        expect_json: bool = True,
    ) -> OutboundResponse:
        """发一次请求（带地址校验、deadline 预算与退避重试）。

        **返回 vs 抛错**的口径：非重试类响应码（如 4xx）**原样返回**给调用方判断；
        重试类失败（超时 / 连接错误 / 5xx / 429）在重试耗尽后抛
        :class:`OutboundRequestError`。
        """
        validated = self._validate(url)
        active_budget = budget if budget is not None else self.new_budget()

        attempts = 0
        last_error: BaseException | None = None
        last_status: int | None = None
        failure_reason = ""
        started = self._clock()

        while True:
            attempts += 1
            timeout = self._timeouts(active_budget)
            try:
                response = self.http.request(
                    method,
                    validated.url,
                    json=json,
                    headers=dict(headers) if headers else None,
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                last_status = None
                failure_reason = f"请求超时（{type(exc).__name__}）"
            except httpx.TransportError as exc:
                last_error = exc
                last_status = None
                failure_reason = f"连接失败（{type(exc).__name__}）"
            else:
                if response.status_code in self.retry_policy.retry_statuses:
                    last_status = response.status_code
                    last_error = None
                    failure_reason = f"服务端返回 HTTP {response.status_code}"
                else:
                    return self._build_response(
                        validated,
                        response,
                        attempts=attempts,
                        elapsed=self._clock() - started,
                        expect_json=expect_json,
                    )

            # 到这里说明本次尝试失败 —— 判断是否还能退避重试
            if attempts > self.retry_policy.max_retries:
                raise OutboundRequestError(
                    f"出站请求失败且重试已耗尽：{failure_reason}"
                    f"（发出 {attempts} 次，重试上限 {self.retry_policy.max_retries} 次，"
                    f"目标 {validated.url}）",
                    attempts=attempts,
                    last_status=last_status,
                    last_error=last_error,
                )

            remaining = active_budget.remaining()
            if remaining <= 0:
                raise DeadlineExceeded(
                    f"出站 deadline 已用尽：{failure_reason}（已发出 {attempts} 次，"
                    f"目标 {validated.url}）"
                )

            delay = self.retry_policy.backoff_delay(attempts)
            self._sleep(min(delay, remaining))

    # -- 内部 ---------------------------------------------------------------

    def _validate(self, url: str | None) -> ValidatedUrl:
        """校验目标地址；不合法 → 告警并抛错（**请求不发出**）。"""
        try:
            return validate_outbound_url(url)
        except OutboundAddressError as exc:
            self.alert(f"目标地址非法，请求未发出：{exc}")
            raise

    def _timeouts(self, budget: DeadlineBudget) -> httpx.Timeout:
        """按剩余预算分配单次连接 / 读取超时。"""
        connect = budget.attempt_timeout(self.connect_timeout)
        read = budget.attempt_timeout(self.read_timeout)
        return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)

    def _build_response(
        self,
        validated: ValidatedUrl,
        response: httpx.Response,
        *,
        attempts: int,
        elapsed: float,
        expect_json: bool,
    ) -> OutboundResponse:
        """把 httpx 响应折成本模块的响应对象。"""
        text = response.text
        body: Any = None
        try:
            body = response.json()
        except ValueError as exc:
            if expect_json:
                raise OutboundProtocolError(
                    f"响应体不符合约定协议（期望 JSON，实际 HTTP {response.status_code}）："
                    f"{text[:200]!r}"
                ) from exc
        return OutboundResponse(
            url=validated.url,
            status_code=response.status_code,
            body=body,
            text=text,
            headers={key: value for key, value in response.headers.items()},
            attempts=attempts,
            elapsed_sec=elapsed,
        )


__all__ = [
    "ALLOWED_SCHEMES",
    "DEFAULT_BACKOFF_BASE",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_DEADLINE_SEC",
    "DEFAULT_READ_TIMEOUT",
    "MAX_BACKOFF_SEC",
    "MAX_RETRY_ATTEMPTS",
    "RETRYABLE_STATUS",
    "DeadlineBudget",
    "DeadlineExceeded",
    "OutboundAddressError",
    "OutboundClient",
    "OutboundError",
    "OutboundProtocolError",
    "OutboundRequestError",
    "OutboundResponse",
    "RetryPolicy",
    "ValidatedUrl",
    "validate_outbound_url",
]
