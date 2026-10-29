"""药师确认 + 撤回 + 回写触发门禁测试。

**替身说明**：本测试用**本地真实 HTTP 服务**（``http.server``）充当 HIS 审方复核
接口——传输层是真 ``httpx``、真 TCP、真 HTTP 报文，被测的**产品代码**走的是
spec.md §2.3 的真实协议（复核地址 + 撤回地址、Bearer 头、JSON 字段全集）。
替身只负责「按剧本回一个响应」，不替代任何产品逻辑。

**本任务要守住的三条线**：

1. **未确认的结论永远不出现在任何回写请求体里**（回写门禁）；
2. **确认后只回写一次**（幂等键 + 状态跃迁双重把关）；
3. **撤回后不再回写**，已发出的要同步撤回通知到 HIS 并留痕
   （《处方审核规则体系》§4：药师是处方审核第一责任人）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from med_rule_fieldmap import cli as cli_module
from med_rule_fieldmap.engine.confirm import (
    ACTION_CONFIRM,
    ACTION_WITHDRAW,
    WRITEBACK_STATE_WITHDRAWN,
    WRITEBACK_STATE_WITHDRAW_PENDING,
    ConfirmService,
    ConfirmationStateError,
    PharmacistIdentityError,
    WritebackGateError,
)
from med_rule_fieldmap.models import (
    Conclusion,
    JudgementRow,
    JudgementState,
    PendingWritebackStatus,
)
from med_rule_fieldmap.outbound.client import OutboundClient, OutboundRequestError
from med_rule_fieldmap.outbound.his_review import (
    WITHDRAW_BODY_FIELDS,
    HisReviewWriter,
    ReviewRequest,
)
from med_rule_fieldmap.outbound.idempotency import WritebackIdempotencyGuard
from med_rule_fieldmap.outbound.retry_queue import PendingWritebackQueue
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
        payload = json.loads(raw) if raw else {}
        self.server.record(  # type: ignore[attr-defined]
            {
                "method": "POST",
                "path": self.path,
                "headers": {key: value for key, value in self.headers.items()},
                "raw": raw,
                "json": payload,
            }
        )
        status, body = self.server.next_response(payload)  # type: ignore[attr-defined]
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:  # pragma: no cover - 静音默认访问日志
        return


class _HisDouble:
    """HIS 审方复核接口替身（本地真实 HTTP 服务）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._responses: list[tuple[int, str | None]] = []
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

    def script(self, *responses: tuple[int, str | None]) -> None:
        """排定响应剧本（依次消费；用尽后重复最后一条）。

        响应体给 ``None`` 表示「回声模式」：按请求里的 ``prescription_no``
        原样回一个成功响应（默认剧本就是回声模式）。
        """
        with self._lock:
            self._responses = list(responses) or [(200, None)]

    def _record(self, payload: dict) -> None:
        with self._lock:
            self.requests.append(payload)

    def _next_response(self, request_payload: dict) -> tuple[int, str]:
        with self._lock:
            if not self._responses:
                script: tuple[int, str | None] = (200, None)
            elif len(self._responses) == 1:
                script = self._responses[0]
            else:
                script = self._responses.pop(0)
        status, body = script
        if body is None:
            body = json.dumps(
                {
                    "code": 0,
                    "data": {
                        "prescription_no": str(
                            request_payload.get("prescription_no") or ""
                        )
                    },
                }
            )
        return status, body

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


def _row(**overrides) -> JudgementRow:
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
        "event_fingerprint": "10732b6ce574",
        "state": JudgementState.DECIDED,
    }
    base.update(overrides)
    return JudgementRow(**base)


def _review_request(**overrides) -> ReviewRequest:
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


@pytest.fixture()
def store(tmp_path: Path):
    db = Store(tmp_path / "state.sqlite3")
    try:
        yield db
    finally:
        db.close()


