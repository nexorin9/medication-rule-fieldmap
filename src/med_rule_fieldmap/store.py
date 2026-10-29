"""状态库：批次表 / 判定表 / 审计表（SQLite，WAL 模式）。

**对应源产品能力**：源仓库 ``contrib/database/webhooks/models.py`` 的
``TableWebhookCall``（每次触发留一条可复核记录，请求/响应副本可导出）与
``core/workflow_actions/handler.py`` 的 ``create_workflow_action()``
（动作实例按序创建，重复触发不产生第二份实例）。

**改写**：源对象的「出站调用留痕」改写成「入站事件身份 + 判定留痕」——
每份落地文件按内容 sha256 取指纹（:mod:`med_rule_fieldmap.ingest.fingerprint`），
批次表以 ``(batch_no, event_fingerprint)`` 为主键；同一批次号 + 同一指纹再次
到达即判为**重复投递**：**只补审计行，不新增判定行**
（spec.md §5「幂等与并发重试」）。

**判定幂等键** = ``sha256(batch_no｜prescription_no｜rule_id｜rule_version)``，
落成判定表主键。同一键重复插入被忽略，因此「HIS 重投」「守护进程重跑」
「规则版本未变的重判」都不会产生第二份判定行；而规则版本一变幂等键随之变，
历史结论保留（重判留痕由 ``engine/statemachine.py`` 负责）。

**为什么重复投递要留痕而不是静默忽略**：药事审核岗看到「HIS 又推了一遍」和
「什么都没发生」是两种完全不同的现场判断。重复投递在审计表里显式记一行，
``writeback_state`` 记为「重复投递」；首次投递的判定行记为「未回写」
（未确认前本产品不回写 HIS）。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from .engine.judge import judge_batch
from .ingest.fingerprint import file_fingerprint
from .ingest.watcher import BatchFile
from .ledger.ledger import MISSING_FIELDS_SEPARATOR
from .models import (
    AuditAction,
    AuditRow,
    BUSINESS_KEY_SEP,
    Conclusion,
    ConfirmationRecord,
    JudgementRow,
    JudgementState,
    JudgementVersion,
    OutboundCallRecord,
    PendingWriteback,
    PendingWritebackStatus,
    RuleRow,
    WritebackAttempt,
    WritebackStatus,
    now_iso,
)

#: 审计行的回写状态：判定已出结论但药师尚未确认，本产品不回写 HIS
WRITEBACK_PENDING = "未回写"
#: 审计行的回写状态：该事件是重复投递，只补审计不新增判定
WRITEBACK_DUPLICATE = "重复投递"

# ---------------------------------------------------------------------------
# 批次进度阶段（守护进程断点续跑依据）
# ---------------------------------------------------------------------------

#: 事件已登记，判定未完成（崩溃点可能在这之后）
STAGE_RECEIVED = "已接收"
#: 判定已落库
STAGE_JUDGED = "已判定"
#: 企业微信批次卡片已发出
STAGE_PUSHED = "已推送"
#: 本批闭环走完（回写已触发或本就无可回写）
STAGE_CLOSED = "已闭环"

#: 阶段顺序（**只前进不后退**）
STAGE_ORDER: tuple[str, ...] = (STAGE_RECEIVED, STAGE_JUDGED, STAGE_PUSHED, STAGE_CLOSED)


def _max_stage(current: str, incoming: str) -> str:
    """取两个阶段里靠后的那个（未知阶段一律当作最靠后，避免倒退）。"""
    if incoming not in STAGE_ORDER:
        return current or incoming
    if current not in STAGE_ORDER:
        return incoming
    return current if STAGE_ORDER.index(current) >= STAGE_ORDER.index(incoming) else incoming


def stage_at_least(stage: str, target: str) -> bool:
    """阶段是否已经走到（或超过）目标阶段。

    守护进程据此判断「这张卡片发过了没」——阶段只前进不后退，
    所以「已推送 / 已闭环」都算发过。
    """
    if stage not in STAGE_ORDER or target not in STAGE_ORDER:
        return False
    return STAGE_ORDER.index(stage) >= STAGE_ORDER.index(target)


class BatchStatus(str, Enum):
    """批次状态（spec.md §4 输出闭环）。

    * :attr:`RECEIVED` —— 事件已登记，尚未落判定；
    * :attr:`JUDGED` —— 本批次判定已落库；
    * :attr:`DUPLICATE` —— 同一批次号 + 同一指纹再次到达，已补审计、未新增判定。
    """

    RECEIVED = "已接收"
    JUDGED = "已判定"
    DUPLICATE = "重复投递"


@dataclass(frozen=True)
class BatchRecord:
    """批次表的一行：批次号 + 事件指纹 + 处方行数 + 判定行数 + 批次状态。"""

    batch_no: str
    event_fingerprint: str
    source_file: str = ""
    source_kind: str = ""
    prescription_count: int = 0
    judgement_count: int = 0
    status: str = BatchStatus.RECEIVED.value
    created_at: str = ""
    updated_at: str = ""

    @property
    def short_fingerprint(self) -> str:
        """指纹短显示形（仅用于终端展示，不作幂等依据）。"""
        return self.event_fingerprint[:12]

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_no": self.batch_no,
            "event_fingerprint": self.event_fingerprint,
            "source_file": self.source_file,
            "source_kind": self.source_kind,
            "prescription_count": self.prescription_count,
            "judgement_count": self.judgement_count,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class BatchProgress:
    """批次进度（守护进程断点续跑依据）。

    **阶段只前进不后退**：已经走到「已闭环」的批次不会被后来的重复投递拉回
    「已接收」——否则「一次重复投递 + 一次崩溃」就会让重启后的守护把已经推过的
    卡片再推一遍。
    """

    batch_no: str
    event_fingerprint: str
    source_path: str = ""
    source_file: str = ""
    source_kind: str = ""
    prescription_count: int = 0
    judgement_rows: int = 0
    pushed_rows: int = 0
    stage: str = STAGE_RECEIVED
    created_at: str = ""
    updated_at: str = ""

    @property
    def finished(self) -> bool:
        """本批是否已走完闭环。"""
        return self.stage == STAGE_CLOSED

    @property
    def card_sent(self) -> bool:
        """这批的卡片是否已经送到（推送成功过）。"""
        return self.pushed_rows > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_no": self.batch_no,
            "event_fingerprint": self.event_fingerprint,
            "source_path": self.source_path,
            "source_file": self.source_file,
            "source_kind": self.source_kind,
            "prescription_count": self.prescription_count,
            "judgement_rows": self.judgement_rows,
            "pushed_rows": self.pushed_rows,
            "stage": self.stage,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class BatchRun:
    """一个批次的运行摘要（守护进程每批次结束写一行，CLI ``status`` 读它）。"""

    batch_no: str
    event_fingerprint: str
    received_rows: int = 0
    judgement_rows: int = 0
    compliant: int = 0
    violation: int = 0
    undeterminable: int = 0
    writeback_sent: int = 0
    writeback_failed: int = 0
    pushed: int = 0
    elapsed_sec: float = 0.0
    resumed: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_no": self.batch_no,
            "event_fingerprint": self.event_fingerprint,
            "received_rows": self.received_rows,
            "judgement_rows": self.judgement_rows,
            "compliant": self.compliant,
            "violation": self.violation,
            "undeterminable": self.undeterminable,
            "writeback_sent": self.writeback_sent,
            "writeback_failed": self.writeback_failed,
            "pushed": self.pushed,
            "elapsed_sec": round(float(self.elapsed_sec), 3),
            "resumed": self.resumed,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class InsertOutcome:
    """一次写入的结果：真正落库多少行、被幂等键挡掉多少行。"""

    inserted: int = 0
    skipped: int = 0
    inserted_rows: tuple[JudgementRow, ...] = ()

    @property
    def total(self) -> int:
        return self.inserted + self.skipped


@dataclass(frozen=True)
class BatchOutcome:
    """一次批次处理的完整结果（供 CLI 摘要与守护进程日志读）。"""

    batch: BatchRecord
    duplicate: bool
    fingerprint: str
    judgement_rows: int = 0
    audit_rows: int = 0
    total_judgements: int = 0
    total_audit: int = 0
    counts: dict[str, int] | None = None
    #: 本次**新增**判定行的三态计数（重复投递时全为 0）
    new_counts: dict[str, int] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_no": self.batch.batch_no,
            "event_fingerprint": self.fingerprint,
            "source_file": self.batch.source_file,
            "duplicate": self.duplicate,
            "new_judgement_rows": self.judgement_rows,
            "audit_rows": self.audit_rows,
            "total_judgements": self.total_judgements,
            "total_audit": self.total_audit,
            "status": self.batch.status,
            "counts": dict(self.counts or {}),
            "new_counts": dict(self.new_counts or {}),
        }


def _conclusion_value(conclusion: object) -> str:
    """结论 → 落库文本（接受枚举 / 中文名 / 英文 wire 值）。"""
    if isinstance(conclusion, Conclusion):
        return conclusion.value
    return Conclusion.from_value(conclusion).value


_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_no           TEXT    NOT NULL,
    event_fingerprint  TEXT    NOT NULL,
    source_file        TEXT    NOT NULL DEFAULT '',
    source_kind        TEXT    NOT NULL DEFAULT '',
    prescription_count INTEGER NOT NULL DEFAULT 0,
    judgement_count    INTEGER NOT NULL DEFAULT 0,
    status             TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    PRIMARY KEY (batch_no, event_fingerprint)
);

CREATE TABLE IF NOT EXISTS judgements (
    idempotency_key    TEXT PRIMARY KEY,
    batch_no           TEXT    NOT NULL,
    prescription_no    TEXT    NOT NULL DEFAULT '',
    rule_id            TEXT    NOT NULL DEFAULT '',
    rule_version       TEXT    NOT NULL DEFAULT '',
    conclusion         TEXT    NOT NULL DEFAULT '',
    severity           TEXT    NOT NULL DEFAULT '',
    reason             TEXT    NOT NULL DEFAULT '',
    basis              TEXT    NOT NULL DEFAULT '',
    missing_fields     TEXT    NOT NULL DEFAULT '',
    event_fingerprint  TEXT    NOT NULL DEFAULT '',
    state              TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_judgements_batch
    ON judgements (batch_no);

CREATE TABLE IF NOT EXISTS judgement_versions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    business_key       TEXT    NOT NULL,
    version            INTEGER NOT NULL,
    batch_no           TEXT    NOT NULL DEFAULT '',
    prescription_no    TEXT    NOT NULL DEFAULT '',
    rule_id            TEXT    NOT NULL DEFAULT '',
    rule_version       TEXT    NOT NULL DEFAULT '',
    conclusion         TEXT    NOT NULL DEFAULT '',
    severity           TEXT    NOT NULL DEFAULT '',
    reason             TEXT    NOT NULL DEFAULT '',
    basis              TEXT    NOT NULL DEFAULT '',
    missing_fields     TEXT    NOT NULL DEFAULT '',
    event_fingerprint  TEXT    NOT NULL DEFAULT '',
    state              TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    UNIQUE (business_key, version)
);

CREATE INDEX IF NOT EXISTS idx_judgement_versions_key
    ON judgement_versions (business_key);

CREATE TABLE IF NOT EXISTS audit (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no           TEXT    NOT NULL,
    prescription_no    TEXT    NOT NULL DEFAULT '',
    rule_id            TEXT    NOT NULL DEFAULT '',
    rule_version       TEXT    NOT NULL DEFAULT '',
    basis              TEXT    NOT NULL DEFAULT '',
    conclusion         TEXT    NOT NULL DEFAULT '',
    event_fingerprint  TEXT    NOT NULL DEFAULT '',
    writeback_state    TEXT    NOT NULL DEFAULT '',
    retry_count        INTEGER NOT NULL DEFAULT 0,
    action             TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit (batch_no);

-- 出站调用留痕（追加式）：一次出站调用记一行——审方复核回写 / 撤回通知 /
-- 企业微信卡片推送 / 待重放队列重放。失败的调用同样留痕，带累计重试次数。
CREATE TABLE IF NOT EXISTS outbound_calls (
    call_key          TEXT    PRIMARY KEY,
    channel           TEXT    NOT NULL DEFAULT '',
    action            TEXT    NOT NULL DEFAULT '',
    judgement_key     TEXT    NOT NULL DEFAULT '',
    batch_no          TEXT    NOT NULL DEFAULT '',
    prescription_no   TEXT    NOT NULL DEFAULT '',
    rule_id           TEXT    NOT NULL DEFAULT '',
    rule_version      TEXT    NOT NULL DEFAULT '',
    conclusion        TEXT    NOT NULL DEFAULT '',
    basis             TEXT    NOT NULL DEFAULT '',
    event_fingerprint TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT '',
    retry_count       INTEGER NOT NULL DEFAULT 0,
    error_code        TEXT    NOT NULL DEFAULT '',
    detail            TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbound_calls_batch
    ON outbound_calls (batch_no);

CREATE TABLE IF NOT EXISTS writeback_requests (
    writeback_key   TEXT    PRIMARY KEY,
    judgement_key   TEXT    NOT NULL,
    request_hash    TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT '',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_writeback_judgement
    ON writeback_requests (judgement_key);

CREATE TABLE IF NOT EXISTS pending_writeback (
    writeback_key     TEXT    PRIMARY KEY,
    judgement_key     TEXT    NOT NULL DEFAULT '',
    batch_no          TEXT    NOT NULL DEFAULT '',
    prescription_no   TEXT    NOT NULL DEFAULT '',
    rule_id           TEXT    NOT NULL DEFAULT '',
    rule_version      TEXT    NOT NULL DEFAULT '',
    request_body      TEXT    NOT NULL DEFAULT '',
    request_hash      TEXT    NOT NULL DEFAULT '',
    event_fingerprint TEXT    NOT NULL DEFAULT '',
    retry_count       INTEGER NOT NULL DEFAULT 0,
    last_error_code   TEXT    NOT NULL DEFAULT '',
    last_error        TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT '',
    queued_at         TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_writeback_status
    ON pending_writeback (status);

CREATE TABLE IF NOT EXISTS confirmations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key   TEXT    NOT NULL,
    business_key      TEXT    NOT NULL DEFAULT '',
    batch_no          TEXT    NOT NULL DEFAULT '',
    prescription_no   TEXT    NOT NULL DEFAULT '',
    rule_id           TEXT    NOT NULL DEFAULT '',
    rule_version      TEXT    NOT NULL DEFAULT '',
    action            TEXT    NOT NULL DEFAULT '',
    pharmacist_id     TEXT    NOT NULL DEFAULT '',
    acted_at          TEXT    NOT NULL,
    note              TEXT    NOT NULL DEFAULT '',
    writeback_state   TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_confirmations_key
    ON confirmations (idempotency_key);

-- 批次进度（守护进程断点续跑依据）：记录每个事件走到了哪一步，
-- 进程重启后据此把没走完的批次接着走完；阶段只前进不后退。
CREATE TABLE IF NOT EXISTS batch_progress (
    batch_no           TEXT    NOT NULL,
    event_fingerprint  TEXT    NOT NULL,
    source_path        TEXT    NOT NULL DEFAULT '',
    source_file        TEXT    NOT NULL DEFAULT '',
    source_kind        TEXT    NOT NULL DEFAULT '',
    prescription_count INTEGER NOT NULL DEFAULT 0,
    judgement_rows     INTEGER NOT NULL DEFAULT 0,
    pushed_rows        INTEGER NOT NULL DEFAULT 0,
    stage              TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    PRIMARY KEY (batch_no, event_fingerprint)
);

-- 运行摘要（每批次一行）：守护进程每批次结束写一行，
-- 运维 CLI 的 status 子命令读它（收到行数 / 三态计数 / 回写数 / 耗时）。
CREATE TABLE IF NOT EXISTS batch_runs (
    batch_no           TEXT    NOT NULL,
    event_fingerprint  TEXT    NOT NULL,
    received_rows      INTEGER NOT NULL DEFAULT 0,
    judgement_rows     INTEGER NOT NULL DEFAULT 0,
    compliant          INTEGER NOT NULL DEFAULT 0,
    violation          INTEGER NOT NULL DEFAULT 0,
    undeterminable     INTEGER NOT NULL DEFAULT 0,
    writeback_sent     INTEGER NOT NULL DEFAULT 0,
    writeback_failed   INTEGER NOT NULL DEFAULT 0,
    pushed             INTEGER NOT NULL DEFAULT 0,
    elapsed_sec        REAL    NOT NULL DEFAULT 0,
    resumed            INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    PRIMARY KEY (batch_no, event_fingerprint)
);
"""


