"""挂起台账：整文件挂起的记录与人工解除。

**为什么要有这张表**：落地文件被判为「截断 / 乱码 / 缺必需列」时，文件**留在
``inbox/`` 原地不动**（入站是只读的），但系统必须留下可核对的痕迹——
挂了几份、为什么挂、缺哪几列、什么时候挂的。药师与信息科看这张表就知道
「哪些批次没审、为什么没审」，而不是面对一份少了一半的台账。

**追加式**：解除动作也是**追加**一条「已解除」记录，不改写历史行——
谁在什么时候解除了哪一份、当时缺哪几列，都留痕。``is_suspended()`` 取
每个文件的**最后一条**记录判断当前状态。

**落盘位置**：`out/suspended_ledger.csv`（与判定台账同目录，便于一起给
信息科）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: 挂起台账文件名（spec.md §2.1「整文件挂起」的落点）
SUSPEND_LEDGER_FILENAME = "suspended_ledger.csv"

#: 挂起台账列（顺序即文件里的列序）
SUSPEND_LEDGER_COLUMNS: tuple[str, ...] = (
    "file_name",
    "source_path",
    "kind",
    "reason",
    "missing_columns",
    "suspended_at",
    "status",
    "released_at",
    "detail",
)

#: 状态：挂起中
STATUS_SUSPENDED = "挂起"
#: 状态：已人工解除
STATUS_RELEASED = "已解除"

#: 缺列清单在 CSV 内的分隔符（与判定台账的 missing_fields 同口径）
MISSING_COLUMNS_SEPARATOR = "|"


def _now_iso() -> str:
    """当前时间（UTC，秒级 ISO8601）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class SuspendRecord:
    """一条挂起记录。

    ``status`` 为 :data:`STATUS_SUSPENDED` 表示该文件当前挂着；
    人工解除会**追加**一条 :data:`STATUS_RELEASED` 记录，历史行不被改写。
    """

    file_name: str
    source_path: str = ""
    kind: str = ""
    reason: str = ""
    missing_columns: tuple[str, ...] = ()
    suspended_at: str = ""
    status: str = STATUS_SUSPENDED
    released_at: str = ""
    detail: str = ""

    def to_record(self) -> dict[str, str]:
        """转成 CSV 行（列序与 :data:`SUSPEND_LEDGER_COLUMNS` 一致）。"""
        return {
            "file_name": self.file_name,
            "source_path": self.source_path,
            "kind": self.kind,
            "reason": self.reason,
            "missing_columns": MISSING_COLUMNS_SEPARATOR.join(self.missing_columns),
            "suspended_at": self.suspended_at,
            "status": self.status,
            "released_at": self.released_at,
            "detail": self.detail,
        }

    @classmethod
    def from_record(cls, record: dict[str, str]) -> "SuspendRecord":
        """从 CSV 行还原。"""
        missing = record.get("missing_columns", "") or ""
        return cls(
            file_name=record.get("file_name", ""),
            source_path=record.get("source_path", ""),
            kind=record.get("kind", ""),
            reason=record.get("reason", ""),
            missing_columns=tuple(
                item for item in missing.split(MISSING_COLUMNS_SEPARATOR) if item
            ),
            suspended_at=record.get("suspended_at", ""),
            status=record.get("status", STATUS_SUSPENDED) or STATUS_SUSPENDED,
            released_at=record.get("released_at", ""),
            detail=record.get("detail", ""),
        )


class SuspendLedger:
    """挂起台账（追加式 CSV）。

    典型用法::

        ledger = SuspendLedger(out_dir / SUSPEND_LEDGER_FILENAME)
        ledger.append(SuspendRecord(file_name="batch_001.csv", kind="文件截断", ...))
        ledger.is_suspended("batch_001.csv")   # → True
        ledger.release("batch_001.csv")        # → 追加一条「已解除」
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # -- 写 -----------------------------------------------------------------

    def append(self, record: SuspendRecord) -> SuspendRecord:
        """追加一条记录；缺 ``suspended_at`` 时补当前时间。

        文件不存在时先写表头；已存在则**只在尾部追加**，不改写历史行。
        """
        if not record.suspended_at:
            record = SuspendRecord(
                file_name=record.file_name,
                source_path=record.source_path,
                kind=record.kind,
                reason=record.reason,
                missing_columns=record.missing_columns,
                suspended_at=_now_iso(),
                status=record.status,
                released_at=record.released_at,
                detail=record.detail,
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.path.exists() and self.path.stat().st_size > 0
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(SUSPEND_LEDGER_COLUMNS))
            if not exists:
                writer.writeheader()
            writer.writerow(record.to_record())
        return record

    def release(
        self,
        file_name: str,
        *,
        detail: str = "",
        released_at: str | None = None,
    ) -> SuspendRecord | None:
        """人工解除：追加一条「已解除」记录。

        原记录保留不动（挂起时间、原因、缺列清单都还在）；台账里同一文件的
        **最后一条**记录即当前状态。文件本来就没挂着时返回 ``None``。
        """
        current = self.latest(file_name)
        if current is None or current.status != STATUS_SUSPENDED:
            return None
        released = SuspendRecord(
            file_name=current.file_name,
            source_path=current.source_path,
            kind=current.kind,
            reason=current.reason,
            missing_columns=current.missing_columns,
            suspended_at=current.suspended_at,
            status=STATUS_RELEASED,
            released_at=released_at or _now_iso(),
            detail=detail or "人工解除，待重投",
        )
        return self.append(released)

    # -- 读 -----------------------------------------------------------------

    def records(self) -> list[SuspendRecord]:
        """读全部记录（按写入顺序）。文件不存在时返回空列表。"""
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return []
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            return [SuspendRecord.from_record(row) for row in reader]

    def latest(self, file_name: str) -> SuspendRecord | None:
        """某文件的**最后一条**记录（当前状态）。"""
        latest: SuspendRecord | None = None
        for record in self.records():
            if record.file_name == file_name:
                latest = record
        return latest

    def is_suspended(self, file_name: str) -> bool:
        """该文件当前是否处于挂起状态。"""
        current = self.latest(file_name)
        return current is not None and current.status == STATUS_SUSPENDED

    def pending(self) -> list[SuspendRecord]:
        """当前仍挂着的文件列表（按首次挂起时间排序）。"""
        pending = [
            record
            for record in self.latest_by_file().values()
            if record.status == STATUS_SUSPENDED
        ]
        return sorted(pending, key=lambda item: (item.suspended_at, item.file_name))

    def latest_by_file(self) -> dict[str, SuspendRecord]:
        """每个文件的最后一条记录（文件名 → 记录）。"""
        latest: dict[str, SuspendRecord] = {}
        for record in self.records():
            latest[record.file_name] = record
        return latest


def default_ledger_path(out_dir: str | Path) -> Path:
    """挂起台账的默认落盘路径（``out/suspended_ledger.csv``）。"""
    return Path(out_dir) / SUSPEND_LEDGER_FILENAME


__all__ = [
    "MISSING_COLUMNS_SEPARATOR",
    "STATUS_RELEASED",
    "STATUS_SUSPENDED",
    "SUSPEND_LEDGER_COLUMNS",
    "SUSPEND_LEDGER_FILENAME",
    "SuspendLedger",
    "SuspendRecord",
    "default_ledger_path",
]
