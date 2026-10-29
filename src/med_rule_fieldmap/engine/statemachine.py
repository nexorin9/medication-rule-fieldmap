"""判定行状态机：待判定 → 已判定 → 药师已确认 → 已回写。

**对应源产品能力**：源仓库 ``core/workflow_actions/handler.py`` 的
``WorkflowActionHandler.create_workflow_action()``——「动作实例按序创建、
类型不允许的字段被拒、非法迁移不放行」。源产品的动作实例落库后由调度器
按序触发，**状态只由动作自己推进**；本轮把同一套语义搬到**判定行**上，
但多了一道源产品没有的关口：**药师确认**。

**为什么状态机要单独成层**：判定**结论**（合规 / 违规 / 不可判定）与判定行
的**状态**是两件事。结论回答「这条处方审得过吗」，状态回答「这条结论走到
哪一步了、能不能回写 HIS」。两者混在一起就会出事故——「不可判定被当成已
判定因而回写」「待判定直接跳已回写」「未经药师确认就写回 HIS」。

**合法跃迁表**（spec.md §5 业务状态生命周期）

=========================  ==================================================
状态                      允许的去向
=========================  ==================================================
待判定                    已判定 / 待补字段 / 已挂起
已判定                    药师已确认 / 待补字段 / 已撤回 / 已挂起
待补字段                  已判定（字段补齐后重判）/ 已挂起
药师已确认                已回写 / 已撤回 / 已挂起
已回写                    已撤回（撤回须同步走 HIS 复核更新接口并留痕）
已撤回                    待判定（重新进入判定流程）
已挂起                    待判定（人工解除挂起后重投）
=========================  ==================================================

**非法跃迁一律拒绝**：「已判定」直接跳「已回写」（跳过药师确认）、
「待补字段」直接跳「药师已确认」（跳过重判）——两条都抛
:class:`IllegalTransitionError` **并记入告警日志**，绝不静默放行，
也绝不把行就地改掉（状态机不静默改数据）。

**重判留痕**：判定表留的是「**当前**那一版」结论；每次重判都往版本表
（``judgement_versions``）追加一条**版本号递增**的新版本，旧版本行
**不被覆盖、不被删除**——「这条结论当初判成什么、后来为什么改」可逐版回放
（飞检与内审语境下的自证需求，见 spec.md §4 ``out/audit_ledger.csv``）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..models import JudgementRow, JudgementState, JudgementVersion, now_iso

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注；运行期导入会与 store 形成循环
    from ..store import Store

logger = logging.getLogger("med_rule_fieldmap.engine")


# ---------------------------------------------------------------------------
# 合法跃迁表
# ---------------------------------------------------------------------------

#: 状态 → 允许的去向（唯一真源；改动这里等于改动业务规则，须同步 spec.md §5）。
LEGAL_TRANSITIONS: Mapping[JudgementState, frozenset[JudgementState]] = {
    JudgementState.PENDING: frozenset(
        {
            JudgementState.DECIDED,
            JudgementState.PENDING_FIELDS,
            JudgementState.SUSPENDED,
        }
    ),
    JudgementState.DECIDED: frozenset(
        {
            JudgementState.CONFIRMED,
            JudgementState.PENDING_FIELDS,
            JudgementState.WITHDRAWN,
            JudgementState.SUSPENDED,
        }
    ),
    JudgementState.PENDING_FIELDS: frozenset(
        {JudgementState.DECIDED, JudgementState.SUSPENDED}
    ),
    JudgementState.CONFIRMED: frozenset(
        {
            JudgementState.WRITTEN_BACK,
            JudgementState.WITHDRAWN,
            JudgementState.SUSPENDED,
        }
    ),
    JudgementState.WRITTEN_BACK: frozenset({JudgementState.WITHDRAWN}),
    JudgementState.WITHDRAWN: frozenset({JudgementState.PENDING}),
    JudgementState.SUSPENDED: frozenset({JudgementState.PENDING}),
}

#: 自动化流程的终点：已回写。此后只允许撤回（须同步走 HIS 复核更新接口）。
PIPELINE_END = JudgementState.WRITTEN_BACK


def legal_targets(state: JudgementState) -> frozenset[JudgementState]:
    """某状态允许的去向（未登记的状态没有去向）。"""
    return LEGAL_TRANSITIONS.get(state, frozenset())


def can_transition(current: JudgementState, target: JudgementState) -> bool:
    """这一跳是否合法。"""
    return target in legal_targets(current)


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class StateMachineError(RuntimeError):
    """状态机相关错误的基类。"""


class IllegalTransitionError(StateMachineError):
    """非法状态跃迁被拒。

    携带起止状态与该状态**允许的去向**，告警日志与排障时一眼能看懂
    「本来该走哪一步」。
    """

    def __init__(
        self,
        from_state: JudgementState,
        to_state: JudgementState,
        *,
        business_key: str = "",
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.business_key = business_key
        self.legal_targets = legal_targets(from_state)
        detail = "、".join(target.value for target in sorted(
            self.legal_targets, key=lambda item: item.value
        )) or "（无）"
        scope = f"（{business_key}）" if business_key else ""
        super().__init__(
            f"非法状态跃迁被拒：{from_state.value} → {to_state.value}{scope}；"
            f"当前状态允许的去向：{detail}"
        )


# ---------------------------------------------------------------------------
# 跃迁留痕
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionRecord:
    """一次合法跃迁的留痕。"""

    business_key: str
    from_state: JudgementState
    to_state: JudgementState
    at: str
    note: str = ""


@dataclass(frozen=True)
class TransitionWarning:
    """一次被拒的非法跃迁（告警日志条目）。"""

    business_key: str
    from_state: JudgementState
    to_state: JudgementState
    at: str
    message: str


class TransitionLog:
    """跃迁日志：合法跃迁留痕，非法跃迁记告警。

    日志在内存里（守护进程与 CLI 各自持有），需要落盘时由调用方导出；
    「非法跃迁」同时写一条标准告警日志（``logging``），运维在守护进程的
    日志里能直接看到被拒的跳转。
    """

    def __init__(self) -> None:
        self._records: list[TransitionRecord] = []
        self._warnings: list[TransitionWarning] = []

    def record(self, record: TransitionRecord) -> TransitionRecord:
        """记一条合法跃迁。"""
        self._records.append(record)
        return record

    def warn(self, warning: TransitionWarning) -> TransitionWarning:
        """记一条非法跃迁告警。

        标准日志（``logging``）由 :func:`transition_row` 统一写——**每次**被拒
        的跃迁都进运维日志，不因调用方没挂日志对象而漏记。
        """
        self._warnings.append(warning)
        return warning

    @property
    def records(self) -> tuple[TransitionRecord, ...]:
        """全部合法跃迁留痕（按发生顺序）。"""
        return tuple(self._records)

    @property
    def warnings(self) -> tuple[TransitionWarning, ...]:
        """全部非法跃迁告警（按发生顺序）。"""
        return tuple(self._warnings)

    def states(self, business_key: str) -> tuple[JudgementState, ...]:
        """某条判定行的跃迁轨迹（含起点，按时间顺序）。"""
        return tuple(
            record.to_state
            for record in self._records
            if record.business_key == business_key
        )

    def __len__(self) -> int:
        return len(self._records)


# ---------------------------------------------------------------------------
# 单条跃迁
# ---------------------------------------------------------------------------


def transition_row(
    row: JudgementRow,
    target: JudgementState,
    *,
    log: TransitionLog | None = None,
    note: str = "",
    at: str | None = None,
) -> JudgementRow:
    """把一条判定行推进到目标状态。

    * 合法 → 返回**新**的行（原行不被就地修改）；
    * 非法 → 记一条告警（``log`` 非空时）并抛 :class:`IllegalTransitionError`，
      **行本身不被改动**——失败不静默改数据。
    """
    if not can_transition(row.state, target):
        error = IllegalTransitionError(
            row.state, target, business_key=row.business_key
        )
        warning = TransitionWarning(
            business_key=row.business_key,
            from_state=row.state,
            to_state=target,
            at=at or now_iso(),
            message=str(error),
        )
        # 告警日志：每次被拒的跃迁都进运维日志，不因没挂日志对象而漏记
        logger.warning(warning.message)
        if log is not None:
            log.warn(warning)
        raise error
    stamp = at or now_iso()
    if log is not None:
        log.record(
            TransitionRecord(
                business_key=row.business_key,
                from_state=row.state,
                to_state=target,
                at=stamp,
                note=note,
            )
        )
    return replace(row, state=target)


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


class JudgementStateMachine:
    """判定行状态机：跃迁校验 + 重判留痕。

    一次实例绑定一个状态库（:class:`med_rule_fieldmap.store.Store`）与一条
    跃迁日志；守护进程的线程池与运维 CLI 可以共用同一个实例（写操作由
    状态库自己的锁串行化）。

    **状态机是判定行状态的唯一推进入口**：所有状态变更都走
    :meth:`advance` / :meth:`advance_persisted`，非法跃迁在落库之前就被拒。
    """

    def __init__(self, store: "Store", *, log: TransitionLog | None = None) -> None:
        self.store = store
        self.log = log if log is not None else TransitionLog()

    # -- 跃迁 -------------------------------------------------------------

    def can_transition(self, current: JudgementState, target: JudgementState) -> bool:
        """这一跳是否合法。"""
        return can_transition(current, target)

    def advance(
        self,
        row: JudgementRow,
        target: JudgementState,
        *,
        note: str = "",
        at: str | None = None,
    ) -> JudgementRow:
        """校验并推进状态（**不落库**）。"""
        return transition_row(row, target, log=self.log, note=note, at=at)

    def advance_persisted(
        self,
        row: JudgementRow,
        target: JudgementState,
        *,
        note: str = "",
    ) -> JudgementRow:
        """校验并推进状态，且落库（状态机是状态推进入口）。"""
        new_row = self.advance(row, target, note=note)
        self.store.set_judgement_state(new_row.idempotency_key, target)
        return new_row

    # -- 常用关口（语义化封装）-------------------------------------------

    def mark_decided(self, row: JudgementRow, *, note: str = "") -> JudgementRow:
        """判定完成：待判定 → 已判定。"""
        return self.advance(row, JudgementState.DECIDED, note=note or "判定完成")

    def mark_pending_fields(
        self, row: JudgementRow, *, note: str = ""
    ) -> JudgementRow:
        """结论不可判定：已判定 → 待补字段（等接口或医师补齐字段）。"""
        return self.advance(
            row, JudgementState.PENDING_FIELDS, note=note or "字段待补齐"
        )

    def confirm(self, row: JudgementRow, *, note: str = "") -> JudgementRow:
        """药师确认：已判定 → 药师已确认（回写的唯一入口条件）。"""
        return self.advance(row, JudgementState.CONFIRMED, note=note or "药师已确认")

    def mark_written_back(self, row: JudgementRow, *, note: str = "") -> JudgementRow:
        """回写成功：药师已确认 → 已回写。"""
        return self.advance(row, JudgementState.WRITTEN_BACK, note=note or "已回写")

    def withdraw(self, row: JudgementRow, *, note: str = "") -> JudgementRow:
        """药师撤回：药师已确认（或已判定）→ 已撤回。"""
        return self.advance(row, JudgementState.WITHDRAWN, note=note or "药师撤回")

    def suspend(self, row: JudgementRow, *, note: str = "") -> JudgementRow:
        """文件损坏 / 事件不可信：→ 已挂起（人工解除后重投）。"""
        return self.advance(row, JudgementState.SUSPENDED, note=note or "已挂起")

    # -- 重判 -------------------------------------------------------------

    def rejudge(
        self,
        row: JudgementRow,
        new_row: JudgementRow,
        *,
        note: str = "",
    ) -> tuple[JudgementVersion, JudgementRow]:
        """重判：生成新一版判定行，旧结论保留为历史版本。

        步骤：

        #. **业务键必须一致**——重判的是同一条处方 × 同一条规则，不换对象；
        #. **状态跃迁校验**（待补字段 → 已判定 是常规路径；状态不变的
           重判只留版本痕迹）；
        #. **旧结论先入版本表**（若还没入过），保证「旧结论保留为历史版本」
           这条约束不被绕过；
        #. **新结论按版本号递增追加**到版本表——历史行不被覆盖、不被删除；
        #. **判定表刷新为当前那一版**，并补一条追加式审计痕迹。

        返回 ``(新版本快照, 刷新后的判定行)``。
        """
        if row.business_key != new_row.business_key:
            raise StateMachineError(
                "重判的业务键必须一致："
                f"{row.business_key!r} ≠ {new_row.business_key!r}"
            )
        business_key = row.business_key

        if row.state is not new_row.state:
            transition_row(
                row,
                new_row.state,
                log=self.log,
                note=note or "字段补齐后重判",
            )

        history = self.store.list_judgement_versions(business_key)
        if not history:
            # 旧结论还没进版本表（手工构造的行 / 历史数据补写）：先补一条，
            # 否则「旧结论保留为历史版本」会被绕过。
            self.store.insert_judgement_version(row.to_version(1))

        version = self.store.next_judgement_version(business_key)
        snapshot = new_row.to_version(version)
        self.store.insert_judgement_version(snapshot)

        # 判定表留的是「当前那一版」；旧结论已在版本表里，刷新不丢历史。
        self.store.refresh_judgement(new_row)
        self.store.append_rejudgement_audit(new_row)

        self.log.record(
            TransitionRecord(
                business_key=business_key,
                from_state=row.state,
                to_state=new_row.state,
                at=now_iso(),
                note=note or f"重判：结论更新为版本 {version}",
            )
        )
        return snapshot, new_row

    def versions(self, business_key: str) -> tuple[JudgementVersion, ...]:
        """某条判定行的全部历史版本（按版本号升序）。"""
        return tuple(self.store.list_judgement_versions(business_key))


__all__ = [
    "LEGAL_TRANSITIONS",
    "PIPELINE_END",
    "IllegalTransitionError",
    "JudgementStateMachine",
    "StateMachineError",
    "TransitionLog",
    "TransitionRecord",
    "TransitionWarning",
    "can_transition",
    "legal_targets",
    "transition_row",
]
