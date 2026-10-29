"""确认回写 1/5 的验收：判定幂等键 + 回写幂等抑制。

覆盖三条硬判据：

1. **判定去重** —— 判定键 ``sha256(batch_no｜prescription_no｜rule_id｜rule_version)``
   建唯一索引；同一批次重复投递两次，**判定行数不变**、审计行数增加；
2. **回写幂等键** —— = 判定键 ＋ 请求体哈希；取值稳定可复现，
   请求体变则键变（结论真变了要能重发），判定键变则键变；
3. **重复投递不重复回写** —— 同一批次投递两次，实际发出的回写请求数**不变**；
   守护进程重启后仍然抑制（幂等身份落状态库，不只在内存里）。
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from med_rule_fieldmap.ingest.fingerprint import business_key, idempotency_key
from med_rule_fieldmap.ingest.watcher import BatchFile
from med_rule_fieldmap.models import (
    Conclusion,
    JudgementRow,
    WritebackStatus,
)
from med_rule_fieldmap.outbound.idempotency import (
    WRITE_BACK_KEY_SEP,
    GuardedWriter,
    WritebackIdempotencyGuard,
    canonical_request_body,
    hash_request_body,
    writeback_idempotency_key,
)
from med_rule_fieldmap.rules.repository import RuleRepository
from med_rule_fieldmap.store import Store, process_batch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"

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


def review_payload(row: JudgementRow) -> dict[str, object]:
    """按 spec.md §2.3 造一份 HIS 审方复核回写请求体。"""
    return {
        "batch_no": row.batch_no,
        "prescription_no": row.prescription_no,
        "rule_id": row.rule_id,
        "rule_version": row.rule_version,
        "conclusion": row.conclusion.wire_value,
        "severity": row.severity or "",
        "reason": row.reason,
        "basis": row.basis,
        "missing_fields": list(row.missing_fields),
        "reviewed_by": "PHARM-007",
        "reviewed_at": "2026-10-28T09:00:00+00:00",
    }


class CountingSender:
    """出站发送替身：只记发了什么、发了多少回（回写客户端在下一条任务接进来）。"""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_times = fail_times

    def __call__(self, judgement_key: str, payload: dict[str, object]) -> str:
        self.calls.append((judgement_key, dict(payload)))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟 HIS 侧 500")
        return "ok"

    @property
    def count(self) -> int:
        return len(self.calls)


# ---------------------------------------------------------------------------
# 判据 1：判定去重（唯一索引 + 重复投递只补审计）
# ---------------------------------------------------------------------------


def test_judgement_idempotency_key_matches_the_spec_formula() -> None:
    """判定键 = sha256(batch_no｜prescription_no｜rule_id｜rule_version)。"""
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
        conclusion=Conclusion.VIOLATION,
    )
    assert row.idempotency_key == idempotency_key("B-1", "RX-1", "RULE-A", "v1")


def test_judgement_table_declares_a_unique_index_on_the_idempotency_key(
    tmp_path: Path,
) -> None:
    """判定表对幂等键建唯一索引——去重是数据库层的硬保证，不是应用层的自觉。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        indexes = store._conn.execute("PRAGMA index_list('judgements')").fetchall()
        unique_keys = set()
        for index in indexes:
            if int(index[2]) != 1:  # 非唯一索引跳过
                continue
            columns = [
                row[2]
                for row in store._conn.execute(
                    f"PRAGMA index_info('{index[1]}')"
                ).fetchall()
            ]
            unique_keys.add(tuple(columns))
        assert ("idempotency_key",) in unique_keys
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
        versions_after_first = store.count_judgement_versions()
        audit_after_first = store.count_audit()

        second = process_batch(store, read_batch(orders), rules)

        assert second.duplicate is True
        assert second.judgement_rows == 0
        assert store.count_judgements() == judgements_after_first
        assert store.count_judgement_versions() == versions_after_first
        assert store.count_audit() == audit_after_first + first.judgement_rows
    finally:
        store.close()


