"""规则调度：一条医嘱行 × 一组规则行 → 候选规则集。

**对应源产品能力**：源仓库 ``contrib/database/field_rules/registries.py`` 的
``before_row_created`` / ``before_row_updated``——源产品在写入前逐行逐规则求值：
每条规则先问「这条规则适不适用于本行」，适用才求值，不适用直接跳过。
本轮把「逐行」改写成「逐处方」，并把适用性判断从源产品的行内钩子里拆成
独立调度层（``engine/sched.py``）；判定内核（``engine/judge.py``）只处理
调度层选出来的候选规则。

**为什么调度层要单独成层**：一条 HIS 医嘱会命中几十条规则，但绝大多数规则
对这条处方**不适用**（「住院患者麻醉药品日量」对门诊处方没有意义）。
不适用的规则**不产生判定行**——台账里塞满噪声，药师根本看不动。

**两档分流（本模块的核心语义）**

===========================  =============================================
适用条件字段的取值状态        调度结果
===========================  =============================================
有值且命中                    候选 → 进判定层
有值但不命中                  不适用 → 不产生判定行
缺失，且该字段是规则声明的依赖  候选 → 判定层落「不可判定」并点名该字段
缺失，且该字段不是规则声明的依赖 不适用 → 不产生判定行（**不判不可判定**）
===========================  =============================================

第三档为什么不是「不适用」：缺 ``rx_type`` 的处方必须能说清「补上
``rx_type`` 能解锁哪几条量限规则」——这正是字段补齐顺序表
（``ledger/priority.py``）的原料。若一律判不适用，这条处方在台账上一行
不留，信息科就不知道该先改哪个接口字段。第四档相反：这类字段对规则结论
没有影响，判不可判定只会制造噪声。

**比较口径**：适用条件两侧都先过归一层（``expression/normalize.py``），
 HIS 落地文件里的写法（``门诊一般患者``）与规则行口径（``门急诊一般``）
才能对上；归一后仍不相等才算「不命中」。归一不动的一律留原文，不猜。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ..expression.normalize import normalize_field
from ..models import PrescriptionRow, RuleRow


class ScheduleStatus(str, Enum):
    """调度结论：候选 / 不适用。"""

    CANDIDATE = "候选"
    NOT_APPLICABLE = "不适用"


@dataclass(frozen=True)
class RuleSchedule:
    """一条规则对一条医嘱的调度结果。

    ``unknown_fields`` 是「适用条件字段缺失、但该字段是规则声明的依赖」的
    字段名——判定层据此把结论落成「不可判定」并点名这些字段。
    """

    rule: RuleRow
    status: ScheduleStatus
    matched_fields: tuple[str, ...] = ()
    unknown_fields: tuple[str, ...] = ()
    unmatched_fields: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_candidate(self) -> bool:
        """本规则是否为候选（要进判定层）。"""
        return self.status is ScheduleStatus.CANDIDATE

    @property
    def rule_id(self) -> str:
        return self.rule.rule_id

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照（CLI ``--json`` 输出用）。"""
        return {
            "rule_id": self.rule.rule_id,
            "status": self.status.value,
            "matched_fields": list(self.matched_fields),
            "unknown_fields": list(self.unknown_fields),
            "unmatched_fields": list(self.unmatched_fields),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# 条件比较
# ---------------------------------------------------------------------------


def _normalized(field: str, text: str) -> str:
    """字段取值的归一形态（两侧同口径才可比）。"""
    return normalize_field(field, text).text


def condition_matches(field: str, condition: object, row: PrescriptionRow) -> bool:
    """单个适用条件是否命中。

    ``condition`` 两种写法：精确值（归一后相等）或 ``{"contains": 子串}``。
    两侧都先归一：HIS 落地写法与规则口径的差异由归一层吸收。
    """
    value = _normalized(field, row.value(field))
    if isinstance(condition, Mapping):
        needle = str(condition.get("contains", ""))
        return bool(needle) and needle in value
    return value == _normalized(field, str(condition))


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------


def match_rule(rule: RuleRow, row: PrescriptionRow) -> RuleSchedule:
    """调度单条规则对这条医嘱是否适用。

    适用性只看 ``applies_when``；``required_fields`` 的缺失与否是**判定层**
    的事（落「不可判定」还是「合规 / 违规」由判定层决定），调度层不越界。
    """
    matched: list[str] = []
    unknown: list[str] = []
    unmatched: list[str] = []

    for name, condition in rule.applies_when.items():
        if row.is_missing(name):
            if name in rule.required_fields:
                # 依赖字段缺失：保留候选，判定层落「不可判定」并点名该字段
                unknown.append(name)
            else:
                # 非依赖字段缺失：对本规则结论无影响 → 不适用，不判不可判定
                unmatched.append(name)
            continue
        if condition_matches(name, condition, row):
            matched.append(name)
        else:
            unmatched.append(name)

    if unmatched:
        detail = "、".join(unmatched)
        missing_dependency = [name for name in unmatched if name not in rule.required_fields]
        if missing_dependency:
            reason = (
                f"适用条件字段 {detail} 缺失且未声明为依赖字段，本规则不适用（不判不可判定）"
            )
        else:
            reason = f"适用条件字段 {detail} 的取值与规则声明口径不一致，本规则不适用"
        return RuleSchedule(
            rule=rule,
            status=ScheduleStatus.NOT_APPLICABLE,
            matched_fields=tuple(matched),
            unknown_fields=tuple(unknown),
            unmatched_fields=tuple(unmatched),
            reason=reason,
        )

    reason = "适用条件全部命中"
    if unknown:
        names = "、".join(unknown)
        reason = (
            f"适用条件字段 {names} 缺失，但该字段是本规则的显式依赖，"
            "将落不可判定并点名该字段"
        )
    return RuleSchedule(
        rule=rule,
        status=ScheduleStatus.CANDIDATE,
        matched_fields=tuple(matched),
        unknown_fields=tuple(unknown),
        reason=reason,
    )


def schedule_rules(
    rules: Sequence[RuleRow], row: PrescriptionRow
) -> tuple[RuleSchedule, ...]:
    """调度整组规则，返回每条规则的调度结果（含不适用）。"""
    return tuple(match_rule(rule, row) for rule in rules)


def select_candidates(
    rules: Sequence[RuleRow], row: PrescriptionRow
) -> tuple[RuleSchedule, ...]:
    """选出候选规则（适用的那批），顺序与入参一致。"""
    return tuple(
        schedule for schedule in schedule_rules(rules, row) if schedule.is_candidate
    )


def candidate_rules(
    rules: Sequence[RuleRow], row: PrescriptionRow
) -> tuple[RuleRow, ...]:
    """候选规则行本体（判定层要用的形态）。"""
    return tuple(schedule.rule for schedule in select_candidates(rules, row))


__all__ = [
    "RuleSchedule",
    "ScheduleStatus",
    "candidate_rules",
    "condition_matches",
    "match_rule",
    "schedule_rules",
    "select_candidates",
]