def _service(store: Store, double, *, with_queue: bool = True) -> ConfirmService:
    """构造一个带真实出站通道的确认服务。"""
    writer = HisReviewWriter(
        OutboundClient(deadline_sec=5.0),
        base_url=double.base_url,
        api_token="TESTTOKEN",
        guard=WritebackIdempotencyGuard(store),
        ledger=store,
    )
    queue = PendingWritebackQueue(store, writer=writer) if with_queue else None
    return ConfirmService(store, writer=writer, queue=queue)


# ---------------------------------------------------------------------------
# 1. 回写门禁：未确认的结论不进任何回写请求体
# ---------------------------------------------------------------------------


def test_unconfirmed_rows_never_enter_writeback_body(store, his_double):
    """已判定 / 待补字段 / 待判定三条状态都不进回写请求体。"""
    decided = _row(prescription_no="RX-DECIDED", state=JudgementState.DECIDED)
    pending_fields = _row(
        prescription_no="RX-PENDING-FIELDS",
        rule_id="RX-NORM-FRONT-DIAG",
        state=JudgementState.PENDING_FIELDS,
        missing_fields=("clinical_diag",),
    )
    pending = _row(prescription_no="RX-PENDING", state=JudgementState.PENDING)
    store.insert_judgements([decided, pending_fields, pending])
    service = _service(store, his_double)

    report = service.trigger_writeback()

    assert report.attempted == 0
    assert report.sent == 0
    assert his_double.requests == [], "未确认的结论一个字节都不该发出去"


def test_only_confirmed_row_is_written_back(store, his_double):
    """同一批次里只有「药师已确认」的那条进回写请求体。"""
    confirmed = _row(prescription_no="RX-CONFIRMED")
    decided = _row(prescription_no="RX-DECIDED", rule_id="RX-OTHER-RULE")
    store.insert_judgements([confirmed, decided])
    service = _service(store, his_double)
    service.confirm(confirmed.idempotency_key, pharmacist_id="pharmacist-007")

    report = service.trigger_writeback()

    assert report.attempted == 1
    assert report.sent == 1
    assert len(his_double.requests) == 1
    body = his_double.requests[0]["json"]
    assert body["prescription_no"] == "RX-CONFIRMED"
    assert "RX-DECIDED" not in his_double.requests[0]["raw"]
    assert body["reviewed_by"] == "pharmacist-007"


def test_confirmed_row_without_trace_is_skipped(store, his_double):
    """状态写着「已确认」但查不到确认留痕 → 跳过并告警（系统不代签）。"""
    row = _row()
    store.insert_judgements([row])
    store.set_judgement_state(row.idempotency_key, JudgementState.CONFIRMED)
    service = _service(store, his_double)

    report = service.trigger_writeback()

    assert report.attempted == 0
    assert his_double.requests == []
    assert service.alerts, "缺确认留痕必须告警"


def test_gate_rejects_non_confirmed_row_at_request_build(store, his_double):
    """门禁在构造请求体这一层再挡一次：非「已确认」状态构造不出请求体。"""
    from med_rule_fieldmap.engine.confirm import WritebackCandidate

    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)
    candidate = WritebackCandidate(judgement=row, confirmation=None)  # type: ignore[arg-type]

    with pytest.raises(WritebackGateError):
        service.build_review_request(candidate)


# ---------------------------------------------------------------------------
# 2. 确认：状态校验 + 药师标识 + 留痕
# ---------------------------------------------------------------------------


def test_confirm_moves_state_and_writes_trace(store, his_double):
    """确认：已判定 → 药师已确认，并追加一条确认留痕。"""
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)

    outcome = service.confirm(row.idempotency_key, pharmacist_id="pharmacist-007")

    assert outcome.state_before is JudgementState.DECIDED
    assert outcome.state_after is JudgementState.CONFIRMED
    stored = store.get_judgement(row.idempotency_key)
    assert stored is not None
    assert stored.state is JudgementState.CONFIRMED
    traces = store.list_confirmations(row.idempotency_key)
    assert len(traces) == 1
    assert traces[0].action == ACTION_CONFIRM
    assert traces[0].pharmacist_id == "pharmacist-007"
    assert traces[0].writeback_state == "已确认未回写"


