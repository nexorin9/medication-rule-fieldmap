"""企业微信批次卡片推送测试。

**替身说明**：本测试用**本地真实 HTTP 服务**（``http.server``）充当企业微信
机器人 webhook 与 HIS 审方复核接口——传输层是真 ``httpx``、真 TCP、真 HTTP 报文；
被测的**产品代码**走的是 spec.md §2.4 的真实协议（POST + ``{"msgtype":"markdown",
"markdown":{"content":…}}``）。替身只负责「按剧本回一个响应」，不替代任何产品逻辑。

覆盖三条硬边界：

1. **推送体结构** —— 请求方法 / 路径 / Content-Type / 请求体三段内容（批次摘要、
   不可判定条目摘要、字段补齐顺序摘要）逐项断言；
2. **推送失败不中断主流程** —— 推送挂了只记告警，判定台账、判定状态与 HIS 回写
   **一个字节都不受影响**；
3. **地址缺失或非法即显式报错并告警** —— 请求**一个字节都不发**，且告警里
   **不出现 webhook 地址里的凭据位**。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from med_rule_fieldmap.config import ENV_WECOM_WEBHOOK_URL, Config
from med_rule_fieldmap.models import Conclusion, JudgementRow, JudgementState
from med_rule_fieldmap.outbound.client import OutboundClient
from med_rule_fieldmap.outbound.his_review import HisReviewWriter, ReviewRequest
from med_rule_fieldmap.outbound.wecom import (
    MAX_CONTENT_CHARS,
    WECOM_CONTENT_KEY,
    WECOM_MARKDOWN_KEY,
    WECOM_MSGTYPE,
    BatchCardSummary,
    PriorityItem,
    UndeterminableItem,
    WeComAddressError,
    WeComNotifier,
    WeComResponseError,
    build_card_content,
    build_payload,
    items_from_gap_entries,
    items_from_priority_table,
    mask_webhook_url,
    summarize_judgements,
)
from med_rule_fieldmap.store import Store

# ---------------------------------------------------------------------------
# 本地 HTTP 替身（真实 HTTP 服务，按剧本回响应）
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """记录请求并按剧本回响应。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        self.server.record(  # type: ignore[attr-defined]
            {
                "method": "POST",
                "path": self.path,
                "headers": {key: value for key, value in self.headers.items()},
                "raw": raw,
                "json": parsed,
            }
        )
        status, payload = self.server.next_response(parsed)  # type: ignore[attr-defined]
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:  # 静音默认访问日志
        return


