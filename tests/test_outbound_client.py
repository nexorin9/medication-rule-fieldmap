"""确认回写 2/5 的验收：出站客户端（地址校验 + deadline 超时预算 + 退避）。

覆盖三条硬判据：

1. **非法地址请求被拒且未发出** —— 地址为空 / 协议不是 http(s) / 主机名不合法 /
   端口越界 / 内嵌凭据，一律 **请求一个字节都不发**，并产生告警；
2. **超时预算在批量回写时生效** —— 一批调用共享一份 deadline 预算，单次连接 /
   读取超时按「剩余预算」分配，前一条慢下来后后面的请求超时**随之收紧**，
   预算用尽则不再发出下一次请求；
3. **重试次数上限** —— 失败按指数退避重试，**最多重试 3 次**（含首次共 4 次请求），
   退避等待逐次拉长并封顶；重试耗尽抛错（**不静默改数据**）。

传输层用 ``httpx.MockTransport`` 替身（测试数据层），产品代码走真实 ``httpx`` 协议。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from med_rule_fieldmap.outbound.client import (
    DEFAULT_BACKOFF_BASE,
    MAX_BACKOFF_SEC,
    MAX_RETRY_ATTEMPTS,
    DeadlineBudget,
    DeadlineExceeded,
    OutboundAddressError,
    OutboundClient,
    OutboundProtocolError,
    OutboundRequestError,
    RetryPolicy,
    validate_outbound_url,
)

# ---------------------------------------------------------------------------
# 夹具：假时钟 / 假等待 / 记录型传输层
# ---------------------------------------------------------------------------


class FakeClock:
    """可推进的单调时钟。"""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> None:
        self.value += delta


class RecordingTransport:
    """记录每次请求，并按脚本返回结果或抛错。"""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.calls: list[httpx.Request] = []
        self._script = list(script or [])

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self._script:
            action = self._script.pop(0)
            if isinstance(action, BaseException):
                raise action
            if isinstance(action, int):
                return httpx.Response(action, json={"code": action})
            return action
        return httpx.Response(200, json={"code": 0, "data": {"prescription_no": "RX-0001"}})

    @property
    def count(self) -> int:
        return len(self.calls)

    def timeouts(self) -> list[dict[str, float]]:
        """每次请求实际拿到的超时配置（connect / read / write / pool）。"""
        return [dict(call.extensions.get("timeout", {})) for call in self.calls]


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def sleeps() -> list[float]:
    return []


@pytest.fixture()
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture()
def client(transport: RecordingTransport, clock: FakeClock, sleeps: list[float]):
    """出站客户端（假时钟 + 假等待 + 记录型传输层）。"""
    return OutboundClient(
        connect_timeout=5.0,
        read_timeout=15.0,
        deadline_sec=30.0,
        sleep=sleeps.append,
        clock=clock,
        transport=httpx.MockTransport(transport.handler),
    )


# ---------------------------------------------------------------------------
# 1. 非法地址：请求被拒且未发出
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("", "目标地址为空"),
        (None, "目标地址为空"),
        ("   ", "目标地址为空"),
        ("ftp://his.example.com/api", "协议只允许"),
        ("file:///etc/passwd", "协议只允许"),
        ("his.example.com/api", "协议只允许"),
        ("http://", "主机名不合法"),
        ("http://his example.com/api", "主机名不合法"),
        ("http://-bad-.example.com/api", "主机名不合法"),
        ("http://his.example.com:0/api", "端口越界"),
        ("http://his.example.com:99999/api", "端口不合法"),
        ("http://user:secret@his.example.com/api", "不得内嵌凭据"),
    ],
)
def test_非法地址请求被拒且未发出(client, transport, url, reason):
    """非法地址 → 抛 OutboundAddressError，传输层一次都没被调用。"""
    with pytest.raises(OutboundAddressError) as excinfo:
        client.post_json(url, {"batch_no": "BATCH-001"})

    assert reason in str(excinfo.value)
    assert transport.count == 0, "地址非法时请求不得发出"


def test_非法地址产生告警(client, transport, caplog):
    """地址非法 → 产生告警（日志 + 客户端内留存文本），供审计导出。"""
    with caplog.at_level(logging.WARNING, logger="med_rule_fieldmap.outbound"):
        with pytest.raises(OutboundAddressError):
            client.post_json("ftp://his.example.com/api", {})

    assert client.alerts, "非法地址必须产生告警"
    assert "请求未发出" in client.alerts[0]
    assert any("目标地址非法" in record.message for record in caplog.records)
    assert transport.count == 0


def test_合法地址通过校验():
    """合法地址 → 拆出协议 / 主机 / 端口 / 路径。"""
    validated = validate_outbound_url("https://his.example.com:8443/api/v1/prescription/review")
    assert validated.scheme == "https"
    assert validated.host == "his.example.com"
    assert validated.port == 8443
    assert validated.path == "/api/v1/prescription/review"

    assert validate_outbound_url("http://10.0.0.7:8080/api").host == "10.0.0.7"
    assert validate_outbound_url("http://[::1]:8080/api").host == "::1"
    assert validate_outbound_url("http://localhost:9000/api").host == "localhost"


def test_合法地址确实发出请求(client, transport):
    """合法地址 → 请求发出，方法 / 路径 / 请求体都对。"""
    response = client.post_json(
        "https://his.example.com/api/v1/prescription/review",
        {"batch_no": "BATCH-001", "prescription_no": "RX-0001"},
    )

    assert transport.count == 1
    request = transport.calls[0]
    assert request.method == "POST"
    assert str(request.url) == "https://his.example.com/api/v1/prescription/review"
    assert response.status_code == 200
    assert response.body["data"]["prescription_no"] == "RX-0001"
    assert response.attempts == 1
    assert response.ok


# ---------------------------------------------------------------------------
# 2. deadline 超时预算：批量回写时生效
# ---------------------------------------------------------------------------


def test_超时预算按剩余时间分配(client, transport, clock):
    """批量回写共享一份预算：前一条慢下来后，后一条的超时被收紧。"""
    budget = client.new_budget(10.0)

    client.post_json("https://his.example.com/api", {"n": 1}, budget=budget)
    first = transport.timeouts()[0]
    assert first["connect"] == pytest.approx(5.0)   # 未超预算 → 用固定上限
    assert first["read"] == pytest.approx(10.0)     # 剩余 10s 压过 15s 上限

    # 第一条请求耗掉了 8 秒预算
    clock.advance(8.0)

    client.post_json("https://his.example.com/api", {"n": 2}, budget=budget)
    second = transport.timeouts()[1]
    assert second["connect"] == pytest.approx(2.0)  # 剩余 2s → 压到 2s
    assert second["read"] == pytest.approx(2.0)


def test_预算用尽不再发出请求(client, transport, clock):
    """预算用尽 → 抛 DeadlineExceeded，且**不再发出**下一次请求。"""
    budget = client.new_budget(4.0)

    client.post_json("https://his.example.com/api", {"n": 1}, budget=budget)
    assert transport.count == 1

    clock.advance(10.0)  # 预算耗尽

    with pytest.raises(DeadlineExceeded):
        client.post_json("https://his.example.com/api", {"n": 2}, budget=budget)

    assert transport.count == 1, "预算用尽后不得再发请求"


def test_默认预算来自客户端配置(clock, transport):
    """没显式给预算时，用客户端配置的 deadline_sec。"""
    client = OutboundClient(
        connect_timeout=5.0,
        read_timeout=15.0,
        deadline_sec=6.0,
        sleep=lambda _seconds: None,
        clock=clock,
        transport=httpx.MockTransport(transport.handler),
    )
    budget = client.new_budget()
    assert budget.remaining() == pytest.approx(6.0)

    client.post_json("https://his.example.com/api", {}, budget=budget)
    assert transport.timeouts()[0]["connect"] == pytest.approx(5.0)


def test_预算对象_剩余时间与到期判定(clock):
    """DeadlineBudget：剩余时间随时间减少，过期后 attempt_timeout 抛错。"""
    budget = DeadlineBudget(5.0, clock=clock)
    assert budget.remaining() == pytest.approx(5.0)
    assert budget.attempt_timeout(30.0) == pytest.approx(5.0)
    assert not budget.expired

    clock.advance(4.0)
    assert budget.remaining() == pytest.approx(1.0)
    assert budget.attempt_timeout(30.0) == pytest.approx(1.0)

    clock.advance(2.0)
    assert budget.expired
    with pytest.raises(DeadlineExceeded):
        budget.attempt_timeout(1.0)


# ---------------------------------------------------------------------------
# 3. 退避重试：次数上限 + 退避节奏
# ---------------------------------------------------------------------------


def test_重试次数上限_连接失败(clock, sleeps):
    """连接一直失败 → 重试 3 次（连首次共 4 次请求）后抛错。"""
    transport = RecordingTransport([httpx.ConnectError("连接被拒")] * 6)
    client = OutboundClient(
        sleep=sleeps.append,
        clock=clock,
        transport=httpx.MockTransport(transport.handler),
    )

    with pytest.raises(OutboundRequestError) as excinfo:
        client.post_json("https://his.example.com/api", {})

    assert transport.count == MAX_RETRY_ATTEMPTS + 1 == 4
    assert excinfo.value.attempts == 4
    assert "重试上限 3 次" in str(excinfo.value)
    assert len(sleeps) == 3, "退避等待次数 = 重试次数"


def test_重试次数上限_服务端5xx(clock, sleeps):
    """服务端一直 500 → 同样重试 3 次后抛错，且**失败不静默**。"""
    transport = RecordingTransport([500] * 6)
    client = OutboundClient(
        sleep=sleeps.append,
        clock=clock,
        transport=httpx.MockTransport(transport.handler),
    )

    with pytest.raises(OutboundRequestError) as excinfo:
        client.post_json("https://his.example.com/api", {})

    assert transport.count == 4
    assert excinfo.value.last_status == 500
    assert excinfo.value.attempts == 4


def test_退避按指数拉长并封顶(clock, sleeps):
    """退避等待逐次翻倍，并在上限处封顶。"""
    transport = RecordingTransport([httpx.ConnectError("连接被拒")] * 6)
    client = OutboundClient(
        sleep=sleeps.append,
        clock=clock,
        transport=httpx.MockTransport(transport.handler),
    )

    with pytest.raises(OutboundRequestError):
        client.post_json("https://his.example.com/api", {})

    assert sleeps == [
        pytest.approx(DEFAULT_BACKOFF_BASE),
        pytest.approx(DEFAULT_BACKOFF_BASE * 2),
        pytest.approx(DEFAULT_BACKOFF_BASE * 4),
    ]
    assert max(sleeps) < MAX_BACKOFF_SEC

    policy = RetryPolicy()
    assert policy.backoff_delay(1) == pytest.approx(DEFAULT_BACKOFF_BASE)
    assert policy.backoff_delay(20) == pytest.approx(MAX_BACKOFF_SEC)


def test_重试后成功(clock, sleeps, transport):
    """第一次 500、第二次 200 → 成功返回，并记下重试次数。"""
    transport._script.extend(
        [500, httpx.Response(200, json={"code": 0, "data": {"prescription_no": "RX-0001"}})]
    )
    client = OutboundClient(
        sleep=sleeps.append,
        clock=clock,
        transport=httpx.MockTransport(transport.handler),
    )

    response = client.post_json("https://his.example.com/api", {})

    assert transport.count == 2
    assert response.ok
    assert response.attempts == 2
    assert response.retried
    assert len(sleeps) == 1


def test_非重试状态码不重试(clock, sleeps, transport):
    """400 属请求本身有问题 → 原样返回，不重试。"""
    transport._script.extend([httpx.Response(400, json={"code": 400, "message": "字段缺失"})])
    client = OutboundClient(
        sleep=sleeps.append,
        clock=clock,
        transport=httpx.MockTransport(transport.handler),
    )

    response = client.post_json("https://his.example.com/api", {})

    assert transport.count == 1
    assert response.status_code == 400
    assert not response.ok
    assert not response.retried
    assert sleeps == []


def test_响应体不符合协议显式报错(clock, transport):
    """声明要 JSON 却拿到非 JSON → 显式报错，不当成成功。"""
    transport._script.extend([httpx.Response(200, text="<html>网关错误页</html>")])
    client = OutboundClient(
        sleep=lambda _seconds: None,
        clock=clock,
        transport=httpx.MockTransport(transport.handler),
    )

    with pytest.raises(OutboundProtocolError):
        client.post_json("https://his.example.com/api", {})


def test_退避等待不越过剩余预算(clock, sleeps):
    """退避等待不得超过剩余预算（预算只剩一点点时不再干等）。"""
    transport = RecordingTransport([httpx.ConnectError("连接被拒")] * 6)
    client = OutboundClient(
        sleep=sleeps.append,
        clock=clock,
        transport=httpx.MockTransport(transport.handler),
    )
    budget = client.new_budget(0.05)

    with pytest.raises((OutboundRequestError, DeadlineExceeded)):
        client.post_json("https://his.example.com/api", {}, budget=budget)

    assert sleeps, "第一次失败后应当退避等待"
    assert sleeps[0] <= 0.05 + 1e-9, "退避等待不得超过剩余预算"