def test_repeat_delivery_insert_is_ignored_by_the_database(
    tmp_path: Path, orders: Path, rules
) -> None:
    """直接往判定表塞同一个幂等键：数据库层挡掉，不是应用层自觉。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        process_batch(store, read_batch(orders), rules)
        row = store.list_judgements()[0]
        with pytest.raises(sqlite3.IntegrityError):
            with store.transaction():
                store._conn.execute(
                    "INSERT INTO judgements (idempotency_key, batch_no, prescription_no, "
                    "rule_id, rule_version, conclusion, severity, reason, basis, "
                    "missing_fields, event_fingerprint, state, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.idempotency_key,
                        row.batch_no,
                        row.prescription_no,
                        row.rule_id,
                        row.rule_version,
                        row.conclusion.value,
                        row.severity or "",
                        row.reason,
                        row.basis,
                        "",
                        row.event_fingerprint,
                        row.state.value,
                        row.created_at,
                    ),
                )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 判据 2：回写幂等键 = 判定键 ＋ 请求体哈希
# ---------------------------------------------------------------------------


def test_request_body_hash_is_insensitive_to_key_order() -> None:
    """同一个请求体，字段书写顺序不同 → 哈希必须相同。"""
    left = {"batch_no": "B-1", "prescription_no": "RX-1", "conclusion": "violation"}
    right = {"conclusion": "violation", "prescription_no": "RX-1", "batch_no": "B-1"}
    assert canonical_request_body(left) == canonical_request_body(right)
    assert hash_request_body(left) == hash_request_body(right)


def test_request_body_hash_changes_with_content() -> None:
    """请求体内容变了哈希必须变——否则结论改了也发不出去。"""
    base = {"prescription_no": "RX-1", "conclusion": "violation"}
    changed = {"prescription_no": "RX-1", "conclusion": "compliant"}
    assert hash_request_body(base) != hash_request_body(changed)


def test_request_body_hash_handles_non_json_values() -> None:
    """非 JSON 原生类型（Decimal / 日期 / 枚举）折成稳定文本，不抛错。"""
    from decimal import Decimal

    payload = {
        "days": Decimal("3.00"),
        "at": "2026-10-28T09:00:00+00:00",
        "conclusion": Conclusion.VIOLATION,
    }
    first = hash_request_body(payload)
    second = hash_request_body(dict(payload))
    assert first == second
    assert len(first) == 64


def test_writeback_idempotency_key_composes_judgement_key_and_body_hash() -> None:
    """回写幂等键 = sha256(判定幂等键 ｜ 请求体哈希)。"""
    payload = {"prescription_no": "RX-1", "conclusion": "violation"}
    judgement_key = idempotency_key("B-1", "RX-1", "RULE-A", "v1")
    expected = hashlib.sha256(
        f"{judgement_key}{WRITE_BACK_KEY_SEP}{hash_request_body(payload)}".encode("utf-8")
    ).hexdigest()
    assert writeback_idempotency_key(judgement_key, payload) == expected
    # 稳定可复现
    assert writeback_idempotency_key(judgement_key, payload) == writeback_idempotency_key(
        judgement_key, payload
    )


def test_writeback_key_changes_when_judgement_key_or_body_changes() -> None:
    """判定键变（规则改版）→ 键变；请求体变（结论变更）→ 键变。"""
    payload = {"prescription_no": "RX-1", "conclusion": "violation"}
    other_payload = {"prescription_no": "RX-1", "conclusion": "compliant"}
    key_v1 = idempotency_key("B-1", "RX-1", "RULE-A", "v1")
    key_v2 = idempotency_key("B-1", "RX-1", "RULE-A", "v2")

    base = writeback_idempotency_key(key_v1, payload)
    assert writeback_idempotency_key(key_v2, payload) != base
    assert writeback_idempotency_key(key_v1, other_payload) != base


def test_writeback_key_rejects_empty_judgement_key() -> None:
    """判定键为空时显式报错——空身份会让重复回写抑制失效。"""
    with pytest.raises(ValueError):
        writeback_idempotency_key("", {"prescription_no": "RX-1"})


def test_canonical_request_body_rejects_non_mapping() -> None:
    """请求体不是映射时显式报错，不静默当成空请求。"""
    with pytest.raises(TypeError):
        canonical_request_body(["RX-1"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 判据 3：重复投递 → 回写请求数不变
# ---------------------------------------------------------------------------


def test_repeat_delivery_does_not_duplicate_writeback_requests(
    tmp_path: Path, orders: Path, rules
) -> None:
    """★ 核心判据：同一批次投递两次，实际发出的回写请求数不变。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    sender = CountingSender()
    try:
        guard = WritebackIdempotencyGuard(store)
        writer = GuardedWriter(sender, guard)

        first = process_batch(store, read_batch(orders), rules)
        for row in store.list_judgements():
            writer.write(row.idempotency_key, review_payload(row))
        requests_after_first = sender.count
        assert requests_after_first == first.judgement_rows > 0

        # HIS 重投同一批次：判定行数不变
        second = process_batch(store, read_batch(orders), rules)
        assert second.duplicate is True
        assert store.count_judgements() == first.judgement_rows

        # 再走一遍回写：全部被幂等键抑制，请求数不变
        outcomes = [
            writer.write(row.idempotency_key, review_payload(row))
            for row in store.list_judgements()
        ]
        assert sender.count == requests_after_first
        assert all(outcome.suppressed for outcome in outcomes)
        assert not any(outcome.sent for outcome in outcomes)
        assert guard.stats()["suppressed"] == len(outcomes)
    finally:
        store.close()