class _HttpDouble:
    """本地 HTTP 替身（企业微信 webhook / HIS 接口共用）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._responses: list[tuple[int, str]] = []
        self._lock = threading.Lock()
        handler = type("_BoundHandler", (_Handler,), {})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.record = self._record  # type: ignore[attr-defined]
        self._server.next_response = self._next_response  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def webhook_url(self) -> str:
        """企业微信机器人形态的地址（query 段里的 key 就是凭据）。"""
        return f"{self.base_url}/cgi-bin/webhook/send?key=SECRET-WEBHOOK-KEY"

    def script(self, *responses: tuple[int, str]) -> None:
        """排定响应剧本（依次消费；用尽后重复最后一条）。"""
        with self._lock:
            self._responses = list(responses)

    def _record(self, payload: dict) -> None:
        with self._lock:
            self.requests.append(payload)

    def _next_response(self, body: dict | None = None) -> tuple[int, str]:
        with self._lock:
            if not self._responses:
                return 200, json.dumps({"errcode": 0, "errmsg": "ok"})
            if len(self._responses) == 1:
                return self._responses[0]
            return self._responses.pop(0)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def http_double():
    double = _HttpDouble()
    try:
        yield double
    finally:
        double.close()


@pytest.fixture()
def client():
    """出站客户端（退避等待用假 sleep，测试不真等）。"""
    return OutboundClient(sleep=lambda _seconds: None)


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state" / "mrfm.sqlite3")


# ---------------------------------------------------------------------------
# 夹具数据
# ---------------------------------------------------------------------------


def _summary() -> BatchCardSummary:
    return BatchCardSummary(
        batch_no="DEMO-BATCH-001",
        received_rows=26,
        judgement_rows=192,
        compliant=170,
        violation=11,
        undeterminable=11,
    )


def _gap_items() -> list[UndeterminableItem]:
    return [
        UndeterminableItem(
            missing_field="rx_type",
            rule_id="RX-LEGAL-MA-OUT-DAYS",
            cause="接口未回传",
            affected_prescriptions=3,
        ),
        UndeterminableItem(
            missing_field="days_qty",
            rule_id="RX-LEGAL-MA-CANCER-OUT-DAYS",
            cause="医嘱未填写",
            affected_prescriptions=2,
        ),
    ]


def _priority_items() -> list[PriorityItem]:
    return [
        PriorityItem(
            priority=1,
            missing_field="rx_type",
            affected_prescriptions=3,
            affected_rules=8,
            route_to="信息科接口岗",
        ),
        PriorityItem(
            priority=2,
            missing_field="days_qty",
            affected_prescriptions=2,
            affected_rules=3,
            route_to="退回医师补写",
        ),
    ]


def _judgement(index: int = 1) -> JudgementRow:
    return JudgementRow(
        batch_no="DEMO-BATCH-001",
        prescription_no=f"RX-{index:04d}",
        rule_id="RX-LEGAL-MA-OUT-DAYS",
        rule_version="2026.10",
        conclusion=Conclusion.VIOLATION,
        severity="严重不合理用药",
        reason="规则 RX-LEGAL-MA-OUT-DAYS 判定：违规。",
        basis="处方审核规则体系 §3",
        state=JudgementState.DECIDED,
    )


# ---------------------------------------------------------------------------
# ① 推送请求体结构
# ---------------------------------------------------------------------------


def test_push_request_body_structure(http_double, client):
    """请求体结构：msgtype=markdown + markdown.content，三段内容齐全。"""
    notifier = WeComNotifier(client, webhook_url=http_double.webhook_url)

    outcome = notifier.push(_summary(), gap_items=_gap_items(), priority_items=_priority_items())

    assert outcome.sent is True
    assert outcome.status_code == 200
    assert outcome.errcode == 0
    assert outcome.attempts == 1

    assert len(http_double.requests) == 1
    request = http_double.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/cgi-bin/webhook/send?key=SECRET-WEBHOOK-KEY"
    assert request["headers"].get("Content-Type") == "application/json"

    body = request["json"]
    # 请求体字段全集：只有 msgtype 与 markdown 两项
    assert tuple(body.keys()) == ("msgtype", WECOM_MARKDOWN_KEY)
    assert body["msgtype"] == WECOM_MSGTYPE
    assert tuple(body[WECOM_MARKDOWN_KEY].keys()) == (WECOM_CONTENT_KEY,)

    content = body[WECOM_MARKDOWN_KEY][WECOM_CONTENT_KEY]
    assert isinstance(content, str) and content

    # 第一段：批次摘要（三态计数齐全）
    assert "DEMO-BATCH-001" in content
    assert "收到 26 行医嘱" in content
    assert "判定 192 条" in content
    assert "合规 170" in content
    assert "违规 11" in content
    assert "不可判定 11" in content

    # 第二段：不可判定条目摘要（点名缺失字段与卡住的规则号）
    assert "**不可判定条目（2 条）**" in content
    assert "`rx_type`" in content
    assert "RX-LEGAL-MA-OUT-DAYS" in content
    assert "影响 3 张处方" in content
    assert "成因：接口未回传" in content

    # 第三段：字段补齐顺序摘要
    assert "**字段补齐顺序（2 项）**" in content
    assert "承接方：信息科接口岗" in content


def test_push_payload_helper_shape():
    """请求体组装函数：结构固定，不多不少。"""
    payload = build_payload("hello")
    assert payload == {"msgtype": "markdown", "markdown": {"content": "hello"}}


def test_card_content_has_three_sections():
    """卡片正文固定三段；没有缺口项时写「无」而不是留空。"""
    content = build_card_content(_summary())
    assert "**不可判定条目（0 条）**" in content
    assert "**字段补齐顺序（0 项）**" in content
    assert content.count("**") >= 6  # 三段标题各一对星号


def test_card_content_is_deterministic():
    """同样的输入必得同样的卡片。"""
    first = build_card_content(_summary(), _gap_items(), _priority_items())
    second = build_card_content(_summary(), _gap_items(), _priority_items())
    assert first == second


def test_card_content_caps_items_and_points_to_ledgers(http_double, client):
    """条目超上限时截断并点名台账位置，不做「悄悄少发几条」。"""
    gap_items = [
        UndeterminableItem(
            missing_field=f"field_{index:03d}",
            rule_id=f"RX-RULE-{index:03d}",
            cause="接口未回传",
            affected_prescriptions=index,
        )
        for index in range(200)
    ]
    notifier = WeComNotifier(client, webhook_url=http_double.webhook_url, max_gap_items=3)

    notifier.push(_summary(), gap_items=gap_items)

    content = http_double.requests[0]["json"][WECOM_MARKDOWN_KEY][WECOM_CONTENT_KEY]
    assert "另有 197 条" in content
    assert "out/undeterminable_ledger.csv" in content
    # 只列 3 条
    assert content.count("成因：接口未回传") == 3


def test_push_truncates_overlong_content(http_double, client):
    """正文超长时截断到上限内，并注明完整清单在哪。"""
    gap_items = [
        UndeterminableItem(
            missing_field=f"field_{index:03d}",
            rule_id=f"RX-RULE-{index:03d}",
            cause="接口未回传",
            affected_prescriptions=index,
        )
        for index in range(200)
    ]
    notifier = WeComNotifier(client, webhook_url=http_double.webhook_url, max_gap_items=200)

    outcome = notifier.push(_summary(), gap_items=gap_items)

    content = http_double.requests[0]["json"][WECOM_MARKDOWN_KEY][WECOM_CONTENT_KEY]
    assert len(content) <= MAX_CONTENT_CHARS
    assert outcome.truncated is True
    assert "已截断" in content
    assert "out/field_gap_priority.csv" in content
    # 截断也要留告警，不静默
    assert any("截断" in alert for alert in notifier.alerts)


def test_gap_items_sorted_by_impact():
    """不可判定条目按受影响处方数降序排，规则号只作稳定兜底。"""
    items = items_from_gap_entries(
        [
            _GapEntryStub("rx_type", "RX-A", "接口未回传", 1),
            _GapEntryStub("days_qty", "RX-B", "医嘱未填写", 5),
            _GapEntryStub("drug_category", "RX-C", "接口未回传", 1),
        ]
    )
    assert [item.missing_field for item in items] == ["days_qty", "drug_category", "rx_type"]
    assert items[0].affected_prescriptions == 5
    assert items[0].cause == "医嘱未填写"


def test_priority_items_sorted_by_rank():
    """字段补齐顺序按名次升序。"""
    items = items_from_priority_table(
        [
            _PriorityStub(2, "days_qty", 2, 3, "退回医师补写"),
            _PriorityStub(1, "rx_type", 3, 8, "信息科接口岗"),
        ]
    )
    assert [item.priority for item in items] == [1, 2]
    assert items[0].route_to == "信息科接口岗"


def test_summarize_judgements_counts_three_states():
    """由判定行汇总出批次摘要（三态计数 + 医嘱行数按去重处方号推导）。"""
    rows = [
        JudgementRow(
            batch_no="B1",
            prescription_no="RX-0001",
            rule_id="R1",
            rule_version="v1",
            conclusion=Conclusion.COMPLIANT,
        ),
        JudgementRow(
            batch_no="B1",
            prescription_no="RX-0001",
            rule_id="R2",
            rule_version="v1",
            conclusion=Conclusion.VIOLATION,
        ),
        JudgementRow(
            batch_no="B1",
            prescription_no="RX-0002",
            rule_id="R1",
            rule_version="v1",
            conclusion=Conclusion.UNDETERMINABLE,
        ),
    ]
    summary = summarize_judgements(rows)
    assert summary.batch_no == "B1"
    assert summary.judgement_rows == 3
    assert summary.received_rows == 2  # 去重后的处方号数
    assert summary.compliant == 1
    assert summary.violation == 1
    assert summary.undeterminable == 1


# ---------------------------------------------------------------------------
# ② 推送失败不中断主流程
# ---------------------------------------------------------------------------


def test_notify_failure_does_not_block_main_flow(http_double, client, store):
    """★ 推送失败只记录告警：判定台账、判定状态与 HIS 回写一个字节都不受影响。"""
    store.insert_judgements([_judgement(1), _judgement(2)])
    before_judgements = store.count_judgements()
    before_audit = store.count_audit()

    # 企业微信返回 500
    http_double.script((500, json.dumps({"errcode": -1, "errmsg": "internal error"})))
    notifier = WeComNotifier(client, webhook_url=http_double.webhook_url)

    outcome = notifier.notify(_summary(), gap_items=_gap_items(), priority_items=_priority_items())

    # 推送侧：失败但不抛异常，告警留痕
    assert outcome.sent is False
    assert outcome.reason
    assert notifier.alerts
    assert any("企业微信" in alert for alert in notifier.alerts)
    # 500 属可重试状态码 → 首次 + 3 次退避重试 = 4 次请求
    assert len(http_double.requests) == 4
    # 失败原因里不带凭据位
    assert "SECRET-WEBHOOK-KEY" not in outcome.reason

    # 主流程侧：判定行数、审计行数、判定状态全部不变
    assert store.count_judgements() == before_judgements
    assert store.count_audit() == before_audit
    assert all(
        row.state == JudgementState.DECIDED for row in store.list_judgements()
    )

    # 主流程侧：回写 HIS 照常发出（推送挂了不挡回写）
    http_double.script((200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})))
    writer = HisReviewWriter(
        client, base_url=http_double.base_url, api_token="TEST-TOKEN", ledger=store
    )
    result = writer.write(
        ReviewRequest(
            batch_no="DEMO-BATCH-001",
            prescription_no="RX-0001",
            rule_id="RX-LEGAL-MA-OUT-DAYS",
            rule_version="2026.10",
            conclusion=Conclusion.VIOLATION,
            severity="严重不合理用药",
            reason="规则 RX-LEGAL-MA-OUT-DAYS 判定：违规。",
            basis="处方审核规则体系 §3",
            reviewed_by="pharmacist-001",
        )
    )
    assert result.sent is True
    assert store.count_judgements() == before_judgements  # 回写不新增判定行


def test_notify_missing_url_returns_not_sent_without_raising(client):
    """地址缺失：notify 兜住异常，只记告警，不抛。"""
    notifier = WeComNotifier(client, webhook_url="")
    outcome = notifier.notify(_summary())
    assert outcome.sent is False
    assert ENV_WECOM_WEBHOOK_URL in outcome.reason
    assert any(ENV_WECOM_WEBHOOK_URL in alert for alert in notifier.alerts)


def test_notify_records_outcome_history(http_double, client):
    """历次推送结果追加式留存，供 CLI / 守护进程读取。"""
    http_double.script((200, json.dumps({"errcode": 0, "errmsg": "ok"})))
    notifier = WeComNotifier(client, webhook_url=http_double.webhook_url)
    notifier.notify(_summary())
    assert len(notifier.outcomes) == 1
    assert notifier.outcomes[0].sent is True
    assert notifier.outcomes[0].batch_no == "DEMO-BATCH-001"


# ---------------------------------------------------------------------------
# ③ 地址缺失或非法 → 显式报错并告警（请求不发出）
# ---------------------------------------------------------------------------


def test_missing_webhook_url_raises_and_alerts(http_double, client):
    """★ 地址缺失：显式抛错 + 告警，请求一个字节都不发。"""
    notifier = WeComNotifier(client, webhook_url="")
    with pytest.raises(WeComAddressError) as excinfo:
        notifier.push(_summary())
    assert ENV_WECOM_WEBHOOK_URL in str(excinfo.value)
    assert notifier.alerts
    assert any(ENV_WECOM_WEBHOOK_URL in alert for alert in notifier.alerts)
    assert http_double.requests == []  # 传输层一次都没被调用


@pytest.mark.parametrize(
    "bad_url",
    [
        "ftp://qyapi.weixin.qq.com/hook",
        "qyapi.weixin.qq.com/cgi-bin/webhook/send",
        "https://qyapi.weixin.qq.com:0/hook",
        "https://qyapi.weixin.qq.com:99999/hook",
        "https://bad host/hook",
    ],
)
def test_invalid_webhook_url_raises_and_alerts(http_double, client, bad_url):
    """★ 地址非法：显式抛错 + 告警，请求一个字节都不发。"""
    notifier = WeComNotifier(client, webhook_url=bad_url)
    with pytest.raises(WeComAddressError):
        notifier.push(_summary())
    assert notifier.alerts
    assert http_double.requests == []


def test_alert_does_not_leak_webhook_key(http_double, client, caplog):
    """★ 告警与日志里不出现 webhook 地址里的凭据位（地址非法这条路径）。"""
    notifier = WeComNotifier(client, webhook_url="ftp://qyapi.weixin.qq.com/hook?key=SECRET-KEY")
    with caplog.at_level("WARNING"):
        with pytest.raises(WeComAddressError):
            notifier.push(_summary())
    joined = "\n".join(notifier.alerts) + caplog.text
    assert "SECRET-KEY" not in joined
    assert "SECRET-WEBHOOK-KEY" not in joined


def test_alert_does_not_leak_webhook_key_on_push_failure(http_double, client, caplog):
    """★ 推送失败这条路径同样不外泄凭据位。

    出站客户端的异常消息里带着**完整目标地址**（含 query 里的 key），
    告警直接引用就会把凭据写进日志——引用前必须脱敏。
    """
    http_double.script((500, json.dumps({"errcode": -1, "errmsg": "boom"})))
    notifier = WeComNotifier(client, webhook_url=http_double.webhook_url)
    with caplog.at_level("WARNING"):
        with pytest.raises(WeComResponseError):
            notifier.push(_summary())
    joined = "\n".join(notifier.alerts) + caplog.text
    assert "SECRET-WEBHOOK-KEY" not in joined
    # 脱敏后仍能看出目标是谁
    assert "127.0.0.1" in joined


def test_mask_webhook_url_hides_query():
    """脱敏：只留「协议://主机/路径」，query 里的凭据不显示。"""
    masked = mask_webhook_url("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET")
    assert masked == "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    assert "SECRET" not in masked
    assert mask_webhook_url("") == "（地址未配置）"
    assert mask_webhook_url("not-a-url") == "（地址不合法）"


