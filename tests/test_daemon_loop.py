"""守护进程主循环测试（主循环 + 心跳 + 批次处理 + 规则热重载 + 优雅退出）。

**替身说明**：本测试用**本地真实 HTTP 服务**（``http.server``）充当企业微信
机器人 webhook 与 HIS 审方复核接口——传输层是真 ``httpx``、真 TCP、真 HTTP 报文；
被测的**产品代码**走的是 spec.md §2.4 的真实协议（POST +
``{"msgtype":"markdown","markdown":{"content":…}}``）与 §2.3 的真实回写地址。
替身只负责「按剧本回一个响应」并记录请求，不替代任何产品逻辑。

覆盖五条硬边界：

1. **投文件即产出** —— 落地目录里出现一份医嘱批次文件后，守护进程在 60 秒内
   产出判定台账、不可判定台账与字段补齐顺序表，并发出**一次**企业微信推送；
2. **规则热重载** —— 运行中改规则文件会被热加载并打一行热重载日志，
   **新规则只影响其后到达的批次**（已在判的批次不被中途改写）；
3. **优雅退出** —— 收到 ``SIGTERM`` 后完成当前批次再退出，不留半批状态；
4. **画面口径** —— banner 行含产品名 / 规则条数 / 入站目录 / 回写目标，
   心跳行格式为 ``[alive] 规则 N 条 · 已处理 M 批 · 待判定 K 条``；
5. **凭据位缺失即显式失败** —— ``HIS_BASE_URL`` 没配时守护进程起不来，
   **不退化成只读旁路**假装还在干活。
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from med_rule_fieldmap.config import Config
from med_rule_fieldmap.daemon import (
    HEARTBEAT_PREFIX,
    PRODUCT_NAME,
    Daemon,
    DaemonError,
    build_daemon,
)
from med_rule_fieldmap.ingest.watcher import InboxWatcher
from med_rule_fieldmap.ledger.ledger import load_judgement_ledger
from med_rule_fieldmap.logging_setup import LEVEL_HEARTBEAT, setup_logging
from med_rule_fieldmap.outbound.his_review import HisCredentialError
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_BATCH = PROJECT_ROOT / "tests" / "data" / "batch" / "demo_batch_001.csv"
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"

#: 守护进程出结果的最长等待（秒）——任务书要求 60 秒内产出
READY_TIMEOUT_SEC = 60.0


# ---------------------------------------------------------------------------
# 本地 HTTP 替身（真实 HTTP 服务，按剧本回响应）
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """记录请求并按剧本回响应（企业微信与 HIS 审方复核共用一个服务）。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": "POST",
                "path": self.path,
                "headers": {key: value for key, value in self.headers.items()},
                "raw": raw,
                "json": parsed,
            }
        )
        prescription_no = ""
        if isinstance(parsed, dict):
            prescription_no = str(parsed.get("prescription_no") or "")
        payload = {
            # 企业微信机器人协议
            "errcode": 0,
            "errmsg": "ok",
            # HIS 审方复核协议
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
        self.server.requests = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self) -> list[dict[str, object]]:
        return list(self.server.requests)  # type: ignore[attr-defined]

    def webhook_requests(self) -> list[dict[str, object]]:
        return [item for item in self.requests if "webhook" in str(item["path"])]

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


def _make_config(tmp_path: Path, local_server: _LocalServer, **env_overrides: str) -> Config:
    """按临时目录 + 本地替身地址造一份配置快照。"""
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
    env.update(env_overrides)
    return Config.from_env(env, base_dir=tmp_path)


def _prepare_dirs(tmp_path: Path) -> None:
    """建落地目录 / 产物目录 / 状态目录，并把样例规则复制过去。"""
    (tmp_path / "inbox").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE_RULES, tmp_path / "rules", dirs_exist_ok=True)


def _build(config: Config) -> Daemon:
    """装配守护进程（监听轮询间隔调快，测试不必等 1 秒一拍）。"""
    watcher = InboxWatcher(
        config.inbox_dir,
        out_dir=config.out_dir,
        poll_interval=0.2,
        settle_checks=2,
    )
    return build_daemon(config, watcher=watcher)