#: 后加的列（表 → 列 → DDL 片段）。老库启动时按这张表补列。
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # 审计表的「动作」列：区分判定事件与出站调用事件。
    ("audit", "action", "TEXT NOT NULL DEFAULT ''"),
    # 批次进度表的「已推送判定行数」列：卡片发过没、发的又是哪几版结论。
    ("batch_progress", "pushed_rows", "INTEGER NOT NULL DEFAULT 0"),
)

#: 依赖迁移列的索引（必须在补列之后建，否则老库建索引会报「no such column」）。
_INDEXES_AFTER_MIGRATION = """
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit (action);
"""


class Store:
    """审方状态库。

    一次运行一个 :class:`Store`；内部用一把可重入锁串行化写操作，
    守护进程的线程池与主循环可以共用同一个实例。
    """

    def __init__(self, db_path: str | Path, *, timeout: float = 30.0) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._in_transaction = False
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=timeout,
            isolation_level=None,  # 自动提交；显式事务由 transaction() 打开
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # -- 生命周期 -----------------------------------------------------------

    def _init_schema(self) -> None:
        """建表（WAL 模式 + 全部表，幂等）。"""
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._migrate_columns()
            self._conn.executescript(_INDEXES_AFTER_MIGRATION)

    def _migrate_columns(self) -> None:
        """补齐后加的列（老库升级用，幂等）。

        ``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表不会补列，因此新增列必须
        在这里显式 ``ALTER TABLE``。只加列、**不删列不改列**——审计表是事件流水，
        改口径会让老批次的历史留痕对不上新口径。
        """
        with self._lock:
            for table, column, ddl in _COLUMN_MIGRATIONS:
                existing = {
                    str(row["name"])
                    for row in self._conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                if not existing or column in existing:
                    continue
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @property
    def journal_mode(self) -> str:
        """当前日志模式（WAL）。"""
        with self._lock:
            row = self._conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]) if row else ""

    def close(self) -> None:
        """关连接。"""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - 关闭失败不掩盖主流程
                pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        """显式事务（``BEGIN IMMEDIATE``）。

        批次登记 + 判定写入 + 审计追加必须原子：任何一步失败都不留下
        「批次已登记但判定没落」的半截状态。嵌套调用复用外层事务。
        """
        with self._lock:
            if self._in_transaction:
                yield
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._in_transaction = True
            try:
                yield
            except Exception:
                self._conn.execute("ROLLBACK")
                self._in_transaction = False
                raise
            else:
                self._conn.execute("COMMIT")
                self._in_transaction = False

    # -- 批次表 -------------------------------------------------------------

    def register_batch(
        self,
        *,
        batch_no: str,
        event_fingerprint: str,
        source_file: str = "",
        source_kind: str = "",
        prescription_count: int = 0,
    ) -> tuple[BatchRecord, bool]:
        """登记一个批次事件，返回 ``(批次记录, 是否重复投递)``。

        重复投递（同批次号 + 同指纹）只更新状态与计数，**不新增批次行**。
        """
        if not batch_no:
            raise ValueError("批次号不能为空")
        if not event_fingerprint:
            raise ValueError("事件指纹不能为空")
        now = now_iso()
        with self._lock:
            existing = self.get_batch(batch_no, event_fingerprint)
            if existing is not None:
                self._conn.execute(
                    "UPDATE batches SET status = ?, updated_at = ?, "
                    "source_file = CASE WHEN ? <> '' THEN ? ELSE source_file END, "
                    "source_kind = CASE WHEN ? <> '' THEN ? ELSE source_kind END "
                    "WHERE batch_no = ? AND event_fingerprint = ?",
                    (
                        BatchStatus.DUPLICATE.value,
                        now,
                        source_file,
                        source_file,
                        source_kind,
                        source_kind,
                        batch_no,
                        event_fingerprint,
                    ),
                )
                record = self.get_batch(batch_no, event_fingerprint)
                assert record is not None  # 刚更新过，必定还在
                return record, True

            self._conn.execute(
                "INSERT INTO batches (batch_no, event_fingerprint, source_file, "
                "source_kind, prescription_count, judgement_count, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    batch_no,
                    event_fingerprint,
                    source_file,
                    source_kind,
                    int(prescription_count),
                    BatchStatus.RECEIVED.value,
                    now,
                    now,
                ),
            )
            record = self.get_batch(batch_no, event_fingerprint)
            assert record is not None
            return record, False

    def update_batch_progress(
        self,
        batch_no: str,
        event_fingerprint: str,
        *,
        judgement_count: int,
        status: str | BatchStatus,
    ) -> None:
        """回写批次的判定行数与状态。"""
        resolved = status.value if isinstance(status, BatchStatus) else status
        with self._lock:
            self._conn.execute(
                "UPDATE batches SET judgement_count = ?, status = ?, updated_at = ? "
                "WHERE batch_no = ? AND event_fingerprint = ?",
                (int(judgement_count), resolved, now_iso(), batch_no, event_fingerprint),
            )

    def get_batch(self, batch_no: str, event_fingerprint: str) -> BatchRecord | None:
        """取一个批次记录（按批次号 + 指纹）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batches WHERE batch_no = ? AND event_fingerprint = ?",
                (batch_no, event_fingerprint),
            ).fetchone()
        return _to_batch_record(row) if row is not None else None

    def find_batch_by_fingerprint(self, event_fingerprint: str) -> BatchRecord | None:
        """按指纹找批次（指纹是内容身份，换名重投也能找到）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batches WHERE event_fingerprint = ? LIMIT 1",
                (event_fingerprint,),
            ).fetchone()
        return _to_batch_record(row) if row is not None else None

    def list_batches(self) -> list[BatchRecord]:
        """全部批次记录（按登记时间排序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batches ORDER BY created_at, batch_no"
            ).fetchall()
        return [_to_batch_record(row) for row in rows]

    def count_batches(self) -> int:
        """批次数。"""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM batches").fetchone()
        return int(row[0]) if row else 0

    # -- 批次进度（断点续跑依据）-------------------------------------------

    def record_batch_progress(
        self,
        *,
        batch_no: str,
        event_fingerprint: str,
        source_path: str = "",
        source_file: str = "",
        source_kind: str = "",
        prescription_count: int = 0,
        judgement_rows: int = 0,
        stage: str = STAGE_RECEIVED,
    ) -> BatchProgress:
        """记录（或推进）一个批次的处理进度。

        **阶段只前进不后退**：已经走到「已闭环」的批次不会被后来的重复投递拉回
        「已接收」——否则「一次重复投递 + 一次崩溃」就会让重启后的守护把已经推过
        的卡片再推一遍（同一份结论推两遍，药师会以为出了两次问题）。
        """
        if not batch_no:
            raise ValueError("批次号不能为空")
        if not event_fingerprint:
            raise ValueError("事件指纹不能为空")
        now = now_iso()
        with self._lock:
            existing = self.get_batch_progress(batch_no, event_fingerprint)
            if existing is None:
                self._conn.execute(
                    "INSERT INTO batch_progress (batch_no, event_fingerprint, "
                    "source_path, source_file, source_kind, prescription_count, "
                    "judgement_rows, stage, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch_no,
                        event_fingerprint,
                        source_path,
                        source_file,
                        source_kind,
                        int(prescription_count),
                        int(judgement_rows),
                        stage,
                        now,
                        now,
                    ),
                )
            else:
                merged = _max_stage(existing.stage, stage)
                self._conn.execute(
                    "UPDATE batch_progress SET "
                    "source_path = CASE WHEN ? <> '' THEN ? ELSE source_path END, "
                    "source_file = CASE WHEN ? <> '' THEN ? ELSE source_file END, "
                    "source_kind = CASE WHEN ? <> '' THEN ? ELSE source_kind END, "
                    "prescription_count = CASE WHEN ? > 0 THEN ? ELSE prescription_count END, "
                    "judgement_rows = CASE WHEN ? > 0 THEN ? ELSE judgement_rows END, "
                    "stage = ?, updated_at = ? "
                    "WHERE batch_no = ? AND event_fingerprint = ?",
                    (
                        source_path,
                        source_path,
                        source_file,
                        source_file,
                        source_kind,
                        source_kind,
                        int(prescription_count),
                        int(prescription_count),
                        int(judgement_rows),
                        int(judgement_rows),
                        merged,
                        now,
                        batch_no,
                        event_fingerprint,
                    ),
                )
            record = self.get_batch_progress(batch_no, event_fingerprint)
            assert record is not None  # 刚写完，必定还在
            return record

    def get_batch_progress(
        self, batch_no: str, event_fingerprint: str
    ) -> BatchProgress | None:
        """取一个批次的进度记录。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batch_progress WHERE batch_no = ? "
                "AND event_fingerprint = ?",
                (batch_no, event_fingerprint),
            ).fetchone()
        return _to_batch_progress(row) if row is not None else None

    def list_batch_progress(self, stage: str | None = None) -> list[BatchProgress]:
        """全部批次进度记录（按登记时间排序）。"""
        with self._lock:
            if stage is None:
                rows = self._conn.execute(
                    "SELECT * FROM batch_progress ORDER BY created_at, batch_no"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM batch_progress WHERE stage = ? "
                    "ORDER BY created_at, batch_no",
                    (stage,),
                ).fetchall()
        return [_to_batch_progress(row) for row in rows]

    def list_unfinished_batches(self) -> list[BatchProgress]:
        """**没走完闭环**的批次（阶段不是「已闭环」）——重启后从这里接着走。"""
        return [
            record
            for record in self.list_batch_progress()
            if record.stage != STAGE_CLOSED
        ]

    def mark_batch_pushed(
        self,
        batch_no: str,
        event_fingerprint: str,
        *,
        judgement_rows: int,
    ) -> BatchProgress | None:
        """记下「这一批的卡片已送达，送的是这 N 条判定」。

        **记的是判定行数而不是一个布尔位**：规则改版后同一批的判定行数会变，
        那时卡片要再推一次（药师得知道有新结论）；规则没变则行数不变，
        重启后不再推第二张。
        """
        now = now_iso()
        with self._lock:
            existing = self.get_batch_progress(batch_no, event_fingerprint)
            stage = STAGE_PUSHED if existing is None else _max_stage(
                existing.stage, STAGE_PUSHED
            )
            self._conn.execute(
                "UPDATE batch_progress SET pushed_rows = ?, stage = ?, updated_at = ? "
                "WHERE batch_no = ? AND event_fingerprint = ?",
                (
                    int(judgement_rows),
                    stage,
                    now,
                    batch_no,
                    event_fingerprint,
                ),
            )
            return self.get_batch_progress(batch_no, event_fingerprint)

    def count_unfinished_batches(self) -> int:
        """没走完闭环的批次数。"""
        return len(self.list_unfinished_batches())

    # -- 运行摘要 -----------------------------------------------------------

    def record_batch_run(self, run: BatchRun) -> BatchRun:
        """记录一个批次的运行摘要（同批次号 + 同指纹覆盖写，不追加）。"""
        if not run.batch_no:
            raise ValueError("批次号不能为空")
        if not run.event_fingerprint:
            raise ValueError("事件指纹不能为空")
        now = now_iso()
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at FROM batch_runs WHERE batch_no = ? "
                "AND event_fingerprint = ?",
                (run.batch_no, run.event_fingerprint),
            ).fetchone()
            created_at = str(existing[0]) if existing else now
            self._conn.execute(
                "INSERT INTO batch_runs (batch_no, event_fingerprint, received_rows, "
                "judgement_rows, compliant, violation, undeterminable, writeback_sent, "
                "writeback_failed, pushed, elapsed_sec, resumed, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(batch_no, event_fingerprint) DO UPDATE SET "
                "received_rows = excluded.received_rows, "
                "judgement_rows = excluded.judgement_rows, "
                "compliant = excluded.compliant, violation = excluded.violation, "
                "undeterminable = excluded.undeterminable, "
                "writeback_sent = excluded.writeback_sent, "
                "writeback_failed = excluded.writeback_failed, "
                "pushed = excluded.pushed, elapsed_sec = excluded.elapsed_sec, "
                "resumed = excluded.resumed, updated_at = excluded.updated_at",
                (
                    run.batch_no,
                    run.event_fingerprint,
                    int(run.received_rows),
                    int(run.judgement_rows),
                    int(run.compliant),
                    int(run.violation),
                    int(run.undeterminable),
                    int(run.writeback_sent),
                    int(run.writeback_failed),
                    int(run.pushed),
                    float(run.elapsed_sec),
                    int(run.resumed),
                    created_at,
                    now,
                ),
            )
        return run

    def list_batch_runs(self) -> list[BatchRun]:
        """全部运行摘要（按登记时间排序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batch_runs ORDER BY created_at, batch_no"
            ).fetchall()
        return [_to_batch_run(row) for row in rows]

    def latest_batch_run(self) -> BatchRun | None:
        """最近一个批次的运行摘要。"""
        runs = self.list_batch_runs()
        return runs[-1] if runs else None

    def summarize_batch_runs(self) -> dict[str, object]:
        """运行摘要合计（CLI ``status`` 读它）。

        口径与 :mod:`med_rule_fieldmap.run_summary` 的进程级摘要一致，
        但这里是**跨进程重启**的累计值——进程重启后进程级摘要会清零，
        落库的这份不会。
        """
        totals: dict[str, object] = {
            "batches": 0,
            "received_rows": 0,
            "judgement_rows": 0,
            "compliant": 0,
            "violation": 0,
            "undeterminable": 0,
            "writeback_sent": 0,
            "writeback_failed": 0,
            "pushed": 0,
            "elapsed_sec": 0.0,
            "resumed": 0,
        }
        for run in self.list_batch_runs():
            totals["batches"] = int(totals["batches"]) + 1
            totals["received_rows"] = int(totals["received_rows"]) + run.received_rows
            totals["judgement_rows"] = int(totals["judgement_rows"]) + run.judgement_rows
            totals["compliant"] = int(totals["compliant"]) + run.compliant
            totals["violation"] = int(totals["violation"]) + run.violation
            totals["undeterminable"] = int(totals["undeterminable"]) + run.undeterminable
            totals["writeback_sent"] = int(totals["writeback_sent"]) + run.writeback_sent
            totals["writeback_failed"] = (
                int(totals["writeback_failed"]) + run.writeback_failed
            )
            totals["pushed"] = int(totals["pushed"]) + run.pushed
            totals["elapsed_sec"] = float(totals["elapsed_sec"]) + float(run.elapsed_sec)
            totals["resumed"] = int(totals["resumed"]) + run.resumed
        totals["elapsed_sec"] = round(float(totals["elapsed_sec"]), 3)
        batches = int(totals["batches"])
        totals["avg_elapsed_sec"] = (
            round(float(totals["elapsed_sec"]) / batches, 3) if batches else 0.0
        )
        return totals

    # -- 判定表 -------------------------------------------------------------

    def insert_judgements(self, rows: Iterable[JudgementRow]) -> InsertOutcome:
        """写入判定行（幂等键冲突则忽略）。

        返回真正落库行数与被幂等键挡掉的行数——两者之和等于传入行数。
        先查已存在的幂等键、再只插没见过的行，因此能明确说出「哪几行是新的」。

        **同时落一条历史版本**（版本号 1）：判定表留的是「当前那一版」，
        版本表留的是「这一版当初判成什么」——重判时旧结论不被覆盖、不被删除
        （``engine/statemachine.py``）。重复投递不新增判定行，也不新增版本行。
        """
        candidates = list(rows)
        if not candidates:
            return InsertOutcome()
        with self._lock:
            existing = self._existing_keys([row.idempotency_key for row in candidates])
            fresh = [row for row in candidates if row.idempotency_key not in existing]
            if fresh:
                payload = [
                    (
                        row.idempotency_key,
                        row.batch_no,
                        row.prescription_no,
                        row.rule_id,
                        row.rule_version,
                        _conclusion_value(row.conclusion),
                        row.severity or "",
                        row.reason,
                        row.basis,
                        MISSING_FIELDS_SEPARATOR.join(row.missing_fields),
                        row.event_fingerprint,
                        row.state.value,
                        row.created_at,
                    )
                    for row in fresh
                ]
                with self.transaction():
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO judgements (idempotency_key, batch_no, "
                        "prescription_no, rule_id, rule_version, conclusion, severity, reason, "
                        "basis, missing_fields, event_fingerprint, state, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        payload,
                    )
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO judgement_versions (business_key, version, "
                        "batch_no, prescription_no, rule_id, rule_version, conclusion, "
                        "severity, reason, basis, missing_fields, event_fingerprint, state, "
                        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                row.business_key,
                                1,
                                row.batch_no,
                                row.prescription_no,
                                row.rule_id,
                                row.rule_version,
                                _conclusion_value(row.conclusion),
                                row.severity or "",
                                row.reason,
                                row.basis,
                                MISSING_FIELDS_SEPARATOR.join(row.missing_fields),
                                row.event_fingerprint,
                                row.state.value,
                                row.created_at,
                            )
                            for row in fresh
                        ],
                    )
        return InsertOutcome(
            inserted=len(fresh),
            skipped=len(candidates) - len(fresh),
            inserted_rows=tuple(fresh),
        )

    def _existing_keys(self, keys: Sequence[str]) -> set[str]:
        """查已存在的幂等键（分批查，避免超出 SQLite 变量数上限）。"""
        found: set[str] = set()
        chunk = 400
        for start in range(0, len(keys), chunk):
            part = list(keys[start : start + chunk])
            placeholders = ",".join("?" * len(part))
            rows = self._conn.execute(
                f"SELECT idempotency_key FROM judgements WHERE idempotency_key IN ({placeholders})",
                part,
            ).fetchall()
            found.update(str(item[0]) for item in rows)
        return found

    def count_judgements(self, batch_no: str | None = None) -> int:
        """判定行数（可按批次过滤）。"""
        with self._lock:
            if batch_no is None:
                row = self._conn.execute("SELECT COUNT(*) FROM judgements").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM judgements WHERE batch_no = ?", (batch_no,)
                ).fetchone()
        return int(row[0]) if row else 0

    def list_judgements(self, batch_no: str | None = None) -> list[JudgementRow]:
        """读回判定行（可按批次过滤）。"""
        with self._lock:
            if batch_no is None:
                rows = self._conn.execute(
                    "SELECT * FROM judgements ORDER BY created_at, idempotency_key"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM judgements WHERE batch_no = ? "
                    "ORDER BY created_at, idempotency_key",
                    (batch_no,),
                ).fetchall()
        return [_to_judgement_row(row) for row in rows]

    def get_judgement(self, idempotency_key: str) -> JudgementRow | None:
        """按幂等键取一条判定行。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM judgements WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return _to_judgement_row(row) if row is not None else None

    def find_judgement_by_business_key(self, business_key: str) -> JudgementRow | None:
        """按**业务键**（``batch_no｜prescription_no｜rule_id｜rule_version``）取判定行。

        业务键是台账与回写请求体里出现的形态，药师在台账上看到的就是它——
        运维通道按业务键定位一条判定时不必先去算 sha256。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM judgements WHERE batch_no || ? || prescription_no || ? || "
                "rule_id || ? || rule_version = ? LIMIT 1",
                (BUSINESS_KEY_SEP, BUSINESS_KEY_SEP, BUSINESS_KEY_SEP, business_key),
            ).fetchone()
        return _to_judgement_row(row) if row is not None else None

    def set_judgement_state(self, idempotency_key: str, state: JudgementState) -> bool:
        """推进一条判定行的状态。

        **本方法不校验跃迁合法性**——合法性由 ``engine/statemachine.py`` 的
        合法跃迁表把守（``JudgementStateMachine.advance`` 先校验再落库）。
        """
        with self._lock:
            before = self._conn.total_changes
            self._conn.execute(
                "UPDATE judgements SET state = ? WHERE idempotency_key = ?",
                (state.value, idempotency_key),
            )
            return (self._conn.total_changes - before) > 0

    def refresh_judgement(self, row: JudgementRow) -> bool:
        """把判定行的**当前**结论刷新为重判后的新结论。

        判定表留的是「当前那一版」；旧结论已经在版本表里（只追加、不覆盖、
        不删除），因此这里刷新不会丢历史。返回是否命中到行。
        """
        with self._lock:
            before = self._conn.total_changes
            self._conn.execute(
                "UPDATE judgements SET conclusion = ?, severity = ?, reason = ?, "
                "basis = ?, missing_fields = ?, event_fingerprint = ?, state = ? "
                "WHERE idempotency_key = ?",
                (
                    _conclusion_value(row.conclusion),
                    row.severity or "",
                    row.reason,
                    row.basis,
                    MISSING_FIELDS_SEPARATOR.join(row.missing_fields),
                    row.event_fingerprint,
                    row.state.value,
                    row.idempotency_key,
                ),
            )
            return (self._conn.total_changes - before) > 0

    # -- 判定版本表（重判留痕）--------------------------------------------

    def insert_judgement_version(self, version_row: JudgementVersion) -> bool:
        """追加一条判定历史版本（只追加）。

        同一个 ``(业务键, 版本号)`` 重复写入被忽略——版本行不覆盖、不删除。
        返回是否真的追加了新行。
        """
        with self._lock:
            before = self._conn.total_changes
            self._conn.execute(
                "INSERT OR IGNORE INTO judgement_versions (business_key, version, "
                "batch_no, prescription_no, rule_id, rule_version, conclusion, severity, "
                "reason, basis, missing_fields, event_fingerprint, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_row.business_key,
                    int(version_row.version),
                    version_row.batch_no,
                    version_row.prescription_no,
                    version_row.rule_id,
                    version_row.rule_version,
                    _conclusion_value(version_row.conclusion),
                    version_row.severity or "",
                    version_row.reason,
                    version_row.basis,
                    MISSING_FIELDS_SEPARATOR.join(version_row.missing_fields),
                    version_row.event_fingerprint,
                    version_row.state.value,
                    version_row.created_at,
                ),
            )
            return (self._conn.total_changes - before) > 0

    def list_judgement_versions(self, business_key: str) -> list[JudgementVersion]:
        """读某个业务键的全部历史版本（按版本号升序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM judgement_versions WHERE business_key = ? "
                "ORDER BY version",
                (business_key,),
            ).fetchall()
        return [_to_judgement_version(row) for row in rows]

    def next_judgement_version(self, business_key: str) -> int:
        """下一个版本号（无历史时为 1）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) FROM judgement_versions WHERE business_key = ?",
                (business_key,),
            ).fetchone()
        latest = int(row[0]) if row and row[0] is not None else 0
        return latest + 1

    def count_judgement_versions(self, business_key: str | None = None) -> int:
        """历史版本行数（可按业务键过滤）。"""
        with self._lock:
            if business_key is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM judgement_versions"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM judgement_versions WHERE business_key = ?",
                    (business_key,),
                ).fetchone()
        return int(row[0]) if row else 0

    # -- 审计表 -------------------------------------------------------------

    def append_rejudgement_audit(self, row: JudgementRow) -> int:
        """为一次重判追加一条审计痕迹，返回追加行数。

        重判后的新结论**尚未经药师确认**，因此回写状态记「未回写」——
        与首次判定同口径：未确认不回写 HIS。
        """
        return self.append_audit(
            [
                AuditRow(
                    batch_no=row.batch_no,
                    prescription_no=row.prescription_no,
                    rule_id=row.rule_id,
                    rule_version=row.rule_version,
                    basis=row.basis,
                    conclusion=row.conclusion,
                    event_fingerprint=row.event_fingerprint,
                    writeback_state=WRITEBACK_PENDING,
                    retry_count=0,
                )
            ]
        )

    def append_audit(self, rows: Iterable[AuditRow]) -> int:
        """追加审计行（**只追加**，不覆盖、不改写历史），返回追加行数。"""
        payload = [
            (
                row.batch_no,
                row.prescription_no,
                row.rule_id,
                row.rule_version,
                row.basis,
                _conclusion_value(row.conclusion),
                row.event_fingerprint,
                row.writeback_state,
                int(row.retry_count),
                str(row.action or ""),
                row.created_at,
            )
            for row in rows
        ]
        if not payload:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT INTO audit (batch_no, prescription_no, rule_id, rule_version, "
                "basis, conclusion, event_fingerprint, writeback_state, retry_count, "
                "action, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            return self._conn.total_changes - before

    def count_audit(
        self, batch_no: str | None = None, action: str | None = None
    ) -> int:
        """审计行数（可按批次 / 动作过滤）。"""
        clauses: list[str] = []
        params: list[object] = []
        if batch_no is not None:
            clauses.append("batch_no = ?")
            params.append(batch_no)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM audit{where}", params
            ).fetchone()
        return int(row[0]) if row else 0

    def list_audit(
        self, batch_no: str | None = None, action: str | None = None
    ) -> list[AuditRow]:
        """读回审计行（按追加顺序）。"""
        clauses: list[str] = []
        params: list[object] = []
        if batch_no is not None:
            clauses.append("batch_no = ?")
            params.append(batch_no)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM audit{where} ORDER BY id", params
            ).fetchall()
        return [_to_audit_row(row) for row in rows]

    # -- 出站调用留痕 --------------------------------------------------------

    def append_outbound_call(self, record: OutboundCallRecord) -> bool:
        """追加一条出站调用留痕，返回是否真的追加了新行。

        **只追加**：同一 ``call_key`` 再次写入不会覆盖旧行——旧行留着，
        新行是「又发了一次」的事实，历史不被改写。
        """
        with self._lock:
            before = self._conn.total_changes
            self._conn.execute(
                "INSERT OR IGNORE INTO outbound_calls (call_key, channel, action, "
                "judgement_key, batch_no, prescription_no, rule_id, rule_version, "
                "conclusion, basis, event_fingerprint, status, retry_count, "
                "error_code, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.call_key,
                    record.channel,
                    record.action,
                    record.judgement_key,
                    record.batch_no,
                    record.prescription_no,
                    record.rule_id,
                    record.rule_version,
                    _conclusion_value(record.conclusion),
                    record.basis,
                    record.event_fingerprint,
                    record.status,
                    int(record.retry_count),
                    record.error_code,
                    record.detail,
                    record.created_at,
                    record.updated_at,
                ),
            )
            return self._conn.total_changes > before

    def list_outbound_calls(self, batch_no: str | None = None) -> list[OutboundCallRecord]:
        """读回出站调用留痕（按写入顺序）。"""
        with self._lock:
            if batch_no is None:
                rows = self._conn.execute(
                    "SELECT * FROM outbound_calls ORDER BY created_at, call_key"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM outbound_calls WHERE batch_no = ? "
                    "ORDER BY created_at, call_key",
                    (batch_no,),
                ).fetchall()
        return [_to_outbound_call_row(row) for row in rows]

    def count_outbound_calls(self, batch_no: str | None = None) -> int:
        """出站调用留痕行数（可按批次过滤）。"""
        with self._lock:
            if batch_no is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM outbound_calls"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM outbound_calls WHERE batch_no = ?", (batch_no,)
                ).fetchone()
        return int(row[0]) if row else 0

    # -- 药师确认 / 撤回留痕 ------------------------------------------------

    def append_confirmation(self, record: ConfirmationRecord) -> bool:
        """追加一条确认 / 撤回留痕（**只追加**，不覆盖、不改写历史）。

        返回是否真的追加了新行。同一���判行确认多次、撤回多次都会逐条留痕——
        「谁在什么时候确认过、后来为什么撤回」是飞检现场要能当场答出的问题。
        """
        with self._lock:
            before = self._conn.total_changes
            self._conn.execute(
                "INSERT INTO confirmations (idempotency_key, business_key, batch_no, "
                "prescription_no, rule_id, rule_version, action, pharmacist_id, "
                "acted_at, note, writeback_state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.idempotency_key,
                    record.business_key,
                    record.batch_no,
                    record.prescription_no,
                    record.rule_id,
                    record.rule_version,
                    record.action,
                    record.pharmacist_id,
                    record.acted_at,
                    record.note,
                    record.writeback_state,
                    record.created_at,
                ),
            )
            return (self._conn.total_changes - before) > 0

    def list_confirmations(self, idempotency_key: str | None = None) -> list[ConfirmationRecord]:
        """读回确认 / 撤回留痕（按追加顺序；可按判定幂等键过滤）。"""
        with self._lock:
            if idempotency_key is None:
                rows = self._conn.execute(
                    "SELECT * FROM confirmations ORDER BY id"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM confirmations WHERE idempotency_key = ? ORDER BY id",
                    (idempotency_key,),
                ).fetchall()
        return [_to_confirmation_record(row) for row in rows]

    def latest_confirmation(self, idempotency_key: str) -> ConfirmationRecord | None:
        """某条判定行的**最新**一条确认 / 撤回留痕（没有则 ``None``）。"""
        records = self.list_confirmations(idempotency_key)
        return records[-1] if records else None

    def count_confirmations(self, idempotency_key: str | None = None) -> int:
        """确认 / 撤回留痕条数（可按判定幂等键过滤）。"""
        with self._lock:
            if idempotency_key is None:
                row = self._conn.execute("SELECT COUNT(*) FROM confirmations").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM confirmations WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
        return int(row[0]) if row else 0

    # -- 出站回写请求（幂等身份） ---------------------------------------------

    def claim_writeback_request(
        self,
        writeback_key: str,
        judgement_key: str,
        request_hash: str = "",
    ) -> tuple[bool, WritebackAttempt | None]:
        """占位一次出站回写（幂等键冲突则不占位）。

        返回 ``(是否新占位, 该键对应的记录)``：

        * 新键 → 占位成功，落状态「已占位」，返回 ``(True, 新记录)``；
        * 旧键 → 不占位、不覆盖，返回 ``(False, 已有记录)``。

        「不覆盖」很关键：已发出过的回写不能因为重投而被改成新状态，
        否则药师端会出现同一条处方两条复核记录。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM writeback_requests WHERE writeback_key = ?",
                (writeback_key,),
            ).fetchone()
            if row is not None:
                return False, _to_writeback_attempt(row)
            stamp = now_iso()
            with self.transaction():
                self._conn.execute(
                    "INSERT OR IGNORE INTO writeback_requests (writeback_key, judgement_key, "
                    "request_hash, status, retry_count, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (
                        writeback_key,
                        judgement_key,
                        request_hash,
                        WritebackStatus.CLAIMED.value,
                        stamp,
                        stamp,
                    ),
                )
            row = self._conn.execute(
                "SELECT * FROM writeback_requests WHERE writeback_key = ?",
                (writeback_key,),
            ).fetchone()
            return True, (_to_writeback_attempt(row) if row is not None else None)

    def mark_writeback_sent(self, writeback_key: str) -> bool:
        """标记回写已成功发出（幂等：只允许从非「已回写」态推进）。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE writeback_requests SET status = ?, updated_at = ? "
                "WHERE writeback_key = ? AND status <> ?",
                (
                    WritebackStatus.SENT.value,
                    now_iso(),
                    writeback_key,
                    WritebackStatus.SENT.value,
                ),
            )
            return cursor.rowcount > 0

    def mark_writeback_failed(self, writeback_key: str) -> bool:
        """标记回写失败并累加重试次数（退避重试队列据此记账）。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE writeback_requests SET status = ?, retry_count = retry_count + 1, "
                "updated_at = ? WHERE writeback_key = ?",
                (WritebackStatus.FAILED.value, now_iso(), writeback_key),
            )
            return cursor.rowcount > 0

    def list_writeback_requests(self) -> list[WritebackAttempt]:
        """读回全部出站回写请求（按占位顺序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM writeback_requests ORDER BY created_at, writeback_key"
            ).fetchall()
        return [_to_writeback_attempt(row) for row in rows]

    def count_writeback_requests(self, status: str | None = None) -> int:
        """出站回写请求数（可按状态过滤）。"""
        with self._lock:
            if status is None:
                row = self._conn.execute("SELECT COUNT(*) FROM writeback_requests").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM writeback_requests WHERE status = ?", (status,)
                ).fetchone()
        return int(row[0]) if row else 0

    # -- 待重放回写队列（退避超次后落库） ---------------------------------------

    def enqueue_pending_writeback(self, record: PendingWriteback) -> PendingWriteback:
        """把一条退避超次的回写落进待重放队列（按幂等键 upsert）。

        **同一条回写只占一行**：再次入队时累加 ``retry_count``、刷新最后错误码与
        入队时间，**不新增行**——否则同一份请求反复失败会在队列里堆出多行，
        重放时就被重复发出。

        **不改判定行状态**：入队只说明「这条回写还没发出去」，
        判定行仍然是「药师已确认」，绝不改成「已回写」。
        """
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM pending_writeback WHERE writeback_key = ?",
                (record.writeback_key,),
            ).fetchone()
            stamp = now_iso()
            if existing is None:
                with self.transaction():
                    self._conn.execute(
                        "INSERT INTO pending_writeback (writeback_key, judgement_key, "
                        "batch_no, prescription_no, rule_id, rule_version, request_body, "
                        "request_hash, event_fingerprint, retry_count, last_error_code, "
                        "last_error, status, queued_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.writeback_key,
                            record.judgement_key,
                            record.batch_no,
                            record.prescription_no,
                            record.rule_id,
                            record.rule_version,
                            record.request_body,
                            record.request_hash,
                            record.event_fingerprint,
                            int(record.retry_count),
                            record.last_error_code,
                            record.last_error,
                            record.status.value,
                            record.queued_at or stamp,
                            stamp,
                        ),
                    )
            else:
                # 已存在 → 累加重试次数、刷新错误码与更新时间；**入队时间保持首次**
                with self.transaction():
                    self._conn.execute(
                        "UPDATE pending_writeback SET retry_count = ?, last_error_code = ?, "
                        "last_error = ?, status = ?, updated_at = ? WHERE writeback_key = ?",
                        (
                            int(existing["retry_count"]) + int(record.retry_count),
                            record.last_error_code,
                            record.last_error,
                            record.status.value,
                            stamp,
                            record.writeback_key,
                        ),
                    )
            row = self._conn.execute(
                "SELECT * FROM pending_writeback WHERE writeback_key = ?",
                (record.writeback_key,),
            ).fetchone()
        return _to_pending_writeback(row)

    def get_pending_writeback(self, writeback_key: str) -> PendingWriteback | None:
        """按回写幂等键取一条待重放记录。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_writeback WHERE writeback_key = ?",
                (writeback_key,),
            ).fetchone()
        return _to_pending_writeback(row) if row is not None else None

    def list_pending_writeback(self, status: str | None = None) -> list[PendingWriteback]:
        """读回待重放记录（按入队时间排序）。"""
        with self._lock:
            if status is None:
                rows = self._conn.execute(
                    "SELECT * FROM pending_writeback ORDER BY queued_at, writeback_key"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM pending_writeback WHERE status = ? "
                    "ORDER BY queued_at, writeback_key",
                    (status,),
                ).fetchall()
        return [_to_pending_writeback(row) for row in rows]

    def mark_pending_replayed(self, writeback_key: str) -> bool:
        """标记一条待重放记录已重放（重放成功，或已被幂等守卫判为早已发出）。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE pending_writeback SET status = ?, updated_at = ? "
                "WHERE writeback_key = ? AND status <> ?",
                (
                    PendingWritebackStatus.REPLAYED.value,
                    now_iso(),
                    writeback_key,
                    PendingWritebackStatus.REPLAYED.value,
                ),
            )
            return cursor.rowcount > 0

    def count_pending_writeback(self, status: str | None = None) -> int:
        """待重放记录数（可按状态过滤）。"""
        with self._lock:
            if status is None:
                row = self._conn.execute("SELECT COUNT(*) FROM pending_writeback").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM pending_writeback WHERE status = ?", (status,)
                ).fetchone()
        return int(row[0]) if row else 0

    def withdraw_pending_writebacks(self, judgement_key: str) -> int:
        """把某条判定行**仍在排队**的回写标为「已撤回」，返回改动条数。

        **只改「待重放」的行**：已经重放成功的（已发出）不改成已撤回——
        那一行留在历史里，是「曾经发出过」的证据。撤回后这些排队行不再被
        重放通道取走（重放只取「待重放」），因此「撤回后不再发出」由状态位
        本身保证，不依赖调用方记得跳过哪几条。
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE pending_writeback SET status = ?, updated_at = ? "
                "WHERE judgement_key = ? AND status = ?",
                (
                    PendingWritebackStatus.WITHDRAWN.value,
                    now_iso(),
                    judgement_key,
                    PendingWritebackStatus.PENDING.value,
                ),
            )
            return cursor.rowcount


