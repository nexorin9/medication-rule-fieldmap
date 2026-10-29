"""判定内核 8/10：判定状态机（待判定 → 已判定 → 药师已确认 → 已回写）。

对应任务书 T20。本轮把三件事定死，后面「确认回写」几条（T26–T30：幂等、
出站客户端、HIS 回写、退避队列、药师确认）全部建在这套状态机之上：

1. **合法跃迁表是唯一真源**（:data:`LEGAL_TRANSITIONS`）——合法跃迁被接受，
   判定行的状态被推进，且**原行不被就地修改**；
2. **非法跃迁一律拒绝**——「已判定」直接跳「已回写」（跳过药师确认）、
   「待补字段」直接跳「药师已确认」（跳过重判）都抛
   :class:`IllegalTransitionError` **并记入告警日志**，行本身不被改动；
3. **重判留痕**——重判生成新一版判定行，旧结论保留为历史版本（版本号递增，
   历史行不被覆盖、不被删除）。

「未确认绝不回写」这条合规底线的执行者就是本模块：``已回写`` 只能从
``药师已确认`` 到达，别的路径一律被拒。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.statemachine import (
    LEGAL_TRANSITIONS,
    PIPELINE_END,
    IllegalTransitionError,
    JudgementStateMachine,
    StateMachineError,
    TransitionLog,
    can_transition,
    legal_targets,
    transition_row,
)
from med_rule_fieldmap.models import Conclusion, JudgementRow, JudgementState
from med_rule_fieldmap.store import WRITEBACK_PENDING, Store

BATCH_NO = "BATCH-001"
RULE_ID = "RX-LEGAL-MA-OUT-DAYS"
RULE_VERSION = "2026.10"
BASIS = "处方审核规则体系 §3 特殊药品处方量限"


def make_row(
    *,
    prescription_no: str = "RX-0001",
    conclusion: Conclusion = Conclusion.VIOLATION,
    state: JudgementState = JudgementState.PENDING,
    reason: str = "规则 RX-LEGAL-MA-OUT-DAYS（版本 2026.10）判定：违规。",
    missing_fields: tuple[str, ...] = (),
) -> JudgementRow:
    """构造一条判定行（字段口径与 spec.md §2.3 一致）。"""
    return JudgementRow(
        batch_no=BATCH_NO,
        prescription_no=prescription_no,
        rule_id=RULE_ID,
        rule_version=RULE_VERSION,
        conclusion=conclusion,
        severity="严重不合理用药",
        reason=reason,
        basis=BASIS,
        missing_fields=missing_fields,
        event_fingerprint="f" * 16,
        state=state,
    )


# ---------------------------------------------------------------------------
# 合法跃迁表
# ---------------------------------------------------------------------------


def test_legal_transition_table_matches_the_documented_lifecycle() -> None:
    assert LEGAL_TRANSITIONS[JudgementState.PENDING] == frozenset(
        {
            JudgementState.DECIDED,
            JudgementState.PENDING_FIELDS,
            JudgementState.SUSPENDED,
        }
    )
    assert LEGAL_TRANSITIONS[JudgementState.DECIDED] == frozenset(
        {
            JudgementState.CONFIRMED,
            JudgementState.PENDING_FIELDS,
            JudgementState.WITHDRAWN,
            JudgementState.SUSPENDED,
        }
    )
    assert LEGAL_TRANSITIONS[JudgementState.PENDING_FIELDS] == frozenset(
        {JudgementState.DECIDED, JudgementState.SUSPENDED}
    )
    assert LEGAL_TRANSITIONS[JudgementState.CONFIRMED] == frozenset(
        {
            JudgementState.WRITTEN_BACK,
            JudgementState.WITHDRAWN,
            JudgementState.SUSPENDED,
        }
    )
    assert LEGAL_TRANSITIONS[JudgementState.WRITTEN_BACK] == frozenset(
        {JudgementState.WITHDRAWN}
    )
    assert LEGAL_TRANSITIONS[JudgementState.WITHDRAWN] == frozenset(
        {JudgementState.PENDING}
    )
    assert LEGAL_TRANSITIONS[JudgementState.SUSPENDED] == frozenset(
        {JudgementState.PENDING}
    )


def test_every_judgement_state_has_a_transition_entry() -> None:
    assert set(LEGAL_TRANSITIONS) == set(JudgementState)
    assert legal_targets(JudgementState.PENDING) == LEGAL_TRANSITIONS[
        JudgementState.PENDING
    ]


# ---------------------------------------------------------------------------
# 合法跃迁
# ---------------------------------------------------------------------------


def test_legal_transition_advances_the_row_without_mutating_the_original() -> None:
    row = make_row(state=JudgementState.PENDING)
    moved = transition_row(row, JudgementState.DECIDED)
    assert moved.state is JudgementState.DECIDED
    assert row.state is JudgementState.PENDING, "原行不得被就地修改"


def test_full_path_to_writeback_goes_through_pharmacist_confirmation() -> None:
    row = make_row(state=JudgementState.PENDING)
    decided = transition_row(row, JudgementState.DECIDED)
    confirmed = transition_row(decided, JudgementState.CONFIRMED)
    written = transition_row(confirmed, JudgementState.WRITTEN_BACK)
    assert written.state is JudgementState.WRITTEN_BACK
    assert PIPELINE_END is JudgementState.WRITTEN_BACK


def test_pending_fields_path_comes_back_to_decided_after_fields_are_filled() -> None:
    row = make_row(state=JudgementState.DECIDED)
    waiting = transition_row(row, JudgementState.PENDING_FIELDS)
    assert waiting.state is JudgementState.PENDING_FIELDS
    redone = transition_row(waiting, JudgementState.DECIDED)
    assert redone.state is JudgementState.DECIDED


def test_withdraw_and_resume_paths() -> None:
    confirmed = make_row(state=JudgementState.CONFIRMED)
    withdrawn = transition_row(confirmed, JudgementState.WITHDRAWN)
    assert withdrawn.state is JudgementState.WITHDRAWN
    resumed = transition_row(withdrawn, JudgementState.PENDING)
    assert resumed.state is JudgementState.PENDING

    pending = make_row(state=JudgementState.PENDING)
    suspended = transition_row(pending, JudgementState.SUSPENDED)
    assert suspended.state is JudgementState.SUSPENDED
    assert transition_row(suspended, JudgementState.PENDING).state is (
        JudgementState.PENDING
    )


# ---------------------------------------------------------------------------
# 非法跃迁
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (JudgementState.DECIDED, JudgementState.WRITTEN_BACK),
        (JudgementState.PENDING_FIELDS, JudgementState.CONFIRMED),
        (JudgementState.PENDING, JudgementState.CONFIRMED),
        (JudgementState.PENDING, JudgementState.WRITTEN_BACK),
        (JudgementState.CONFIRMED, JudgementState.DECIDED),
        (JudgementState.WRITTEN_BACK, JudgementState.DECIDED),
    ],
)
def test_illegal_transitions_are_rejected_and_logged(
    from_state: JudgementState, to_state: JudgementState
) -> None:
    row = make_row(state=from_state)
    log = TransitionLog()
    with pytest.raises(IllegalTransitionError) as excinfo:
        transition_row(row, to_state, log=log)
    error = excinfo.value
    assert error.from_state is from_state
    assert error.to_state is to_state
    assert row.state is from_state, "被拒的跃迁不得改动判定行"
    assert len(log.warnings) == 1
    warning = log.warnings[0]
    assert warning.from_state is from_state
    assert warning.to_state is to_state
    assert from_state.value in warning.message
    assert to_state.value in warning.message


def test_illegal_transition_message_lists_the_legal_targets() -> None:
    row = make_row(state=JudgementState.DECIDED)
    with pytest.raises(IllegalTransitionError) as excinfo:
        transition_row(row, JudgementState.WRITTEN_BACK)
    message = str(excinfo.value)
    assert "已判定" in message and "已回写" in message
    assert "药师已确认" in message, "报错要指出本来该走哪一步"


def test_illegal_transition_is_written_to_the_standard_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = make_row(state=JudgementState.PENDING_FIELDS)
    with caplog.at_level(logging.WARNING, logger="med_rule_fieldmap.engine"):
        with pytest.raises(IllegalTransitionError):
            transition_row(row, JudgementState.CONFIRMED)
    assert any("非法状态跃迁被拒" in record.message for record in caplog.records)


def test_writeback_is_only_reachable_after_pharmacist_confirmation() -> None:
    """「未确认绝不回写」：已回写只能从药师已确认到达。"""
    assert JudgementState.WRITTEN_BACK not in legal_targets(JudgementState.DECIDED)
    assert JudgementState.WRITTEN_BACK not in legal_targets(
        JudgementState.PENDING_FIELDS
    )
    assert JudgementState.WRITTEN_BACK in legal_targets(JudgementState.CONFIRMED)
    assert not can_transition(JudgementState.DECIDED, JudgementState.WRITTEN_BACK)
    assert can_transition(JudgementState.CONFIRMED, JudgementState.WRITTEN_BACK)


# ---------------------------------------------------------------------------
# 状态机落库
# ---------------------------------------------------------------------------


def test_advance_persisted_writes_the_state_to_the_store(tmp_path: Path) -> None:
    with Store(tmp_path / "mrfm.sqlite3") as store:
        row = make_row()
        store.insert_judgements([row])
        machine = JudgementStateMachine(store)
        moved = machine.advance_persisted(row, JudgementState.DECIDED)
        assert moved.state is JudgementState.DECIDED
        stored = store.get_judgement(row.idempotency_key)
        assert stored is not None
        assert stored.state is JudgementState.DECIDED


def test_state_machine_helpers_walk_the_documented_lifecycle(tmp_path: Path) -> None:
    with Store(tmp_path / "mrfm.sqlite3") as store:
        row = make_row()
        store.insert_judgements([row])
        machine = JudgementStateMachine(store)
        decided = machine.mark_decided(row)
        confirmed = machine.confirm(decided)
        written = machine.mark_written_back(confirmed)
        assert written.state is JudgementState.WRITTEN_BACK
        assert machine.log.states(row.business_key) == (
            JudgementState.DECIDED,
            JudgementState.CONFIRMED,
            JudgementState.WRITTEN_BACK,
        )
        assert machine.withdraw(written).state is JudgementState.WITHDRAWN


def test_state_machine_pending_fields_helper(tmp_path: Path) -> None:
    with Store(tmp_path / "mrfm.sqlite3") as store:
        row = make_row(state=JudgementState.DECIDED)
        store.insert_judgements([row])
        machine = JudgementStateMachine(store)
        waiting = machine.mark_pending_fields(row)
        assert waiting.state is JudgementState.PENDING_FIELDS
        assert machine.advance(waiting, JudgementState.DECIDED).state is (
            JudgementState.DECIDED
        )


# ---------------------------------------------------------------------------
# 重判留痕
# ---------------------------------------------------------------------------


def test_rejudge_appends_a_new_version_and_keeps_the_old_conclusion(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "mrfm.sqlite3") as store:
        old = make_row(
            conclusion=Conclusion.UNDETERMINABLE,
            state=JudgementState.PENDING_FIELDS,
            reason="规则 RX-LEGAL-MA-OUT-DAYS（版本 2026.10）判定：不可判定。",
            missing_fields=("rx_type",),
        )
        store.insert_judgements([old])
        machine = JudgementStateMachine(store)

        new = make_row(
            conclusion=Conclusion.VIOLATION,
            state=JudgementState.DECIDED,
            reason="规则 RX-LEGAL-MA-OUT-DAYS（版本 2026.10）判定：违规。日量超限。",
        )
        snapshot, refreshed = machine.rejudge(old, new)

        assert snapshot.version == 2
        assert snapshot.conclusion is Conclusion.VIOLATION
        history = machine.versions(old.business_key)
        assert [item.version for item in history] == [1, 2], "版本号递增"
        assert history[0].conclusion is Conclusion.UNDETERMINABLE, "旧结论保留"
        assert history[0].missing_fields == ("rx_type",)
        assert history[0].reason == old.reason, "历史行不被覆盖"
        assert history[1].conclusion is Conclusion.VIOLATION

        stored = store.get_judgement(old.idempotency_key)
        assert stored is not None
        assert stored.conclusion is Conclusion.VIOLATION, "判定表留当前那一版"
        assert stored.state is JudgementState.DECIDED
        assert refreshed.conclusion is Conclusion.VIOLATION


def test_rejudge_records_the_old_conclusion_when_history_is_empty(
    tmp_path: Path,
) -> None:
    """旧结论还没进版本表时，重判先把旧结论补成历史版本，不绕过留痕约束。"""
    with Store(tmp_path / "mrfm.sqlite3") as store:
        old = make_row(
            conclusion=Conclusion.UNDETERMINABLE, state=JudgementState.PENDING_FIELDS
        )
        machine = JudgementStateMachine(store)
        new = make_row(conclusion=Conclusion.COMPLIANT, state=JudgementState.DECIDED)
        snapshot, _ = machine.rejudge(old, new)
        history = machine.versions(old.business_key)
        assert [item.version for item in history] == [1, 2]
        assert history[0].conclusion is Conclusion.UNDETERMINABLE
        assert snapshot.version == 2
        assert snapshot.conclusion is Conclusion.COMPLIANT


def test_rejudge_keeps_every_earlier_version_after_several_rounds(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "mrfm.sqlite3") as store:
        first = make_row(
            conclusion=Conclusion.UNDETERMINABLE,
            state=JudgementState.PENDING_FIELDS,
            missing_fields=("rx_type",),
        )
        store.insert_judgements([first])
        machine = JudgementStateMachine(store)
        second = make_row(conclusion=Conclusion.VIOLATION, state=JudgementState.DECIDED)
        machine.rejudge(first, second)
        third = make_row(conclusion=Conclusion.COMPLIANT, state=JudgementState.DECIDED)
        machine.rejudge(second, third)

        history = machine.versions(first.business_key)
        assert [item.version for item in history] == [1, 2, 3]
        assert [item.conclusion for item in history] == [
            Conclusion.UNDETERMINABLE,
            Conclusion.VIOLATION,
            Conclusion.COMPLIANT,
        ]
        assert history[0].missing_fields == ("rx_type",)
        assert history[0].reason == first.reason


def test_rejudge_rejects_a_different_business_key(tmp_path: Path) -> None:
    with Store(tmp_path / "mrfm.sqlite3") as store:
        machine = JudgementStateMachine(store)
        old = make_row()
        other = make_row(prescription_no="RX-0002")
        with pytest.raises(StateMachineError) as excinfo:
            machine.rejudge(old, other)
        assert "业务键" in str(excinfo.value)
        assert store.count_judgement_versions() == 0


def test_rejudge_refuses_an_illegal_state_jump(tmp_path: Path) -> None:
    """重判也不许跳状态：失败不留半截版本。"""
    with Store(tmp_path / "mrfm.sqlite3") as store:
        old = make_row(state=JudgementState.PENDING_FIELDS)
        store.insert_judgements([old])
        machine = JudgementStateMachine(store)
        new = make_row(state=JudgementState.WRITTEN_BACK)
        with pytest.raises(IllegalTransitionError):
            machine.rejudge(old, new)
        assert store.count_judgement_versions(old.business_key) == 1
        assert len(machine.log.warnings) == 1


def test_rejudge_appends_an_audit_trail_and_stays_unwritten(tmp_path: Path) -> None:
    """重判是结论变更：审计表追加一条痕迹；未确认 → 回写状态仍是「未回写」。"""
    with Store(tmp_path / "mrfm.sqlite3") as store:
        old = make_row(
            conclusion=Conclusion.UNDETERMINABLE, state=JudgementState.PENDING_FIELDS
        )
        store.insert_judgements([old])
        before = store.count_audit()
        machine = JudgementStateMachine(store)
        new = make_row(conclusion=Conclusion.VIOLATION, state=JudgementState.DECIDED)
        machine.rejudge(old, new)

        audit = store.list_audit()
        assert len(audit) == before + 1
        assert audit[-1].conclusion is Conclusion.VIOLATION
        assert audit[-1].writeback_state == WRITEBACK_PENDING


def test_version_history_rows_are_never_overwritten(tmp_path: Path) -> None:
    with Store(tmp_path / "mrfm.sqlite3") as store:
        row = make_row(reason="第一版归因")
        store.insert_judgements([row])
        assert store.count_judgement_versions(row.business_key) == 1
        assert store.insert_judgement_version(row.to_version(1)) is False
        history = store.list_judgement_versions(row.business_key)
        assert len(history) == 1
        assert history[0].reason == "第一版归因"


def test_duplicate_delivery_adds_neither_judgement_rows_nor_versions(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "mrfm.sqlite3") as store:
        row = make_row()
        store.insert_judgements([row])
        store.insert_judgements([row])
        assert store.count_judgements() == 1
        assert store.count_judgement_versions() == 1
