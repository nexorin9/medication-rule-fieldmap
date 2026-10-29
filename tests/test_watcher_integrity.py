"""入站 3/4 的验收：目录监听 + 批次切分 + 完整性挂起（整文件不部分判定）。

覆盖三条硬判据：

1. 投一份正常文件 → 被切分成批次（行数与批次号都对）；
2. 投一份截断文件 → **整文件挂起，且不产生任何判定行**；
3. 投一份缺必需列文件 → 被挂起并列出缺哪几列。

外加：写完判定（大小/mtime 连续两次不变才切批次）、写入超时挂起、
人工解除后重投、挂起台账的追加与解除语义、HL7 通道的等价行为。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from med_rule_fieldmap.ingest.integrity import (
    KIND_EMPTY_FILE,
    KIND_MISSING_COLUMNS,
    KIND_TRUNCATED,
    KIND_UNDECODABLE,
    check_file_integrity,
)
from med_rule_fieldmap.ingest.suspend import (
    STATUS_RELEASED,
    STATUS_SUSPENDED,
    SUSPEND_LEDGER_COLUMNS,
    SuspendLedger,
)
from med_rule_fieldmap.ingest.watcher import (
    EVENT_BATCH,
    EVENT_SUSPENDED,
    InboxWatcher,
)

# ---------------------------------------------------------------------------
# 样例数据
# ---------------------------------------------------------------------------

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


def write_csv(path: Path, rows: int = 3, *, batch_no: str = "BATCH-001") -> Path:
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


def write_truncated_csv(path: Path) -> Path:
    """写一份被写了一半的 CSV：末行字段数不足且行尾未闭合。"""
    lines = [HEADER, ROW, ROW.replace("RX-0001", "RX-0002")]
    partial = ROW.replace("RX-0001", "RX-0003")[:60]  # 写到一半就断了
    lines.append(partial)
    path.write_text("\n".join(lines), encoding="utf-8")  # 故意不加结尾换行
    return path


def write_missing_column_csv(path: Path, missing: str = "drug_code") -> Path:
    """写一份缺必需列的 CSV。"""
    columns = [name for name in HEADER.split(",") if name != missing]
    path.write_text(",".join(columns) + "\n" + ",".join([""] * len(columns)) + "\n",
                    encoding="utf-8")
    return path


HL7_MESSAGE = (
    "MSH|^~\\&|HIS|医院|HIS|医院|20261028083000||ORM^O01|MSG-001|P|2.5\r"
    "PID|1||PT-0001^^^HIS^MR||患者甲||19740101|M\r"
    "PV1|1|I|病房^01^01||||VISIT-0001|PR-001^医师甲\r"
    "ORC|NW|ORD-0001||20261028083000|PR-001^医师甲\r"
    "RXO|1|1|阿司匹林肠溶片|1|片|口服|每日一次\r"
    "RXE|1|1|阿司匹林肠溶片^0.1g^片||1|片|口服|每日一次\r"
)


def write_hl7(path: Path, *, truncate: bool = False) -> Path:
    """写一份 HL7 医嘱消息；``truncate=True`` 时把末段砍掉一半。"""
    text = HL7_MESSAGE
    if truncate:
        text = text[: text.rindex("\r") + 1] + "RXE|1|1|阿司匹林肠溶片^0.1g^片||1|片|口服"
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    """返回 ``(inbox_dir, out_dir)``。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    return inbox, out


def make_watcher(workspace, **kwargs) -> InboxWatcher:
    """构造一个监听器（默认关掉 watchdog，走轮询路径，保证行为确定）。"""
    inbox, out = workspace
    kwargs.setdefault("use_watchdog", False)
    return InboxWatcher(inbox, out_dir=out, **kwargs)


# ---------------------------------------------------------------------------
# 1. 正常文件 → 切分批次
# ---------------------------------------------------------------------------


