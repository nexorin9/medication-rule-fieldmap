"""入站 4/4 的验收：事件文件指纹 + 批次记录 + 判定幂等键。

覆盖三条硬判据：

1. **指纹稳定可复现** —— 同一份文件反复算，指纹一致；内容变了指纹变；
2. **重复投递只补审计、不新增判定行** —— 同一批次号 + 同一指纹再次到达，
   判定行数不变、审计行数增加；
3. **判定业务键口径一致** —— 幂等键 = sha256(batch_no｜prescription_no｜
   rule_id｜rule_version)，与 :attr:`models.JudgementRow.idempotency_key` 同源。

外加：批次表计数、审计追加式语义、规则版本变更时历史结论保留、
CLI ``--state-db`` 重跑不重复落台账。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from med_rule_fieldmap.cli import main
from med_rule_fieldmap.ingest.fingerprint import (
    FingerprintError,
    business_key,
    file_fingerprint,
    idempotency_key,
    short_fingerprint,
)
from med_rule_fieldmap.ingest.watcher import BatchFile
from med_rule_fieldmap.models import (
    AuditRow,
    Conclusion,
    JudgementRow,
    JudgementState,
)
from med_rule_fieldmap.store import (
    WRITEBACK_DUPLICATE,
    WRITEBACK_PENDING,
    BatchStatus,
    Store,
    process_batch,
)
from med_rule_fieldmap.rules.repository import RuleRepository

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
# 样例数据与夹具
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
    """样例规则集（12 条生效规则）。"""
    repository = RuleRepository(SAMPLE_RULES)
    repository.load()
    assert repository.effective_rules
    return repository.effective_rules


@pytest.fixture()
def orders(tmp_path: Path) -> Path:
    """一份待投递的落地批次文件。"""
    return write_orders(tmp_path / "orders.csv", rows=3)


def read_batch(path: Path) -> BatchFile:
    """把落地文件读成批次（测试里绕开 watcher，直接构造批次对象）。"""
    from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows

    rows = read_prescription_rows(path)
    return BatchFile(
        path=path,
        batch_no=rows[0].batch_no or path.stem,
        rows=tuple(rows),
        source_kind="csv",
    )


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------


def test_file_fingerprint_is_stable_and_matches_sha256_of_content(orders: Path) -> None:
    """同一份文件反复算，指纹一致，且等于内容本身的 sha256。"""
    first = file_fingerprint(orders)
    second = file_fingerprint(orders)
    assert first == second
    expected = hashlib.sha256(orders.read_bytes()).hexdigest()
    assert first == expected
    assert len(first) == 64


def test_file_fingerprint_changes_when_content_changes(orders: Path) -> None:
    """内容变了指纹必须变——否则重投会被误判成同一事件。"""
    before = file_fingerprint(orders)
    orders.write_text(orders.read_text(encoding="utf-8") + "追加行\n", encoding="utf-8")
    assert file_fingerprint(orders) != before


def test_file_fingerprint_of_renamed_copy_is_identical(tmp_path: Path, orders: Path) -> None:
    """换名重投（内容没变）指纹不变——HIS 导出文件名常带时间戳。"""
    renamed = tmp_path / "orders_20261028_0900.csv"
    shutil.copyfile(orders, renamed)
    assert file_fingerprint(renamed) == file_fingerprint(orders)


def test_file_fingerprint_on_missing_file_raises_explicitly(tmp_path: Path) -> None:
    """文件读不到时显式报错，不返回空串（空指纹会把两次投递混成一次）。"""
    with pytest.raises(FingerprintError):
        file_fingerprint(tmp_path / "不存在的文件.csv")


def test_short_fingerprint_is_only_for_display() -> None:
    """短指纹只用于终端展示，且必须拒绝非正长度。"""
    fingerprint = "a" * 64
    assert short_fingerprint(fingerprint) == "a" * 12
    assert len(short_fingerprint(fingerprint, 8)) == 8
    with pytest.raises(ValueError):
        short_fingerprint(fingerprint, 0)


def test_business_key_and_idempotency_key_match_the_row_property() -> None:
    """业务键口径与 models.JudgementRow.idempotency_key 同源。"""
    key = business_key("B-1", "RX-1", "RULE-A", "v1")
    assert key == "B-1｜RX-1｜RULE-A｜v1"
    assert idempotency_key("B-1", "RX-1", "RULE-A", "v1") == hashlib.sha256(
        key.encode("utf-8")
    ).hexdigest()

    row = JudgementRow(
        batch_no="B-1",
        prescription_no="RX-1",
        rule_id="RULE-A",
        rule_version="v1",
        conclusion=Conclusion.COMPLIANT,
    )
    assert row.idempotency_key == idempotency_key("B-1", "RX-1", "RULE-A", "v1")


def test_idempotency_key_changes_when_rule_version_changes() -> None:
    """规则版本变了就是新结论——幂等键必须跟着变，历史结论不被覆盖。"""
    assert idempotency_key("B-1", "RX-1", "RULE-A", "v1") != idempotency_key(
        "B-1", "RX-1", "RULE-A", "v2"
    )


# ---------------------------------------------------------------------------
# 状态库：批次表 / 判定表 / 审计表
# ---------------------------------------------------------------------------


def test_store_uses_wal_and_creates_tables(tmp_path: Path) -> None:
    """状态库走 WAL（守护进程并发读写的底座），三张表就位。"""
    store = Store(tmp_path / "state" / "mrfm.sqlite3")
    try:
        assert store.journal_mode.lower() == "wal"
        assert store.count_batches() == 0
        assert store.count_judgements() == 0
        assert store.count_audit() == 0
    finally:
        store.close()
    assert (tmp_path / "state" / "mrfm.sqlite3").exists()


def test_first_delivery_writes_judgements_and_audit(tmp_path: Path, orders: Path, rules) -> None:
    """首次投递：判定行与审计行都落库，批次计数对得上。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        outcome = process_batch(store, read_batch(orders), rules)
        assert outcome.duplicate is False
        assert outcome.judgement_rows > 0
        assert outcome.audit_rows == outcome.judgement_rows
        assert store.count_judgements() == outcome.judgement_rows
        assert store.count_audit() == outcome.judgement_rows
        assert store.count_batches() == 1

        record = store.get_batch(outcome.batch.batch_no, outcome.fingerprint)
        assert record is not None
        assert record.prescription_count == 3
        assert record.judgement_count == outcome.judgement_rows
        assert record.status == BatchStatus.JUDGED.value
    finally:
        store.close()


