"""HIS 审方复核回写测试。

**替身说明**：本测试用**本地真实 HTTP 服务**（``http.server``）充当 HIS 审方复核
接口——传输层是真 ``httpx``、真 TCP、真 HTTP 报文，被测的**产品代码**走的是
spec.md §2.3 的真实协议（方法 / 路径 / Bearer 头 / JSON 字段全集）。
替身只负责「按剧本回一个响应」，不替代任何产品逻辑。

覆盖三类硬边界：

1. **请求形态** —— 方法、路径、``Authorization``、JSON 字段全集逐项断言；
2. **实体绑定歧义** —— 响应 ``prescription_no`` 与请求不一致 → 进挂起队列 + 告警
   + **不落库**（判定行状态不动、审计行不增）；
3. **凭据缺失** —— ``HIS_BASE_URL`` / ``HIS_API_TOKEN`` 缺失 → **请求一个字节都不发**。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from med_rule_fieldmap.config import Config
from med_rule_fieldmap.models import Conclusion, JudgementRow, JudgementState
from med_rule_fieldmap.outbound.client import OutboundClient, OutboundProtocolError
from med_rule_fieldmap.outbound.his_review import (
    BINDING_AMBIGUITY_COLUMNS,
    BINDING_AMBIGUITY_FILENAME,
    REVIEW_BODY_FIELDS,
    BindingAmbiguityLedger,
    HisBindingAmbiguityError,
    HisCredentialError,
    HisResponseError,
    HisReviewWriter,
    ReviewRequest,
    build_review_url,
    main,
    preflight,
)
from med_rule_fieldmap.outbound.idempotency import WritebackIdempotencyGuard
from med_rule_fieldmap.store import Store

# ---------------------------------------------------------------------------
# 本地 HIS 替身（真实 HTTP 服务，按剧本回响应）
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """记录请求并按剧本回响应。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        self.server.record(  # type: ignore[attr-defined]
            {
                "method": "POST",
                "path": self.path,
                "headers": {key: value for key, value in self.headers.items()},
                "raw": raw,
                "json": json.loads(raw) if raw else None,
            }
        )
        status, payload = self.server.next_response()  # type: ignore[attr-defined]
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:  # 静音默认访问日志
        return


class _HisDouble:
    """HIS 审方复核接口替身（本地真实 HTTP 服务）。"""

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

    def script(self, *responses: tuple[int, str]) -> None:
        """排定响应剧本（依次消费；用尽后重复最后一条）。"""
        with self._lock:
            self._responses = list(responses)

    def _record(self, payload: dict) -> None:
        with self._lock:
            self.requests.append(payload)

    def _next_response(self) -> tuple[int, str]:
        with self._lock:
            if not self._responses:
                return 200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})
            if len(self._responses) == 1:
                return self._responses[0]
            return self._responses.pop(0)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def his_double():
    double = _HisDouble()
    try:
        yield double
    finally:
        double.close()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _request(**overrides) -> ReviewRequest:
    base = {
        "batch_no": "DEMO-BATCH-001",
        "prescription_no": "RX-0001",
        "rule_id": "RX-LEGAL-MA-OUT-DAYS",
        "rule_version": "2026.10",
        "conclusion": Conclusion.VIOLATION,
        "severity": "严重不合理用药",
        "reason": "规则 RX-LEGAL-MA-OUT-DAYS（版本 2026.10）判定：违规。",
        "basis": "处方审核规则体系 §3 特殊药品处方量限",
        "missing_fields": (),
        "reviewed_by": "pharmacist-007",
        "reviewed_at": "2026-10-28T08:30:00+00:00",
        "event_fingerprint": "10732b6ce574",
    }
    base.update(overrides)
    return ReviewRequest(**base)


def _writer(double, **kwargs) -> HisReviewWriter:
    return HisReviewWriter(
        OutboundClient(deadline_sec=5.0),
        base_url=double.base_url,
        api_token="TESTTOKEN",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. 请求形态：方法 / 路径 / Bearer / JSON 字段全集
# ---------------------------------------------------------------------------


def test_request_method_path_and_headers(his_double):
    """方法、路径、Bearer 头、Content-Type 逐项对上 spec.md §2.3。"""
    his_double.script((200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})))
    writer = _writer(his_double)

    outcome = writer.write(_request())

    assert outcome.sent is True
    assert outcome.suppressed is False
    assert outcome.status_code == 200
    assert outcome.attempts == 1
    assert len(his_double.requests) == 1
    captured = his_double.requests[0]
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/prescription/review"
    assert captured["headers"]["Authorization"] == "Bearer TESTTOKEN"
    assert captured["headers"]["Content-Type"] == "application/json"


