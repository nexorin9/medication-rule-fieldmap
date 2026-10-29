"""皮试判定（《处方审核规则体系》§1 适宜性审核第 1 项）：空值 ≠ 显式否。

**对应源产品能力**：源仓库 ``contrib/database/field_rules/registries.py`` 的
``before_row_created`` / ``before_row_updated``（写入前逐行逐规则求值）与
``FieldRuleValidity.error_text``（归因位）——本轮把源产品的二值 ``is_valid``
扩成三态，并把「显式否定」与「字段缺失」在皮试这一条上分成**两个互不相同**的结论。

**这条规则为什么单列一册**

§1 适宜性审核第 1 项原文：「规定必须做皮试的药品，是否注明过敏试验及结果判定」。
落到现场数据上，「没写皮试结果」有两种截然不同的含义：

======================  =========================================================
落地取值                  判定结论
======================  =========================================================
``skin_test_result`` 留空   **不可判定**——系统判不了（字段没落地），
                      落字段缺口台账，信息科补接口字段后可重判
写成 ``未注明`` / ``未做``    **违规**——已知不合格（写明了没做皮试），
                      可直接自动拦截
写成 ``阴性``              **合规**——皮试已做且结果为阴性
======================  =========================================================

两者若混成「非空即有值」，最常见的现场错误就是「没填皮试结果」被当成
「皮试阴性」放过——一条本该拦下的处方就这么进了划价收费与调配环节
（《处方审核规则体系》§4：所有处方经审核通过后方可进入划价收费和调配环节）。

**结论不写死在代码里**

本模块只做三件事，结论本身由规则行表达：

#. **依赖声明**：皮试结果的取用必须由规则行在 ``required_fields`` 里显式声明
   （``skin_test_result``）。字段没落地 → 落「不可判定」并点名该字段。
   换句话说，「空值 → 不可判定」不是本模块写死的分支，而是
   **规则行声明 ``required_fields`` 的自然结果**。
#. **空值三档**：缺失 / 显式否 / 有值，由归一层（``expression/normalize.py``）
   与受限求值器（``expression/evaluator.py``）给出，别名（``未做`` / ``未知`` /
   ``不详``）在归一层统一收到 ``未注明``。
#. **表达式求值**：合规还是违规由规则行的 ``expression`` 决定（受限表达式，
   无 ``eval``、无函数调用）。本模块**不写**
   「if 结果 == 未注明 then 违规」这种结论分支——那是把审方口径焊死在代码里，
   规则维护岗改一条阈值都得改代码。

**「必须做皮试的药品清单」从哪来**

属药品说明书数据（厂商资产），《处方审核规则体系》§3 末段已明写
「严禁模型凭记忆生成」。本轮不落药品库：清单由**规则行的 ``applies_when`` 声明**
（药事审核岗维护），:func:`skin_test_scope` 把当前生效的皮试范围汇总出来，
供规则体检（``ledger/rule_health.py``）与接口字段对接使用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ..expression.errors import ExpressionLanguageError
from ..expression.evaluator import (
    EvalState,
    EvaluationError,
    FieldState,
    evaluate_expression as evaluate_expression_text,
)
from ..expression.normalize import normalize_context, normalize_field
from ..models import Conclusion, PrescriptionRow, RuleRow
from .evidence import build_reason
from .sched import condition_matches, match_rule

# ---------------------------------------------------------------------------
# 口径
# ---------------------------------------------------------------------------

#: 皮试结果字段（医嘱落地列口径，spec.md §2.1）。
SKIN_TEST_FIELD = "skin_test_result"

#: 皮试规则的依据出处（《处方审核规则体系》§1 适宜性审核第 1 项）。
SKIN_TEST_BASIS = (
    "处方审核规则体系 §1 适宜性审核第 1 项"
    "（规定必须做皮试的药品，是否注明过敏试验及结果判定）"
)

#: 皮试结果里「已知没做 / 没写明」的规范取值。
#:
#: 取自 ``expression/normalize.py`` 的 ``SKIN_TEST_ALIASES`` 归一结果：
#: ``未做`` / ``未行`` / ``未做皮试`` / ``未标注`` / ``未知`` / ``不详`` 全部归到
#: ``未注明``，因此它们在判定层是**同一个分支**。
EXPLICIT_NO_VALUE = "未注明"

#: 皮试结果里「已做且阴性」的规范取值。
NEGATIVE_VALUE = "阴性"


class SkinTestStatus(str, Enum):
    """皮试判定结论（与三态判定内核一致，多一个「不适用」）。"""

    NOT_APPLICABLE = "不适用"
    COMPLIANT = "合规"
    VIOLATION = "违规"
    UNDETERMINABLE = "不可判定"


#: 皮试结论 → 判定内核的三态结论。
SKIN_TEST_CONCLUSIONS: Mapping[SkinTestStatus, Conclusion] = {
    SkinTestStatus.COMPLIANT: Conclusion.COMPLIANT,
    SkinTestStatus.VIOLATION: Conclusion.VIOLATION,
    SkinTestStatus.UNDETERMINABLE: Conclusion.UNDETERMINABLE,
}


class SkinTestConfigurationError(RuntimeError):
    """规则行写法与皮试口径冲突（**配置错误**，不得降级成「不可判定」）。

    与「数据缺字段」严格分列：规则行写错时若静默降级成「不可判定」，
    就会把一条配置问题伪装成业务上的「审不了」，规则体检永远查不出真因。
    """


# ---------------------------------------------------------------------------
# 取值形态
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkinTestValue:
    """皮试结果字段的取值形态（缺失 / 显式否 / 有值 三档）。"""

    raw: str
    canonical: str
    state: FieldState

    @property
    def is_missing(self) -> bool:
        """字段缺失（没落地 / 留空 / 纯空白）。"""
        return self.state is FieldState.MISSING

    @property
    def is_explicit_no(self) -> bool:
        """字段有值，但值是显式否定（写明了没做皮试）。"""
        return self.state is FieldState.EXPLICIT_NO

    @property
    def is_value(self) -> bool:
        """字段有正常取值。"""
        return self.state is FieldState.VALUE

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照。"""
        return {
            "raw": self.raw,
            "canonical": self.canonical,
            "state": self.state.value,
        }


