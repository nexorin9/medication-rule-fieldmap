"""守护进程「画面」测试：逐行判定行与推送回执行。

**替身说明**：本测试用**本地真实 HTTP 服务**（``http.server``）充当企业微信
机器人 webhook 与 HIS 审方复核接口——传输层是真 ``httpx``、真 TCP、真 HTTP
报文；被测的**产品代码**走的是 spec.md §2.3 / §2.4 的真实协议。替身只负责
「按剧本回一个响应」并记录请求，不替代任何产品逻辑。

**为什么单独测这一层**：本产品的交付形态是**事件驱动守护**，唯一的画面就是
**服务运行日志**（``docs/concepts/visual-target.md`` 的视觉目标卡）。视觉目标卡
承诺画面里有「缩进一格的逐行判定」与「推送回执行」两档元素——这两档一旦被
改坏，画面就与目标不符，而**台账与回写仍然照常跑**，光靠台账类断言发现不了。
本文件把这两档元素钉住，让 ``docs/screenshots/`` 里的实测截图有据可依。
"""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from med_rule_fieldmap.config import Config
from med_rule_fieldmap.daemon import Daemon, build_daemon
from med_rule_fieldmap.ingest.watcher import BatchFile, InboxWatcher
from med_rule_fieldmap.models import Conclusion

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_BATCH = PROJECT_ROOT / "tests" / "data" / "batch" / "demo_batch_001.csv"
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"