def test_request_body_field_set_is_exact(his_double):
    """JSON 字段全集严格等于 spec.md §2.3 的 11 个字段，且取值正确。"""
    his_double.script((200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})))
    writer = _writer(his_double)

    writer.write(_request(missing_fields=("rx_type", "days_qty")))

    body = his_double.requests[0]["json"]
    assert tuple(body.keys()) == REVIEW_BODY_FIELDS
    assert set(body.keys()) == set(REVIEW_BODY_FIELDS)
    assert body["batch_no"] == "DEMO-BATCH-001"
    assert body["prescription_no"] == "RX-0001"
    assert body["rule_id"] == "RX-LEGAL-MA-OUT-DAYS"
    assert body["rule_version"] == "2026.10"
    assert body["conclusion"] == "violation"
    assert body["severity"] == "严重不合理用药"
    assert body["basis"] == "处方审核规则体系 §3 特殊药品处方量限"
    assert body["missing_fields"] == ["rx_type", "days_qty"]
    assert body["reviewed_by"] == "pharmacist-007"
    assert body["reviewed_at"] == "2026-10-28T08:30:00+00:00"


def test_conclusion_wire_values_are_three_states(his_double):
    """三态结论各有英文 wire 取值（合规 / 违规 / 不可判定）。"""
    his_double.script(
        (200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})),
    )
    writer = _writer(his_double)
    seen = []
    for conclusion in (Conclusion.COMPLIANT, Conclusion.VIOLATION, Conclusion.UNDETERMINABLE):
        writer.write(_request(conclusion=conclusion))
        seen.append(his_double.requests[-1]["json"]["conclusion"])
    assert seen == ["compliant", "violation", "undeterminable"]


def test_event_fingerprint_not_in_body(his_double):
    """事件文件指纹只留本地审计，不进 HIS 请求体（spec.md §2.3 无此字段）。"""
    his_double.script((200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})))
    writer = _writer(his_double)

    writer.write(_request(event_fingerprint="10732b6ce574"))

    body = his_double.requests[0]["json"]
    assert "event_fingerprint" not in body
    assert set(body.keys()) == set(REVIEW_BODY_FIELDS)


def test_review_url_is_built_from_base_url():
    """回写地址 = {HIS_BASE_URL}/api/v1/prescription/review。"""
    assert build_review_url("https://his.internal:8443") == (
        "https://his.internal:8443/api/v1/prescription/review"
    )
    assert build_review_url("https://his.internal/") == (
        "https://his.internal/api/v1/prescription/review"
    )


# ---------------------------------------------------------------------------
# 2. 实体绑定歧义：进挂起队列 + 告警 + 不落库
# ---------------------------------------------------------------------------


def test_binding_ambiguity_queues_and_never_writes_back(his_double, tmp_path: Path):
    """响应处方号与请求不一致 → 挂起队列 + 告警，判定行状态不动、审计不增。"""
    his_double.script(
        (200, json.dumps({"code": 0, "data": {"prescription_no": "RX-9999"}})),
    )
    queue = BindingAmbiguityLedger(tmp_path / BINDING_AMBIGUITY_FILENAME)
    store = Store(tmp_path / "state.sqlite3")
    judgement = JudgementRow(
        batch_no="DEMO-BATCH-001",
        prescription_no="RX-0001",
        rule_id="RX-LEGAL-MA-OUT-DAYS",
        rule_version="2026.10",
        conclusion=Conclusion.VIOLATION,
        severity="严重不合理用药",
        reason="归因",
        basis="处方审核规则体系 §3",
        event_fingerprint="10732b6ce574",
        state=JudgementState.CONFIRMED,
    )
    store.insert_judgements([judgement])
    audit_before = store.count_audit()

    writer = _writer(his_double, queue=queue, ledger=store)

    with pytest.raises(HisBindingAmbiguityError) as excinfo:
        writer.write(_request())

    assert "RX-9999" in str(excinfo.value)
    assert "RX-0001" in str(excinfo.value)
    # 告警
    assert writer.alerts, "绑定歧义必须产生告警"
    assert "实体绑定歧义" in writer.alerts[0]
    # 挂起队列：一条记录，两个处方号都在
    records = queue.records()
    assert len(records) == 1
    assert records[0].requested_prescription_no == "RX-0001"
    assert records[0].returned_prescription_no == "RX-9999"
    assert records[0].status == "待人工核对"
    assert records[0].rule_id == "RX-LEGAL-MA-OUT-DAYS"
    # 队列文件的列序固定
    header = queue.path.read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == list(BINDING_AMBIGUITY_COLUMNS)
    # 不落库：判定行状态不动、审计行数不变
    stored = store.get_judgement(judgement.idempotency_key)
    assert stored is not None
    assert stored.state is JudgementState.CONFIRMED, "绑定歧义不得把判定行标成已回写"
    assert store.count_audit() == audit_before, "绑定歧义不得补审计行"
    store.close()