def test_confirm_requires_pharmacist_identity(store, his_double):
    """没有药师标识 → 显式拒绝（系统不代签）。"""
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)

    with pytest.raises(PharmacistIdentityError):
        service.confirm(row.idempotency_key, pharmacist_id="")

    stored = store.get_judgement(row.idempotency_key)
    assert stored is not None and stored.state is JudgementState.DECIDED


def test_confirm_pending_fields_is_rejected_with_missing_fields(store, his_double):
    """待补字段的判定不能确认：报错要点名缺哪个字段。"""
    row = _row(
        state=JudgementState.PENDING_FIELDS,
        missing_fields=("rx_type", "drug_category"),
    )
    store.insert_judgements([row])
    service = _service(store, his_double)

    with pytest.raises(ConfirmationStateError) as excinfo:
        service.confirm(row.idempotency_key, pharmacist_id="pharmacist-007")

    message = str(excinfo.value)
    assert "待补字段" in message
    assert "rx_type" in message
    assert "drug_category" in message


def test_double_confirm_is_rejected(store, his_double):
    """重复确认 → 显式拒绝（不产生第二条留痕）。"""
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)
    service.confirm(row.idempotency_key, pharmacist_id="pharmacist-007")

    with pytest.raises(ConfirmationStateError):
        service.confirm(row.idempotency_key, pharmacist_id="pharmacist-008")

    assert len(store.list_confirmations(row.idempotency_key)) == 1


def test_confirm_accepts_business_key(store, his_double):
    """业务键（台账上药师看到的那一串）也能定位到判定行。"""
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)

    outcome = service.confirm(row.business_key, pharmacist_id="pharmacist-007")

    assert outcome.judgement.idempotency_key == row.idempotency_key


# ---------------------------------------------------------------------------
# 3. 确认后触发：只回写一次
# ---------------------------------------------------------------------------


def test_trigger_after_confirm_writes_exactly_once(store, his_double):
    """确认后触发 → 发出一次；再触发一次 → 一个字节都不多发。"""
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)
    service.confirm(row.idempotency_key, pharmacist_id="pharmacist-007")

    first = service.trigger_writeback()
    second = service.trigger_writeback()

    assert first.sent == 1
    assert first.attempted == 1
    assert second.attempted == 0
    assert second.sent == 0
    assert len(his_double.requests) == 1
    stored = store.get_judgement(row.idempotency_key)
    assert stored is not None and stored.state is JudgementState.WRITTEN_BACK


def test_successful_writeback_appends_audit_row(store, his_double):
    """回写成功 → 追加一条「已回写」的审计行。"""
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)
    service.confirm(row.idempotency_key, pharmacist_id="pharmacist-007")

    service.trigger_writeback()

    audit_rows = store.list_audit("DEMO-BATCH-001")
    assert len(audit_rows) == 1
    assert audit_rows[0].writeback_state == "已回写"


# ---------------------------------------------------------------------------
# 4. 撤回：排队中的回写作废
# ---------------------------------------------------------------------------


def test_withdraw_before_send_marks_pending_withdrawn(store, his_double):
    """撤回一条还没发出的结论 → 排队中的回写标成「已撤回」，不再发出。"""
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)
    service.confirm(row.idempotency_key, pharmacist_id="pharmacist-007")

    # 先让这条回写失败一次，落进待重放队列
    queue = service.queue
    queue.enqueue(
        _review_request(),
        OutboundRequestError("HIS 返回 500", attempts=4, last_status=500),
    )
    assert store.count_pending_writeback(PendingWritebackStatus.PENDING.value) == 1

    outcome = service.withdraw(row.idempotency_key, pharmacist_id="pharmacist-007")

    assert outcome.state_after is JudgementState.WITHDRAWN
    assert outcome.pending_withdrawn == 1
    assert store.count_pending_writeback(PendingWritebackStatus.PENDING.value) == 0
    assert store.count_pending_writeback(PendingWritebackStatus.WITHDRAWN.value) == 1

    # 撤回后不得再发出
    report = queue.replay()
    assert report.attempted == 0
    assert his_double.requests == []