# ---------------------------------------------------------------------------
# ④ 企业微信应答的判定
# ---------------------------------------------------------------------------


def test_push_raises_on_http_error(http_double, client):
    """HTTP 非 2xx：显式抛错 + 告警。"""
    http_double.script((500, json.dumps({"errcode": -1, "errmsg": "boom"})))
    notifier = WeComNotifier(client, webhook_url=http_double.webhook_url)
    with pytest.raises(WeComResponseError) as excinfo:
        notifier.push(_summary())
    assert "HTTP 500" in str(excinfo.value)
    assert notifier.alerts


def test_push_raises_on_business_errcode(http_double, client):
    """业务 errcode != 0：HTTP 200 也不算送达，显式抛错 + 告警。"""
    http_double.script((200, json.dumps({"errcode": 93000, "errmsg": "invalid webhook url"})))
    notifier = WeComNotifier(client, webhook_url=http_double.webhook_url)
    with pytest.raises(WeComResponseError) as excinfo:
        notifier.push(_summary())
    assert "93000" in str(excinfo.value)
    assert "invalid webhook url" in str(excinfo.value)
    assert notifier.alerts


def test_push_accepts_response_without_errcode(http_double, client):
    """应答里既无 errcode 也无 code：HTTP 2xx 即视为送达，不臆造失败。"""
    http_double.script((200, json.dumps({"ok": True})))
    notifier = WeComNotifier(client, webhook_url=http_double.webhook_url)
    outcome = notifier.push(_summary())
    assert outcome.sent is True
    assert outcome.errcode is None