def test_binding_ambiguity_does_not_mark_writeback_sent(his_double, tmp_path: Path):
    """绑定歧义时幂等守卫不得把这条回写标成「已回写」。"""
    his_double.script(
        (200, json.dumps({"code": 0, "data": {"prescription_no": "RX-9999"}})),
    )
    guard = WritebackIdempotencyGuard()
    writer = _writer(his_double, guard=guard)

    with pytest.raises(HisBindingAmbiguityError):
        writer.write(_request())

    stats = guard.stats()
    assert stats["sent"] == 0
    assert stats["failed"] == 1


def test_binding_ambiguity_record_is_appended_not_overwritten(his_double, tmp_path: Path):
    """挂起队列是追加式的：第二条歧义记录不覆盖第一条。"""
    his_double.script(
        (200, json.dumps({"code": 0, "data": {"prescription_no": "RX-9999"}})),
    )
    queue = BindingAmbiguityLedger(tmp_path / BINDING_AMBIGUITY_FILENAME)
    writer = _writer(his_double, queue=queue)

    for _ in range(2):
        with pytest.raises(HisBindingAmbiguityError):
            writer.write(_request())

    assert len(queue.records()) == 2
    assert len(queue.pending()) == 2


# ---------------------------------------------------------------------------
# 3. 凭据缺失：显式失败，一个字节都不发
# ---------------------------------------------------------------------------


def test_missing_his_base_url_fails_explicitly(his_double, tmp_path: Path):
    """HIS_BASE_URL 未配置 → 显式失败，且请求一个都没发出去。"""
    config = Config.from_env({"MRFM_BASE_DIR": str(tmp_path)})

    with pytest.raises(HisCredentialError) as excinfo:
        HisReviewWriter.from_config(config)

    assert "HIS_BASE_URL" in str(excinfo.value)
    assert his_double.requests == []


def test_missing_his_api_token_refuses_to_send(his_double, tmp_path: Path):
    """HIS_API_TOKEN 未配置 → 拒绝发出请求并告警。"""
    config = Config.from_env(
        {"MRFM_BASE_DIR": str(tmp_path), "HIS_BASE_URL": his_double.base_url}
    )

    with pytest.raises(HisCredentialError) as excinfo:
        HisReviewWriter.from_config(config)

    assert "HIS_API_TOKEN" in str(excinfo.value)
    assert his_double.requests == []


def test_credentials_are_read_from_environment_only(his_double, tmp_path: Path):
    """凭据只从环境变量读：配置面给出 base url 与令牌后即可构造。"""
    config = Config.from_env(
        {
            "MRFM_BASE_DIR": str(tmp_path),
            "HIS_BASE_URL": his_double.base_url,
            "HIS_API_TOKEN": "ENVTOKEN",
        }
    )
    writer = HisReviewWriter.from_config(config)

    his_double.script((200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})))
    writer.write(_request())

    assert his_double.requests[0]["headers"]["Authorization"] == "Bearer ENVTOKEN"


# ---------------------------------------------------------------------------
# 4. 响应异常：不静默当成功
# ---------------------------------------------------------------------------


def test_business_code_not_zero_raises(his_double):
    """业务 code != 0 → 显式抛错，不当成功。"""
    his_double.script((200, json.dumps({"code": 500, "message": "处方不存在"})))
    writer = _writer(his_double)

    with pytest.raises(HisResponseError) as excinfo:
        writer.write(_request())

    assert "500" in str(excinfo.value)
    assert writer.alerts


def test_http_error_status_raises(his_double):
    """HTTP 非 2xx → 显式抛错。"""
    his_double.script((400, json.dumps({"code": 400, "message": "参数错误"})))
    writer = _writer(his_double)

    with pytest.raises(HisResponseError) as excinfo:
        writer.write(_request())

    assert "400" in str(excinfo.value)


def test_non_json_body_raises_protocol_error(his_double):
    """响应体不是 JSON → 协议错误，不静默当成功。"""
    his_double.script((200, "<html>502 Bad Gateway</html>"))
    writer = _writer(his_double)

    with pytest.raises(OutboundProtocolError):
        writer.write(_request())


