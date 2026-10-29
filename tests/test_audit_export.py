"""推送与审计 2/2 的验收：追加式审计表 + 结构化日志 + 审计 CSV 导出。

覆盖本任务的三条硬判据：

1. **审计导出行数 == 判定行数** —— 一批医嘱判一次就有一条判定事件；
   **重复投递只补审计不新增判定**（审计行数增加、判定行数不变）；
2. **``basis`` 列非空** —— 一条结论说不出依据出处，审计台账就等于没有台账；
   缺依据时**显式报错且不落盘**，不导出一份看着正常、实则漏了依据的表；
3. **每次出站调用都留痕** —— 回写 / 撤回 / 推送 / 重放各留一行，
   **失败的调用同样留痕**并带累计重试次数（飞检现场要答得出「发了几次都没成」）。

外加：``action`` 列把「判定事件」与「出站调用事件」分开数、
同输入两次导出逐字相同、老库启动时补 ``action`` 列、
结构化日志的三层信息层级与心跳间隔口径。
"""

from __future__ import annotations

import io
import logging
import sqlite3
from pathlib import Path

import pytest

from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.ingest.watcher import BatchFile
from med_rule_fieldmap.ledger.audit_export import (
    AUDIT_LEDGER_COLUMNS,
    AuditExportError,
    build_audit_records,
    check_audit_invariant,
    export_audit_ledger,
    load_audit_ledger,
    summarize_audit,
)
from med_rule_fieldmap.logging_setup import (
    LEVEL_AUDIT,
    LEVEL_BATCH,
    LEVEL_HEARTBEAT,
    LEVEL_ROW,
    Heartbeat,
    HierarchicalFormatter,
    heartbeat_interval,
    setup_logging,
)
from med_rule_fieldmap.models import AuditAction, AuditRow, Conclusion
from med_rule_fieldmap.outbound.audit import (
    CHANNEL_HIS,
    STATUS_FAILED,
    STATUS_SENT,
    AuditRecorder,
    build_call_key,
)
from med_rule_fieldmap.rules.repository import RuleRepository
from med_rule_fieldmap.store import Store, process_batch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"

HEADER = (
    "batch_no,order_no,prescription_no,visit_no,patient_ref,dept_code,dept_name,"
    "prescriber_id,prescriber_name,drug_code,drug_name,spec,dosage_form,"
    "dosage_text,dosage_value,dosage_unit,route,freq,quantity,rx_type,"
    "drug_category,days_qty,skin_test_result,clinical_diag,age_value,age_unit,"
    "event_time"
)