def test_normal_file_is_cut_into_batch(workspace):
    """投一份正常文件 → 切分成批次，行数与批次号都对。"""
    inbox, _ = workspace
    write_csv(inbox / "batch_001.csv", rows=3)

    watcher = make_watcher(workspace)
    assert watcher.poll_once() == []  # 第一次只观察，不处理

    events = watcher.poll_once()
    assert len(events) == 1
    event = events[0]
    assert event.kind == EVENT_BATCH
    assert event.batch is not None
    assert event.batch.prescription_count == 3
    assert event.batch.batch_no == "BATCH-001"
    assert event.batch.source_kind == "csv"
    assert [row.prescription_no for row in event.batch.rows] == [
        "RX-0001",
        "RX-0002",
        "RX-0003",
    ]


def test_batch_is_not_reprocessed(workspace):
    """同一文件不会被重复切批次。"""
    inbox, _ = workspace
    write_csv(inbox / "batch_001.csv", rows=2)

    watcher = make_watcher(workspace)
    watcher.poll_once()
    assert len(watcher.poll_once()) == 1
    assert watcher.poll_once() == []


def test_file_still_growing_is_not_cut(workspace):
    """大小/mtime 变了 → 不算写完，不切批次。"""
    inbox, _ = workspace
    target = write_csv(inbox / "batch_001.csv", rows=2)

    watcher = make_watcher(workspace)
    watcher.poll_once()  # 第一次观察

    target.write_text(  # 文件还在写
        target.read_text(encoding="utf-8") + ROW.replace("RX-0001", "RX-0009") + "\n",
        encoding="utf-8",
    )
    assert watcher.poll_once() == []  # 变了 → 重新计数，不切批次

    assert len(watcher.poll_once()) == 1  # 稳定后才切


# ---------------------------------------------------------------------------
# 2. 截断文件 → 整文件挂起，零判定行
# ---------------------------------------------------------------------------


def test_truncated_file_is_suspended_without_any_judgement(workspace):
    """投一份截断文件 → 整文件挂起，且不产生任何判定行。"""
    inbox, out = workspace
    write_truncated_csv(inbox / "batch_001.csv")

    watcher = make_watcher(workspace)
    watcher.poll_once()
    events = watcher.poll_once()

    assert len(events) == 1
    event = events[0]
    assert event.kind == EVENT_SUSPENDED
    assert event.batch is None  # 没有批次 → 判定层拿不到任何行
    assert event.report is not None
    assert event.report.kind == KIND_TRUNCATED
    assert "整文件挂起" in event.report.reason

    # 挂起台账有记录，文件仍在 inbox 原地（入站只读）
    ledger = SuspendLedger(out / "suspended_ledger.csv")
    assert ledger.is_suspended("batch_001.csv")
    assert (inbox / "batch_001.csv").exists()


def test_truncated_file_produces_no_rows_across_polls(workspace):
    """挂起的文件不产出任何处方行（多轮轮询也不产出）。"""
    inbox, _ = workspace
    write_truncated_csv(inbox / "batch_001.csv")

    watcher = make_watcher(workspace)
    for _ in range(4):
        for event in watcher.poll_once():
            assert event.kind != EVENT_BATCH
            assert event.batch is None


def test_undecodable_file_is_suspended(workspace):
    """乱码文件 → 整文件挂起。"""
    inbox, _ = workspace
    (inbox / "batch_001.csv").write_bytes(b"\xff\xfe\xff\xfe" * 8)

    watcher = make_watcher(workspace)
    watcher.poll_once()
    events = watcher.poll_once()

    assert len(events) == 1
    assert events[0].kind == EVENT_SUSPENDED
    assert events[0].report.kind == KIND_UNDECODABLE


def test_empty_file_is_suspended(workspace):
    """空文件 → 整文件挂起。"""
    inbox, _ = workspace
    (inbox / "batch_001.csv").write_text("   \n", encoding="utf-8")

    watcher = make_watcher(workspace)
    watcher.poll_once()
    events = watcher.poll_once()

    assert len(events) == 1
    assert events[0].kind == EVENT_SUSPENDED
    assert events[0].report.kind == KIND_EMPTY_FILE