def test_guard_suppresses_duplicates_across_process_restart(
    tmp_path: Path, orders: Path, rules
) -> None:
    """守护进程重启后幂等身份仍在（落状态库），不会把同一条结论再发一遍。"""
    db_path = tmp_path / "mrfm.sqlite3"
    store = Store(db_path)
    sender = CountingSender()
    try:
        process_batch(store, read_batch(orders), rules)
        guard = WritebackIdempotencyGuard(store)
        writer = GuardedWriter(sender, guard)
        for row in store.list_judgements():
            writer.write(row.idempotency_key, review_payload(row))
        sent_before_restart = sender.count
        assert sent_before_restart > 0
    finally:
        store.close()

    # 重启：新建状态库连接与守卫（从库里恢复已发出的回写身份）
    store = Store(db_path)
    sender_after_restart = CountingSender()
    try:
        guard = WritebackIdempotencyGuard(store)
        writer = GuardedWriter(sender_after_restart, guard)
        for row in store.list_judgements():
            outcome = writer.write(row.idempotency_key, review_payload(row))
            assert outcome.suppressed is True
        assert sender_after_restart.count == 0
        assert sent_before_restart > 0
    finally:
        store.close()


def test_changed_request_body_is_allowed_to_send_again(
    tmp_path: Path, orders: Path, rules
) -> None:
    """药师改了结论 / 换了确认人（请求体变）→ 允许再发一条，不被幂等键误杀。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    sender = CountingSender()
    try:
        process_batch(store, read_batch(orders), rules)
        guard = WritebackIdempotencyGuard(store)
        writer = GuardedWriter(sender, guard)
        row = store.list_judgements()[0]

        first = writer.write(row.idempotency_key, review_payload(row))
        assert first.sent is True

        # 撤回后由另一位药师重新确认：请求体变了 → 允许再发
        changed = review_payload(row)
        changed["reviewed_by"] = "PHARM-009"
        second = writer.write(row.idempotency_key, changed)
        assert second.sent is True
        assert second.suppressed is False
        assert second.writeback_key != first.writeback_key
        assert sender.count == 2

        # 结论真的改了也一样放行
        changed_conclusion = review_payload(row)
        changed_conclusion["conclusion"] = (
            "compliant" if row.conclusion.wire_value != "compliant" else "violation"
        )
        third = writer.write(row.idempotency_key, changed_conclusion)
        assert third.sent is True
        assert sender.count == 3
    finally:
        store.close()


def test_only_one_of_concurrent_claims_is_allowed() -> None:
    """并发重复回写：同一份请求只放行一条（其余被抑制）。"""
    guard = WritebackIdempotencyGuard()
    payload = {"prescription_no": "RX-1", "conclusion": "violation"}
    judgement_key = idempotency_key("B-1", "RX-1", "RULE-A", "v1")

    decisions = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        decision = guard.claim(judgement_key, payload)
        with lock:
            decisions.append(decision)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(decisions) == 8
    assert sum(1 for decision in decisions if decision.allowed) == 1
    assert sum(1 for decision in decisions if not decision.allowed) == 7


def test_failed_send_is_recorded_and_retry_is_allowed(
    tmp_path: Path, orders: Path, rules
) -> None:
    """发送失败：记失败、异常不吞、允许按退避策略重试（同一份请求）。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    sender = CountingSender(fail_times=1)
    try:
        process_batch(store, read_batch(orders), rules)
        guard = WritebackIdempotencyGuard(store)
        writer = GuardedWriter(sender, guard)
        row = store.list_judgements()[0]
        payload = review_payload(row)

        with pytest.raises(RuntimeError):
            writer.write(row.idempotency_key, payload)

        attempts = store.list_writeback_requests()
        assert len(attempts) == 1
        assert attempts[0].status is WritebackStatus.FAILED
        assert attempts[0].retry_count == 1

        # 失败后允许重试：同一份请求再发一次（不是重复投递）
        retried = writer.write(row.idempotency_key, payload)
        assert retried.sent is True
        assert sender.count == 2
        assert store.list_writeback_requests()[0].status is WritebackStatus.SENT
    finally:
        store.close()