# ---------------------------------------------------------------------------
# ⑤ 预检（运维通道）
# ---------------------------------------------------------------------------


def test_preflight_reports_missing_and_valid_url(http_double):
    """预检不发请求，只报地址齐备与否；凭据位不显示。"""
    missing = preflight_lines({})
    assert any(ENV_WECOM_WEBHOOK_URL in line and line.startswith("[缺失]") for line in missing)

    ready = preflight_lines({ENV_WECOM_WEBHOOK_URL: http_double.webhook_url})
    assert any(line.startswith("[合法]") for line in ready)
    assert "SECRET-WEBHOOK-KEY" not in "\n".join(ready)


def test_preflight_reports_invalid_url():
    """地址非法时预检报「[非法]」。"""
    lines = preflight_lines({ENV_WECOM_WEBHOOK_URL: "ftp://qyapi.weixin.qq.com/hook"})
    assert any(line.startswith("[非法]") for line in lines)


def test_package_entry_routes_push_preflight(monkeypatch, capsys):
    """包级运维入口按参数路由：``--check-push`` 走推送预检，缺地址退出码 2。"""
    from med_rule_fieldmap.outbound.__main__ import main as outbound_main

    monkeypatch.delenv(ENV_WECOM_WEBHOOK_URL, raising=False)
    assert outbound_main(["--check-push"]) == 2
    assert ENV_WECOM_WEBHOOK_URL in capsys.readouterr().out