def test_missing_returned_prescription_no_is_ambiguity(his_double, tmp_path: Path):
    """响应里没有 prescription_no → 同样按绑定歧义处理（不落库）。"""
    his_double.script((200, json.dumps({"code": 0, "data": {}})))
    queue = BindingAmbiguityLedger(tmp_path / BINDING_AMBIGUITY_FILENAME)
    writer = _writer(his_double, queue=queue)

    with pytest.raises(HisBindingAmbiguityError):
        writer.write(_request())

    assert queue.records()[0].returned_prescription_no == ""


# ---------------------------------------------------------------------------
# 5. 请求体自检：业务键与药师标识缺一不可
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["batch_no", "prescription_no", "rule_id", "rule_version"],
)
def test_missing_business_key_field_blocks_request(his_double, field):
    """业务键四段缺一 → 请求发不出去。"""
    writer = _writer(his_double)

    with pytest.raises(HisResponseError) as excinfo:
        writer.write(_request(**{field: ""}))

    assert field in str(excinfo.value)
    assert his_double.requests == []


def test_missing_reviewed_by_blocks_request(his_double):
    """缺药师标识 → 请求发不出去（系统不代签）。"""
    writer = _writer(his_double)

    with pytest.raises(HisResponseError) as excinfo:
        writer.write(_request(reviewed_by=""))

    assert "reviewed_by" in str(excinfo.value)
    assert his_double.requests == []


# ---------------------------------------------------------------------------
# 6. 幂等：同一条结论只发一次
# ---------------------------------------------------------------------------


def test_idempotency_guard_suppresses_second_write(his_double):
    """同一份请求体第二次被幂等守卫抑制，HTTP 只发一次。"""
    his_double.script((200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})))
    guard = WritebackIdempotencyGuard()
    writer = _writer(his_double, guard=guard)

    first = writer.write(_request())
    second = writer.write(_request())

    assert first.sent is True
    assert second.sent is False
    assert second.suppressed is True
    assert len(his_double.requests) == 1


def test_successful_write_appends_one_audit_row(his_double, tmp_path: Path):
    """回写成功 → 追加一条审计行（回写状态 = 已回写）。"""
    his_double.script((200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})))
    store = Store(tmp_path / "state.sqlite3")
    writer = _writer(his_double, ledger=store)

    writer.write(_request())

    audit_rows = store.list_audit("DEMO-BATCH-001")
    assert len(audit_rows) == 1
    assert audit_rows[0].writeback_state == "已回写"
    assert audit_rows[0].rule_id == "RX-LEGAL-MA-OUT-DAYS"
    assert audit_rows[0].basis == "处方审核规则体系 §3 特殊药品处方量限"
    store.close()


# ---------------------------------------------------------------------------
# 7. 预检入口
# ---------------------------------------------------------------------------


def test_preflight_reports_missing_credentials(tmp_path: Path, capsys):
    """缺凭据 → 预检未通过，退出码 2（显式失败，不静默跳过）。"""
    config = Config.from_env({"MRFM_BASE_DIR": str(tmp_path)})
    lines = preflight(config)

    assert any("HIS_BASE_URL" in line and line.startswith("[缺失]") for line in lines)
    assert any("HIS_API_TOKEN" in line and line.startswith("[缺失]") for line in lines)


def test_preflight_passes_with_full_credentials(his_double, tmp_path: Path):
    """凭据齐备且地址合法 → 预检通过。"""
    config = Config.from_env(
        {
            "MRFM_BASE_DIR": str(tmp_path),
            "HIS_BASE_URL": his_double.base_url,
            "HIS_API_TOKEN": "TOKEN123",
        }
    )
    lines = preflight(config)

    assert any(line.startswith("[合法]") for line in lines)
    assert any(line.startswith("[已配置]") for line in lines)
    assert not any(line.startswith(("[缺失]", "[非法]")) for line in lines)


def test_preflight_entry_returns_exit_code_2(tmp_path: Path, monkeypatch, capsys):
    """预检入口在缺凭据时返回退出码 2。"""
    monkeypatch.setenv("MRFM_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("HIS_BASE_URL", raising=False)
    monkeypatch.delenv("HIS_API_TOKEN", raising=False)

    code = main([])

    assert code == 2
    assert "预检未通过" in capsys.readouterr().err


def test_preflight_entry_returns_exit_code_0(his_double, tmp_path: Path, monkeypatch):
    """凭据齐备时预检退出码 0。"""
    monkeypatch.setenv("MRFM_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("HIS_BASE_URL", his_double.base_url)
    monkeypatch.setenv("HIS_API_TOKEN", "TOKEN123")

    code = main([])

    assert code == 0