def test_repeat_delivery_keeps_judgement_rows_and_adds_audit(
    tmp_path: Path, orders: Path, rules
) -> None:
    """★ 核心判据：同一批次重复投递 → 判定行数不变、审计行数增加。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        first = process_batch(store, read_batch(orders), rules)
        judgements_after_first = store.count_judgements()
        audit_after_first = store.count_audit()

        second = process_batch(store, read_batch(orders), rules)

        assert second.duplicate is True
        assert second.fingerprint == first.fingerprint
        assert second.judgement_rows == 0
        assert store.count_judgements() == judgements_after_first
        assert store.count_audit() == audit_after_first + first.judgement_rows
        assert store.count_batches() == 1  # 批次行不新增
    finally:
        store.close()


def test_renamed_redelivery_is_treated_as_the_same_event(
    tmp_path: Path, orders: Path, rules
) -> None:
    """换名重投（内容相同）同样识别为重复投递。"""
    renamed = tmp_path / "orders_retry.csv"
    shutil.copyfile(orders, renamed)
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        process_batch(store, read_batch(orders), rules)
        judgement_rows = store.count_judgements()
        outcome = process_batch(store, read_batch(renamed), rules)
        assert outcome.duplicate is True
        assert store.count_judgements() == judgement_rows
    finally:
        store.close()


def test_changed_content_is_a_new_event_but_judgements_are_not_duplicated(
    tmp_path: Path, orders: Path, rules
) -> None:
    """内容变了 = 新事件（不是重复投递），但同键判定行不重复落库。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        process_batch(store, read_batch(orders), rules)
        judgement_rows = store.count_judgements()
        audit_rows = store.count_audit()

        orders.write_text(
            orders.read_text(encoding="utf-8") + "BATCH-001,ORD-9,RX-9,VISIT-9,PT-9,"
            "D101,心血管内科门诊,PR-001,医师甲,DC-001,阿司匹林肠溶片,0.1g:1片,片剂,"
            "每次1片,1,片,口服,每日一次,1,门急诊一般,普通药品,3,,原发性高血压,52,岁,"
            "2026-10-28 08:31:00\n",
            encoding="utf-8",
        )
        outcome = process_batch(store, read_batch(orders), rules)
        assert outcome.duplicate is False
        assert outcome.fingerprint != ""
        # 新事件带来一条新处方的新判定行，历史结论不被覆盖
        assert store.count_judgements() > judgement_rows
        assert store.count_audit() > audit_rows
    finally:
        store.close()


