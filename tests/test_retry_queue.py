"""退避重试超次 → 待重放队列（``pending_writeback``）测试。

**替身说明**：本测试用**本地真实 HTTP 服务**（``http.server``）充当 HIS 审方复核
接口——传输层是真 ``httpx``、真 TCP、真 HTTP 报文；被测的**产品代码**走的是
spec.md §2.3 的真实协议（方法 / 路径 / Bearer 头 / JSON 字段全集）。
替身只负责「按剧本回一个响应」，不替代任何产品逻辑。

覆盖三条硬边界：

1. **退避超次落队列** —— 连续注入 4 次 500 → 重试 3 次后落 ``pending_writeback``
   + **产生告警**；判定行状态**不动**（绝不改成「已回写」）；
2. **失败不静默改数据** —— 入队不新增判定行、不补审计行、不改判定状态；
3. **重放不重复** —— 队列可重放；早已回写成功的结论被幂等键抑制，
   重放一遍**不会**让药师端出现两条复核记录。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from med_rule_fieldmap.models import (
    Conclusion,
    JudgementRow,
    JudgementState,
    PendingWritebackStatus,
)
from med_rule_fieldmap.outbound.client import OutboundClient, OutboundRequestError
from med_rule_fieldmap.outbound.his_review import HisReviewWriter, ReviewRequest
from med_rule_fieldmap.outbound.idempotency import WritebackIdempotencyGuard
from med_rule_fieldmap.outbound.retry_queue import (
    PENDING_WRITEBACK_COLUMNS,
    PENDING_WRITEBACK_FILENAME,
    REPLAY_FAILED,
    REPLAY_SENT,
    REPLAY_SUPPRESSED,
    PendingWritebackQueue,
    RetryQueueError,
    record_from_request,
    request_from_record,
    summarize_queue,
)
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
        status, payload = self.server.next_response(  # type: ignore[attr-defined]
            json.loads(raw) if raw else None
        )
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
        self._echo = False
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
            self._echo = False

    def script_echo(self) -> None:
        """成功模式：回一个与请求**同处方号**的成功响应。

        重放多条时用这个模式——固定回一个处方号会把别的处方判成实体绑定歧义。
        """
        with self._lock:
            self._responses = []
            self._echo = True

    def _record(self, payload: dict) -> None:
        with self._lock:
            self.requests.append(payload)

    def _next_response(self, body: dict | None = None) -> tuple[int, str]:
        with self._lock:
            if self._echo:
                rx = str((body or {}).get("prescription_no") or "")
                return 200, json.dumps({"code": 0, "data": {"prescription_no": rx}})
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


def _writer(double, store=None, guard=None, sleeps=None) -> HisReviewWriter:
    return HisReviewWriter(
        # 退避等待用假 sleep（退避时长本身在 test_outbound_client.py 里断言），
        # 这里只关心「发了几次、落没落队列」
        OutboundClient(deadline_sec=5.0, sleep=(sleeps.append if sleeps is not None else (lambda _: None))),
        base_url=double.base_url,
        api_token="TESTTOKEN",
        guard=guard,
        ledger=store,
    )


def _judgement(**overrides) -> JudgementRow:
    base = {
        "batch_no": "DEMO-BATCH-001",
        "prescription_no": "RX-0001",
        "rule_id": "RX-LEGAL-MA-OUT-DAYS",
        "rule_version": "2026.10",
        "conclusion": Conclusion.VIOLATION,
        "severity": "严重不合理用药",
        "reason": "归因",
        "basis": "处方审核规则体系 §3",
        "event_fingerprint": "10732b6ce574",
        "state": JudgementState.CONFIRMED,
    }
    base.update(overrides)
    return JudgementRow(**base)


# ---------------------------------------------------------------------------
# 1. 退避超次 → 落 pending_writeback + 告警
# ---------------------------------------------------------------------------


def test_four_500s_enqueue_pending_and_alert(his_double, tmp_path: Path):
    """连续 4 次 500 → 重试 3 次后落 pending_writeback 并产生告警。"""
    his_double.script((500, json.dumps({"code": 500, "message": "服务不可用"})))
    store = Store(tmp_path / "state.sqlite3")
    guard = WritebackIdempotencyGuard(store)
    writer = _writer(his_double, store=store, guard=guard)
    queue = PendingWritebackQueue(store, writer=writer)

    with pytest.raises(OutboundRequestError):
        queue.send(_request())

    # 退避：首次 + 3 次重试 = 4 次请求
    assert len(his_double.requests) == 4
    # 落 pending_writeback
    pending = queue.pending()
    assert len(pending) == 1
    record = pending[0]
    assert record.batch_no == "DEMO-BATCH-001"
    assert record.prescription_no == "RX-0001"
    assert record.rule_id == "RX-LEGAL-MA-OUT-DAYS"
    assert record.retry_count == 3, "重试次数 = 发出次数 − 1（首次请求不算重试）"
    assert "500" in record.last_error_code
    assert record.status is PendingWritebackStatus.PENDING
    assert record.queued_at, "入队时间必须落库"
    # 请求体完整落库（重放要按原样再发）
    payload = json.loads(record.request_body)
    assert payload["batch_no"] == "DEMO-BATCH-001"
    assert payload["prescription_no"] == "RX-0001"
    assert payload["conclusion"] == "violation"
    assert payload["reviewed_by"] == "pharmacist-007"
    # 告警
    assert queue.alerts, "退避超次必须产生告警"
    assert "待重放" in queue.alerts[0]
    assert "RX-0001" in queue.alerts[0]
    store.close()


def test_enqueue_keeps_request_body_and_identity(his_double, tmp_path: Path):
    """落库的请求体可原样还原成 ReviewRequest（重放发的是同一份）。"""
    his_double.script((500, json.dumps({"code": 500})))
    store = Store(tmp_path / "state.sqlite3")
    writer = _writer(his_double, store=store, guard=WritebackIdempotencyGuard(store))
    queue = PendingWritebackQueue(store, writer=writer)

    with pytest.raises(OutboundRequestError):
        queue.send(_request())

    record = queue.pending()[0]
    restored = request_from_record(record)
    assert restored.to_payload() == _request().to_payload()
    assert restored.event_fingerprint == "10732b6ce574"
    assert restored.judgement_key == _request().judgement_key
    store.close()


def test_same_writeback_key_is_upserted_not_duplicated(his_double, tmp_path: Path):
    """同一条回写反复失败 → 队列里只有一行，重试次数累加。"""
    his_double.script((500, json.dumps({"code": 500})))
    store = Store(tmp_path / "state.sqlite3")
    writer = _writer(his_double, store=store, guard=WritebackIdempotencyGuard(store))
    queue = PendingWritebackQueue(store, writer=writer)

    for _ in range(2):
        with pytest.raises(OutboundRequestError):
            queue.send(_request())

    pending = queue.pending()
    assert len(pending) == 1, "同一份回写只占一行，不堆成多行"
    assert pending[0].retry_count == 6, "累计重试次数 = 3 + 3"
    assert store.count_pending_writeback() == 1
    store.close()


# ---------------------------------------------------------------------------
# 2. 失败不静默改数据
# ---------------------------------------------------------------------------


def test_enqueue_does_not_touch_judgement_or_audit(his_double, tmp_path: Path):
    """入队不动判定行状态、不补审计行（绝不标成「已回写」）。"""
    his_double.script((500, json.dumps({"code": 500})))
    store = Store(tmp_path / "state.sqlite3")
    judgement = _judgement()
    store.insert_judgements([judgement])
    audit_before = store.count_audit()
    writer = _writer(his_double, store=store, guard=WritebackIdempotencyGuard(store))
    queue = PendingWritebackQueue(store, writer=writer)

    with pytest.raises(OutboundRequestError):
        queue.send(_request())

    stored = store.get_judgement(judgement.idempotency_key)
    assert stored is not None
    assert stored.state is JudgementState.CONFIRMED, "待重放不得把判定行标成已回写"
    assert store.count_audit() == audit_before, "入队不补审计行"
    assert store.count_judgements() == 1, "入队不新增判定行"
    # 回写身份表停在「回写失败」，不是「已回写」
    assert store.count_writeback_requests("已回写") == 0
    assert store.count_writeback_requests("回写失败") == 1
    store.close()


def test_pending_record_carries_required_fields(his_double, tmp_path: Path):
    """pending_writeback 记录批次号 / 处方号 / 规则号 / 请求体 / 重试次数 / 错误码 / 入队时间。"""
    his_double.script((500, json.dumps({"code": 500})))
    store = Store(tmp_path / "state.sqlite3")
    writer = _writer(his_double, store=store, guard=WritebackIdempotencyGuard(store))
    queue = PendingWritebackQueue(store, writer=writer)

    with pytest.raises(OutboundRequestError):
        queue.send(_request())

    record = queue.pending()[0]
    assert record.batch_no
    assert record.prescription_no
    assert record.rule_id
    assert record.request_body
    assert record.retry_count == 3
    assert record.last_error_code
    assert record.queued_at
    assert record.last_error, "最后一次失败原因必须留痕"
    store.close()


def test_record_from_request_reads_attempts_and_status():
    """退避耗尽的异常 → 重试次数与错误码从异常里读出来。"""
    from med_rule_fieldmap.outbound.idempotency import writeback_idempotency_key

    request = _request()
    error = OutboundRequestError("重试耗尽", attempts=4, last_status=503)
    record = record_from_request(request, error)

    assert record.retry_count == 3
    assert record.last_error_code == "HTTP 503"
    assert record.writeback_key == writeback_idempotency_key(
        request.judgement_key, request.to_payload()
    )
    assert record.status is PendingWritebackStatus.PENDING


def test_request_from_record_rejects_broken_body():
    """请求体坏了 → 显式报错，不静默跳过。"""
    from med_rule_fieldmap.models import PendingWriteback

    broken = PendingWriteback(
        writeback_key="k",
        judgement_key="j",
        request_body="{不是 JSON",
    )
    with pytest.raises(RetryQueueError):
        request_from_record(broken)


# ---------------------------------------------------------------------------
# 3. 重放：可重新发出，且不重复
# ---------------------------------------------------------------------------


def test_replay_resends_pending_and_marks_replayed(his_double, tmp_path: Path):
    """HIS 恢复后重放：请求按原样再发一次，记录标成「已重放」。"""
    his_double.script((500, json.dumps({"code": 500})))
    store = Store(tmp_path / "state.sqlite3")
    guard = WritebackIdempotencyGuard(store)
    writer = _writer(his_double, store=store, guard=guard)
    queue = PendingWritebackQueue(store, writer=writer)

    with pytest.raises(OutboundRequestError):
        queue.send(_request())
    assert len(his_double.requests) == 4

    # HIS 恢复
    his_double.script_echo()
    report = queue.replay()

    assert report.attempted == 1
    assert report.sent == 1
    assert report.failed == 0
    assert report.outcomes[0].status == REPLAY_SENT
    # 4 次失败 + 1 次重放成功 = 5 次请求
    assert len(his_double.requests) == 5
    # 重放发的是同一份请求体
    replayed_body = his_double.requests[-1]["json"]
    assert replayed_body["batch_no"] == "DEMO-BATCH-001"
    assert replayed_body["prescription_no"] == "RX-0001"
    assert replayed_body["rule_id"] == "RX-LEGAL-MA-OUT-DAYS"
    assert replayed_body["conclusion"] == "violation"
    assert replayed_body["reviewed_by"] == "pharmacist-007"
    assert his_double.requests[-1]["headers"]["Authorization"] == "Bearer TESTTOKEN"
    # 队列记录标成已重放
    assert queue.pending() == []
    assert queue.records()[0].status is PendingWritebackStatus.REPLAYED
    store.close()


def test_replay_twice_does_not_duplicate_writeback(his_double, tmp_path: Path):
    """重放一遍不会重复回写：已重放的记录不再被取走。"""
    his_double.script((500, json.dumps({"code": 500})))
    store = Store(tmp_path / "state.sqlite3")
    guard = WritebackIdempotencyGuard(store)
    writer = _writer(his_double, store=store, guard=guard)
    queue = PendingWritebackQueue(store, writer=writer)

    with pytest.raises(OutboundRequestError):
        queue.send(_request())

    his_double.script((200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})))
    first = queue.replay()
    requests_after_first = len(his_double.requests)
    second = queue.replay()

    assert first.sent == 1
    assert second.attempted == 0, "已重放的记录不再进重放通道"
    assert second.sent == 0
    assert len(his_double.requests) == requests_after_first, "第二次重放一个请求都不发"
    store.close()


def test_replay_suppresses_already_sent_writeback(his_double, tmp_path: Path):
    """早已回写成功的结论：重放时被幂等键抑制，一个请求都不发。"""
    his_double.script((200, json.dumps({"code": 0, "data": {"prescription_no": "RX-0001"}})))
    store = Store(tmp_path / "state.sqlite3")
    guard = WritebackIdempotencyGuard(store)
    writer = _writer(his_double, store=store, guard=guard)
    queue = PendingWritebackQueue(store, writer=writer)

    # 先正常回写成功（幂等键已记为「已回写」）
    writer.write(_request())
    assert len(his_double.requests) == 1

    # 再手工把同一条塞进待重放队列（模拟历史遗留 / 人工误操作）
    record = record_from_request(
        _request(), OutboundRequestError("历史失败", attempts=4, last_status=500)
    )
    store.enqueue_pending_writeback(record)
    assert len(queue.pending()) == 1

    report = queue.replay()

    assert report.attempted == 1
    assert report.suppressed == 1
    assert report.sent == 0
    assert report.outcomes[0].status == REPLAY_SUPPRESSED
    assert len(his_double.requests) == 1, "幂等抑制：一个请求都不发"
    assert queue.pending() == []
    store.close()


def test_replay_failure_keeps_record_pending(his_double, tmp_path: Path):
    """重放仍失败 → 记录继续留在队列里（不标已重放），等下次重放。"""
    his_double.script((500, json.dumps({"code": 500})))
    store = Store(tmp_path / "state.sqlite3")
    guard = WritebackIdempotencyGuard(store)
    writer = _writer(his_double, store=store, guard=guard)
    queue = PendingWritebackQueue(store, writer=writer)

    with pytest.raises(OutboundRequestError):
        queue.send(_request())

    report = queue.replay()

    assert report.failed == 1
    assert report.sent == 0
    assert report.outcomes[0].status == REPLAY_FAILED
    pending = queue.pending()
    assert len(pending) == 1, "重放失败仍留在队列里"
    assert pending[0].retry_count == 6, "重试次数继续累加"
    store.close()


def test_replay_without_writer_raises(tmp_path: Path):
    """没配回写通道 → 显式报错，不静默跳过。"""
    store = Store(tmp_path / "state.sqlite3")
    queue = PendingWritebackQueue(store)

    with pytest.raises(RetryQueueError):
        queue.replay()
    store.close()


def test_replay_respects_limit(his_double, tmp_path: Path):
    """--limit 生效：只重放前 N 条，其余仍留在队列里。"""
    his_double.script((500, json.dumps({"code": 500})))
    store = Store(tmp_path / "state.sqlite3")
    guard = WritebackIdempotencyGuard(store)
    writer = _writer(his_double, store=store, guard=guard)
    queue = PendingWritebackQueue(store, writer=writer)

    for index in range(2):
        with pytest.raises(OutboundRequestError):
            queue.send(_request(prescription_no=f"RX-000{index + 1}"))
    assert len(queue.pending()) == 2

    # HIS 恢复后只重放 1 条
    his_double.script_echo()
    report = queue.replay(limit=1)

    assert report.attempted == 1
    assert report.sent == 1
    assert len(queue.pending()) == 1, "只重放了一条，另一条仍待重放"
    assert len(queue.records()) == 2
    store.close()


# ---------------------------------------------------------------------------
# 4. 导出：运维与质控可读的队列文件
# ---------------------------------------------------------------------------


def test_export_csv_has_fixed_columns(his_double, tmp_path: Path):
    """队列导出 CSV 列序固定，含批次号 / 处方号 / 规则号 / 重试次数 / 错误码 / 入队时间。"""
    his_double.script((500, json.dumps({"code": 500})))
    store = Store(tmp_path / "state.sqlite3")
    writer = _writer(his_double, store=store, guard=WritebackIdempotencyGuard(store))
    queue = PendingWritebackQueue(
        store, writer=writer, csv_path=tmp_path / PENDING_WRITEBACK_FILENAME
    )

    with pytest.raises(OutboundRequestError):
        queue.send(_request())

    target = queue.export_csv()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == list(PENDING_WRITEBACK_COLUMNS)
    assert len(lines) == 2, "一行表头 + 一行记录"
    assert "RX-0001" in lines[1]
    store.close()


def test_summarize_queue_counts_by_status(his_double, tmp_path: Path):
    """队列概览按状态计数，并给出累计重试次数。"""
    his_double.script((500, json.dumps({"code": 500})))
    store = Store(tmp_path / "state.sqlite3")
    writer = _writer(his_double, store=store, guard=WritebackIdempotencyGuard(store))
    queue = PendingWritebackQueue(store, writer=writer)

    with pytest.raises(OutboundRequestError):
        queue.send(_request())

    counts = summarize_queue(queue.records())
    assert counts["待重放"] == 1
    assert counts["累计重试次数"] == 3
    store.close()
