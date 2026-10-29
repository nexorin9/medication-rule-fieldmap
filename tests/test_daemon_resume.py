"""守护进程韧性测试（断点续跑 + 单实例锁 + 运行摘要）。

**替身说明**：本测试用**本地真实 HTTP 服务**（``http.server``）充当企业微信
机器人 webhook 与 HIS 审方复核接口——传输层是真 ``httpx``、真 TCP、真 HTTP
报文；被测的**产品代码**走的是 spec.md §2.4 的真实推送协议与 §2.3 的真实回写
地址。替身只负责「按剧本回一个响应」并记录请求，不替代任何产品逻辑。

覆盖三组硬边界：

1. **断点续跑** —— 进程在批次的任意一步被 kill 后重启，守护进程从**最后没走完
   的那一步**接着走：判定不重复、卡片不重推、回写不重发；源文件不在了或被改过
   时保留断点等人工重投，**绝不拿别的数据凑数、也绝不静默丢弃**；
2. **单实例锁** —— 同一个状态库同时只允许一个守护进程在跑；第二个实例显式
   报错退出并打印占用进程信息；进程被 ``kill -9`` 后锁由内核自动释放
   （不留「谁来都起不来」的死锁）；
3. **运行摘要** —— 每批次结束打印一行摘要（收到行数 / 合规 / 违规 / 不可判定 /
   回写数 / 耗时），并落库供运维 CLI 的 ``status`` 子命令读取（跨进程重启也还在）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from med_rule_fieldmap.cli import main as cli_main
from med_rule_fieldmap.config import Config
from med_rule_fieldmap.daemon import (
    EXIT_ALREADY_RUNNING,
    Daemon,
    build_daemon,
    main as daemon_main,
)
from med_rule_fieldmap.daemon_lock import (
    DaemonLockedError,
    SingleInstanceLock,
    holder_of,
    lock_path_for,
)
from med_rule_fieldmap.ingest.fingerprint import file_fingerprint
from med_rule_fieldmap.ingest.watcher import (
    EVENT_BATCH,
    BatchFile,
    InboxWatcher,
    WatcherEvent,
)
from med_rule_fieldmap.ledger.ledger import load_judgement_ledger
from med_rule_fieldmap.rules.repository import RuleRepository
from med_rule_fieldmap.store import (
    STAGE_CLOSED,
    STAGE_JUDGED,
    STAGE_RECEIVED,
    Store,
    process_batch,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_BATCH = PROJECT_ROOT / "tests" / "data" / "batch" / "demo_batch_001.csv"
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"

#: 守护进程出结果的最长等待（秒）
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
        # 剧本：让企业微信推送失败（测「推送失败留断点、下次启动重推」）。
        if getattr(self.server, "fail_webhook", False) and "webhook" in self.path:
            data = json.dumps({"errcode": 93000, "errmsg": "invalid webhook"}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
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
        self.server.requests = []  # type: ignore[attr-defined]
        self.server.fail_webhook = False  # type: ignore[attr-defined]
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

    def review_requests(self) -> list[dict[str, object]]:
        return [
            item
            for item in self.requests
            if str(item["path"]).endswith("/api/v1/prescription/review")
        ]

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


def _make_config(tmp_path: Path, local_server: _LocalServer) -> Config:
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
    return Config.from_env(env, base_dir=tmp_path)


def _prepare_dirs(tmp_path: Path) -> Path:
    """建落地目录 / 产物目录 / 状态目录，复制样例规则与样例批次文件。"""
    (tmp_path / "inbox").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE_RULES, tmp_path / "rules", dirs_exist_ok=True)
    inbox_file = tmp_path / "inbox" / "demo_batch_001.csv"
    shutil.copy(SAMPLE_BATCH, inbox_file)
    return inbox_file


def _build(config: Config) -> Daemon:
    """装配守护进程（轮询间隔调快，测试不必等 1 秒一拍）。"""
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


def _rules_for(config: Config) -> list:
    """载入生效规则集（续跑用同一份规则，避免口径漂移）。"""
    repository = RuleRepository(config.rules_dir)
    repository.load()
    return list(repository.effective_rules)


def _cut_batch(path: Path) -> BatchFile:
    """把样例文件切成一个批次（用真实的读入链路）。"""
    watcher = InboxWatcher(path.parent, out_dir=path.parent)
    batch = watcher.cut_batch(path)
    assert batch is not None, "样例批次文件切分失败"
    return batch


# ---------------------------------------------------------------------------
# 1. 断点续跑
# ---------------------------------------------------------------------------


def test_resume_judges_interrupted_batch_without_duplicate_rows(
    tmp_path, local_server
):
    """判定途中被 kill → 重启后续跑：判定行一条不多、一条不少。"""
    inbox_file = _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    batch = _cut_batch(inbox_file)
    fingerprint = file_fingerprint(inbox_file)

    # 模拟「判定前崩溃」：进度只记到「已接收」，判定行一条都没有。
    store = Store(config.state_db)
    try:
        store.record_batch_progress(
            batch_no=batch.batch_no,
            event_fingerprint=fingerprint,
            source_path=str(inbox_file),
            source_file=inbox_file.name,
            source_kind=batch.source_kind,
            prescription_count=batch.prescription_count,
            stage=STAGE_RECEIVED,
        )
        assert store.count_judgements() == 0
        assert len(store.list_unfinished_batches()) == 1
    finally:
        store.close()

    # 「重启」：新进程、新守护进程，从断点接着走。
    daemon = _build(config)
    try:
        resumed = daemon.resume_pending_batches()
        assert len(resumed) == 1, "断点续跑没有把没走完的批次接上"
        assert resumed[0].batch_no == batch.batch_no

        rows = load_judgement_ledger(config.out_dir / "judgement_ledger.csv")
        assert len(rows) == resumed[0].judgement_rows
        # 逐处方 × 逐规则恰好一行：重判没有产生第二份。
        keys = [(r["prescription_no"], r["rule_id"]) for r in rows]
        assert len(keys) == len(set(keys)), "断点续跑产生了重复判定行"

        # 判定完 → 卡片也发出去了（之前没发过）。
        assert len(local_server.webhook_requests()) == 1
        # 闭环完成，断点被清掉。
        store = Store(config.state_db)
        try:
            assert store.count_unfinished_batches() == 0
            progress = store.list_batch_progress()[0]
            assert progress.stage == STAGE_CLOSED
        finally:
            store.close()
    finally:
        daemon.close()


def test_resume_does_not_repeat_judgement_when_already_judged(
    tmp_path, local_server
):
    """判定已完成但没推卡片 → 重启后只补推送，判定行数不变。"""
    inbox_file = _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    batch = _cut_batch(inbox_file)
    fingerprint = file_fingerprint(inbox_file)

    # 模拟「判定提交后、推送前崩溃」：判定行已落库，阶段停在「已判定」。
    store = Store(config.state_db)
    try:
        outcome = process_batch(store, batch, _rules_for(config))
        store.record_batch_progress(
            batch_no=batch.batch_no,
            event_fingerprint=fingerprint,
            source_path=str(inbox_file),
            source_file=inbox_file.name,
            source_kind=batch.source_kind,
            prescription_count=batch.prescription_count,
            judgement_rows=outcome.total_judgements,
            stage=STAGE_JUDGED,
        )
        before = store.count_judgements()
        assert before > 0
    finally:
        store.close()

    daemon = _build(config)
    try:
        resumed = daemon.resume_pending_batches()
        assert len(resumed) == 1
        assert resumed[0].judgement_rows == before, "续跑把判定重做了一遍"

        store = Store(config.state_db)
        try:
            assert store.count_judgements() == before, "续跑多出了判定行"
            assert store.count_unfinished_batches() == 0
        finally:
            store.close()
        # 卡片补发了一次（之前没发过）。
        assert len(local_server.webhook_requests()) == 1
    finally:
        daemon.close()


def test_resume_does_not_push_second_card_after_crash(tmp_path, local_server):
    """卡片已发出但没回写 → 重启后**不再推第二张**，只补回写。"""
    inbox_file = _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    batch = _cut_batch(inbox_file)
    fingerprint = file_fingerprint(inbox_file)

    store = Store(config.state_db)
    try:
        outcome = process_batch(store, batch, _rules_for(config))
        store.record_batch_progress(
            batch_no=batch.batch_no,
            event_fingerprint=fingerprint,
            source_path=str(inbox_file),
            source_file=inbox_file.name,
            source_kind=batch.source_kind,
            prescription_count=batch.prescription_count,
            judgement_rows=outcome.total_judgements,
            stage=STAGE_JUDGED,
        )
        # 卡片上次已经发出去了（记下推送时那批判定的行数）。
        store.mark_batch_pushed(
            batch.batch_no, fingerprint, judgement_rows=outcome.total_judgements
        )
        before = store.count_judgements()
    finally:
        store.close()

    # 卡片上次已发出（崩溃发生在推送之后、回写之前）。
    daemon = _build(config)
    try:
        resumed = daemon.resume_pending_batches()
        assert len(resumed) == 1
        assert resumed[0].pushed is False, "续跑又推了一张卡片"
        # 一次卡片都没发出去。
        assert local_server.webhook_requests() == []
        store = Store(config.state_db)
        try:
            assert store.count_judgements() == before
            assert store.count_unfinished_batches() == 0
        finally:
            store.close()
    finally:
        daemon.close()


def test_resume_keeps_breakpoint_when_source_file_missing(tmp_path, local_server):
    """源文件已不在落地目录 → 保留断点 + 告警，不静默丢弃、不拿别的数据凑。"""
    inbox_file = _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    batch = _cut_batch(inbox_file)
    fingerprint = file_fingerprint(inbox_file)

    store = Store(config.state_db)
    try:
        store.record_batch_progress(
            batch_no=batch.batch_no,
            event_fingerprint=fingerprint,
            source_path=str(inbox_file),
            source_file=inbox_file.name,
            source_kind=batch.source_kind,
            prescription_count=batch.prescription_count,
            stage=STAGE_RECEIVED,
        )
    finally:
        store.close()

    # 源文件被人挪走了（入站只读，但运维会清目录）。
    inbox_file.unlink()

    daemon = _build(config)
    try:
        resumed = daemon.resume_pending_batches()
        assert resumed == [], "源文件不在了却硬判定了"
        assert daemon.store.count_judgements() == 0
        # 断点还在：等人工重投，不静默丢弃。
        assert daemon.store.count_unfinished_batches() == 1
    finally:
        daemon.close()


def test_resume_refuses_when_source_content_changed(tmp_path, local_server):
    """源文件内容变了 → 不按旧断点续跑（否则会用新数据解释旧结论）。"""
    inbox_file = _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    batch = _cut_batch(inbox_file)
    fingerprint = file_fingerprint(inbox_file)

    store = Store(config.state_db)
    try:
        store.record_batch_progress(
            batch_no=batch.batch_no,
            event_fingerprint=fingerprint,
            source_path=str(inbox_file),
            source_file=inbox_file.name,
            source_kind=batch.source_kind,
            prescription_count=batch.prescription_count,
            stage=STAGE_RECEIVED,
        )
    finally:
        store.close()

    # 文件内容被改了（换批次号）。
    inbox_file.write_text(
        SAMPLE_BATCH.read_text(encoding="utf-8").replace(
            "DEMO-BATCH-001", "DEMO-BATCH-999"
        ),
        encoding="utf-8",
    )

    daemon = _build(config)
    try:
        resumed = daemon.resume_pending_batches()
        assert resumed == [], "源文件变了却按旧断点续跑了"
        assert daemon.store.count_judgements() == 0
        # 断点保留：这条事件仍要靠正常投递流程按新内容处理。
        assert daemon.store.count_unfinished_batches() == 1
    finally:
        daemon.close()


def test_closed_batch_is_not_resumed_again(tmp_path, local_server):
    """已闭环的批次重启后不再处理：零重复判定、零重复推送。"""
    inbox_file = _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    batch = _cut_batch(inbox_file)
    fingerprint = file_fingerprint(inbox_file)

    store = Store(config.state_db)
    try:
        outcome = process_batch(store, batch, _rules_for(config))
        store.record_batch_progress(
            batch_no=batch.batch_no,
            event_fingerprint=fingerprint,
            source_path=str(inbox_file),
            source_file=inbox_file.name,
            source_kind=batch.source_kind,
            prescription_count=batch.prescription_count,
            judgement_rows=outcome.total_judgements,
            stage=STAGE_CLOSED,
        )
        before = store.count_judgements()
    finally:
        store.close()

    daemon = _build(config)
    try:
        assert daemon.resume_pending_batches() == []
        assert daemon.store.count_judgements() == before
        assert local_server.webhook_requests() == []
    finally:
        daemon.close()


def test_resume_after_real_restart_produces_no_duplicate_judgements(
    tmp_path, local_server
):
    """真跑一遍守护 → 进程退出 → 再起一个守护：判定不重复、卡片不重推。"""
    inbox_file = _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)

    daemon = _build(config)
    try:
        daemon.on_event(
            WatcherEvent(
                kind=EVENT_BATCH,
                path=inbox_file,
                batch=_cut_batch(inbox_file),
            )
        )
        before = daemon.store.count_judgements()
        assert before > 0
        assert len(local_server.webhook_requests()) == 1
        progress = daemon.store.list_batch_progress()[0]
        assert progress.stage == STAGE_CLOSED
    finally:
        daemon.close()

    # 「重启」后的守护：断点续跑——已闭环的批次不再处理。
    daemon2 = _build(config)
    try:
        assert daemon2.resume_pending_batches() == []
        assert daemon2.store.count_judgements() == before, "重启后判定行数变了"
        assert len(local_server.webhook_requests()) == 1, "重启后又推了一张卡片"
        assert daemon2.store.count_unfinished_batches() == 0
    finally:
        daemon2.close()


def test_restart_reprocessing_same_file_does_not_push_second_card(
    tmp_path, local_server
):
    """落地文件留在 inbox → 重启后又被看到一次，但卡片**不推第二张**。

    入站只读，落地文件不会被挪走；守护进程重启后会重新看到它。
    判定幂等键保证不产生第二份判定行，推送去重保证不推第二张卡片。
    """
    inbox_file = _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    batch = _cut_batch(inbox_file)

    daemon = _build(config)
    try:
        daemon.handle_batch(batch)
        before = daemon.store.count_judgements()
        assert len(local_server.webhook_requests()) == 1
    finally:
        daemon.close()

    # 「重启」：同一个文件又被处理一次。
    daemon2 = _build(config)
    try:
        daemon2.handle_batch(_cut_batch(inbox_file))
        assert daemon2.store.count_judgements() == before, "重启后多出了判定行"
        assert len(local_server.webhook_requests()) == 1, "重启后又推了一张卡片"
    finally:
        daemon2.close()


def test_failed_push_keeps_breakpoint_and_is_retried(tmp_path, local_server):
    """推送失败 → 留断点（下次启动重推），重推不产生第二份判定行。"""
    inbox_file = _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)

    local_server.server.fail_webhook = True  # type: ignore[attr-defined]
    daemon = _build(config)
    try:
        daemon.handle_batch(_cut_batch(inbox_file))
        before = daemon.store.count_judgements()
        assert before > 0
        # 卡片没送出去 → 批次留断点（等下次启动重推）。
        assert daemon.store.count_unfinished_batches() == 1
        assert daemon.last_outcome is not None
        assert daemon.last_outcome.pushed is False
    finally:
        daemon.close()

    # 下次启动：推送通道恢复 → 续跑把卡片补发出去，判定不重做。
    local_server.server.fail_webhook = False  # type: ignore[attr-defined]
    daemon2 = _build(config)
    try:
        resumed = daemon2.resume_pending_batches()
        assert len(resumed) == 1
        assert resumed[0].pushed is True
        assert daemon2.store.count_judgements() == before, "重推多出了判定行"
        assert daemon2.store.count_unfinished_batches() == 0
    finally:
        daemon2.close()


# ---------------------------------------------------------------------------
# 2. 单实例锁
# ---------------------------------------------------------------------------


def test_second_instance_is_rejected_with_holder_info(tmp_path):
    """第二个实例拿不到锁 → 显式报错，错误信息点名占用进程。"""
    path = lock_path_for(tmp_path / "state" / "mrfm.sqlite3")
    first = SingleInstanceLock(path, state_db=tmp_path / "state" / "mrfm.sqlite3")
    holder = first.acquire()
    try:
        assert holder.pid == os.getpid()
        second = SingleInstanceLock(path)
        with pytest.raises(DaemonLockedError) as excinfo:
            second.acquire()
        message = str(excinfo.value)
        assert f"pid {os.getpid()}" in message
        assert "重复回写" in message
        assert excinfo.value.holder.pid == os.getpid()
    finally:
        first.release()


def test_lock_can_be_reacquired_after_release(tmp_path):
    """放掉锁之后，另一个实例能正常拿到（不留死锁）。"""
    path = lock_path_for(tmp_path / "state" / "mrfm.sqlite3")
    first = SingleInstanceLock(path)
    first.acquire()
    first.release()
    second = SingleInstanceLock(path)
    second.acquire()
    try:
        assert second.acquired is True
    finally:
        second.release()


def test_lock_is_released_when_process_is_killed(tmp_path):
    """持锁进程被 ``kill -9`` → 锁由内核释放，另一个实例能拿到。"""
    path = lock_path_for(tmp_path / "state" / "mrfm.sqlite3")
    script = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT / 'src')!r})\n"
        "from med_rule_fieldmap.daemon_lock import SingleInstanceLock\n"
        f"lock = SingleInstanceLock({str(path)!r})\n"
        "lock.acquire()\n"
        "print('locked', flush=True)\n"
        "time.sleep(120)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        assert line == "locked", f"子进程没有拿到锁：{line!r}"

        # 父进程拿不到锁。
        with pytest.raises(DaemonLockedError):
            SingleInstanceLock(path).acquire()

        # kill -9 之后锁由内核释放。
        proc.kill()
        proc.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                lock = SingleInstanceLock(path)
                lock.acquire()
            except DaemonLockedError:
                time.sleep(0.1)
                continue
            lock.release()
            break
        else:  # pragma: no cover - 内核释放应当是即时的
            pytest.fail("持锁进程被 kill 后锁没有释放")
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()
            proc.wait(timeout=10)


def test_daemon_entrypoint_rejects_second_instance(tmp_path, local_server, capsys):
    """守护进程入口：已有实例在跑 → 退出码 3 + 打印占用进程信息。"""
    _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    lock = SingleInstanceLock(lock_path_for(config.state_db), state_db=config.state_db)
    lock.acquire()
    try:
        code = daemon_main(
            [
                "--inbox",
                str(config.inbox_dir),
                "--out",
                str(config.out_dir),
                "--rules",
                str(config.rules_dir),
                "--state-db",
                str(config.state_db),
            ]
        )
        assert code == EXIT_ALREADY_RUNNING
        captured = capsys.readouterr()
        assert "已被另一个守护进程占用" in captured.err
        assert f"pid {os.getpid()}" in captured.err
    finally:
        lock.release()


def test_holder_of_reports_running_daemon(tmp_path):
    """``holder_of`` 能看出守护在不在跑（CLI status 用它）。"""
    path = lock_path_for(tmp_path / "state" / "mrfm.sqlite3")
    assert holder_of(path) is None
    lock = SingleInstanceLock(path)
    lock.acquire()
    try:
        holder = holder_of(path)
        assert holder is not None
        assert holder.pid == os.getpid()
    finally:
        lock.release()
    assert holder_of(path) is None


# ---------------------------------------------------------------------------
# 3. 运行摘要
# ---------------------------------------------------------------------------


def test_run_summary_line_and_persisted_metrics(tmp_path, local_server):
    """每批次结束打印一行摘要，并落库（CLI status 读得到）。"""
    inbox_file = _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    try:
        batch = _cut_batch(inbox_file)
        outcome = daemon.handle_batch(batch)

        line = daemon.run_summary_line(outcome)
        assert "运行摘要" in line
        assert f"收到 {outcome.received_rows} 行" in line
        assert f"合规 {outcome.compliant}" in line
        assert f"违规 {outcome.violation}" in line
        assert f"不可判定 {outcome.undeterminable}" in line
        assert f"回写 {outcome.writeback_sent} 条" in line
        assert "耗时" in line

        runs = daemon.store.list_batch_runs()
        assert len(runs) == 1
        run = runs[0]
        assert run.batch_no == outcome.batch_no
        assert run.received_rows == outcome.received_rows
        assert run.judgement_rows == outcome.judgement_rows
        assert run.compliant == outcome.compliant
        assert run.violation == outcome.violation
        assert run.undeterminable == outcome.undeterminable
        assert run.writeback_sent == outcome.writeback_sent
        assert run.elapsed_sec > 0
    finally:
        daemon.close()


def test_cli_status_reads_run_summary(tmp_path, local_server, capsys):
    """CLI ``status`` 读到运行摘要（累计 + 最近批次 + 守护是否在跑）。"""
    inbox_file = _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    daemon = _build(config)
    try:
        daemon.handle_batch(_cut_batch(inbox_file))
    finally:
        daemon.close()

    code = cli_main(["status", "--db", str(config.state_db), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["daemon_running"] is False
    assert payload["batches"] == 1
    assert payload["judgement_rows"] > 0
    assert payload["pending_review"] == payload["judgement_rows"]
    assert payload["totals"]["received_rows"] == 26
    assert payload["totals"]["compliant"] > 0
    assert payload["latest_batch"]["batch_no"] == "DEMO-BATCH-001"
    assert payload["latest_batch"]["elapsed_sec"] > 0


def test_cli_status_text_output(tmp_path, local_server, capsys):
    """CLI ``status`` 文本输出有人读的摘要。"""
    _prepare_dirs(tmp_path)
    config = _make_config(tmp_path, local_server)
    code = cli_main(["status", "--db", str(config.state_db)])
    assert code == 0
    out = capsys.readouterr().out
    assert "守护进程：未在运行" in out
    assert "状态库" in out