# ---------------------------------------------------------------------------
# 批次处理主路径
# ---------------------------------------------------------------------------


def process_batch(
    store: Store,
    batch: BatchFile,
    rules: Sequence[RuleRow],
    *,
    fingerprint: str | None = None,
) -> BatchOutcome:
    """处理一个落地批次：算指纹 → 判定 → 入库（含重复投递去重）。

    **重复投递的处置**：同一批次号 + 同一事件指纹再次到达 → 判为重复投递，
    审计表**追加**一批留痕（``writeback_state`` = 重复投递），
    而**判定行数不变**——去重靠判定幂等键（判定表主键），不是靠「重复就跳过」：
    规则版本未变时幂等键相同，一行都不会新增；规则改版后重投时版本变了、
    幂等键随之变，新版本结论照常入库，历史结论保留。

    ``fingerprint``：调用方已经算过指纹时传进来（守护进程在判定之前就要把
    指纹记进批次进度表，避免「判定中途崩溃 → 批次不可见」）。
    """
    fingerprint = fingerprint if fingerprint else file_fingerprint(batch.path)
    judgement = judge_batch(
        batch.rows,
        rules,
        batch_no=batch.batch_no,
        source_file=batch.path.name,
        event_fingerprint=fingerprint,
    )
    # 判定完成即置「已判定」；合法跃迁表由 engine/statemachine.py 负责，
    # 本模块只落事实状态，不做跃迁校验。
    rows = [
        replace(row, state=JudgementState.DECIDED, event_fingerprint=fingerprint)
        for row in judgement.rows
    ]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.conclusion.value] = counts.get(row.conclusion.value, 0) + 1

    with store.transaction():
        record, duplicate = store.register_batch(
            batch_no=batch.batch_no,
            event_fingerprint=fingerprint,
            source_file=batch.path.name,
            source_kind=batch.source_kind,
            prescription_count=batch.prescription_count,
        )
        # 去重靠**判定幂等键**（判定表主键），不靠「重复就跳过」：
        # 同一份事件重投时规则版本未变 → 幂等键相同 → 一行都不会新增；
        # 规则改版后重投 → 版本变了、幂等键变了 → 新版本结论照常入库，
        # 历史结论保留（重判留痕由 engine/statemachine.py 负责）。
        insert_outcome = store.insert_judgements(rows)

        audit_state = WRITEBACK_DUPLICATE if duplicate else WRITEBACK_PENDING
        audit_rows = [
            AuditRow(
                batch_no=row.batch_no,
                prescription_no=row.prescription_no,
                rule_id=row.rule_id,
                rule_version=row.rule_version,
                basis=row.basis,
                conclusion=row.conclusion,
                event_fingerprint=fingerprint,
                writeback_state=audit_state,
                retry_count=0,
                action=AuditAction.JUDGEMENT.value,
            )
            for row in rows
        ]
        appended = store.append_audit(audit_rows)

        total_judgements = store.count_judgements(batch.batch_no)
        store.update_batch_progress(
            batch.batch_no,
            fingerprint,
            judgement_count=total_judgements,
            status=BatchStatus.DUPLICATE if duplicate else BatchStatus.JUDGED,
        )
        record = store.get_batch(batch.batch_no, fingerprint) or record

    return BatchOutcome(
        batch=record,
        duplicate=duplicate,
        fingerprint=fingerprint,
        judgement_rows=insert_outcome.inserted,
        audit_rows=appended,
        total_judgements=total_judgements,
        total_audit=store.count_audit(batch.batch_no),
        counts=counts,
        new_counts=_count_conclusions(insert_outcome.inserted_rows),
    )