# ---------------------------------------------------------------------------
# 3. 缺必需列 → 挂起并列出缺列
# ---------------------------------------------------------------------------


def test_missing_required_column_is_suspended_with_column_list(workspace):
    """投一份缺必需列文件 → 挂起并列出缺哪几列。"""
    inbox, out = workspace
    write_missing_column_csv(inbox / "batch_001.csv", missing="drug_code")

    watcher = make_watcher(workspace)
    watcher.poll_once()
    events = watcher.poll_once()

    assert len(events) == 1
    event = events[0]
    assert event.kind == EVENT_SUSPENDED
    assert event.report.kind == KIND_MISSING_COLUMNS
    assert event.report.missing_columns == ("drug_code",)
    assert "drug_code" in event.report.reason

    ledger = SuspendLedger(out / "suspended_ledger.csv")
    assert ledger.is_suspended("batch_001.csv")
    current = ledger.latest("batch_001.csv")
    assert current.missing_columns == ("drug_code",)
    assert current.status == STATUS_SUSPENDED
    assert current.suspended_at


def test_missing_columns_are_listed_in_ledger_csv(workspace):
    """挂起台账 CSV 的缺列清单列可核对。"""
    inbox, out = workspace
    write_missing_column_csv(inbox / "batch_001.csv", missing="visit_no")

    watcher = make_watcher(workspace)
    watcher.poll_once()
    watcher.poll_once()

    ledger = SuspendLedger(out / "suspended_ledger.csv")
    records = ledger.records()
    assert len(records) == 1
    assert records[0].missing_columns == ("visit_no",)

    text = (out / "suspended_ledger.csv").read_text(encoding="utf-8")
    header = text.splitlines()[0].split(",")
    assert header == list(SUSPEND_LEDGER_COLUMNS)
    assert "visit_no" in text


def test_optional_missing_columns_do_not_suspend(workspace):
    """★ 常缺字段缺列**不**整文件挂起——那是「判不了」的来源，不是文件坏了。"""
    inbox, _ = workspace
    # 去掉 rx_type / drug_category / days_qty / skin_test_result 四列
    optional = {"rx_type", "drug_category", "days_qty", "skin_test_result"}
    columns = [name for name in HEADER.split(",") if name not in optional]
    (inbox / "batch_001.csv").write_text(
        ",".join(columns) + "\n" + ",".join([""] * len(columns)) + "\n",
        encoding="utf-8",
    )

    watcher = make_watcher(workspace)
    watcher.poll_once()
    events = watcher.poll_once()

    assert len(events) == 1
    assert events[0].kind == EVENT_BATCH


# ---------------------------------------------------------------------------
# 人工解除后重投
# ---------------------------------------------------------------------------


def test_suspended_file_can_be_released_and_reprocessed(workspace):
    """人工解除挂起后，文件重新投递即可被重新处理。"""
    inbox, out = workspace
    target = write_truncated_csv(inbox / "batch_001.csv")

    watcher = make_watcher(workspace)
    watcher.poll_once()
    watcher.poll_once()
    assert watcher.ledger.is_suspended("batch_001.csv")

    # 运维修好文件后重投
    write_csv(target, rows=2)
    assert watcher.release("batch_001.csv") is True

    # 重投改变了文件内容 → 重新计两次稳定观察再切批次
    assert watcher.poll_once() == []
    events = watcher.poll_once()
    assert len(events) == 1
    assert events[0].kind == EVENT_BATCH
    assert events[0].batch.prescription_count == 2
    assert not watcher.ledger.is_suspended("batch_001.csv")


def test_release_of_unknown_file_returns_false(workspace):
    """解除一个没挂着的文件 → 明确返回 False，不静默成功。"""
    watcher = make_watcher(workspace)
    assert watcher.release("nope.csv") is False


