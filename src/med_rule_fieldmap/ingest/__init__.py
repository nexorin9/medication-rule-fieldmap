"""入站层：把 HIS 医嘱落地目录里的文件读成内部处方行。

已落地两种入站格式的读取：

* **CSV** —— 表头名映射 / 别名归一 / 编码兜底 / 必需列完整性校验 / 类型化读数；
* **HL7 v2 ``ORM^O01``** —— 段字段映射到与 CSV 等价的处方行、缺项置空、
  非医嘱消息显式跳过并告警。

目录监听与整文件挂起：

* **完整性校验**（``integrity.py``）—— 先校验、后切批次：空文件 / 编码不可识别 /
  缺表头 / 文件截断 / 缺必需列五类整文件级问题一律**不做部分判定**；
* **挂起台账**（``suspend.py``）—— 挂起原因、缺列清单、挂起与解除留痕
  （追加式，解除不改写历史行）；
* **目录监听**（``watcher.py``）—— watchdog 优先、轮询兜底；文件大小与 mtime
  连续两次不变才认为写入完成，再切批次；
* **事件指纹**（``fingerprint.py``）—— 落地文件按内容 sha256 取指纹，
  同一批次号 + 同一指纹再次到达即判为重复投递（只补审计、不新增判定行）。
"""

from .fingerprint import (
    FINGERPRINT_ALGORITHM,
    FingerprintError,
    business_key,
    bytes_fingerprint,
    file_fingerprint,
    idempotency_key,
    short_fingerprint,
    text_fingerprint,
)
from .csv_reader import (
    REQUIRED_COLUMNS,
    CsvReadError,
    TypedCell,
    list_csv_files,
    normalize_header_name,
    read_prescription_rows,
    read_typed_cell,
    read_typed_cells,
)
from .hl7_orm import (
    HL7_COVERED_FIELDS,
    HL7_FIELD_MAP,
    HL7_SUFFIXES,
    Hl7OrderBatch,
    Hl7ParseError,
    SkippedMessage,
    is_order_message,
    list_hl7_files,
    parse_hl7_timestamp,
    parse_message,
    parse_order_messages,
    read_order_rows,
)
from .integrity import (
    KIND_EMPTY_FILE,
    KIND_MISSING_COLUMNS,
    KIND_MISSING_HEADER,
    KIND_OK,
    KIND_TRUNCATED,
    KIND_UNDECODABLE,
    IntegrityReport,
    check_csv_integrity,
    check_file_integrity,
    check_hl7_integrity,
)
from .suspend import (
    STATUS_RELEASED,
    STATUS_SUSPENDED,
    SUSPEND_LEDGER_COLUMNS,
    SUSPEND_LEDGER_FILENAME,
    SuspendLedger,
    SuspendRecord,
    default_ledger_path,
)
from .watcher import (
    EVENT_BATCH,
    EVENT_SUSPENDED,
    BatchFile,
    InboxWatcher,
    WatcherEvent,
    watchdog_available,
)

__all__ = [
    "EVENT_BATCH",
    "EVENT_SUSPENDED",
    "FINGERPRINT_ALGORITHM",
    "HL7_COVERED_FIELDS",
    "HL7_FIELD_MAP",
    "HL7_SUFFIXES",
    "KIND_EMPTY_FILE",
    "KIND_MISSING_COLUMNS",
    "KIND_MISSING_HEADER",
    "KIND_OK",
    "KIND_TRUNCATED",
    "KIND_UNDECODABLE",
    "REQUIRED_COLUMNS",
    "STATUS_RELEASED",
    "STATUS_SUSPENDED",
    "SUSPEND_LEDGER_COLUMNS",
    "SUSPEND_LEDGER_FILENAME",
    "BatchFile",
    "CsvReadError",
    "FingerprintError",
    "Hl7OrderBatch",
    "Hl7ParseError",
    "InboxWatcher",
    "IntegrityReport",
    "SkippedMessage",
    "SuspendLedger",
    "SuspendRecord",
    "TypedCell",
    "WatcherEvent",
    "business_key",
    "bytes_fingerprint",
    "check_csv_integrity",
    "check_file_integrity",
    "check_hl7_integrity",
    "default_ledger_path",
    "file_fingerprint",
    "idempotency_key",
    "is_order_message",
    "list_csv_files",
    "list_hl7_files",
    "normalize_header_name",
    "parse_hl7_timestamp",
    "parse_message",
    "parse_order_messages",
    "read_order_rows",
    "read_prescription_rows",
    "read_typed_cell",
    "read_typed_cells",
    "short_fingerprint",
    "text_fingerprint",
    "watchdog_available",
]