def test_withdrawn_row_is_not_a_writeback_candidate(store, his_double):
    """撤回后的判定不再出现在待回写候选里。"""
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)
    service.confirm(row.idempotency_key, pharmacist_id="pharmacist-007")
    service.withdraw(row.idempotency_key, pharmacist_id="pharmacist-007")

    report = service.trigger_writeback()

    assert report.attempted == 0
    assert his_double.requests == []


def test_withdraw_writes_trace(store, his_double):
    """撤回也留痕（谁撤的、什么时候撤的）。"""
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)
    service.confirm(row.idempotency_key, pharmacist_id="pharmacist-007")

    service.withdraw(row.idempotency_key, pharmacist_id="pharmacist-008", note="误判")

    traces = store.list_confirmations(row.idempotency_key)
    assert [item.action for item in traces] == [ACTION_CONFIRM, ACTION_WITHDRAW]
    assert traces[1].pharmacist_id == "pharmacist-008"
    assert traces[1].note == "误判"


# ---------------------------------------------------------------------------
# 5. 撤回：已发出的同步撤回通知到 HIS
# ---------------------------------------------------------------------------


def test_withdraw_after_send_notifies_his_and_leaves_trace(store, his_double):
    """已回写的结论被撤回 → 同步一条撤回通知到 HIS，并留下撤回痕迹。"""
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)
    service.confirm(row.idempotency_key, pharmacist_id="pharmacist-007")
    service.trigger_writeback()
    assert len(his_double.requests) == 1

    outcome = service.withdraw(row.idempotency_key, pharmacist_id="pharmacist-008")

    assert outcome.withdrawn_notified is True
    assert outcome.record.writeback_state == WRITEBACK_STATE_WITHDRAWN
    assert len(his_double.requests) == 2
    withdraw_call = his_double.requests[1]
    assert withdraw_call["path"] == "/api/v1/prescription/review/withdraw"
    assert withdraw_call["headers"]["Authorization"] == "Bearer TESTTOKEN"
    body = withdraw_call["json"]
    assert tuple(body.keys()) == WITHDRAW_BODY_FIELDS
    assert body["withdrawn_by"] == "pharmacist-008"
    assert body["reviewed_by"] == "pharmacist-007"
    assert body["prescription_no"] == "RX-0001"
    # 撤回痕迹：审计行 + 确认留痕
    audit_states = [item.writeback_state for item in store.list_audit("DEMO-BATCH-001")]
    assert "已撤回" in audit_states


def test_withdraw_sync_failure_is_not_silently_swallowed(store, his_double):
    """撤回同步失败 → 状态记「撤回待同步」+ 告警，不假装成功。"""
    his_double.script((200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})))
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)
    service.confirm(row.idempotency_key, pharmacist_id="pharmacist-007")
    service.trigger_writeback()

    # 撤回通知这一发返回 500
    his_double.script((500, json.dumps({"code": 500, "message": "服务端异常"})))
    outcome = service.withdraw(row.idempotency_key, pharmacist_id="pharmacist-008")

    assert outcome.withdrawn_notified is False
    assert outcome.record.writeback_state == WRITEBACK_STATE_WITHDRAW_PENDING
    assert outcome.alerts, "同步失败必须产生告警"


def test_resync_withdrawals_replays_pending_withdrawal(store, his_double):
    """「撤回待同步」的记录可以重投，重投成功后补一条「已撤回」留痕。"""
    row = _row()
    store.insert_judgements([row])
    service = _service(store, his_double)
    service.confirm(row.idempotency_key, pharmacist_id="pharmacist-007")
    service.trigger_writeback()

    his_double.script((500, json.dumps({"code": 500, "message": "服务端异常"})))
    service.withdraw(row.idempotency_key, pharmacist_id="pharmacist-008")
    assert (
        store.latest_confirmation(row.idempotency_key).writeback_state
        == WRITEBACK_STATE_WITHDRAW_PENDING
    )

    his_double.script((200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})))
    report = service.resync_withdrawals()

    assert report.sent == 1
    assert report.failed == 0
    assert (
        store.latest_confirmation(row.idempotency_key).writeback_state
        == WRITEBACK_STATE_WITHDRAWN
    )