# ---------------------------------------------------------------------------
# 本地 HTTP 替身（真实 HTTP 服务，按剧本回响应）
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """企业微信与 HIS 审方复核共用一个本地真实服务。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        prescription_no = ""
        if isinstance(parsed, dict):
            prescription_no = str(parsed.get("prescription_no") or "")
        payload = {
            "errcode": 0,
            "errmsg": "ok",
            "code": 0,
            "data": {"prescription_no": prescription_no},
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:  # pragma: no cover
        """别往测试输出里刷访问日志。"""


class _LocalServer:
    """本地 HTTP 替身服务（后台线程跑）。"""

    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def local_server():
    server = _LocalServer()
    try:
        yield server
    finally:
        server.close()


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def _prepare(tmp_path: Path) -> Path:
    """建目录、复制规则、把样例批次投进落地目录，返回落地文件路径。"""
    inbox = tmp_path / "inbox"
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE_RULES, tmp_path / "rules", dirs_exist_ok=True)
    target = inbox / SAMPLE_BATCH.name
    shutil.copy2(SAMPLE_BATCH, target)
    return target


def _make_config(tmp_path: Path, local_server: _LocalServer) -> Config:
    env = {
        "HIS_BASE_URL": local_server.base_url,
        "HIS_API_TOKEN": "test-token",
        "WECOM_WEBHOOK_URL": f"{local_server.base_url}/cgi-bin/webhook/send?key=test-key",
        "MRFM_INBOX_DIR": str(tmp_path / "inbox"),
        "MRFM_OUT_DIR": str(tmp_path / "out"),
        "MRFM_STATE_DB": str(tmp_path / "state" / "mrfm.sqlite3"),
        "MRFM_RULES_DIR": str(tmp_path / "rules"),
        "MRFM_HEARTBEAT_SEC": "1",
    }
    return Config.from_env(env, base_dir=tmp_path)


def _build(config: Config) -> Daemon:
    watcher = InboxWatcher(
        config.inbox_dir,
        out_dir=config.out_dir,
        poll_interval=0.2,
        settle_checks=2,
    )
    return build_daemon(config, watcher=watcher)


def _cut_batch(path: Path) -> BatchFile:
    watcher = InboxWatcher(path.parent, out_dir=path.parent)
    batch = watcher.cut_batch(path)
    assert batch is not None, "样例批次文件切分失败"
    return batch


# ---------------------------------------------------------------------------
# 1. 逐行判定行
# ---------------------------------------------------------------------------


def test_逐行判定只列违规与不可判定(tmp_path, local_server, caplog):
    """逐行判定行覆盖全部需人工处理的结论，且**不刷合规结论**。"""
    landing = _prepare(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    caplog.set_level(1)
    try:
        batch = _cut_batch(landing)
        daemon.handle_batch(batch)
        messages = [record.getMessage() for record in caplog.records]
        rows = daemon.store.list_judgements(batch.batch_no)
    finally:
        daemon.close()

    actionable = [row for row in rows if row.conclusion != Conclusion.COMPLIANT]
    compliant = [row for row in rows if row.conclusion == Conclusion.COMPLIANT]
    assert actionable, "样例批次应当有需人工处理的结论"
    assert compliant, "样例批次应当有合规结论"

    # 引导行：说明「下面这些要人工处理」
    lead = [text for text in messages if text.startswith("逐行判定：")]
    assert lead, "画面上应有逐行判定的引导行"
    assert f"以下 {len(actionable)} 条需要人工处理" in lead[0]
    assert f"合规 {len(compliant)} 条" in lead[0]

    # 每条需人工处理的结论都有一行，且点名处方号 / 规则号 / 结论
    for row in actionable:
        prefix = f"判定 {row.prescription_no} · {row.rule_id} · {row.conclusion.value} · "
        assert any(text.startswith(prefix) for text in messages), (
            f"缺少逐行判定行：{prefix}"
        )

    # 合规结论**不进画面**（否则会把要看的几十行淹没）
    for row in compliant:
        prefix = f"判定 {row.prescription_no} · {row.rule_id} · 合规"
        assert not any(text.startswith(prefix) for text in messages), (
            f"合规结论不该逐行刷屏：{prefix}"
        )


def test_不可判定行点名缺失字段(tmp_path, local_server, caplog):
    """不可判定的逐行判定行要点名缺哪个字段（信息科据此排接口补齐顺序）。"""
    landing = _prepare(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    caplog.set_level(1)
    try:
        batch = _cut_batch(landing)
        daemon.handle_batch(batch)
        messages = [record.getMessage() for record in caplog.records]
        rows = daemon.store.list_judgements(batch.batch_no)
    finally:
        daemon.close()

    undeterminable = [
        row for row in rows if row.conclusion == Conclusion.UNDETERMINABLE
    ]
    assert undeterminable, "样例批次应当有不可判定结论"
    for row in undeterminable:
        prefix = f"判定 {row.prescription_no} · {row.rule_id} · 不可判定 · "
        hit = [text for text in messages if text.startswith(prefix)]
        assert hit, f"缺少不可判定逐行判定行：{prefix}"
        assert "缺少 " in hit[0]
        for field in row.missing_fields:
            assert field in hit[0], f"逐行判定行没点名缺失字段 {field}"


def test_违规行给出违规要点(tmp_path, local_server, caplog):
    """违规的逐行判定行给出违规要点（不是把整段归因文本原样刷出来）。"""
    landing = _prepare(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    caplog.set_level(1)
    try:
        batch = _cut_batch(landing)
        daemon.handle_batch(batch)
        messages = [record.getMessage() for record in caplog.records]
        rows = daemon.store.list_judgements(batch.batch_no)
    finally:
        daemon.close()

    violations = [row for row in rows if row.conclusion == Conclusion.VIOLATION]
    assert violations, "样例批次应当有违规结论"
    for row in violations:
        prefix = f"判定 {row.prescription_no} · {row.rule_id} · 违规 · "
        hit = [text for text in messages if text.startswith(prefix)]
        assert hit, f"缺少违规逐行判定行：{prefix}"
        detail = hit[0][len(prefix) :]
        assert detail, "违规逐行判定行不能没有要点"
        # 要点来自归因文本（违规原因），但**不带患者可识别明细**
        assert "依据：" not in detail


# ---------------------------------------------------------------------------
# 2. 推送回执行
# ---------------------------------------------------------------------------


def test_推送回执行记送达与卡片长度(tmp_path, local_server, caplog):
    """推送成功后画面上有一行回执行（送达 / HTTP 状态 / 卡片长度 / 尝试次数）。"""
    landing = _prepare(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    caplog.set_level(1)
    try:
        batch = _cut_batch(landing)
        outcome = daemon.handle_batch(batch)
        messages = [record.getMessage() for record in caplog.records]
    finally:
        daemon.close()

    assert outcome.pushed, "样例批次应当推送成功"
    receipt = [text for text in messages if text.startswith("推送 企业微信 · ")]
    assert receipt, "画面上应有推送回执行"
    assert "已送达" in receipt[0]
    assert "HTTP 200" in receipt[0]
    assert "卡片" in receipt[0]
    assert "尝试 1 次" in receipt[0]
    # webhook 地址是凭据位，**不得出现在画面上**
    assert "key=test-key" not in receipt[0]
    assert local_server.base_url not in receipt[0]


def test_推送未送达时回执行记未送达(tmp_path, local_server, caplog, monkeypatch):
    """推送未送达时回执行写「未送达」，且**不影响判定与回写**。"""
    from med_rule_fieldmap.outbound.wecom import PushOutcome

    landing = _prepare(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    caplog.set_level(1)

    def _not_sent(*args: object, **kwargs: object) -> PushOutcome:
        return PushOutcome(
            batch_no="DEMO-BATCH-001",
            sent=False,
            status_code=500,
            reason="企业微信推送失败：HTTP 500",
        )

    monkeypatch.setattr(daemon.notifier, "notify", _not_sent)
    try:
        batch = _cut_batch(landing)
        outcome = daemon.handle_batch(batch)
        messages = [record.getMessage() for record in caplog.records]
    finally:
        daemon.close()

    assert not outcome.pushed
    receipt = [text for text in messages if text.startswith("推送 企业微信 · ")]
    assert receipt, "推送失败也要有回执行"
    assert "未送达" in receipt[0]
    # 判定照常落盘
    assert outcome.judgement_rows > 0


# ---------------------------------------------------------------------------
# 3. 画面分层的日志级别
# ---------------------------------------------------------------------------


def test_逐行判定与推送回执行走次级日志档(tmp_path, local_server, caplog):
    """逐行判定 / 推送回执行走 LEVEL_ROW（次级），批次摘要走 LEVEL_BATCH。"""
    landing = _prepare(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    caplog.set_level(1)
    try:
        batch = _cut_batch(landing)
        daemon.handle_batch(batch)
        levels = {record.getMessage(): record.levelno for record in caplog.records}
    finally:
        daemon.close()

    from med_rule_fieldmap.logging_setup import LEVEL_BATCH, LEVEL_ROW

    summary = [text for text in levels if text.startswith("批次 ") and "收到" in text]
    assert summary, "画面上应有批次摘要行"
    assert levels[summary[0]] == LEVEL_BATCH

    row_lines = [text for text in levels if text.startswith("判定 ")]
    assert row_lines, "画面上应有逐行判定行"
    assert all(levels[text] == LEVEL_ROW for text in row_lines)

    receipt = [text for text in levels if text.startswith("推送 企业微信 · ")]
    assert receipt, "画面上应有推送回执行"
    assert levels[receipt[0]] == LEVEL_ROW


def test_心跳与审计走最暗灰那档(tmp_path, local_server, caplog):
    """心跳行与审计行走 LEVEL_HEARTBEAT / LEVEL_AUDIT（最暗灰，不刷屏）。"""
    landing = _prepare(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    caplog.set_level(1)
    try:
        batch = _cut_batch(landing)
        daemon.handle_batch(batch)
        daemon.emit_heartbeat()
        pairs = [(record.getMessage(), record.levelno) for record in caplog.records]
    finally:
        daemon.close()

    from med_rule_fieldmap.logging_setup import LEVEL_AUDIT, LEVEL_HEARTBEAT

    # 心跳行的正文是「规则 N 条 · 已处理 M 批 · 待判定 K 条」（[alive] 前缀由渲染器补）
    heartbeat = [level for text, level in pairs if "已处理" in text and "待判定" in text]
    assert heartbeat, "画面上应有心跳行"
    assert all(level == LEVEL_HEARTBEAT for level in heartbeat)

    audit = [level for text, level in pairs if text.startswith("批次 ") and "事件指纹" in text]
    assert audit, "画面上应有审计行"
    assert all(level == LEVEL_AUDIT for level in audit)