def test_rejudge_with_new_rule_version_keeps_history(tmp_path: Path, orders: Path, rules) -> None:
    """规则改版后重判：版本变了幂等键就变，新旧结论都在库里。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        process_batch(store, read_batch(orders), rules)
        before = store.count_judgements()

        bumped = []
        for rule in rules:
            bumped.append(
                type(rule)(
                    rule_id=rule.rule_id,
                    layer=rule.layer,
                    basis=rule.basis,
                    version=f"{rule.version}-bumped",
                    effective_from=rule.effective_from,
                    effective_to=rule.effective_to,
                    applies_when=rule.applies_when,
                    required_fields=rule.required_fields,
                    expression=rule.expression,
                    severity=rule.severity,
                    missing_conclusion=rule.missing_conclusion,
                    forbidden_terms=rule.forbidden_terms,
                    note=rule.note,
                    source_file=rule.source_file,
                )
            )
        outcome = process_batch(store, read_batch(orders), bumped)
        assert outcome.judgement_rows > 0
        assert store.count_judgements() == before * 2
        # 旧版本结论仍在
        versions = {row.rule_version for row in store.list_judgements()}
        assert len(versions) == 2
    finally:
        store.close()


def test_judgement_rows_have_exactly_one_row_per_prescription_rule_pair(
    tmp_path: Path, orders: Path, rules
) -> None:
    """逐处方 × 逐规则恰好一行：幂等键主键保证不重不漏。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        outcome = process_batch(store, read_batch(orders), rules)
        rows = store.list_judgements()
        keys = {(row.prescription_no, row.rule_id, row.rule_version) for row in rows}
        assert len(keys) == len(rows)
        assert len(rows) == outcome.judgement_rows
        assert all(row.state is JudgementState.DECIDED for row in rows)
    finally:
        store.close()


def test_audit_is_append_only_and_marks_duplicate_delivery(
    tmp_path: Path, orders: Path, rules
) -> None:
    """审计行只追加：历史行不被改写，重复投递显式留痕。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        process_batch(store, read_batch(orders), rules)
        process_batch(store, read_batch(orders), rules)
        audit_rows = store.list_audit()
        states = [row.writeback_state for row in audit_rows]
        assert WRITEBACK_PENDING in states
        assert WRITEBACK_DUPLICATE in states
        # 首次投递的审计行仍原样保留
        first_rows = [row for row in audit_rows if row.writeback_state == WRITEBACK_PENDING]
        assert len(first_rows) == len(audit_rows) // 2
        assert all(row.basis for row in audit_rows)
    finally:
        store.close()


def test_append_audit_does_not_overwrite_history(tmp_path: Path) -> None:
    """直接调审计表：追加后旧行内容不变。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        row = AuditRow(
            batch_no="B-1",
            rule_id="RULE-A",
            rule_version="v1",
            basis="依据甲",
            conclusion=Conclusion.COMPLIANT,
            writeback_state=WRITEBACK_PENDING,
        )
        assert store.append_audit([row]) == 1
        assert store.append_audit([row]) == 1
        assert store.count_audit() == 2
        assert {item.basis for item in store.list_audit()} == {"依据甲"}
    finally:
        store.close()


def test_register_batch_requires_batch_no_and_fingerprint(tmp_path: Path) -> None:
    """批次号或指纹为空时显式报错——空身份会让重复投递识别失效。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        with pytest.raises(ValueError):
            store.register_batch(batch_no="", event_fingerprint="abc")
        with pytest.raises(ValueError):
            store.register_batch(batch_no="B-1", event_fingerprint="")
    finally:
        store.close()


def test_failed_transaction_leaves_no_half_written_batch(
    tmp_path: Path, orders: Path, rules
) -> None:
    """事务失败不留半截状态：批次登记与判定写入要么都成，要么都不成。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.register_batch(
                    batch_no="B-ROLLBACK",
                    event_fingerprint="f" * 64,
                    prescription_count=1,
                )
                raise RuntimeError("模拟判定阶段失败")
        assert store.count_batches() == 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# CLI：--state-db 与 state 子命令
# ---------------------------------------------------------------------------


