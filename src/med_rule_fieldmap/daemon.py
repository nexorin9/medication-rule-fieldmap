"""常驻守护进程：监听医嘱落地目录，事件到达即判定、落盘、推送、回写。

**交付形态**（spec.md §3 / §6）：本产品是**事件驱动守护**，不是「跑一次出一张表」
的脚本。守护进程常驻在药学部内网单机上::

    python -m med_rule_fieldmap.daemon

**主路径**（入口 → 主循环 → 产出，与源产品的调用链同构）::

    落地目录事件
      → 切批次（ingest/watcher.py）
      → 逐处方 × 逐规则三态判定（engine/judge.py）
      → 台账落盘 + 字段缺口聚合（ledger/*.py）
      → 推企业微信批次卡片（outbound/wecom.py）
      → 回写**已确认**结论到 HIS（outbound/his_review.py）

**源产品能力对应**（spec.md §7 / §8）：源仓库
``contrib/automation/nodes/node_types.py``（节点类型注册表）与
``automation/workflows/graph_handler.py``（图执行 + 异步任务）——源产品把
「监听 → 取数 → 逐节点执行 → 出站调用」做成常驻的自动化运行时。
**改写**：本轮把「图执行」改写成**单进程守护 + 一条心跳线程**：主线程跑监听与
批次处理（一次只处理一个批次，批次之间不留半批状态），心跳线程只负责常驻画面
的节奏行；源产品的「节点类型注册表」改写成**规则行仓库**（``rules/repository.py``），
规则集可在运行中**热重载**，不必重启进程。

**三条硬边界**：

1. **未确认绝不回写** —— 只有状态为「药师已确认」且有确认留痕的结论才会进回写
   请求体（门禁在 ``engine/confirm.py``，本模块只负责调用，不自己拼请求体）；
2. **推送失败不阻断主流程** —— 企业微信挂了只记告警，判定台账与 HIS 回写
   一个字节都不受影响；
3. **不静默跳过** —— ``HIS_BASE_URL`` 未配置时守护进程**显式失败并退出**，
   不退化成「只读旁路」假装还在干活。

**进程结构**：主线程 = 监听 + 批次处理；心跳线程 = 周期行（``[alive] …``）。
收到 ``SIGTERM`` / ``SIGINT`` 后**完成当前批次再退出**（不留半批状态）。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .config import Config, ConfigError
from .daemon_lock import DaemonLockedError, SingleInstanceLock, lock_path_for
from .engine.confirm import ConfirmService
from .ingest.fingerprint import file_fingerprint
from .ingest.watcher import (
    EVENT_BATCH,
    EVENT_SUSPENDED,
    BatchFile,
    InboxWatcher,
    WatcherEvent,
)
from .ledger.audit_export import export_audit_ledger
from .ledger.gap import (
    build_gap_entries,
    index_prescriptions,
    write_undeterminable_ledger,
)
from .ledger.ledger import LEDGER_FILENAME, write_judgement_ledger
from .ledger.priority import build_priority_table, write_priority_table
from .logging_setup import (
    LEVEL_HEARTBEAT,
    Heartbeat,
    get_logger,
    log_alert,
    log_audit,
    log_batch_summary,
    log_row_detail,
    log_writeback,
    setup_logging,
)
from .models import Conclusion, JudgementRow, JudgementState, PrescriptionRow
from .outbound.client import OutboundClient
from .outbound.his_review import (
    BINDING_AMBIGUITY_FILENAME,
    BindingAmbiguityLedger,
    HisCredentialError,
    HisReviewWriter,
)
from .outbound.idempotency import WritebackIdempotencyGuard
from .outbound.retry_queue import PENDING_WRITEBACK_FILENAME, PendingWritebackQueue
from .outbound.wecom import (
    WeComNotifier,
    items_from_gap_entries,
    items_from_priority_table,
    summarize_judgements,
)
from .rules.repository import RuleRepository
from .run_summary import record_batch
from .store import (
    STAGE_CLOSED,
    STAGE_JUDGED,
    STAGE_RECEIVED,
    BatchProgress,
    BatchRun,
    Store,
    process_batch,
)

#: 产品名（banner 行用）
PRODUCT_NAME = "合理用药审方字段对照系统"

#: 心跳行前缀（与 logging_setup 的 ``[alive]`` 前缀一致）
HEARTBEAT_PREFIX = "[alive]"

#: 退出码：正常退出
EXIT_OK = 0
#: 退出码：配置缺失 / 运行失败
EXIT_ERROR = 2
#: 退出码：状态库已被另一个守护进程占用（本实例拒绝启动）
EXIT_ALREADY_RUNNING = 3

#: 「还没走完药师确认这条路的判定行」= 待药师复核的工作队列长度。
#: 心跳行里的「待判定 K 条」取这个口径——系统已判出结论、但结论还没被药师
#: 复核确认，就是**待判定**；一旦确认（进入「药师已确认」及以后）就不再计入。
PENDING_REVIEW_STATES: tuple[JudgementState, ...] = (
    JudgementState.PENDING,
    JudgementState.DECIDED,
    JudgementState.PENDING_FIELDS,
)


class DaemonError(RuntimeError):
    """守护进程无法继续（配置缺失、生效规则集为空等）。"""


#: 归因文本里「依据：」之前的那段就是违规要点（evidence.py 的固定句式）
_BASIS_MARKER = "依据："
_VIOLATION_MARKER = "判定：违规。"


def _row_detail(row: JudgementRow) -> str:
    """逐行判定行的末段摘要（一行说清「为什么」）。

    * 不可判定 → ``缺少 rx_type|drug_category|days_qty``（点名缺失字段）；
    * 违规 → 归因文本里「依据：」之前的违规要点；
    * 其他 → 归因文本首句（截断到 60 字，避免刷屏）。
    """
    if row.conclusion == Conclusion.UNDETERMINABLE:
        missing = "|".join(row.missing_fields or ())
        return f"缺少 {missing}" if missing else "缺少字段未记录"
    reason = row.reason or ""
    if row.conclusion == Conclusion.VIOLATION and _VIOLATION_MARKER in reason:
        detail = reason.split(_VIOLATION_MARKER, 1)[1].split(_BASIS_MARKER, 1)[0].strip()
        if detail:
            return detail
    first = reason.split("。", 1)[0].strip()
    return first if first else "（归因文本未记录）"


def _format_push_receipt(result: Any) -> str:
    """推送回执行：送达与否 + HTTP 状态 + 卡片长度 + 尝试次数。

    **不打印 webhook 地址**——地址里的 key 是凭据位（``mask_webhook_url``
    负责脱敏）。
    """
    if bool(getattr(result, "sent", False)):
        return (
            f"推送 企业微信 · 已送达 · HTTP {getattr(result, 'status_code', 0)} · "
            f"卡片 {getattr(result, 'content_chars', 0)} 字 · "
            f"尝试 {getattr(result, 'attempts', 0)} 次"
        )
    reason = str(getattr(result, "reason", "") or "原因未记录")
    return f"推送 企业微信 · 未送达 · {reason}"


@dataclass
class BatchOutcome:
    """一个批次在守护进程里的处理结果（供测试与运维核对）。"""

    batch_no: str
    source_file: str = ""
    received_rows: int = 0
    judgement_rows: int = 0
    compliant: int = 0
    violation: int = 0
    undeterminable: int = 0
    duplicate: bool = False
    fingerprint: str = ""
    ledger_path: Path | None = None
    gap_path: Path | None = None
    priority_path: Path | None = None
    audit_path: Path | None = None
    pushed: bool = False
    push_reason: str = ""
    writeback_sent: int = 0
    writeback_failed: int = 0
    elapsed_sec: float = 0.0

    def to_dict(self) -> dict[str, object]:
        """转成可 JSON 化的字典。"""
        return {
            "batch_no": self.batch_no,
            "source_file": self.source_file,
            "received_rows": self.received_rows,
            "judgement_rows": self.judgement_rows,
            "compliant": self.compliant,
            "violation": self.violation,
            "undeterminable": self.undeterminable,
            "duplicate": self.duplicate,
            "event_fingerprint": self.fingerprint,
            "ledger_path": str(self.ledger_path) if self.ledger_path else "",
            "gap_ledger_path": str(self.gap_path) if self.gap_path else "",
            "field_gap_priority_path": str(self.priority_path) if self.priority_path else "",
            "audit_ledger_path": str(self.audit_path) if self.audit_path else "",
            "pushed": self.pushed,
            "push_reason": self.push_reason,
            "writeback_sent": self.writeback_sent,
            "writeback_failed": self.writeback_failed,
            "elapsed_sec": round(self.elapsed_sec, 3),
        }


class Daemon:
    """事件驱动守护进程。

    参数
    ----
    config:
        配置快照（路径、凭据位、心跳间隔全部从这里来）。
    store:
        状态库（批次 / 判定 / 审计 / 回写身份都落这里）。
    repository:
        规则行仓库（负责载入校验与**热重载**）。
    watcher:
        落地目录监听器（watchdog 优先，不可用时退化为轮询）。
    notifier:
        企业微信推送通道（``notify()`` 入口：失败只告警不抛）。
    service:
        药师确认 + 回写门禁服务（本模块只调用，不自己拼回写请求体）。
    lock:
        状态库单实例锁（可选；进程入口会自己建一把）。
    """

    def __init__(
        self,
        config: Config,
        *,
        store: Store,
        repository: RuleRepository,
        watcher: InboxWatcher,
        notifier: WeComNotifier,
        service: ConfirmService,
        out_dir: Path | None = None,
        heartbeat_sec: int | None = None,
        logger_: logging.Logger | None = None,
        lock: SingleInstanceLock | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.repository = repository
        self.watcher = watcher
        self.notifier = notifier
        self.service = service
        self.lock = lock
        self.out_dir = Path(out_dir) if out_dir is not None else config.out_dir
        self._logger = logger_ or get_logger("med_rule_fieldmap.daemon")
        self._heartbeat = Heartbeat(
            int(heartbeat_sec if heartbeat_sec is not None else config.heartbeat_sec),
            logger_=get_logger("med_rule_fieldmap.heartbeat"),
        )
        self._stop_event = threading.Event()
        self._batches = 0
        self._suspended = 0
        self._pushes = 0
        self._last_outcome: BatchOutcome | None = None
        self._outcomes: list[BatchOutcome] = []
        self._prescriptions: dict[str, PrescriptionRow] = {}
        self._heartbeat_thread: threading.Thread | None = None
        self._installed_signals: list[int] = []
        # 规则行还没载入过就先载入一次（守护进程起手就要有生效规则集，
        # 否则 banner 行上的「规则 N 条」是 0，看着像没在工作）。
        if not self.repository.rules:
            self.repository.load()
            for error in self.repository.errors:
                log_alert(self._logger, f"规则被拒（不影响其余规则生效）：{error}")

    # -- 只读属性 -----------------------------------------------------------

    @property
    def rules(self) -> tuple:
        """当前生效规则集。"""
        return self.repository.effective_rules

    @property
    def batches_processed(self) -> int:
        """已处理批次数。"""
        return self._batches

    @property
    def suspended_files(self) -> int:
        """被整文件挂起的落地文件数。"""
        return self._suspended

    @property
    def pushes(self) -> int:
        """已发出的企业微信卡片数。"""
        return self._pushes

    @property
    def last_outcome(self) -> BatchOutcome | None:
        """最近一个批次的处理结果。"""
        return self._last_outcome

    @property
    def outcomes(self) -> list[BatchOutcome]:
        """历次批次处理结果（按发生顺序）。"""
        return list(self._outcomes)

    @property
    def stop_event(self) -> threading.Event:
        """停止事件（守护进程退出时置位）。"""
        return self._stop_event

    # -- 画面（banner / 心跳）-----------------------------------------------

    def banner(self) -> str:
        """启动横幅：产品名 + 规则条数 + 入站目录 + 出站回写目标。"""
        try:
            target = self.config.his_review_url
        except ConfigError:
            target = "（未配置 HIS_BASE_URL）"
        return (
            f"{PRODUCT_NAME} · 规则 {len(self.rules)} 条 · "
            f"入站 {self.config.inbox_dir} · 回写 {target}"
        )

    def detail_line(self) -> str:
        """横幅下面那行：产物目录 / 状态库 / 心跳节奏。"""
        return (
            f"产物 {self.out_dir} · 状态库 {self.config.state_db} · "
            f"心跳 {self._heartbeat.interval_sec}s · "
            f"推送 {'已配置' if self.config.wecom_webhook_url else '未配置（推送会告警，不阻断判定）'}"
        )

    def heartbeat_message(self) -> str:
        """心跳行正文（不含前缀）：``规则 N 条 · 已处理 M 批 · 待判定 K 条``。

        前缀 ``[alive] `` 由日志渲染器补上（``LEVEL_HEARTBEAT`` 的固定前缀），
        所以这里**不要再拼一次**，否则终端上会变成 ``[alive] [alive] …``。
        """
        return (
            f"规则 {len(self.rules)} 条 · 已处理 {self._batches} 批 · "
            f"待判定 {self.pending_review_count()} 条"
        )

    def heartbeat_line(self) -> str:
        """心跳行（操作员在终端看到的那一行）。

        形如 ``[alive] 规则 18 条 · 已处理 1 批 · 待判定 192 条``。
        """
        return f"{HEARTBEAT_PREFIX} {self.heartbeat_message()}"

    def pending_review_count(self) -> int:
        """待药师复核的结论条数（还没确认的判定行）。"""
        return sum(
            1
            for row in self.store.list_judgements()
            if row.state in PENDING_REVIEW_STATES
        )

    def emit_heartbeat(self, now: float | None = None) -> bool:
        """打一行心跳（到点才打），返回是否真的打了这一行。"""
        return self._heartbeat.beat(self.heartbeat_message(), now)

    # -- 规则热重载 ---------------------------------------------------------

    def reload_rules(self) -> bool:
        """规则热重载：规则文件变了就重新载入，返回是否真的重载了。

        **只影响其后到达的批次**：热重载发生在批次之间（监听事件之间），
        正在判定中的批次用的是它开始时那份规则快照，不会被中途改写。
        """
        before = len(self.repository.effective_rules)
        changed = self.repository.reload_if_changed()
        if not changed:
            return False
        for error in self.repository.errors:
            log_alert(self._logger, f"规则被拒（不影响其余规则生效）：{error}")
        after = len(self.repository.effective_rules)
        log_row_detail(
            self._logger,
            f"规则已热重载：生效 {after} 条（原 {before} 条）· 只影响其后到达的批次",
        )
        return True

    # -- 批次处理 -----------------------------------------------------------

    def handle_batch(self, batch: BatchFile) -> BatchOutcome:
        """处理一个批次：判定 → 落盘 → 聚合 → 推送 → 回写。

        整条链在**一个事务**里推进：状态库写失败会回滚，不留半批状态
        （台账是文件，落盘失败则整批判为失败并告警）。

        **进度先落盘再判定**：指纹在判定之前就算好并记进批次进度表，
        这样「判定途中进程被 kill」也留下可见的断点——重启后守护进程知道
        这一批还没判完，可以接着走（见 :meth:`resume_pending_batches`）。
        """
        started = time.monotonic()
        rules = self.repository.effective_rules
        if not rules:
            # 空规则集会让每一张处方都「合规」——那是假结论，必须拒绝。
            raise DaemonError(
                f"生效规则集为空（规则目录 {self.config.rules_dir}）："
                "本批不做判定，拒绝用空规则集产出「全部合规」的结论。"
            )

        fingerprint = file_fingerprint(batch.path)
        self._record_intake(batch, fingerprint)
        self._prescriptions.update(index_prescriptions(batch.rows))

        outcome = process_batch(self.store, batch, rules, fingerprint=fingerprint)
        self._advance(
            batch.batch_no,
            fingerprint,
            STAGE_JUDGED,
            judgement_rows=outcome.total_judgements,
        )
        return self._finalize_batch(
            batch_no=batch.batch_no,
            source_file=batch.path.name,
            received_rows=len(batch.rows),
            fingerprint=fingerprint,
            counts=outcome.counts or {},
            new_counts=outcome.new_counts or {},
            new_judgement_rows=outcome.judgement_rows,
            duplicate=outcome.duplicate,
            started=started,
            resumed=False,
            push=True,
        )

    # -- 批次进度 -----------------------------------------------------------

    def _record_intake(self, batch: BatchFile, fingerprint: str) -> None:
        """登记一个批次事件（判定之前）：阶段「已接收」。"""
        self.store.record_batch_progress(
            batch_no=batch.batch_no,
            event_fingerprint=fingerprint,
            source_path=str(batch.path),
            source_file=batch.path.name,
            source_kind=batch.source_kind,
            prescription_count=batch.prescription_count,
            stage=STAGE_RECEIVED,
        )

    def _advance(
        self,
        batch_no: str,
        fingerprint: str,
        stage: str,
        *,
        judgement_rows: int = 0,
    ) -> None:
        """推进批次进度阶段（阶段只前进不后退）。"""
        if not fingerprint:
            return
        self.store.record_batch_progress(
            batch_no=batch_no,
            event_fingerprint=fingerprint,
            judgement_rows=judgement_rows,
            stage=stage,
        )

    # -- 断点续跑 -----------------------------------------------------------

    def resume_pending_batches(self) -> list[BatchOutcome]:
        """启动时从断点续跑：把上次没走完的批次接着走完。

        **零重复判定 / 零重复推送 / 零重复回写**：

        * **判定**：判定幂等键（``sha256(batch_no｜prescription_no｜rule_id｜
          rule_version)``）保证重判不会产生第二份判定行；
        * **推送**：批次进度阶段记着「卡片发过没有」，已发过的不再发第二张
          （同一份结论推两遍，药师会以为出了两次问题）；
        * **回写**：回写幂等键（``outbound/idempotency.py``）保证同一份请求
          不重复发。

        **源文件不在了就不硬来**：判定没做完而源文件已被移走时，只记告警并
        保留断点，等人工重投——绝不拿别的事件凑数，也绝不静默丢弃。
        """
        resumed: list[BatchOutcome] = []
        pending = self.store.list_unfinished_batches()
        if not pending:
            return resumed
        log_row_detail(self._logger, f"断点续跑：发现 {len(pending)} 个没走完的批次")
        for progress in pending:
            try:
                outcome = self._resume_one(progress)
            except Exception as exc:
                log_alert(
                    self._logger,
                    f"断点续跑失败（批次 {progress.batch_no} 保留断点等下次重试）：{exc}",
                )
                self._logger.debug("断点续跑失败明细", exc_info=True)
                continue
            if outcome is not None:
                resumed.append(outcome)
        return resumed

    def _resume_one(self, progress: BatchProgress) -> BatchOutcome | None:
        """续跑一个批次（按它停在哪一步决定接着做什么）。"""
        rows = self.store.list_judgements(progress.batch_no)
        if progress.stage == STAGE_RECEIVED and not rows:
            # 判定没做完 → 源文件还在就重判（幂等键保证不产生重复行）。
            source = Path(progress.source_path) if progress.source_path else None
            if source is None or not source.is_file():
                log_alert(
                    self._logger,
                    f"批次 {progress.batch_no} 判定未完成，且源文件已不在落地目录"
                    f"（{progress.source_path or '（未记录路径）'}）：保留断点，等人工重投",
                )
                return None
            current = file_fingerprint(source)
            if current != progress.event_fingerprint:
                log_alert(
                    self._logger,
                    f"批次 {progress.batch_no} 的源文件内容已变化（指纹 {current[:12]} ≠ "
                    f"{progress.event_fingerprint[:12]}）：不按旧断点续跑，"
                    "改由正常投递流程按新内容处理",
                )
                return None
            batch = self.watcher.cut_batch(source)
            if batch is None:
                log_alert(
                    self._logger,
                    f"批次 {progress.batch_no} 的源文件已无法解析：保留断点，等人工重投",
                )
                return None
            log_row_detail(
                self._logger,
                f"断点续跑：批次 {progress.batch_no} 判定未完成，按源文件重判"
                f"（{len(batch.rows)} 行）",
            )
            return self.handle_batch(batch)

        # 判定已完成：只补台账与回写；卡片发过的不再重推
        # （「发过没」由 _finalize_batch 按已推送的判定行数判断，
        #  规则改版导致判定行数变了时，卡片该再推一次还是要推）。
        self._reindex_prescriptions(progress)
        log_row_detail(
            self._logger,
            f"断点续跑：批次 {progress.batch_no} 判定已完成（{len(rows)} 条），"
            "补齐台账与回写",
        )
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.conclusion.value] = counts.get(row.conclusion.value, 0) + 1
        return self._finalize_batch(
            batch_no=progress.batch_no,
            source_file=progress.source_file,
            received_rows=progress.prescription_count,
            fingerprint=progress.event_fingerprint,
            counts=counts,
            new_counts={},
            new_judgement_rows=0,
            duplicate=False,
            started=time.monotonic(),
            resumed=True,
            push=True,
        )

    # -- 收尾 ---------------------------------------------------------------

    def _reindex_prescriptions(self, progress: BatchProgress) -> None:
        """续跑时补一次处方索引（字段缺口台账要按科室聚合）。

        **只在源文件内容没变时补**：文件被改过就不能拿新内容去解释旧结论的
        科室归属（台账会指向一个错的科室）。
        """
        source = Path(progress.source_path) if progress.source_path else None
        if source is None or not source.is_file():
            return
        try:
            if file_fingerprint(source) != progress.event_fingerprint:
                return
            batch = self.watcher.cut_batch(source)
        except OSError:
            return
        if batch is not None:
            self._prescriptions.update(index_prescriptions(batch.rows))

    def _finalize_batch(
        self,
        *,
        batch_no: str,
        source_file: str,
        received_rows: int,
        fingerprint: str,
        counts: dict[str, int],
        new_counts: dict[str, int],
        new_judgement_rows: int,
        duplicate: bool,
        started: float,
        resumed: bool,
        push: bool,
    ) -> BatchOutcome:
        """批次收尾：落盘 → 推送 → 回写 → 运行摘要 → 推进到闭环。

        ``push=False`` 用于**断点续跑**：该批次的卡片上次已经发过，
        重启后只补台账与回写，不再推第二张。
        """
        rows = self.store.list_judgements(batch_no)
        all_rows = self.store.list_judgements()

        # -- 落盘：判定台账 + 字段缺口台账 + 字段补齐顺序表 ---------------
        ledger_path = write_judgement_ledger(all_rows, self.out_dir, filename=LEDGER_FILENAME)
        gap_entries = build_gap_entries(all_rows, prescriptions=self._prescriptions)
        gap_path = write_undeterminable_ledger(gap_entries, self.out_dir)
        priority_items = build_priority_table(gap_entries)
        priority_path = write_priority_table(priority_items, self.out_dir)
        audit_path: Path | None = None
        try:
            audit_path, _check = export_audit_ledger(self.store, self.out_dir)
        except Exception as exc:  # 审计导出失败不吞：告警 + 不写「已导出」
            log_alert(self._logger, f"审计台账导出失败（本批判定不受影响）：{exc}")

        record_batch(
            received_rows=received_rows,
            judgement_rows=new_judgement_rows,
            counts=new_counts,
        )

        # -- 推送：失败只告警，判定台账与回写一个字节都不受影响 ------------
        pushed = False
        push_failed = False
        push_reason = ""
        push_receipt = ""
        # **卡片发过就不重发**：落地文件留在 inbox（入站只读），守护进程重启后
        # 会再看到它一次；那一批的卡片如果已经送出去了，就不能再推第二张
        # （同一份结论推两遍，药师会以为出了两次问题）。
        # 比对的是**已推送的判定行数**而不是一个布尔位：规则改版后同一批的
        # 判定行数会变，那时卡片要再推一次（药师得知道有新结论）。
        progress = self.store.get_batch_progress(batch_no, fingerprint)
        already_pushed = (
            progress is not None
            and progress.pushed_rows > 0
            and progress.pushed_rows == len(rows)
        )
        if push and already_pushed:
            push = False
            log_row_detail(
                self._logger,
                f"批次 {batch_no}：卡片已发过（{progress.stage if progress else ''} · "
                f"已推 {progress.pushed_rows if progress else 0} 条判定），不重推",
            )
        if push:
            summary = summarize_judgements(
                rows,
                batch_no=batch_no,
                received_rows=received_rows,
            )
            push_result = self.notifier.notify(
                summary,
                gap_items=items_from_gap_entries(gap_entries),
                priority_items=items_from_priority_table(priority_items),
            )
            pushed = bool(push_result.sent)
            push_reason = push_result.reason or ""
            push_receipt = _format_push_receipt(push_result)
            if pushed:
                self._pushes += 1
                self.store.mark_batch_pushed(
                    batch_no, fingerprint, judgement_rows=len(rows)
                )
            else:
                # **没配 webhook** = 本来就不推（推送是递话，不是改业务状态），
                # 不算「没走完」；**配了还失败** = 卡片没送到药师手里，
                # 留断点等下次启动重推（重推不会产生第二份判定行）。
                push_failed = bool(self.config.wecom_webhook_url)
                log_alert(
                    self._logger,
                    f"企业微信推送未送达（不影响判定与回写）：{push_reason or '原因未记录'}"
                    + ("（留断点，下次启动重推）" if push_failed else ""),
                )
        else:
            log_row_detail(
                self._logger,
                f"批次 {batch_no}：本批不推卡片（已推过或推送未启用）",
            )

        # -- 回写：只发「药师已确认」的结论（门禁在 engine/confirm.py）------
        sent, failed = self._trigger_writeback(batch_no)

        elapsed = time.monotonic() - started
        result = BatchOutcome(
            batch_no=batch_no,
            source_file=source_file,
            received_rows=received_rows,
            judgement_rows=len(rows),
            compliant=int(counts.get("合规", 0)),
            violation=int(counts.get("违规", 0)),
            undeterminable=int(counts.get("不可判定", 0)),
            duplicate=duplicate,
            fingerprint=fingerprint,
            ledger_path=ledger_path,
            gap_path=gap_path,
            priority_path=priority_path,
            audit_path=audit_path,
            pushed=pushed,
            push_reason=push_reason,
            writeback_sent=sent,
            writeback_failed=failed,
            elapsed_sec=elapsed,
        )
        self._last_outcome = result
        self._outcomes.append(result)
        self._batches += 1

        self._record_run_summary(result, resumed=resumed)

        extra = " · 重复投递（只补审计，未新增判定行）" if result.duplicate else ""
        if resumed:
            extra += " · 断点续跑"
        log_batch_summary(
            self._logger,
            f"批次 {result.batch_no}（来源 {result.source_file}）："
            f"收到 {result.received_rows} 行 · 命中规则 {result.judgement_rows} 条 · "
            f"合规 {result.compliant} · 违规 {result.violation} · "
            f"不可判定 {result.undeterminable} · "
            f"判定台账 → {ledger_path}{extra}",
        )
        self._log_row_judgements(rows)
        log_row_detail(
            self._logger,
            f"字段缺口台账 → {gap_path}（{len(gap_entries)} 组）· "
            f"字段补齐顺序表 → {priority_path}（{len(priority_items)} 个字段）",
        )
        if push_receipt:
            log_row_detail(self._logger, push_receipt)
        log_writeback(
            self._logger,
            f"回写：发出 {sent} 条 · 失败 {failed} 条（只发药师已确认的结论）",
        )
        log_audit(
            self._logger,
            f"批次 {result.batch_no} · 判定 {result.judgement_rows} 条 · "
            f"事件指纹 {result.fingerprint[:12] if result.fingerprint else '（无）'}",
        )
        log_row_detail(self._logger, self.run_summary_line(result))
        if not push_failed:
            self._advance(batch_no, fingerprint, STAGE_CLOSED)
        return result

    def run_summary_line(self, outcome: BatchOutcome) -> str:
        """运行摘要行（每批次结束打印一行）。

        口径：收到行数 / 合规 / 违规 / 不可判定 / 回写数 / 耗时。
        与批次摘要行（最显眼那行）不是同一件事——批次摘要行讲的是「这批判出了
        什么」，运行摘要行讲的是「这一轮跑了多久、发出去了多少」。
        """
        return (
            f"运行摘要：批次 {outcome.batch_no} · 收到 {outcome.received_rows} 行 · "
            f"合规 {outcome.compliant} · 违规 {outcome.violation} · "
            f"不可判定 {outcome.undeterminable} · 回写 {outcome.writeback_sent} 条 · "
            f"耗时 {outcome.elapsed_sec:.2f}s"
        )

    def _log_row_judgements(self, rows: Sequence[JudgementRow]) -> None:
        """逐行判定：把**需要人工处理**的结论逐条打进画面（缩进一格）。

        **只列违规与不可判定两类**：一批 200 行医嘱可能有 170 行合规，
        全打出来会把真正要看的几十行淹没。合规条数由批次摘要行承担，
        合规明细在判定台账里（``out/judgement_ledger.csv``）。

        不可判定行点名**缺哪个字段**（信息科据此排接口补齐顺序），
        违规行给出**违规要点**（归因文本里「依据：」之前的那句）。
        """
        actionable = [
            row
            for row in rows
            if row.conclusion != Conclusion.COMPLIANT
        ]
        if not actionable:
            return
        compliant = len(rows) - len(actionable)
        log_row_detail(
            self._logger,
            f"逐行判定：以下 {len(actionable)} 条需要人工处理"
            f"（合规 {compliant} 条计入判定台账）",
        )
        for row in actionable:
            log_row_detail(
                self._logger,
                f"判定 {row.prescription_no} · {row.rule_id} · "
                f"{row.conclusion.value} · {_row_detail(row)}",
            )

    def _record_run_summary(self, outcome: BatchOutcome, *, resumed: bool) -> None:
        """把运行摘要落库（CLI ``status`` 读它；跨进程重启也还在）。"""
        self.store.record_batch_run(
            BatchRun(
                batch_no=outcome.batch_no,
                event_fingerprint=outcome.fingerprint,
                received_rows=outcome.received_rows,
                judgement_rows=outcome.judgement_rows,
                compliant=outcome.compliant,
                violation=outcome.violation,
                undeterminable=outcome.undeterminable,
                writeback_sent=outcome.writeback_sent,
                writeback_failed=outcome.writeback_failed,
                pushed=1 if outcome.pushed else 0,
                elapsed_sec=outcome.elapsed_sec,
                resumed=1 if resumed else 0,
            )
        )

    def _trigger_writeback(self, batch_no: str | None = None) -> tuple[int, int]:
        """触发回写，返回（发出条数，失败条数）。

        只有「药师已确认」的结论会进请求体；本批没有确认过的结论时，
        这是一次零请求的空转——**不报错、不告警**（没确认就没有该发的）。
        """
        try:
            report = self.service.trigger_writeback(batch_no=batch_no)
        except Exception as exc:
            log_alert(self._logger, f"回写触发失败（判定行状态不动）：{exc}")
            return 0, 0
        sent = int(getattr(report, "sent", 0) or 0)
        failed = int(getattr(report, "failed", 0) or 0)
        for alert in getattr(report, "alerts", ()) or ():
            log_alert(self._logger, str(alert))
        return sent, failed

    # -- 事件处理 -----------------------------------------------------------

    def on_event(self, event: WatcherEvent) -> None:
        """监听事件回调（同步执行，保证同一时刻只有一个批次在处理）。"""
        try:
            if event.kind == EVENT_BATCH and event.batch is not None:
                self.reload_rules()
                self.handle_batch(event.batch)
                return
            if event.kind == EVENT_SUSPENDED:
                self._suspended += 1
                log_alert(
                    self._logger,
                    f"落地文件 {event.path.name} 被整文件挂起：{event.reason}"
                    "（不做部分判定，挂起台账见 out/suspended_ledger.csv）",
                )
                return
            log_alert(self._logger, f"收到未知监听事件（已忽略）：{event.kind}")
        except Exception as exc:
            # 一个批次处理失败**不许**带走整个守护进程：状态库事务已回滚，
            # 批次状态留在「未处理」，下一轮重投即可重来。
            log_alert(
                self._logger,
                f"批次处理失败（已回滚，不留半批状态）：{exc}",
            )
            self._logger.debug("批次处理失败明细", exc_info=True)

    # -- 优雅退出 -----------------------------------------------------------

    def install_signal_handlers(self) -> list[int]:
        """装上 ``SIGTERM`` / ``SIGINT`` 处理器，返回装成功的信号号列表。

        **必须在主线程调用**（CPython 的限制）；非主线程时返回空列表，
        守护进程仍可用 ``stop()`` 停。
        """
        installed: list[int] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self.handle_signal)
            except (ValueError, OSError):  # pragma: no cover - 非主线程 / 平台不支持
                continue
            installed.append(int(sig))
        self._installed_signals = installed
        return installed

    def handle_signal(self, signum: int, frame: Any = None) -> None:
        """信号回调：**只置停止标志**，当前批次照常做完。"""
        name = signal.Signals(signum).name if signum else "?"
        log_row_detail(
            self._logger,
            f"收到 {name}：完成当前批次后退出（不留半批状态）",
        )
        self._stop_event.set()

    def stop(self) -> None:
        """请求守护进程退出（等价于收到 SIGTERM）。"""
        self._stop_event.set()

    def wait(self, timeout: float | None = None) -> bool:
        """等待守护进程退出。"""
        return self._stop_event.wait(timeout)

    # -- 主循环 -------------------------------------------------------------

    def run(self) -> int:
        """守护进程主循环（阻塞直到收到停止信号）。"""
        self.install_signal_handlers()
        log_batch_summary(self._logger, self.banner())
        log_row_detail(self._logger, self.detail_line())
        # 断点续跑：先把上次没走完的批次接着走完，再进监听循环。
        try:
            self.resume_pending_batches()
        except Exception as exc:  # 续跑失败不许带走守护进程
            log_alert(self._logger, f"断点续跑异常（不影响监听）：{exc}")
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="mrfm-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        try:
            # 监听循环：watchdog 可用时事件到达即处理，不可用时退化为轮询。
            # 停止标志只在**批次之间**被检查，因此当前批次一定会做完。
            self.watcher.run(self._stop_event, on_event=self.on_event)
        finally:
            self._stop_event.set()
            if self._heartbeat_thread is not None:
                self._heartbeat_thread.join(timeout=5)
            log_batch_summary(
                self._logger,
                f"{PRODUCT_NAME} 已停止 · 共处理 {self._batches} 批 · "
                f"挂起 {self._suspended} 个文件 · 推送 {self._pushes} 次",
            )
        return EXIT_OK

    def _heartbeat_loop(self) -> None:
        """心跳线程：周期打一行 ``[alive] …``（守护活着就要看得见）。"""
        while not self._stop_event.is_set():
            try:
                self.emit_heartbeat()
            except Exception as exc:  # pragma: no cover - 心跳失败不许带走进程
                log_alert(self._logger, f"心跳行渲染失败：{exc}")
            self._stop_event.wait(max(1, int(self._heartbeat.interval_sec)))

    # -- 收尾 ---------------------------------------------------------------

    def close(self) -> None:
        """关掉状态库与出站客户端，并放掉单实例锁。"""
        try:
            self.store.close()
        except Exception:  # pragma: no cover - 关闭失败不影响退出
            pass
        if self.lock is not None:
            try:
                self.lock.release()
            except Exception:  # pragma: no cover - 解锁失败不影响退出
                pass


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def build_daemon(
    config: Config,
    *,
    store: Store | None = None,
    repository: RuleRepository | None = None,
    watcher: InboxWatcher | None = None,
    notifier: WeComNotifier | None = None,
    service: ConfirmService | None = None,
    client: OutboundClient | None = None,
    logger_: logging.Logger | None = None,
    lock: SingleInstanceLock | None = None,
) -> Daemon:
    """按配置面装配守护进程。

    **凭据位缺失 → 显式失败**（``HIS_BASE_URL`` / ``HIS_API_TOKEN`` 缺一即抛
    :class:`HisCredentialError`）：回写是本产品唯一改变业务状态的路径，
    缺凭据时必须显式失败并退出，不静默跳过、不退化成只读旁路。

    企业微信推送地址缺失**不**在这里抛：推送是递话，缺个 webhook 不该让
    守护进程起不来（推送失败会留告警）。
    """
    out_dir = config.out_dir
    resolved_store = store if store is not None else Store(config.state_db)
    resolved_repository = repository if repository is not None else RuleRepository(
        config.rules_dir
    )
    resolved_client = client if client is not None else OutboundClient()

    if notifier is None:
        # 推送也留痕（outbound_calls 表）：断点续跑据此判断「这张卡片发过了没」。
        notifier = WeComNotifier.from_config(
            config, client=resolved_client, ledger=resolved_store
        )
    if service is None:
        guard = WritebackIdempotencyGuard(resolved_store)
        queue = PendingWritebackQueue(
            resolved_store,
            csv_path=out_dir / PENDING_WRITEBACK_FILENAME,
        )
        writer = HisReviewWriter.from_config(
            config,
            client=resolved_client,
            queue=BindingAmbiguityLedger(out_dir / BINDING_AMBIGUITY_FILENAME),
            guard=guard,
            ledger=resolved_store,
        )
        service = ConfirmService(resolved_store, writer=writer, queue=queue)
    if watcher is None:
        watcher = InboxWatcher(config.inbox_dir, out_dir=out_dir)

    return Daemon(
        config,
        store=resolved_store,
        repository=resolved_repository,
        watcher=watcher,
        notifier=notifier,
        service=service,
        out_dir=out_dir,
        logger_=logger_,
        lock=lock,
    )


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构造守护进程的命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="med_rule_fieldmap.daemon",
        description="合理用药审方字段对照系统 · 常驻守护进程",
    )
    parser.add_argument("--inbox", default=None, help="医嘱落地目录（只读）")
    parser.add_argument("--out", default=None, help="产物输出目录")
    parser.add_argument("--rules", default=None, help="规则行目录")
    parser.add_argument("--state-db", default=None, help="状态库 SQLite 路径")
    parser.add_argument(
        "--heartbeat-sec",
        type=int,
        default=None,
        help="心跳间隔（秒）",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="日志级别（DEBUG / INFO / WARNING）",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="日志不上色（重定向到文件时用）",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> Config:
    """把命令行参数叠加到环境变量配置上。"""
    config = Config.from_env()
    overrides: dict[str, Any] = {}
    if args.inbox:
        overrides["inbox_dir"] = Path(args.inbox).expanduser().resolve()
    if args.out:
        overrides["out_dir"] = Path(args.out).expanduser().resolve()
    if args.rules:
        overrides["rules_dir"] = Path(args.rules).expanduser().resolve()
    if args.state_db:
        overrides["state_db"] = Path(args.state_db).expanduser().resolve()
    if args.heartbeat_sec:
        overrides["heartbeat_sec"] = int(args.heartbeat_sec)
    if not overrides:
        return config
    import dataclasses

    return dataclasses.replace(config, **overrides)


def main(argv: Sequence[str] | None = None) -> int:
    """守护进程命令行入口。"""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    level = getattr(logging, str(args.log_level).upper(), logging.INFO)
    # 守护进程的「画面」是三个自定义层级（批次摘要 / 逐行判定 / 审计与心跳），
    # 它们的级别低于 INFO——用标准日志门槛会把产品画面整个挡掉
    # （见 logging_setup 的 LEVEL_HEARTBEAT 说明）。门槛取两者更低的那个。
    effective_level = min(int(level), LEVEL_HEARTBEAT)
    setup_logging(level=effective_level, color=False if args.no_color else None, force=True)
    # httpx 的请求日志不属于产品画面（它会把 webhook 地址原样打出来），压掉。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # watchdog 的内部事件流水（NativeEvent / queue_event）也不是产品画面。
    # 注意：macOS 上这条日志挂在 ``fsevents`` 这个日志器名下，不在 ``watchdog`` 下。
    logging.getLogger("watchdog").setLevel(logging.WARNING)
    logging.getLogger("fsevents").setLevel(logging.WARNING)

    config = _config_from_args(args)
    daemon: Daemon | None = None
    # 单实例锁：同一个状态库已经有守护在跑 → 显式报错退出并打印占用进程信息。
    # 两个实例对着同一个 HIS 各回写一遍，药师端会出现重复的复核记录。
    lock = SingleInstanceLock(lock_path_for(config.state_db), state_db=config.state_db)
    try:
        lock.acquire()
    except DaemonLockedError as exc:
        print(f"[daemon] 启动失败：{exc}", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except Exception as exc:
        print(f"[daemon] 启动失败：单实例锁不可用：{exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        daemon = build_daemon(config, lock=lock)
    except (ConfigError, HisCredentialError) as exc:
        print(f"[daemon] 启动失败：{exc}", file=sys.stderr)
        lock.release()
        return EXIT_ERROR
    except Exception as exc:
        print(f"[daemon] 启动失败：{exc}", file=sys.stderr)
        lock.release()
        return EXIT_ERROR

    try:
        return daemon.run()
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        daemon.stop()
        return EXIT_OK
    finally:
        daemon.close()


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
