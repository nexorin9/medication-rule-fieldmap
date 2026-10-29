"""判定内核 10/10 的验收：判定证据链回放（每条结论可追到规则版本与依据）。

对应任务书 T22。回放件是医务质控抽查与飞检自证要的东西——「这条结论凭什么
这么判」的完整链路（批次 → 处方 → 规则号 → 规则版本 → 依据出处 → 归因文本 →
事件文件指纹 → 当前状态 → 写入时间），而不是让人去翻日志。

覆盖三条硬判据：

1. **回放行数与该批次判定行数一致** —— 一条不多、一条不少；
2. **每行 basis 非空** —— 依据出处缺失就是链断了；
3. **按规则号过滤结果正确** —— 过滤后只剩该规则的判定行。

外加：结论类型过滤、回放件列固定且可读回、状态机推进后回放显示**新状态**、
事件文件指纹进回放件、证据链自检点名断链、同输入两次导出逐字相同、
非法过滤值显式报错、CLI ``replay`` 端到端导出回放件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from med_rule_fieldmap.cli import main
from med_rule_fieldmap.engine.statemachine import transition_row
from med_rule_fieldmap.ledger.evidence_replay import (
    REPLAY_COLUMNS,
    EvidenceIssue,
    ReplayError,
    build_replay_entries,
    chain_health,
    check_evidence_chain,
    load_replay_csv,
    replay_filename,
    replay_from_store,
    summarize_replay,
    write_replay_csv,
)
from med_rule_fieldmap.models import Conclusion, JudgementRow, JudgementState
from med_rule_fieldmap.rules.repository import RuleRepository
from med_rule_fieldmap.store import Store, process_batch
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.ingest.watcher import BatchFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"

BATCH_NO = "BATCH-REPLAY-001"
RULE_ID = "RX-LEGAL-MA-OUT-DAYS"
OTHER_RULE_ID = "RX-NORM-FRONT-DIAG"
RULE_VERSION = "2026.10"
BASIS = "处方审核规则体系 §3 特殊药品处方量限"
FINGERPRINT = "a" * 16


def make_row(
    *,
    prescription_no: str = "RX-0001",
    batch_no: str = BATCH_NO,
    rule_id: str = RULE_ID,
    conclusion: Conclusion = Conclusion.VIOLATION,
    state: JudgementState = JudgementState.DECIDED,
    basis: str = BASIS,
    rule_version: str = RULE_VERSION,
    event_fingerprint: str = FINGERPRINT,
    reason: str = "规则 RX-LEGAL-MA-OUT-DAYS（版本 2026.10）判定：违规。",
) -> JudgementRow:
    """构造一条判定行（字段口径与 spec.md §2.3 一致）。"""
    return JudgementRow(
        batch_no=batch_no,
        prescription_no=prescription_no,
        rule_id=rule_id,
        rule_version=rule_version,
        conclusion=conclusion,
        severity="严重不合理用药",
        reason=reason,
        basis=basis,
        missing_fields=(),
        event_fingerprint=event_fingerprint,
        state=state,
    )


def build_store(rows: list[JudgementRow], tmp_path: Path) -> Store:
    """把判定行写进一个临时状态库。"""
    store = Store(tmp_path / "state.sqlite3")
    with store.transaction():
        store.insert_judgements(rows)
    return store


# ---------------------------------------------------------------------------
# 判据 1：回放行数与该批次判定行数一致
# ---------------------------------------------------------------------------


def test_replay_row_count_matches_the_batch_judgement_rows(tmp_path: Path) -> None:
    rows = [
        make_row(prescription_no="RX-0001"),
        make_row(prescription_no="RX-0002", conclusion=Conclusion.COMPLIANT),
        make_row(prescription_no="RX-0003", rule_id=OTHER_RULE_ID),
    ]
    store = build_store(rows, tmp_path)
    try:
        entries = replay_from_store(store, BATCH_NO)
        assert len(entries) == store.count_judgements(BATCH_NO) == len(rows)
    finally:
        store.close()


def test_replay_only_covers_the_requested_batch(tmp_path: Path) -> None:
    rows = [
        make_row(prescription_no="RX-0001"),
        make_row(prescription_no="RX-9001", batch_no="BATCH-OTHER"),
    ]
    store = build_store(rows, tmp_path)
    try:
        entries = replay_from_store(store, BATCH_NO)
        assert len(entries) == 1
        assert {entry.batch_no for entry in entries} == {BATCH_NO}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 判据 2：每行 basis 非空
# ---------------------------------------------------------------------------


def test_every_replay_row_carries_its_basis() -> None:
    entries = build_replay_entries([make_row(), make_row(prescription_no="RX-0002")])
    assert entries
    for entry in entries:
        assert entry.basis.strip(), f"{entry.rule_id} 的回放行缺依据出处"


def test_replay_file_rows_all_have_non_empty_basis(tmp_path: Path) -> None:
    entries = build_replay_entries([make_row(), make_row(prescription_no="RX-0002")])
    target = write_replay_csv(entries, tmp_path / "out", batch_no=BATCH_NO)
    records = load_replay_csv(target)
    assert len(records) == len(entries)
    assert all(record["basis"].strip() for record in records)


# ---------------------------------------------------------------------------
# 判据 3：按规则号过滤结果正确
# ---------------------------------------------------------------------------


def test_filter_by_rule_id_keeps_only_that_rule() -> None:
    rows = [
        make_row(prescription_no="RX-0001", rule_id=RULE_ID),
        make_row(prescription_no="RX-0002", rule_id=OTHER_RULE_ID),
        make_row(prescription_no="RX-0003", rule_id=RULE_ID),
    ]
    entries = build_replay_entries(rows, rule_ids=[RULE_ID])
    assert len(entries) == 2
    assert {entry.rule_id for entry in entries} == {RULE_ID}
    assert {entry.prescription_no for entry in entries} == {"RX-0001", "RX-0003"}


def test_filter_by_rule_id_on_store_matches_the_filtered_build(tmp_path: Path) -> None:
    rows = [
        make_row(prescription_no="RX-0001", rule_id=RULE_ID),
        make_row(prescription_no="RX-0002", rule_id=OTHER_RULE_ID),
    ]
    store = build_store(rows, tmp_path)
    try:
        entries = replay_from_store(store, BATCH_NO, rule_ids=[OTHER_RULE_ID])
    finally:
        store.close()
    assert [entry.rule_id for entry in entries] == [OTHER_RULE_ID]


def test_filter_by_conclusion_keeps_only_that_conclusion() -> None:
    rows = [
        make_row(prescription_no="RX-0001", conclusion=Conclusion.VIOLATION),
        make_row(prescription_no="RX-0002", conclusion=Conclusion.COMPLIANT),
        make_row(prescription_no="RX-0003", conclusion=Conclusion.UNDETERMINABLE),
    ]
    entries = build_replay_entries(rows, conclusions=["违规"])
    assert [entry.conclusion for entry in entries] == ["违规"]


def test_conclusion_filter_accepts_the_english_wire_value() -> None:
    rows = [
        make_row(prescription_no="RX-0001", conclusion=Conclusion.VIOLATION),
        make_row(prescription_no="RX-0002", conclusion=Conclusion.COMPLIANT),
    ]
    entries = build_replay_entries(rows, conclusions=["violation"])
    assert [entry.prescription_no for entry in entries] == ["RX-0001"]


def test_unknown_conclusion_filter_is_rejected_loudly() -> None:
    with pytest.raises(ValueError):
        build_replay_entries([make_row()], conclusions=["大概不合规"])


def test_empty_filter_lists_mean_no_filtering() -> None:
    rows = [
        make_row(prescription_no="RX-0001", rule_id=RULE_ID),
        make_row(prescription_no="RX-0002", rule_id=OTHER_RULE_ID),
    ]
    assert len(build_replay_entries(rows, rule_ids=[])) == 2
    assert len(build_replay_entries(rows, conclusions=[])) == 2


# ---------------------------------------------------------------------------
# 回放件本体：列固定 / 可读回 / 排序稳定
# ---------------------------------------------------------------------------


def test_replay_file_name_follows_the_batch_number() -> None:
    assert replay_filename("DEMO-BATCH-001") == "replay_DEMO-BATCH-001.csv"
    # 批次号里的路径分隔符不进文件名
    assert "/" not in replay_filename("../../etc/passwd")


def test_replay_csv_has_the_fixed_column_set(tmp_path: Path) -> None:
    entries = build_replay_entries([make_row()])
    target = write_replay_csv(entries, tmp_path / "out", batch_no=BATCH_NO)
    assert target.name == f"replay_{BATCH_NO}.csv"
    records = load_replay_csv(target)
    assert list(records[0].keys()) == list(REPLAY_COLUMNS)


def test_replay_csv_carries_the_whole_evidence_chain(tmp_path: Path) -> None:
    entries = build_replay_entries([make_row()])
    target = write_replay_csv(entries, tmp_path / "out", batch_no=BATCH_NO)
    record = load_replay_csv(target)[0]
    assert record["batch_no"] == BATCH_NO
    assert record["prescription_no"] == "RX-0001"
    assert record["rule_id"] == RULE_ID
    assert record["rule_version"] == RULE_VERSION
    assert record["conclusion"] == "违规"
    assert record["basis"] == BASIS
    assert record["event_fingerprint"] == FINGERPRINT
    assert record["state"] == "已判定"
    assert record["written_at"]


def test_replay_output_is_byte_identical_across_runs(tmp_path: Path) -> None:
    rows = [
        make_row(prescription_no="RX-0001"),
        make_row(prescription_no="RX-0002", rule_id=OTHER_RULE_ID),
    ]
    first = write_replay_csv(
        build_replay_entries(rows), tmp_path / "out", batch_no=BATCH_NO
    )
    second = write_replay_csv(
        build_replay_entries(list(reversed(rows))), tmp_path / "out", batch_no=BATCH_NO
    )
    assert first.read_bytes() == second.read_bytes()


def test_replay_summary_counts_the_three_states() -> None:
    rows = [
        make_row(prescription_no="RX-0001", conclusion=Conclusion.VIOLATION),
        make_row(prescription_no="RX-0002", conclusion=Conclusion.COMPLIANT),
        make_row(prescription_no="RX-0003", conclusion=Conclusion.UNDETERMINABLE),
    ]
    counts = summarize_replay(build_replay_entries(rows))
    assert counts == {"合规": 1, "违规": 1, "不可判定": 1}


# ---------------------------------------------------------------------------
# 状态与指纹：回放取当前状态，不取历史版本
# ---------------------------------------------------------------------------


def test_replay_reports_the_state_after_the_pharmacist_confirmed_it() -> None:
    row = make_row(state=JudgementState.DECIDED)
    confirmed = transition_row(row, JudgementState.CONFIRMED)
    entries = build_replay_entries([confirmed])
    assert entries[0].state == "药师已确认"


def test_replay_reports_the_event_fingerprint_of_the_source_file() -> None:
    entries = build_replay_entries([make_row(event_fingerprint="deadbeefdeadbeef")])
    assert entries[0].event_fingerprint == "deadbeefdeadbeef"


# ---------------------------------------------------------------------------
# 证据链自检：断链点名
# ---------------------------------------------------------------------------


def test_chain_check_names_the_broken_links() -> None:
    rows = [
        make_row(prescription_no="RX-0001", basis=""),
        make_row(prescription_no="RX-0002", event_fingerprint=""),
        make_row(prescription_no="RX-0003", rule_version=""),
        make_row(prescription_no="RX-0004", state=JudgementState.PENDING),
    ]
    issues = check_evidence_chain(build_replay_entries(rows))
    kinds = {issue.kind for issue in issues}
    assert kinds == {"缺依据出处", "缺事件文件指纹", "缺规则版本", "未走完流程"}
    assert all(isinstance(issue, EvidenceIssue) for issue in issues)
    assert "RX-0001" in str(issues[0])


def test_a_complete_chain_reports_no_issue() -> None:
    entries = build_replay_entries([make_row(state=JudgementState.CONFIRMED)])
    assert check_evidence_chain(entries) == []
    health = chain_health(entries)
    assert health == {"总条数": 1, "断链条数": 0, "链完整条数": 1}


def test_chain_health_counts_broken_rows_not_broken_links() -> None:
    """一条结论同时缺两环，仍只算一条断链。"""
    row = make_row(basis="", event_fingerprint="")
    health = chain_health(build_replay_entries([row]))
    assert health == {"总条数": 1, "断链条数": 1, "链完整条数": 0}


def test_replay_rejects_a_source_without_the_list_interface() -> None:
    with pytest.raises(ReplayError):
        replay_from_store(object(), BATCH_NO)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 端到端：CLI judge --state-db → CLI replay → 回放件
# ---------------------------------------------------------------------------


def test_cli_replay_exports_the_replay_file_for_a_judged_batch(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    state_db = tmp_path / "state" / "mrfm.sqlite3"
    assert (
        main(
            [
                "judge",
                "--inbox",
                str(BATCH_DIR),
                "--out",
                str(out_dir),
                "--rules",
                str(SAMPLE_RULES),
                "--state-db",
                str(state_db),
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "replay",
                "--db",
                str(state_db),
                "--batch",
                "DEMO-BATCH-001",
                "--out",
                str(out_dir),
            ]
        )
        == 0
    )

    replay_path = out_dir / "replay_DEMO-BATCH-001.csv"
    assert replay_path.exists()
    records = load_replay_csv(replay_path)
    store = Store(state_db)
    try:
        assert len(records) == store.count_judgements("DEMO-BATCH-001")
    finally:
        store.close()
    assert all(record["basis"].strip() for record in records)


def test_cli_replay_filters_by_rule_and_conclusion(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    state_db = tmp_path / "state" / "mrfm.sqlite3"
    main(
        [
            "judge",
            "--inbox",
            str(BATCH_DIR),
            "--out",
            str(out_dir),
            "--rules",
            str(SAMPLE_RULES),
            "--state-db",
            str(state_db),
        ]
    )
    assert (
        main(
            [
                "replay",
                "--db",
                str(state_db),
                "--batch",
                "DEMO-BATCH-001",
                "--out",
                str(out_dir),
                "--rule",
                "RX-LEGAL-MA-OUT-DAYS",
                "--conclusion",
                "不可判定",
                "--json",
            ]
        )
        == 0
    )
    records = load_replay_csv(out_dir / "replay_DEMO-BATCH-001.csv")
    assert records
    assert {record["rule_id"] for record in records} == {"RX-LEGAL-MA-OUT-DAYS"}
    assert {record["conclusion"] for record in records} == {"不可判定"}


def test_replay_fills_severity_source_and_grade_when_rules_are_supplied() -> None:
    """给了规则集就补出「严重度来源」与「点评分级」两列。"""
    entries = build_replay_entries(
        [make_row(rule_id="RX-NORM-FRONT-DIAG")],
        severity_sources={"RX-NORM-FRONT-DIAG": "层次回落"},
        grades={"RX-NORM-FRONT-DIAG": "不规范处方"},
    )
    assert entries[0].severity_source == "层次回落"
    assert entries[0].grade == "不规范处方"


def test_replay_leaves_the_enrichment_columns_empty_without_rules() -> None:
    entries = build_replay_entries([make_row()])
    assert entries[0].severity_source == ""
    assert entries[0].grade == ""


def test_cli_replay_with_rules_fills_the_enrichment_columns(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    state_db = tmp_path / "state" / "mrfm.sqlite3"
    main(
        [
            "judge",
            "--inbox",
            str(BATCH_DIR),
            "--out",
            str(out_dir),
            "--rules",
            str(SAMPLE_RULES),
            "--state-db",
            str(state_db),
        ]
    )
    assert (
        main(
            [
                "replay",
                "--db",
                str(state_db),
                "--batch",
                "DEMO-BATCH-001",
                "--out",
                str(out_dir),
                "--rules",
                str(SAMPLE_RULES),
            ]
        )
        == 0
    )
    records = load_replay_csv(out_dir / "replay_DEMO-BATCH-001.csv")
    assert records
    assert all(record["severity_source"] in {"规则行", "层次回落"} for record in records)
    assert all(
        record["grade"] in {"不适宜处方", "不规范处方", "超常处方"} for record in records
    )
    # 严重度来源与归因文本里的回落说明互相对得上
    for record in records:
        if record["severity_source"] == "层次回落":
            assert "回落" in record["reason"]
        else:
            assert "回落" not in record["reason"]


def test_cli_replay_reports_a_missing_state_db(tmp_path: Path, capsys) -> None:
    assert main(["replay", "--db", str(tmp_path / "nope.sqlite3"), "--batch", "B"]) == 2
    assert "不存在" in capsys.readouterr().err


def test_cli_replay_reports_an_unknown_batch(tmp_path: Path, capsys) -> None:
    state_db = tmp_path / "state.sqlite3"
    store = Store(state_db)
    store.close()
    assert (
        main(["replay", "--db", str(state_db), "--batch", "NO-SUCH-BATCH"]) == 2
    )
    assert "没有匹配的判定行" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 库层：store → 回放的直连路径（不经 CLI）
# ---------------------------------------------------------------------------


def test_replay_from_store_reads_a_real_processed_batch(tmp_path: Path) -> None:
    """真跑一遍批次处理，再从状态库回放——链路与 CLI 走的是同一条。"""
    repository = RuleRepository(SAMPLE_RULES)
    repository.load()
    rules = repository.effective_rules
    rows = read_prescription_rows(BATCH_DIR / "demo_batch_001.csv")
    batch = BatchFile(
        path=BATCH_DIR / "demo_batch_001.csv",
        batch_no="DEMO-BATCH-001",
        rows=tuple(rows),
        source_kind="csv",
    )
    store = Store(tmp_path / "state.sqlite3")
    try:
        outcome = process_batch(store, batch, rules)
        entries = replay_from_store(store, "DEMO-BATCH-001")
        assert len(entries) == outcome.total_judgements == store.count_judgements(
            "DEMO-BATCH-001"
        )
        assert all(entry.basis.strip() for entry in entries)
        assert all(entry.event_fingerprint == outcome.fingerprint for entry in entries)
        assert all(entry.state == "已判定" for entry in entries)
    finally:
        store.close()