@dataclass(frozen=True)
class SkinTestEvaluation:
    """一次皮试判定的结果。"""

    status: SkinTestStatus
    conclusion: Conclusion | None = None
    detail: str = ""
    reason: str = ""
    missing_fields: tuple[str, ...] = ()
    value: SkinTestValue | None = None
    expression: str = ""
    rule_id: str = ""

    @property
    def is_undeterminable(self) -> bool:
        """是否落「不可判定」（字段没落地，系统判不了）。"""
        return self.status is SkinTestStatus.UNDETERMINABLE

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照（CLI ``--json`` 输出用）。"""
        return {
            "rule_id": self.rule_id,
            "status": self.status.value,
            "conclusion": self.conclusion.value if self.conclusion else "",
            "detail": self.detail,
            "reason": self.reason,
            "missing_fields": list(self.missing_fields),
            "value": self.value.to_dict() if self.value else {},
            "expression": self.expression,
        }


# ---------------------------------------------------------------------------
# 规则行识别与配置校验
# ---------------------------------------------------------------------------


def is_skin_test_rule(rule: RuleRow) -> bool:
    """这条规则是不是皮试规则。

    判据是**规则行显式声明了依赖** ``skin_test_result``——不是按规则号前缀猜。
    声明即「没有这个字段我判不了」，这条边界由规则行自己声明，引擎不替它猜。
    """
    return SKIN_TEST_FIELD in rule.required_fields


def validate_skin_test_rule(rule: RuleRow) -> tuple[str, ...]:
    """校验一条皮试规则行，返回配置问题清单（空清单 = 通过）。

    三条硬约束：

    #. 必须声明依赖 ``skin_test_result``，否则判定无从谈起；
    #. ``missing_conclusion`` 必须是「不可判定」——把「字段没落地」声明成「违规」
       就把**空值与显式否混成了一档**，正是本条要守住的边界；
    #. 不带 ``limit_ref`` / ``forbidden_terms``——量限与词表走各自的判定路径，
       混在皮试规则里会让人误以为两条路径都跑了。
    """
    issues: list[str] = []
    if not is_skin_test_rule(rule):
        issues.append(
            f"规则 {rule.rule_id} 未在 required_fields 里声明 {SKIN_TEST_FIELD}，"
            "不能作为皮试规则判定"
        )
    if rule.missing_conclusion is not Conclusion.UNDETERMINABLE:
        issues.append(
            f"规则 {rule.rule_id} 的 missing_conclusion 声明为「{rule.missing_conclusion.value}」，"
            "皮试结果字段未落地只能落「不可判定」（空值 ≠ 显式否）"
        )
    if rule.limit_ref:
        issues.append(
            f"规则 {rule.rule_id} 同时声明了 limit_ref，量限走矩阵路径，不走皮试路径"
        )
    if rule.forbidden_terms:
        issues.append(
            f"规则 {rule.rule_id} 同时声明了 forbidden_terms，词表命中走词表路径，不走皮试路径"
        )
    return tuple(issues)


# ---------------------------------------------------------------------------
# 取值分类
# ---------------------------------------------------------------------------


def classify_skin_test_value(row: PrescriptionRow) -> SkinTestValue:
    """把皮试结果字段分类成三档（缺失 / 显式否 / 有值）。

    归一层负责别名收敛（``未做`` / ``未知`` / ``不详`` → ``未注明``），
    本模块只做分类，不猜、不改写落地值。
    """
    normalized = normalize_field(SKIN_TEST_FIELD, row.value(SKIN_TEST_FIELD))
    return SkinTestValue(
        raw=normalized.raw,
        canonical=normalized.text,
        state=normalized.state,
    )


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def _detail_for_missing(missing: Sequence[str]) -> str:
    """字段没落地时的归因说明。"""
    names = "、".join(missing)
    return (
        f"皮试结果字段 {names} 未落地（医嘱没填写或接口未回传），"
        "系统判不了，须药师人工审核；字段补齐后可重判"
    )


def _detail_for_value(value: SkinTestValue, expression: str) -> str:
    """字段有值时的归因说明（区分显式否定与正常取值）。"""
    if value.is_explicit_no:
        return (
            f"皮试结果字段 {SKIN_TEST_FIELD} 落地为「{value.canonical}」"
            "（显式否定值：写明了没做皮试，不是字段没落地）"
        )
    return f"皮试结果字段 {SKIN_TEST_FIELD} 落地为「{value.canonical}」"


def evaluate_skin_test(
    rule: RuleRow,
    row: PrescriptionRow,
    *,
    cache: object | None = None,
) -> SkinTestEvaluation:
    """按规则行判定一条医嘱的皮试结论（三态 + 不适用）。

    判定链（与 ``engine/judge.py`` 的通用路径同序，但把皮试这一条的边界写死成契约）：

    #. **配置校验**：规则行不合皮试口径 → 抛 :class:`SkinTestConfigurationError`
       （配置错误不降级）；
    #. **适用性**：``applies_when`` 不命中 → 不适用，不产生判定行；
    #. **依赖检查**：``required_fields`` 里任一字段缺失或为空 → 不可判定并点名；
    #. **表达式求值**：字段齐备 → 按规则行的受限表达式求值得合规 / 违规。

    ``cache`` 是表达式解析树缓存（``expression/cache.py``），缺省用进程级默认缓存。
    """
    issues = validate_skin_test_rule(rule)
    if issues:
        raise SkinTestConfigurationError("；".join(issues))

    schedule = match_rule(rule, row)
    if not schedule.is_candidate:
        return SkinTestEvaluation(
            status=SkinTestStatus.NOT_APPLICABLE,
            detail=schedule.reason,
            rule_id=rule.rule_id,
        )

    missing = row.missing_fields(rule.required_fields)
    value = classify_skin_test_value(row)
    if missing:
        return SkinTestEvaluation(
            status=SkinTestStatus.UNDETERMINABLE,
            conclusion=Conclusion.UNDETERMINABLE,
            detail=_detail_for_missing(missing),
            missing_fields=missing,
            value=value,
            expression=rule.expression,
            rule_id=rule.rule_id,
        )

    context = normalize_context(row)
    try:
        result = evaluate_expression_text(
            rule.expression, context, cache=cache  # type: ignore[arg-type]
        )
    except (EvaluationError, ExpressionLanguageError) as exc:
        raise SkinTestConfigurationError(
            f"规则 {rule.rule_id} 的表达式不可求值：{exc}"
        ) from exc

    detail = _detail_for_value(value, rule.expression)
    if result.state is EvalState.TRUE:
        status = SkinTestStatus.VIOLATION
    elif result.state is EvalState.FALSE:
        status = SkinTestStatus.COMPLIANT
    else:
        status = SkinTestStatus.UNDETERMINABLE

    gaps = tuple(result.missing_fields) + tuple(result.uncomparable_fields)
    return SkinTestEvaluation(
        status=status,
        conclusion=SKIN_TEST_CONCLUSIONS[status],
        detail=detail,
        missing_fields=gaps,
        value=value,
        expression=rule.expression,
        rule_id=rule.rule_id,
    )


def render_skin_test_reason(
    evaluation: SkinTestEvaluation,
    rule: RuleRow,
    row: PrescriptionRow | None = None,
) -> str:
    """把皮试判定结果组装成归因文本（固定句式 + 隐私约束）。

    与通用路径共用 ``engine/evidence.py`` 的句式，句中带出规则号、规则版本、
    依据出处；不可判定句式额外点名缺失字段名。
    """
    if evaluation.conclusion is None:
        return evaluation.detail
    return build_reason(
        evaluation.conclusion,
        rule,
        detail=evaluation.detail,
        missing_fields=evaluation.missing_fields,
        fields=row,
    )


# ---------------------------------------------------------------------------
# 「必须做皮试」的药品范围（规则驱动，不落药品库）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkinTestScopeEntry:
    """一条皮试范围声明：某个字段满足某个条件即须做皮试。"""

    rule_id: str
    field: str
    condition: object
    description: str

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照。"""
        return {
            "rule_id": self.rule_id,
            "field": self.field,
            "condition": self.condition,
            "description": self.description,
        }