def test_release_is_append_only(workspace):
    """解除动作是**追加**一条记录，历史挂起行不被改写。"""
    inbox, out = workspace
    write_truncated_csv(inbox / "batch_001.csv")

    watcher = make_watcher(workspace)
    watcher.poll_once()
    watcher.poll_once()
    watcher.release("batch_001.csv")

    ledger = SuspendLedger(out / "suspended_ledger.csv")
    records = ledger.records()
    assert len(records) == 2
    assert records[0].status == STATUS_SUSPENDED
    assert records[1].status == STATUS_RELEASED
    assert records[1].released_at
    assert records[0].suspended_at == records[1].suspended_at


# ---------------------------------------------------------------------------
# 写入超时 → 疑似截断挂起
# ---------------------------------------------------------------------------


def test_write_deadline_suspends_continuously_growing_file(workspace):
    """文件一直写不完 → 超过截止时间后按疑似截断挂起。"""
    inbox, _ = workspace
    target = write_csv(inbox / "batch_001.csv", rows=1)

    ticks = iter([0.0, 10.0, 20.0, 30.0, 40.0])
    watcher = make_watcher(workspace, write_deadline_sec=15.0, clock=lambda: next(ticks))

    watcher.poll_once()  # t=0 首次观察
    target.write_text(
        target.read_text(encoding="utf-8") + ROW.replace("RX-0001", "RX-0002") + "\n",
        encoding="utf-8",
    )
    assert watcher.poll_once() == []  # t=10 仍在写，未到截止时间

    target.write_text(
        target.read_text(encoding="utf-8") + ROW.replace("RX-0001", "RX-0003") + "\n",
        encoding="utf-8",
    )
    events = watcher.poll_once()  # t=20 已超截止时间
    assert len(events) == 1
    assert events[0].kind == EVENT_SUSPENDED
    assert events[0].report.kind == KIND_TRUNCATED
    assert "写入未完成" in events[0].report.reason


# ---------------------------------------------------------------------------
# HL7 通道等价行为
# ---------------------------------------------------------------------------


def test_hl7_file_is_cut_into_batch(workspace):
    """HL7 医嘱文件同样切分批次。"""
    inbox, _ = workspace
    write_hl7(inbox / "batch_001.hl7")

    watcher = make_watcher(workspace)
    watcher.poll_once()
    events = watcher.poll_once()

    assert len(events) == 1
    assert events[0].kind == EVENT_BATCH
    assert events[0].batch.source_kind == "hl7"
    assert events[0].batch.prescription_count == 1
    assert events[0].batch.batch_no == "MSG-001"


def test_truncated_hl7_file_is_suspended(workspace):
    """HL7 末段被写了一半 → 整文件挂起。"""
    inbox, _ = workspace
    write_hl7(inbox / "batch_001.hl7", truncate=True)

    watcher = make_watcher(workspace)
    watcher.poll_once()
    events = watcher.poll_once()

    assert len(events) == 1
    assert events[0].kind == EVENT_SUSPENDED
    assert events[0].report.kind == KIND_TRUNCATED


# ---------------------------------------------------------------------------
# 完整性校验的直接判据
# ---------------------------------------------------------------------------


def test_integrity_check_reports_missing_columns_directly(tmp_path):
    """完整性校验单独调用时给出缺列清单。"""
    target = write_missing_column_csv(tmp_path / "batch_001.csv", missing="event_time")
    report = check_file_integrity(target)
    assert not report.ok
    assert report.kind == KIND_MISSING_COLUMNS
    assert report.missing_columns == ("event_time",)
    assert "event_time" in report.summary()


def test_integrity_check_passes_for_good_file(tmp_path):
    """完整文件校验通过。"""
    target = write_csv(tmp_path / "batch_001.csv", rows=1)
    report = check_file_integrity(target)
    assert report.ok
    assert report.kind == "通过"


def test_temporary_files_are_ignored(workspace):
    """导出过程中的临时文件不参与监听。"""
    inbox, _ = workspace
    write_csv(inbox / "batch_001.csv.tmp", rows=1)
    write_csv(inbox / "batch_001.csv.part", rows=1)

    watcher = make_watcher(workspace)
    assert watcher.list_candidate_files() == []
    watcher.poll_once()
    assert watcher.poll_once() == []