def _count_conclusions(rows: Iterable[JudgementRow]) -> dict[str, int]:
    """按三态结论计数（缺失的结论计 0）。"""
    counts = {member.value: 0 for member in Conclusion}
    for row in rows:
        counts[row.conclusion.value] += 1
    return counts


# ---------------------------------------------------------------------------
# 行转换
# ---------------------------------------------------------------------------


def _to_batch_record(row: sqlite3.Row) -> BatchRecord:
    return BatchRecord(
        batch_no=str(row["batch_no"]),
        event_fingerprint=str(row["event_fingerprint"]),
        source_file=str(row["source_file"]),
        source_kind=str(row["source_kind"]),
        prescription_count=int(row["prescription_count"]),
        judgement_count=int(row["judgement_count"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _to_batch_progress(row: sqlite3.Row) -> BatchProgress:
    return BatchProgress(
        batch_no=str(row["batch_no"]),
        event_fingerprint=str(row["event_fingerprint"]),
        source_path=str(row["source_path"]),
        source_file=str(row["source_file"]),
        source_kind=str(row["source_kind"]),
        prescription_count=int(row["prescription_count"]),
        judgement_rows=int(row["judgement_rows"]),
        pushed_rows=int(row["pushed_rows"]),
        stage=str(row["stage"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _to_batch_run(row: sqlite3.Row) -> BatchRun:
    return BatchRun(
        batch_no=str(row["batch_no"]),
        event_fingerprint=str(row["event_fingerprint"]),
        received_rows=int(row["received_rows"]),
        judgement_rows=int(row["judgement_rows"]),
        compliant=int(row["compliant"]),
        violation=int(row["violation"]),
        undeterminable=int(row["undeterminable"]),
        writeback_sent=int(row["writeback_sent"]),
        writeback_failed=int(row["writeback_failed"]),
        pushed=int(row["pushed"]),
        elapsed_sec=float(row["elapsed_sec"]),
        resumed=int(row["resumed"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _to_judgement_row(row: sqlite3.Row) -> JudgementRow:
    missing = str(row["missing_fields"])
    return JudgementRow(
        batch_no=str(row["batch_no"]),
        prescription_no=str(row["prescription_no"]),
        rule_id=str(row["rule_id"]),
        rule_version=str(row["rule_version"]),
        conclusion=Conclusion.from_value(str(row["conclusion"])),
        severity=str(row["severity"]) or None,
        reason=str(row["reason"]),
        basis=str(row["basis"]),
        missing_fields=tuple(
            item for item in missing.split(MISSING_FIELDS_SEPARATOR) if item
        ),
        event_fingerprint=str(row["event_fingerprint"]),
        state=JudgementState(str(row["state"])),
        created_at=str(row["created_at"]),
    )


def _to_judgement_version(row: sqlite3.Row) -> JudgementVersion:
    missing = str(row["missing_fields"])
    return JudgementVersion(
        business_key=str(row["business_key"]),
        version=int(row["version"]),
        batch_no=str(row["batch_no"]),
        prescription_no=str(row["prescription_no"]),
        rule_id=str(row["rule_id"]),
        rule_version=str(row["rule_version"]),
        conclusion=Conclusion.from_value(str(row["conclusion"])),
        severity=str(row["severity"]) or None,
        reason=str(row["reason"]),
        basis=str(row["basis"]),
        missing_fields=tuple(
            item for item in missing.split(MISSING_FIELDS_SEPARATOR) if item
        ),
        event_fingerprint=str(row["event_fingerprint"]),
        state=JudgementState(str(row["state"])),
        created_at=str(row["created_at"]),
    )


def _to_audit_row(row: sqlite3.Row) -> AuditRow:
    return AuditRow(
        batch_no=str(row["batch_no"]),
        prescription_no=str(row["prescription_no"]),
        rule_id=str(row["rule_id"]),
        rule_version=str(row["rule_version"]),
        basis=str(row["basis"]),
        conclusion=Conclusion.from_value(str(row["conclusion"])),
        event_fingerprint=str(row["event_fingerprint"]),
        writeback_state=str(row["writeback_state"]),
        retry_count=int(row["retry_count"]),
        action=str(row["action"] or AuditAction.JUDGEMENT.value),
        created_at=str(row["created_at"]),
    )


def _to_outbound_call_row(row: sqlite3.Row) -> OutboundCallRecord:
    return OutboundCallRecord(
        call_key=str(row["call_key"]),
        channel=str(row["channel"]),
        action=str(row["action"]),
        judgement_key=str(row["judgement_key"]),
        batch_no=str(row["batch_no"]),
        prescription_no=str(row["prescription_no"]),
        rule_id=str(row["rule_id"]),
        rule_version=str(row["rule_version"]),
        conclusion=Conclusion.from_value(str(row["conclusion"])),
        basis=str(row["basis"]),
        event_fingerprint=str(row["event_fingerprint"]),
        status=str(row["status"]),
        retry_count=int(row["retry_count"]),
        error_code=str(row["error_code"]),
        detail=str(row["detail"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _to_confirmation_record(row: sqlite3.Row) -> ConfirmationRecord:
    return ConfirmationRecord(
        idempotency_key=str(row["idempotency_key"]),
        business_key=str(row["business_key"]),
        batch_no=str(row["batch_no"]),
        prescription_no=str(row["prescription_no"]),
        rule_id=str(row["rule_id"]),
        rule_version=str(row["rule_version"]),
        action=str(row["action"]),
        pharmacist_id=str(row["pharmacist_id"]),
        acted_at=str(row["acted_at"]),
        note=str(row["note"]),
        writeback_state=str(row["writeback_state"]),
        created_at=str(row["created_at"]),
    )


def _to_writeback_attempt(row: sqlite3.Row) -> WritebackAttempt:
    return WritebackAttempt(
        writeback_key=str(row["writeback_key"]),
        judgement_key=str(row["judgement_key"]),
        request_hash=str(row["request_hash"]),
        status=WritebackStatus(str(row["status"])),
        retry_count=int(row["retry_count"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _to_pending_writeback(row: sqlite3.Row) -> PendingWriteback:
    return PendingWriteback(
        writeback_key=str(row["writeback_key"]),
        judgement_key=str(row["judgement_key"]),
        batch_no=str(row["batch_no"]),
        prescription_no=str(row["prescription_no"]),
        rule_id=str(row["rule_id"]),
        rule_version=str(row["rule_version"]),
        request_body=str(row["request_body"]),
        request_hash=str(row["request_hash"]),
        event_fingerprint=str(row["event_fingerprint"]),
        retry_count=int(row["retry_count"]),
        last_error_code=str(row["last_error_code"]),
        last_error=str(row["last_error"]),
        status=PendingWritebackStatus(str(row["status"])),
        queued_at=str(row["queued_at"]),
        updated_at=str(row["updated_at"]),
    )


__all__ = [
    "WRITEBACK_DUPLICATE",
    "WRITEBACK_PENDING",
    "BatchOutcome",
    "BatchRecord",
    "BatchStatus",
    "InsertOutcome",
    "Store",
    "process_batch",
]