def test_withdraw_from_pending_fields_is_rejected(store, his_double):
    """待补字段的判定不处在可撤回状态 → 显式拒绝。"""
    row = _row(state=JudgementState.PENDING_FIELDS, missing_fields=("rx_type",))
    store.insert_judgements([row])
    service = _service(store, his_double)

    with pytest.raises(ConfirmationStateError):
        service.withdraw(row.idempotency_key, pharmacist_id="pharmacist-007")


# ---------------------------------------------------------------------------
# 6. 运维 CLI：确认 / 撤回 / 触发回写
# ---------------------------------------------------------------------------


def test_cli_confirm_and_withdraw(store, his_double, monkeypatch, capsys):
    """CLI：确认 → 触发回写 → 撤回，三步都能跑通。"""
    row = _row()
    store.insert_judgements([row])
    db_path = str(store.db_path)
    store.close()
    monkeypatch.setenv("HIS_BASE_URL", his_double.base_url)
    monkeypatch.setenv("HIS_API_TOKEN", "TESTTOKEN")

    code = cli_module.main(["confirm", "--db", db_path, "--key", row.idempotency_key,
                            "--by", "pharmacist-007"])
    assert code == 0
    out = capsys.readouterr().out
    assert "确认" in out
    assert "药师已确认" in out

    code = cli_module.main(["confirm", "--db", db_path, "--trigger"])
    assert code == 0
    assert len(his_double.requests) == 1

    code = cli_module.main(["confirm", "--db", db_path, "--key", row.idempotency_key,
                            "--by", "pharmacist-008", "--withdraw"])
    assert code == 0
    out = capsys.readouterr().out
    assert "撤回" in out
    assert len(his_double.requests) == 2
    assert his_double.requests[1]["path"] == "/api/v1/prescription/review/withdraw"


def test_cli_confirm_requires_pharmacist(store, capsys):
    """CLI 缺药师标识 → 退出码 2。"""
    row = _row()
    store.insert_judgements([row])
    db_path = str(store.db_path)
    store.close()

    code = cli_module.main(["confirm", "--db", db_path, "--key", row.idempotency_key])

    assert code == 2
    assert "药师标识" in capsys.readouterr().err


def test_cli_trigger_without_credentials_fails_explicitly(store, monkeypatch, capsys):
    """没配 HIS 凭据就触发回写 → 显式失败（退出码 2），不静默跳过。"""
    row = _row()
    store.insert_judgements([row])
    db_path = str(store.db_path)
    store.close()
    monkeypatch.delenv("HIS_BASE_URL", raising=False)
    monkeypatch.delenv("HIS_API_TOKEN", raising=False)

    code = cli_module.main(["confirm", "--db", db_path, "--trigger"])

    assert code == 2
    assert "HIS_BASE_URL" in capsys.readouterr().err


def test_cli_list_shows_only_confirmed_candidates(store, capsys):
    """CLI --list：待回写候选里只有已确认的结论。"""
    confirmed = _row(prescription_no="RX-CONFIRMED")
    decided = _row(prescription_no="RX-DECIDED", rule_id="RX-OTHER")
    store.insert_judgements([confirmed, decided])
    db_path = str(store.db_path)
    store.close()

    service = ConfirmService(store)  # 仅用于确认动作
    store = Store(db_path)
    try:
        service = ConfirmService(store)
        service.confirm(confirmed.idempotency_key, pharmacist_id="pharmacist-007")
    finally:
        store.close()

    code = cli_module.main(["confirm", "--db", db_path, "--list"])

    assert code == 0
    out = capsys.readouterr().out
    assert "RX-CONFIRMED" in out
    assert "RX-DECIDED" not in out