def test_package_entry_routes_writeback_preflight(monkeypatch, capsys):
    """包级运维入口默认仍走回写预检（既有契约不变）。"""
    from med_rule_fieldmap.outbound.__main__ import main as outbound_main

    monkeypatch.delenv("HIS_BASE_URL", raising=False)
    monkeypatch.delenv("HIS_API_TOKEN", raising=False)
    assert outbound_main(["--check"]) == 2
    assert "HIS_BASE_URL" in capsys.readouterr().out


def preflight_lines(env: dict[str, str]) -> list[str]:
    """跑一遍预检（不发请求）。"""
    from med_rule_fieldmap.outbound.wecom import preflight

    return preflight(Config.from_env(env, base_dir=Path("/tmp")))


# ---------------------------------------------------------------------------
# 小替身（鸭子类型，验证 adapter 不依赖 ledger 包）
# ---------------------------------------------------------------------------


class _GapEntryStub:
    def __init__(self, missing_field: str, rule_id: str, cause: str, count: int) -> None:
        self.missing_field = missing_field
        self.rule_id = rule_id
        self.cause = cause
        self.affected_prescriptions = count


class _PriorityStub:
    def __init__(
        self,
        priority: int,
        missing_field: str,
        prescriptions: int,
        rules: int,
        route_to: str,
    ) -> None:
        self.priority = priority
        self.missing_field = missing_field
        self.affected_prescriptions = prescriptions
        self.affected_rules = rules
        self.route_to = route_to