def test_store_writeback_request_ledger_records_status_and_never_overwrites(
    tmp_path: Path,
) -> None:
    """回写请求表：状态可追、重复占位不覆盖已发出的记录。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    try:
        claimed, existing = store.claim_writeback_request("WB-1", "JDG-1", "hash-1")
        assert claimed is True
        assert existing is not None
        assert existing.judgement_key == "JDG-1"
        assert existing.status is WritebackStatus.CLAIMED
        assert store.count_writeback_requests() == 1

        claimed_again, existing_again = store.claim_writeback_request("WB-1", "JDG-1", "hash-1")
        assert claimed_again is False
        assert existing_again is not None
        assert existing_again.judgement_key == "JDG-1"

        assert store.mark_writeback_sent("WB-1") is True
        # 已发出的记录不被重复占位改写回「已占位」
        claimed_third, existing_third = store.claim_writeback_request("WB-1", "JDG-1", "hash-1")
        assert claimed_third is False
        assert existing_third is not None
        assert existing_third.status is WritebackStatus.SENT

        assert store.count_writeback_requests() == 1
        assert store.count_writeback_requests(WritebackStatus.SENT.value) == 1
    finally:
        store.close()


def test_guard_stats_report_allowed_and_suppressed_counts() -> None:
    """运行统计能说清「放行了多少、抑制了多少」。"""
    guard = WritebackIdempotencyGuard()
    sender = CountingSender()
    writer = GuardedWriter(sender, guard)
    payload = {"prescription_no": "RX-1", "conclusion": "violation"}
    judgement_key = idempotency_key("B-1", "RX-1", "RULE-A", "v1")

    writer.write(judgement_key, payload)
    writer.write(judgement_key, payload)

    stats = guard.stats()
    assert stats["allowed"] == 1
    assert stats["suppressed"] == 1
    assert stats["sent"] == 1
    assert stats["total"] == 1


def test_renamed_redelivery_is_also_suppressed(
    tmp_path: Path, orders: Path, rules
) -> None:
    """HIS 换名重投（内容相同）：判定不重复、回写请求数也不变。"""
    store = Store(tmp_path / "mrfm.sqlite3")
    sender = CountingSender()
    try:
        process_batch(store, read_batch(orders), rules)
        guard = WritebackIdempotencyGuard(store)
        writer = GuardedWriter(sender, guard)
        for row in store.list_judgements():
            writer.write(row.idempotency_key, review_payload(row))
        requests_after_first = sender.count

        renamed = tmp_path / "orders_retry.csv"
        shutil.copyfile(orders, renamed)
        outcome = process_batch(store, read_batch(renamed), rules)
        assert outcome.duplicate is True

        for row in store.list_judgements():
            writer.write(row.idempotency_key, review_payload(row))
        assert sender.count == requests_after_first
    finally:
        store.close()