ROW = (
    "BATCH-001,ORD-0001,RX-0001,VISIT-0001,PT-0001,D101,心血管内科门诊,"
    "PR-001,医师甲,DC-001,阿司匹林肠溶片,0.1g:1片,片剂,每次1片,1,片,口服,每日一次,"
    "1,门急诊一般,普通药品,3,,原发性高血压,52,岁,2026-10-28 08:30:00"
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def write_orders(path: Path, rows: int = 3, *, batch_no: str = "BATCH-001") -> Path:
    """写一份完整（26 列齐全）的样例医嘱 CSV。"""
    lines = [HEADER]
    for index in range(rows):
        row = ROW.replace("RX-0001", f"RX-{index + 1:04d}").replace(
            "VISIT-0001", f"VISIT-{index + 1:04d}"
        )
        row = row.replace("PT-0001", f"PT-{index + 1:04d}")
        row = row.replace("BATCH-001", batch_no)
        lines.append(row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def rules():
    """样例规则集。"""
    repository = RuleRepository(SAMPLE_RULES)
    repository.load()
    assert repository.effective_rules
    return repository.effective_rules


@pytest.fixture()
def orders(tmp_path: Path) -> Path:
    """一份待投递的落地批次文件。"""
    return write_orders(tmp_path / "orders.csv", rows=3)


@pytest.fixture()
def store(tmp_path: Path):
    """状态库。"""
    instance = Store(tmp_path / "state" / "mrfm.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


def read_batch(path: Path) -> BatchFile:
    """把落地文件读成批次。"""
    rows = read_prescription_rows(path)
    return BatchFile(
        path=path,
        batch_no=rows[0].batch_no or path.stem,
        rows=tuple(rows),
        source_kind="csv",
    )


# ---------------------------------------------------------------------------
# 审计导出：行数与依据
# ---------------------------------------------------------------------------


def test_first_delivery_audit_rows_equal_judgement_rows(
    store: Store, orders: Path, rules, tmp_path: Path
) -> None:
    """★ 审计导出行数 == 判定行数（一条判定一条审计事件）。"""
    outcome = process_batch(store, read_batch(orders), rules)
    assert outcome.judgement_rows > 0

    path, check = export_audit_ledger(store, tmp_path / "out")
    records = load_audit_ledger(path)

    assert len(records) == store.count_judgements("BATCH-001")
    assert check.judgement_events == store.count_judgements("BATCH-001")
    assert check.judgement_rows_match is True
    assert check.basis_ok is True
    assert check.outbound_events == 0


def test_duplicate_delivery_adds_audit_rows_but_not_judgements(
    store: Store, orders: Path, rules, tmp_path: Path
) -> None:
    """★ 重复投递：审计行数增加、判定行数不变。"""
    first = process_batch(store, read_batch(orders), rules)
    judgements_before = store.count_judgements("BATCH-001")
    audit_before = store.count_audit("BATCH-001")
    assert audit_before == judgements_before

    second = process_batch(store, read_batch(orders), rules)
    judgements_after = store.count_judgements("BATCH-001")
    audit_after = store.count_audit("BATCH-001")

    assert second.duplicate is True
    assert judgements_after == judgements_before, "重复投递不得新增判定行"
    assert audit_after == audit_before + judgements_before, "重复投递必须补审计行"
    assert first.judgement_rows == judgements_before

    path, check = export_audit_ledger(
        store, tmp_path / "out", expected_judgements=judgements_after
    )
    records = load_audit_ledger(path)
    assert len(records) == audit_after
    assert check.judgement_events == audit_after
    assert check.judgement_rows_match is False, "审计行多于判定行（重复投递补留）是预期"


def test_audit_ledger_columns_are_frozen(store: Store, orders: Path, rules, tmp_path: Path) -> None:
    """审计台账列固定 14 列，读回一致。"""
    process_batch(store, read_batch(orders), rules)
    path, _ = export_audit_ledger(store, tmp_path / "out")
    records = load_audit_ledger(path)

    assert path.name == "audit_ledger.csv"
    assert len(records) > 0
    for record in records:
        assert tuple(record.keys()) == AUDIT_LEDGER_COLUMNS
    assert AUDIT_LEDGER_COLUMNS == (
        "audit_id",
        "action",
        "batch_no",
        "prescription_no",
        "rule_id",
        "rule_version",
        "conclusion",
        "basis",
        "event_fingerprint",
        "writeback_state",
        "retry_count",
        "channel",
        "error_code",
        "created_at",
    )


def test_every_audit_row_carries_basis(store: Store, orders: Path, rules, tmp_path: Path) -> None:
    """★ ``basis`` 列非空——每条结论都说得出依据出处。"""
    process_batch(store, read_batch(orders), rules)
    path, check = export_audit_ledger(store, tmp_path / "out")
    records = load_audit_ledger(path)

    assert all(record["basis"].strip() for record in records)
    assert all(record["rule_id"].strip() for record in records)
    assert check.basis_missing == 0


def test_missing_basis_raises_and_writes_nothing(store: Store, tmp_path: Path) -> None:
    """★ 缺依据出处 → 显式报错且**不落盘**（不导出看着正常、实则漏依据的表）。"""
    store.append_audit(
        [
            AuditRow(
                batch_no="BATCH-009",
                prescription_no="RX-009",
                rule_id="RX-NO-BASIS",
                rule_version="v1",
                basis="",
                conclusion=Conclusion.VIOLATION,
                action=AuditAction.JUDGEMENT.value,
            )
        ]
    )
    out_dir = tmp_path / "out"
    with pytest.raises(AuditExportError) as excinfo:
        export_audit_ledger(store, out_dir)
    assert "basis" in str(excinfo.value)
    assert not (out_dir / "audit_ledger.csv").exists(), "缺依据时不得落盘"


def test_two_exports_are_byte_identical(store: Store, orders: Path, rules, tmp_path: Path) -> None:
    """同输入两次导出逐字相同——飞检现场两份审计件要对得上。"""
    process_batch(store, read_batch(orders), rules)
    first, _ = export_audit_ledger(store, tmp_path / "out1")
    second, _ = export_audit_ledger(store, tmp_path / "out2")
    assert first.read_bytes() == second.read_bytes()


def test_check_audit_invariant_counts_events(store: Store, orders: Path, rules) -> None:
    """自检把判定事件与出站调用事件分开数。"""
    process_batch(store, read_batch(orders), rules)
    recorder = AuditRecorder(store)
    recorder.record_outbound_call(
        channel=CHANNEL_HIS,
        action=AuditAction.WRITEBACK.value,
        status=STATUS_SENT,
        batch_no="BATCH-001",
        prescription_no="RX-0001",
        rule_id="RX-LEGAL-MA-OUT-DAYS",
        basis="处方审核规则体系 §3 特殊药品处方量限",
        conclusion=Conclusion.VIOLATION,
    )

    records = build_audit_records(store, "BATCH-001")
    check = check_audit_invariant(records, store.count_judgements("BATCH-001"))
    assert check.judgement_events == store.count_judgements("BATCH-001")
    assert check.outbound_events == 1
    assert check.total == check.judgement_events + check.outbound_events
    assert summarize_audit(records)[AuditAction.JUDGEMENT.value] == check.judgement_events


# ---------------------------------------------------------------------------
# 出站调用留痕
# ---------------------------------------------------------------------------


def test_successful_outbound_call_is_recorded(store: Store, tmp_path: Path) -> None:
    """一次成功的回写在审计流水里留一行（动作 = 回写，通道 = HIS）。"""
    recorder = AuditRecorder(store)
    recorder.record_outbound_call(
        channel=CHANNEL_HIS,
        action=AuditAction.WRITEBACK.value,
        status=STATUS_SENT,
        batch_no="BATCH-001",
        prescription_no="RX-0001",
        rule_id="RX-LEGAL-MA-OUT-DAYS",
        rule_version="v1",
        basis="处方审核规则体系 §3 特殊药品处方量限",
        conclusion=Conclusion.VIOLATION,
        retry_count=0,
    )
    assert store.count_outbound_calls() == 1

    path, check = export_audit_ledger(store, tmp_path / "out")
    records = load_audit_ledger(path)
    assert len(records) == 1
    row = records[0]
    assert row["action"] == AuditAction.WRITEBACK.value
    assert row["channel"] == CHANNEL_HIS
    assert row["writeback_state"] == STATUS_SENT
    assert row["basis"].strip()
    assert check.outbound_events == 1
    assert check.judgement_events == 0


def test_failed_outbound_call_is_also_recorded_with_retry_count(
    store: Store, tmp_path: Path
) -> None:
    """★ 失败的出站调用同样留痕，并带累计重试次数与错误码。"""
    recorder = AuditRecorder(store)
    recorder.record_outbound_call(
        channel=CHANNEL_HIS,
        action=AuditAction.WRITEBACK.value,
        status=STATUS_FAILED,
        batch_no="BATCH-001",
        prescription_no="RX-0001",
        rule_id="RX-LEGAL-MA-OUT-DAYS",
        rule_version="v1",
        basis="处方审核规则体系 §3 特殊药品处方量限",
        conclusion=Conclusion.VIOLATION,
        retry_count=3,
        error_code="HTTP 500",
        detail="退避重试耗尽，已落待重放队列",
    )

    path, _ = export_audit_ledger(store, tmp_path / "out")
    row = load_audit_ledger(path)[0]
    assert row["writeback_state"] == STATUS_FAILED
    assert row["retry_count"] == "3"
    assert row["error_code"] == "HTTP 500"
    assert "待重放" in row["audit_id"] or row["audit_id"]


def test_outbound_call_key_is_stable_and_distinguishes_attempts() -> None:
    """事件号稳定；重试（第 2 次尝试）另起一行，不覆盖旧行。"""
    first = build_call_key(CHANNEL_HIS, "回写", "JK-1", "HASH", 1)
    again = build_call_key(CHANNEL_HIS, "回写", "JK-1", "HASH", 1)
    second = build_call_key(CHANNEL_HIS, "回写", "JK-1", "HASH", 2)
    assert first == again
    assert first != second


def test_audit_recorder_degrades_to_warning_when_store_cannot_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """状态库不支持出站留痕时降级为告警，不让主流程失败。"""

    class _Legacy:
        def append_audit(self, rows):  # pragma: no cover - 仅协议占位
            return len(list(rows))

    recorder = AuditRecorder(_Legacy())
    with caplog.at_level(logging.WARNING):
        record = recorder.record_outbound_call(
            channel=CHANNEL_HIS,
            action=AuditAction.WRITEBACK.value,
            status=STATUS_SENT,
        )
    assert record.status == STATUS_SENT
    assert "未留痕" in caplog.text


# ---------------------------------------------------------------------------
# 状态库：老库补列
# ---------------------------------------------------------------------------


def test_store_migrates_legacy_audit_table_without_action_column(tmp_path: Path) -> None:
    """老库（审计表没有 ``action`` 列）启动时补列，历史行读出来按「判定」处理。"""
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE audit (
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
            created_at         TEXT    NOT NULL
        );
        INSERT INTO audit (batch_no, prescription_no, rule_id, rule_version, basis,
                           conclusion, event_fingerprint, writeback_state, retry_count,
                           created_at)
        VALUES ('BATCH-LEGACY', 'RX-0001', 'RX-A', 'v1', '依据出处',
                '违规', 'FP', '未回写', 0, '2026-10-28T08:30:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    store = Store(db_path)
    try:
        rows = store.list_audit("BATCH-LEGACY")
        assert len(rows) == 1
        assert rows[0].action == AuditAction.JUDGEMENT.value
        assert rows[0].basis == "依据出处"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 结构化日志
# ---------------------------------------------------------------------------


def _render(level: int, message: str) -> str:
    record = logging.LogRecord(
        name="med_rule_fieldmap.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    return HierarchicalFormatter(color=False).format(record)


def test_log_levels_render_three_visual_layers() -> None:
    """★ 三层信息层级：批次摘要最显眼 / 逐行判定缩进 / 审计与心跳最暗灰前缀。"""
    batch = _render(LEVEL_BATCH, "批次 B-1 · 收到 26 行")
    row = _render(LEVEL_ROW, "判定 RX-0002 · 违规")
    audit = _render(LEVEL_AUDIT, "批次 B-1 · 判定 26 条")
    alive = _render(LEVEL_HEARTBEAT, "规则 18 条 · 已处理 1 批")

    assert batch == "批次 B-1 · 收到 26 行"
    assert row == "  判定 RX-0002 · 违规", "逐行判定缩进一格"
    assert audit.startswith("[audit] ")
    assert alive.startswith("[alive] ")
    assert "  " not in batch.split("批次")[0]


def test_log_formatter_adds_color_only_when_enabled() -> None:
    record = logging.LogRecord(
        name="med_rule_fieldmap.test",
        level=LEVEL_BATCH,
        pathname=__file__,
        lineno=1,
        msg="批次 B-1",
        args=(),
        exc_info=None,
    )
    plain = HierarchicalFormatter(color=False).format(record)
    colored = HierarchicalFormatter(color=True).format(record)
    assert plain == "批次 B-1"
    assert "\x1b[" in colored
    assert colored.endswith("\x1b[0m")


def test_heartbeat_interval_reads_environment_variable() -> None:
    """心跳间隔读 ``MRFM_HEARTBEAT_SEC``；取值非法时回落默认值。"""
    assert heartbeat_interval({}) == 30
    assert heartbeat_interval({"MRFM_HEARTBEAT_SEC": "45"}) == 45
    assert heartbeat_interval({"MRFM_HEARTBEAT_SEC": "不是数字"}) == 30
    assert heartbeat_interval({"MRFM_HEARTBEAT_SEC": "0"}) == 30
    assert heartbeat_interval({"MRFM_HEARTBEAT_SEC": "-5"}) == 30


def test_heartbeat_only_emits_on_interval(caplog: pytest.LogCaptureFixture) -> None:
    """心跳到点才出一行，不刷屏。"""
    clock = {"now": 0.0}
    logger = logging.getLogger("med_rule_fieldmap.test.heartbeat")
    logger.setLevel(LEVEL_HEARTBEAT)
    heartbeat = Heartbeat(30, logger_=logger, clock=lambda: clock["now"])

    with caplog.at_level(LEVEL_HEARTBEAT, logger=logger.name):
        assert heartbeat.beat("心跳 1", now=0.0) is True
        assert heartbeat.beat("心跳 2", now=10.0) is False
        assert heartbeat.beat("心跳 3", now=30.0) is True
        assert heartbeat.beat("心跳 4", now=45.0) is False

    assert caplog.text.count("心跳") == 2


def test_setup_logging_writes_hierarchical_lines() -> None:
    """``setup_logging`` 把日志按层级写到给定流。"""
    stream = io.StringIO()
    root = setup_logging(level=LEVEL_AUDIT, stream=stream, color=False, force=True)
    try:
        logger = logging.getLogger("med_rule_fieldmap.test.setup")
        logger.log(LEVEL_BATCH, "批次 B-1")
        logger.log(LEVEL_ROW, "判定 RX-0001")
        logger.log(LEVEL_AUDIT, "审计一行")
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
    output = stream.getvalue()
    assert "批次 B-1" in output
    assert "  判定 RX-0001" in output
    assert "[audit] 审计一行" in output