def _wait_for(predicate, timeout: float = READY_TIMEOUT_SEC, interval: float = 0.2) -> bool:
    """轮询等待条件成立（最多 timeout 秒）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _DaemonRunner:
    """在后台线程里跑守护进程，便于测试观察它的产出。"""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.daemon.run()
        except BaseException as exc:  # pragma: no cover - 断言时才会走到
            self.error = exc

    def start(self) -> "_DaemonRunner":
        self.thread.start()
        return self

    def join(self, timeout: float = 30.0) -> None:
        self.thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# 1. 投文件即产出
# ---------------------------------------------------------------------------


def test_daemon_produces_ledgers_and_pushes_card(tmp_path, local_server):
    """投一份样例批次文件 → 60 秒内产出三份台账并发出一次企业微信推送。"""
    _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    runner = _DaemonRunner(daemon).start()
    try:
        # 先投文件：守护进程起来时目录是空的，事件到达即处理。
        shutil.copy(SAMPLE_BATCH, tmp_path / "inbox" / "demo_batch_001.csv")

        ledger = tmp_path / "out" / "judgement_ledger.csv"
        assert _wait_for(lambda: ledger.exists() and len(load_judgement_ledger(ledger)) > 0), (
            "守护进程 60 秒内没有产出判定台账"
        )

        records = load_judgement_ledger(ledger)
        # 26 行医嘱 × 命中的规则条数：逐处方 × 逐规则恰好一行。
        assert len(records) > 26
        assert {r["batch_no"] for r in records} == {"DEMO-BATCH-001"}
        # 三态结论都在台账里，一条不落。
        conclusions = {r["conclusion"] for r in records}
        assert conclusions <= {"合规", "违规", "不可判定"}
        assert "不可判定" in conclusions

        # 另外两份台账（字段缺口 / 补齐顺序）也要落盘。
        assert (tmp_path / "out" / "undeterminable_ledger.csv").exists()
        assert (tmp_path / "out" / "field_gap_priority.csv").exists()

        # 推送：恰好一次，正文是 markdown 卡片。
        assert _wait_for(lambda: len(local_server.webhook_requests()) >= 1), (
            "守护进程没有发出企业微信推送"
        )
        pushes = local_server.webhook_requests()
        assert len(pushes) == 1
        body = pushes[0]["json"]
        assert isinstance(body, dict)
        assert body["msgtype"] == "markdown"
        content = body["markdown"]["content"]
        assert "DEMO-BATCH-001" in content
        assert "不可判定" in content

        assert daemon.batches_processed == 1
        assert daemon.pushes == 1
        assert daemon.last_outcome is not None
        assert daemon.last_outcome.duplicate is False
        assert daemon.last_outcome.judgement_rows == len(records)
    finally:
        daemon.stop()
        runner.join(10)


# ---------------------------------------------------------------------------
# 2. 规则热重载：只影响其后到达的批次
# ---------------------------------------------------------------------------


def test_rules_hot_reload_only_affects_later_batches(tmp_path, local_server):
    """改规则文件 → 热重载日志出现；已在判的批次不被中途改写。"""
    _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    runner = _DaemonRunner(daemon).start()
    try:
        shutil.copy(SAMPLE_BATCH, tmp_path / "inbox" / "demo_batch_001.csv")
        ledger = tmp_path / "out" / "judgement_ledger.csv"
        assert _wait_for(lambda: ledger.exists() and len(load_judgement_ledger(ledger)) > 0)
        first_rows = len(load_judgement_ledger(ledger))
        rule_count_before = len(daemon.rules)

        # 追加一条新规则（合法 → 会被载入生效集）。
        extra = tmp_path / "rules" / "extra.yaml"
        extra.write_text(
            "rules:\n"
            "  - rule_id: RX-DAEMON-HOT-RELOAD\n"
            "    version: v1\n"
            "    layer: 合法性\n"
            "    basis: 守护进程热重载验收用例\n"
            "    severity: 严重不合理用药\n"
            "    required_fields: [days_qty]\n"
            "    expression: days_qty > 0\n"
            "    missing_conclusion: 不可判定\n",
            encoding="utf-8",
        )

        # 下一批到达时才热重载（新规则只影响其后到达的批次）。
        second = SAMPLE_BATCH.read_text(encoding="utf-8").replace(
            "DEMO-BATCH-001", "DEMO-BATCH-002"
        )
        (tmp_path / "inbox" / "demo_batch_002.csv").write_text(second, encoding="utf-8")
        assert _wait_for(lambda: daemon.batches_processed >= 2), "第二批没有到达"
        assert len(daemon.rules) == rule_count_before + 1
        assert daemon.repository.by_rule_id("RX-DAEMON-HOT-RELOAD") is not None

        # 新规则对后续批次生效：命中行数比第一批多（每张处方多一条判定）。
        rows = load_judgement_ledger(ledger)
        second_batch = [r for r in rows if r["rule_id"] == "RX-DAEMON-HOT-RELOAD"]
        assert second_batch, "热重载后的新规则没有对后续批次生效"
        assert {r["batch_no"] for r in second_batch} == {"DEMO-BATCH-002"}
        assert len(rows) > first_rows
    finally:
        daemon.stop()
        runner.join(10)


def test_reload_rules_reports_change(tmp_path, local_server):
    """``reload_rules()`` 只在规则真变了时返回 True，并打一行热重载日志。"""
    _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    try:
        assert daemon.reload_rules() is False  # 规则没动过
        (tmp_path / "rules" / "extra.yaml").write_text(
            "rules:\n"
            "  - rule_id: RX-DAEMON-HOT-RELOAD\n"
            "    version: v1\n"
            "    layer: 合法性\n"
            "    basis: 守护进程热重载验收用例\n"
            "    severity: 严重不合理用药\n"
            "    required_fields: [days_qty]\n"
            "    expression: days_qty > 0\n"
            "    missing_conclusion: 不可判定\n",
            encoding="utf-8",
        )
        assert daemon.reload_rules() is True
    finally:
        daemon.close()


# ---------------------------------------------------------------------------
# 3. 优雅退出
# ---------------------------------------------------------------------------


def test_sigterm_finishes_current_batch_and_exits(tmp_path, local_server):
    """收到 SIGTERM → 当前批次做完再退出，不留半批状态。"""
    _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)

    previous = signal.getsignal(signal.SIGTERM)
    installed = daemon.install_signal_handlers()
    try:
        assert int(signal.SIGTERM) in installed, "SIGTERM 处理器没有装上"
        runner = _DaemonRunner(daemon).start()
        try:
            shutil.copy(SAMPLE_BATCH, tmp_path / "inbox" / "demo_batch_001.csv")
            ledger = tmp_path / "out" / "judgement_ledger.csv"
            assert _wait_for(lambda: ledger.exists() and len(load_judgement_ledger(ledger)) > 0)

            # 真的发一个 SIGTERM（处理器已装上，进程不会被默认动作杀掉）。
            os.kill(os.getpid(), signal.SIGTERM)
            runner.join(30)
            assert not runner.thread.is_alive(), "守护进程收到 SIGTERM 后没有退出"
            assert runner.error is None

            # 不留半批状态：台账完整（表头 + 全部判定行），且行数与批次结论一致。
            records = load_judgement_ledger(ledger)
            assert len(records) == daemon.last_outcome.judgement_rows
            assert daemon.batches_processed == 1
        finally:
            daemon.stop()
            runner.join(10)
    finally:
        signal.signal(signal.SIGTERM, previous)
        daemon.close()


def test_stop_request_is_graceful(tmp_path, local_server):
    """``stop()`` 等价于 SIGTERM：批之间退出，不打断正在处理的批次。"""
    _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    runner = _DaemonRunner(daemon).start()
    try:
        shutil.copy(SAMPLE_BATCH, tmp_path / "inbox" / "demo_batch_001.csv")
        ledger = tmp_path / "out" / "judgement_ledger.csv"
        assert _wait_for(lambda: ledger.exists() and len(load_judgement_ledger(ledger)) > 0)
        daemon.stop()
        runner.join(30)
        assert not runner.thread.is_alive()
        assert runner.error is None
    finally:
        daemon.stop()
        runner.join(10)
        daemon.close()


# ---------------------------------------------------------------------------
# 4. 画面口径
# ---------------------------------------------------------------------------


def test_banner_and_heartbeat_lines(tmp_path, local_server):
    """banner 行四要素齐全；心跳行格式固定。"""
    _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    try:
        banner = daemon.banner()
        assert PRODUCT_NAME in banner
        assert "规则" in banner
        assert str(config.inbox_dir) in banner
        assert "/api/v1/prescription/review" in banner

        line = daemon.heartbeat_line()
        assert line.startswith(HEARTBEAT_PREFIX)
        assert "规则" in line and "已处理" in line and "待判定" in line
        assert "已处理 0 批" in line
    finally:
        daemon.close()


def test_heartbeat_counts_pending_review(tmp_path, local_server):
    """心跳里的「待判定」是还没确认的结论条数。"""
    _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    runner = _DaemonRunner(daemon).start()
    try:
        shutil.copy(SAMPLE_BATCH, tmp_path / "inbox" / "demo_batch_001.csv")
        ledger = tmp_path / "out" / "judgement_ledger.csv"
        assert _wait_for(lambda: ledger.exists() and len(load_judgement_ledger(ledger)) > 0)
        expected = len(load_judgement_ledger(ledger))
        assert daemon.pending_review_count() == expected
        assert f"待判定 {expected} 条" in daemon.heartbeat_line()
    finally:
        daemon.stop()
        runner.join(10)
        daemon.close()


def test_rendered_heartbeat_line_has_single_prefix(tmp_path, local_server):
    """终端上看到的心跳行只带一次 ``[alive]`` 前缀。"""
    _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    stream = io.StringIO()
    try:
        setup_logging(level=LEVEL_HEARTBEAT, stream=stream, color=False, force=True)
        assert daemon.emit_heartbeat() is True
        rendered = stream.getvalue()
        assert rendered.count("[alive]") == 1, f"心跳行前缀重复：{rendered!r}"
        assert daemon.heartbeat_line() in rendered
    finally:
        setup_logging(level=logging.INFO, force=True)
        daemon.close()


# ---------------------------------------------------------------------------
# 5. 凭据位缺失即显式失败
# ---------------------------------------------------------------------------


def test_missing_his_credentials_fail_explicitly(tmp_path, local_server):
    """``HIS_BASE_URL`` 缺失 → 装配显式失败，不退化成只读旁路。"""
    _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server, HIS_BASE_URL="")
    with pytest.raises(HisCredentialError) as excinfo:
        build_daemon(config)
    assert "HIS_BASE_URL" in str(excinfo.value)


def test_empty_rule_set_refuses_to_judge(tmp_path, local_server):
    """生效规则集为空 → 拒绝判定（否则每张处方都会「合规」）。"""
    _prepare_dirs(tmp_path)
    empty_rules = tmp_path / "empty_rules"
    empty_rules.mkdir()
    config = _make_config(tmp_path, local_server, MRFM_RULES_DIR=str(empty_rules))
    daemon = _build(config)
    try:
        from med_rule_fieldmap.ingest.watcher import BatchFile

        batch = BatchFile(
            path=tmp_path / "inbox" / "x.csv",
            batch_no="B-EMPTY",
            rows=(),
        )
        with pytest.raises(DaemonError):
            daemon.handle_batch(batch)
    finally:
        daemon.close()


def test_repository_rejects_invalid_rule_without_affecting_others(tmp_path, local_server):
    """非法规则被拒也不影响其余规则生效（守护进程热重载时的同一纪律）。"""
    _prepare_dirs(tmp_path)
    (tmp_path / "rules" / "broken.yaml").write_text(
        "rules:\n"
        "  - rule_id: RX-BROKEN\n"
        "    version: v1\n"
        "    layer: 合法性\n"
        "    expression: days_qty > 1\n",
        encoding="utf-8",
    )
    config = _make_config(tmp_path, local_server)
    repository = RuleRepository(config.rules_dir)
    repository.load()
    assert repository.errors, "缺 basis 的规则应当被拒"
    assert len(repository.effective_rules) >= 10, "非法规则不应影响其余规则生效"
