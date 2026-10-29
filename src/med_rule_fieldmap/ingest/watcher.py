"""入站目录监听：文件写完即切批次；写坏的文件整文件挂起。

**对应源产品能力**：源仓库 ``backend/src/advocate/connection.py`` 的
``validating_create_connection()``（**先校验、再动作**：校验不过就显式抛错，
不带着不合法目标往下走）与 ``attempt_timeout(timeout, deadline)``
（单次动作的预算压到「剩余 deadline」）。**改写**：入站侧同样「先校验、
再动作」——落地文件必须先过完整性校验才切批次；写入未在截止时间内稳定的
文件按「疑似截断」挂起，而不是抢着读半份数据。

**为什么不能一见文件就读**：HIS 导出是**边写边落盘**的。文件刚出现时通常
只写了一半，此刻去读会拿到一份看起来正常的半份数据。因此本模块要求
**文件大小与 mtime 连续两次观察都不变**才认为写入完成，再切批次。

**为什么要有挂起**：写入没写完、编码乱了、必需列缺了 —— 这三种情况都
**整文件挂起**，绝不部分判定（半份数据会让药师拿到一份看起来正常、实则
漏判的台账）。挂起台账记下原因与缺列清单，文件留在 ``inbox/`` 原地
（入站只读），人工解除后重投即可。

**监听方式**：优先用 ``watchdog`` 的文件系统事件（事件到达即唤醒）；
``watchdog`` 不可用时退化为 1 秒轮询 ``os.scandir``。两条路径共用同一套
「稳定性判定 + 完整性校验 + 切批次」逻辑，因此行为一致。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..models import PrescriptionRow
from .csv_reader import CSV_SUFFIXES, CsvReadError, read_prescription_rows
from .hl7_orm import HL7_SUFFIXES, Hl7ParseError, read_order_rows
from .integrity import KIND_TRUNCATED, IntegrityReport, check_file_integrity
from .suspend import (
    STATUS_SUSPENDED,
    SuspendLedger,
    SuspendRecord,
    default_ledger_path,
)

logger = logging.getLogger("med_rule_fieldmap.ingest")

#: 事件类型：文件已写完并切分出批次
EVENT_BATCH = "batch"
#: 事件类型：文件被整文件挂起
EVENT_SUSPENDED = "suspended"

#: 落地目录里忽略的后缀（HIS 导出过程中的临时文件）
IGNORED_SUFFIXES: tuple[str, ...] = (".tmp", ".part", ".crdownload", ".filepart", ".download")

#: 默认轮询间隔（秒）——watchdog 不可用时的退化路径
DEFAULT_POLL_INTERVAL = 1.0
#: 默认「连续两次观察不变才算写完」的次数
DEFAULT_SETTLE_CHECKS = 2
#: 默认写入截止时间（秒）：文件在此时间内始终不稳定即判为疑似截断并挂起
DEFAULT_WRITE_DEADLINE_SEC = 300.0


@dataclass
class FileState:
    """一个候选文件的写入状态观察记录。"""

    size: int
    mtime_ns: int
    stable_count: int = 1
    first_seen: float = 0.0

    def signature(self) -> tuple[int, int]:
        """稳定性签名：``(大小, mtime)``。"""
        return self.size, self.mtime_ns


@dataclass(frozen=True)
class BatchFile:
    """切分出来的一批医嘱（一个落地文件 = 一个批次）。

    ``batch_no`` 取自文件内容（``batch_no`` 列 / HL7 ``MSH-10``），文件里没写
    时回落到文件名（spec.md §2.1）。
    """

    path: Path
    batch_no: str
    rows: tuple[PrescriptionRow, ...] = ()
    warnings: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    source_kind: str = "csv"

    @property
    def prescription_count(self) -> int:
        """本批医嘱行数。"""
        return len(self.rows)


@dataclass(frozen=True)
class WatcherEvent:
    """一次监听产生的事件。

    * :data:`EVENT_BATCH` —— 文件写完且校验通过，已切出批次（``batch`` 非空）；
    * :data:`EVENT_SUSPENDED` —— 文件被整文件挂起（``report`` 非空），
      **不产生任何判定行**。
    """

    kind: str
    path: Path
    batch: BatchFile | None = None
    report: IntegrityReport | None = None
    reason: str = ""

    @property
    def is_batch(self) -> bool:
        return self.kind == EVENT_BATCH

    @property
    def is_suspended(self) -> bool:
        return self.kind == EVENT_SUSPENDED


def watchdog_available() -> bool:
    """``watchdog`` 是否可用（不可用时退化为轮询）。"""
    try:  # pragma: no cover - 取决于运行环境是否装了 watchdog
        import watchdog.events  # noqa: F401
        import watchdog.observers  # noqa: F401
    except Exception:  # pragma: no cover - 缺依赖或平台不支持
        return False
    return True


class InboxWatcher:
    """医嘱落地目录监听器。

    参数
    ----
    inbox_dir:
        医嘱落地目录（**只读**）。
    out_dir:
        产物目录，挂起台账落在这里（``out/suspended_ledger.csv``）。
    poll_interval:
        轮询间隔（秒）；watchdog 可用时作为事件等待的超时兜底。
    settle_checks:
        「连续几次观察不变才算写完」；默认 2。
    write_deadline_sec:
        写入截止时间（秒）：文件在此时间内始终不稳定即判为疑似截断并挂起。
    suspend_ledger:
        挂起台账；缺省时按 ``out_dir`` 建默认台账。
    use_watchdog:
        是否用 watchdog；``None`` 表示自动探测。
    clock:
        时钟（单调递增秒）；测试可注入假时钟。
    """

    def __init__(
        self,
        inbox_dir: str | Path,
        *,
        out_dir: str | Path = "out",
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        settle_checks: int = DEFAULT_SETTLE_CHECKS,
        write_deadline_sec: float = DEFAULT_WRITE_DEADLINE_SEC,
        suspend_ledger: SuspendLedger | None = None,
        use_watchdog: bool | None = None,
        clock=None,
    ) -> None:
        self.inbox_dir = Path(inbox_dir)
        self.out_dir = Path(out_dir)
        self.poll_interval = float(poll_interval)
        self.settle_checks = max(1, int(settle_checks))
        self.write_deadline_sec = float(write_deadline_sec)
        self.ledger = suspend_ledger or SuspendLedger(default_ledger_path(self.out_dir))
        self._clock = clock or time.monotonic
        if use_watchdog is None:
            use_watchdog = watchdog_available()
        self.use_watchdog = bool(use_watchdog)
        self._states: dict[Path, FileState] = {}
        self._processed: set[Path] = set()
        self._wake = threading.Event()

    # -- 目录扫描 -----------------------------------------------------------

    def list_candidate_files(self) -> list[Path]:
        """列出落地目录里的候选批次文件（按文件名排序，保证处理顺序稳定）。"""
        if not self.inbox_dir.is_dir():
            return []
        candidates: list[Path] = []
        for entry in os.scandir(self.inbox_dir):
            if not entry.is_file():
                continue
            name = entry.name
            if name.startswith("."):
                continue  # 隐藏文件
            suffix = Path(name).suffix.lower()
            if suffix in IGNORED_SUFFIXES:
                continue  # 导出过程中的临时文件
            if suffix not in CSV_SUFFIXES and suffix not in HL7_SUFFIXES:
                continue
            candidates.append(Path(entry.path))
        return sorted(candidates, key=lambda item: item.name)

    # -- 一次轮询 -----------------------------------------------------------

    def poll_once(self) -> list[WatcherEvent]:
        """扫描一次目录，返回本轮产生的事件列表。

        每个候选文件走三步：

        1. **稳定性观察**：``(大小, mtime)`` 与上次一致才算数，连续
           ``settle_checks`` 次不变才认为写入完成；
        2. **完整性校验**：不通过 → 整文件挂起（挂起台账 + 告警），**零判定行**；
        3. **切分批次**：通过 → 解析成处方行，产出批次事件。
        """
        events: list[WatcherEvent] = []
        for path in self.list_candidate_files():
            if path in self._processed:
                continue
            if self.ledger.is_suspended(path.name):
                continue  # 已挂起且未解除：不重复处理，等人工放行

            state = self._states.get(path)
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover - 文件在扫描期间被移走
                self._states.pop(path, None)
                continue

            now = self._clock()
            if state is None:
                self._states[path] = FileState(
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    stable_count=1,
                    first_seen=now,
                )
                continue  # 第一次看到：等下一次观察再判断是否写完

            if state.signature() == (stat.st_size, stat.st_mtime_ns):
                state.stable_count += 1
            else:
                state.size = stat.st_size
                state.mtime_ns = stat.st_mtime_ns
                state.stable_count = 1

            if state.stable_count < self.settle_checks:
                if now - state.first_seen > self.write_deadline_sec:
                    events.append(self._suspend_for_timeout(path, state, now))
                continue

            event = self._process(path)
            if event is not None:
                events.append(event)
        return events

    def _suspend_for_timeout(
        self, path: Path, state: FileState, now: float
    ) -> WatcherEvent:
        """写入超过截止时间仍不稳定 → 疑似截断，整文件挂起。"""
        reason = (
            f"文件 {path.name} 在 {self.write_deadline_sec:.0f} 秒内持续变化"
            f"（当前 {state.size} 字节），判定为写入未完成，疑似截断，整文件挂起"
        )
        report = IntegrityReport(path, KIND_TRUNCATED, reason)
        self._record_suspend(path, report)
        return WatcherEvent(
            kind=EVENT_SUSPENDED, path=path, report=report, reason=report.reason
        )

    def _process(self, path: Path) -> WatcherEvent | None:
        """校验完整性 → 切批次或挂起。"""
        try:
            report = check_file_integrity(path)
        except OSError as exc:
            report = IntegrityReport(path, KIND_TRUNCATED, f"文件无法读取：{exc}")
        if not report.ok:
            self._record_suspend(path, report)
            return WatcherEvent(
                kind=EVENT_SUSPENDED, path=path, report=report, reason=report.reason
            )

        try:
            batch = self._cut_batch(path)
        except (CsvReadError, Hl7ParseError) as exc:
            report = IntegrityReport(path, KIND_TRUNCATED, str(exc))
            self._record_suspend(path, report)
            return WatcherEvent(
                kind=EVENT_SUSPENDED, path=path, report=report, reason=report.reason
            )

        self._processed.add(path)
        for warning in report.warnings:
            logger.warning("[watch] %s", warning)
        return WatcherEvent(kind=EVENT_BATCH, path=path, batch=batch)

    def cut_batch(self, path: Path) -> BatchFile | None:
        """把一份落地文件切分成批次（**不**做完整性校验）。

        公开入口：守护进程的**断点续跑**要用它把上次没判完的源文件重新读成
        批次（见 ``daemon.py`` 的 ``_resume_one``）。解析失败返回 ``None``
        ——续跑宁可留断点等人重投，也不拿一份半截数据凑数。
        """
        try:
            return self._cut_batch(Path(path))
        except (CsvReadError, Hl7ParseError, OSError) as exc:
            logger.warning("[watch] 文件无法切分为批次：%s（%s）", path, exc)
            return None

    def _cut_batch(self, path: Path) -> BatchFile:
        """把一份写完且校验通过的文件切分成批次。"""
        if path.suffix.lower() in HL7_SUFFIXES:
            parsed = read_order_rows(path)
            rows = list(parsed.rows)
            warnings = list(parsed.warnings)
            skipped = [item.reason for item in parsed.skipped]
            source_kind = "hl7"
        else:
            rows = read_prescription_rows(path)
            warnings = []
            skipped = []
            source_kind = "csv"

        batch_no = ""
        if rows:
            batch_no = rows[0].batch_no.strip()
        if not batch_no:
            batch_no = path.stem

        return BatchFile(
            path=path,
            batch_no=batch_no,
            rows=tuple(rows),
            warnings=tuple(warnings),
            skipped=tuple(skipped),
            source_kind=source_kind,
        )

    def _record_suspend(self, path: Path, report: IntegrityReport) -> None:
        """写挂起台账 + 告警日志（挂起的文件**不产生任何判定行**）。"""
        record = SuspendRecord(
            file_name=path.name,
            source_path=str(path),
            kind=report.kind,
            reason=report.reason,
            missing_columns=report.missing_columns,
            status=STATUS_SUSPENDED,
        )
        self.ledger.append(record)
        logger.warning("[watch] %s", report.summary())

    # -- 人工解除 -----------------------------------------------------------

    def release(self, file_name: str, *, detail: str = "") -> bool:
        """人工解除挂起：解除后该文件会在下一轮轮询里重新处理（可重投）。

        稳定性观察记录**保留**：文件没动过就直接重判；动过（重投了新内容）
        则重新计两次稳定观察再切批次。
        """
        released = self.ledger.release(file_name, detail=detail)
        if released is None:
            return False
        path = self.inbox_dir / file_name
        self._processed.discard(path)
        logger.info(
            "[watch] 已人工解除挂起：%s（原因：%s），等待重投",
            file_name,
            released.reason,
        )
        return True

    def pending_suspended(self) -> list[SuspendRecord]:
        """当前仍挂着的文件列表。"""
        return self.ledger.pending()

    def pending_candidates(self) -> list[Path]:
        """尚未处理、也未挂起的候选文件（运维看「还有多少份在等」）。"""
        return [
            path
            for path in self.list_candidate_files()
            if path not in self._processed and not self.ledger.is_suspended(path.name)
        ]

    # -- 常驻循环 -----------------------------------------------------------

    def run(self, stop_event: threading.Event, on_event=None) -> None:
        """常驻监听循环。

        * watchdog 可用 → 用文件系统事件唤醒（事件到达即处理）；
        * watchdog 不可用 → 每 ``poll_interval`` 秒轮询一次；
        * 收到 ``stop_event`` 后退出（守护进程的优雅退出由调用方驱动）。
        """
        observer = None
        if self.use_watchdog:
            observer = self._start_observer()
        try:
            while not stop_event.is_set():
                for event in self.poll_once():
                    if on_event is not None:
                        on_event(event)
                self._wait(stop_event)
        finally:
            if observer is not None:
                observer.stop()
                observer.join(timeout=5)

    def _wait(self, stop_event: threading.Event) -> None:
        """等待下一次唤醒（watchdog 事件或轮询超时）。"""
        if self._wake.is_set():
            self._wake.clear()
            return
        stop_event.wait(self.poll_interval)

    def _start_observer(self):
        """启动 watchdog 观察者；任何异常都退化为轮询。"""
        try:  # pragma: no cover - 取决于运行环境是否装了 watchdog
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            watcher = self

            class _Handler(FileSystemEventHandler):
                def on_any_event(self, event) -> None:  # pragma: no cover - 事件回调
                    watcher._wake.set()

            observer = Observer()
            observer.schedule(_Handler(), str(self.inbox_dir), recursive=False)
            observer.start()
            logger.info("[watch] 已启用 watchdog 文件系统事件监听")
            return observer
        except Exception as exc:  # pragma: no cover - 平台不支持时退化
            logger.warning(
                "[watch] watchdog 不可用（%s），退化为 %.1f 秒轮询",
                exc,
                self.poll_interval,
            )
            return None


__all__ = [
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_SETTLE_CHECKS",
    "DEFAULT_WRITE_DEADLINE_SEC",
    "EVENT_BATCH",
    "EVENT_SUSPENDED",
    "IGNORED_SUFFIXES",
    "BatchFile",
    "FileState",
    "InboxWatcher",
    "WatcherEvent",
    "watchdog_available",
]