def test_cli_judge_with_state_db_dedups_on_rerun(
    tmp_path: Path, capsys, orders: Path
) -> None:
    """同一批次跑两遍：第二遍是重复投递，台账行数不变、审计行数翻倍。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copyfile(orders, inbox / orders.name)
    db = tmp_path / "state" / "mrfm.sqlite3"
    out_dir = tmp_path / "out"

    exit_code = main(
        [
            "judge",
            "--inbox",
            str(inbox),
            "--out",
            str(out_dir),
            "--rules",
            str(SAMPLE_RULES),
            "--state-db",
            str(db),
        ]
    )
    assert exit_code == 0
    first_out = capsys.readouterr().out
    assert "重复投递" not in first_out
    assert "事件指纹" in first_out

    from med_rule_fieldmap.ledger.ledger import load_judgement_ledger

    ledger_path = out_dir / "judgement_ledger.csv"
    first_rows = len(load_judgement_ledger(ledger_path))

    exit_code = main(
        [
            "judge",
            "--inbox",
            str(inbox),
            "--out",
            str(out_dir),
            "--rules",
            str(SAMPLE_RULES),
            "--state-db",
            str(db),
        ]
    )
    assert exit_code == 0
    second_out = capsys.readouterr().out
    assert "重复投递" in second_out

    second_rows = len(load_judgement_ledger(ledger_path))
    assert second_rows == first_rows  # 台账不因重投而多出一份

    store = Store(db)
    try:
        assert store.count_batches() == 1
        assert store.count_judgements() == first_rows
        assert store.count_audit() == first_rows * 2
    finally:
        store.close()


def test_cli_judge_ledger_does_not_duplicate_rows_for_renamed_redelivery(
    tmp_path: Path, capsys, orders: Path
) -> None:
    """同一批次换名重投：台账按批次去重，不会多出一份判定行。"""
    from med_rule_fieldmap.ledger.ledger import load_judgement_ledger

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copyfile(orders, inbox / "orders.csv")
    shutil.copyfile(orders, inbox / "orders_retry.csv")
    out_dir = tmp_path / "out"

    exit_code = main(
        [
            "judge",
            "--inbox",
            str(inbox),
            "--out",
            str(out_dir),
            "--rules",
            str(SAMPLE_RULES),
            "--state-db",
            str(tmp_path / "state" / "mrfm.sqlite3"),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "重复投递" in out

    records = load_judgement_ledger(out_dir / "judgement_ledger.csv")
    keys = {(r["prescription_no"], r["rule_id"], r["rule_version"]) for r in records}
    assert len(keys) == len(records)  # 台账里没有重复行


def test_cli_state_subcommand_reports_batches_and_counts(
    tmp_path: Path, capsys, orders: Path
) -> None:
    """``state`` 子命令报得出批次、判定行数与审计行数。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copyfile(orders, inbox / orders.name)
    db = tmp_path / "state" / "mrfm.sqlite3"

    assert (
        main(
            [
                "judge",
                "--inbox",
                str(inbox),
                "--out",
                str(tmp_path / "out"),
                "--rules",
                str(SAMPLE_RULES),
                "--state-db",
                str(db),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["state", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "批次 BATCH-001" in out
    assert "状态 已判定" in out
    assert "合计：判定" in out


def test_cli_state_subcommand_json_output(tmp_path: Path, capsys, orders: Path) -> None:
    """``state --json`` 输出结构化快照，便于科员接脚本。"""
    import json

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copyfile(orders, inbox / orders.name)
    db = tmp_path / "state" / "mrfm.sqlite3"

    main(
        [
            "judge",
            "--inbox",
            str(inbox),
            "--out",
            str(tmp_path / "out"),
            "--rules",
            str(SAMPLE_RULES),
            "--state-db",
            str(db),
        ]
    )
    capsys.readouterr()

    assert main(["state", "--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["batches"][0]["batch_no"] == "BATCH-001"
    assert payload["batches"][0]["event_fingerprint"]
    assert payload["judgement_rows"] > 0
    assert payload["audit_rows"] > 0


def test_cli_state_subcommand_fails_when_db_missing(tmp_path: Path, capsys) -> None:
    """状态库不存在时显式失败，不假装「没有批次」以外的任何结论。"""
    assert main(["state", "--db", str(tmp_path / "nope.sqlite3")]) == 2
    assert "尚未落过批次" in capsys.readouterr().err