@dataclass(frozen=True)
class SkinTestScope:
    """本院当前生效的「必须做皮试」范围（由规则行汇总）。"""

    entries: tuple[SkinTestScopeEntry, ...] = ()

    @property
    def rule_ids(self) -> tuple[str, ...]:
        """声明了皮试范围的规则号。"""
        return tuple(entry.rule_id for entry in self.entries)

    @property
    def is_empty(self) -> bool:
        """范围为空（本院当前没有任何规则声明必须做皮试的药品）。"""
        return not self.entries

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照。"""
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "rule_ids": list(self.rule_ids),
        }


def describe_condition(field: str, condition: object) -> str:
    """把一条适用条件写成人读的一句话（供规则体检与接口对接清单使用）。"""
    if isinstance(condition, Mapping):
        needle = str(condition.get("contains", "")).strip()
        if needle:
            return f"{field} 含「{needle}」"
        return f"{field} 满足条件 {dict(condition)}"
    return f"{field} 等于「{condition}」"


def skin_test_scope(rules: Sequence[RuleRow]) -> SkinTestScope:
    """汇总当前规则集声明的「必须做皮试」范围。

    范围来自规则行的 ``applies_when``，**不在代码里写死药品清单**：
    《处方审核规则体系》§3 末段明写药品说明书数据属厂商资产、严禁凭记忆生成，
    正式环境应由药事审核岗维护药品目录接入，本轮只把规则行里已经声明的范围报出来。
    """
    entries: list[SkinTestScopeEntry] = []
    for rule in rules:
        if not is_skin_test_rule(rule):
            continue
        for field, condition in rule.applies_when.items():
            entries.append(
                SkinTestScopeEntry(
                    rule_id=rule.rule_id,
                    field=field,
                    condition=condition,
                    description=describe_condition(field, condition),
                )
            )
    return SkinTestScope(entries=tuple(entries))


def is_skin_test_required(row: PrescriptionRow, rules: Sequence[RuleRow]) -> bool:
    """这条医嘱用的药是否属于「必须做皮试」的范围。

    判据是规则行的 ``applies_when``：命中任一皮试规则的适用条件即为「须做皮试」。
    这是字段缺口台账的口径来源——「这张处方该做皮试却没填皮试结果」是接口字段
    缺口，不是审方结论。
    """
    for rule in rules:
        if not is_skin_test_rule(rule):
            continue
        if not rule.applies_when:
            return True
        if all(
            condition_matches(field, condition, row)
            for field, condition in rule.applies_when.items()
        ):
            return True
    return False


__all__ = [
    "EXPLICIT_NO_VALUE",
    "NEGATIVE_VALUE",
    "SKIN_TEST_BASIS",
    "SKIN_TEST_CONCLUSIONS",
    "SKIN_TEST_FIELD",
    "SkinTestConfigurationError",
    "SkinTestEvaluation",
    "SkinTestScope",
    "SkinTestScopeEntry",
    "SkinTestStatus",
    "SkinTestValue",
    "classify_skin_test_value",
    "describe_condition",
    "evaluate_skin_test",
    "is_skin_test_required",
    "is_skin_test_rule",
    "render_skin_test_reason",
    "skin_test_scope",
    "validate_skin_test_rule",
]
