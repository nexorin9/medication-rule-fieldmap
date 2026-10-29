"""诊断相符性（《处方审核规则体系》§1 适宜性审核第 2 项）：**只做字段完备性暴露，不做实质判定**。

**这条规则为什么单列一册**

§1 适宜性审核第 2 项原文：「处方用药与临床诊断的相符性」。落到引擎上，它与
§1 其余各项有一个根本区别：**这一条判不了**。

判定「硝酸甘油用于高血压是否相符」需要药品说明书里的适应症（《处方审核规则体系》
§4 审核依据来源之一、§5 末段明写「药品说明书、相互作用配伍知识库是数据资产，
须另行接入，严禁模型凭记忆生成」）。本轮**不接**该知识库——它属厂商资产，
不是靠规则行能补上的。于是这条规则只有两个诚实的结论：

=========================  =========================================================
落地情况                   判定结论
=========================  =========================================================
药品名 / 临床诊断缺任一项    **不可判定**——字段没落地，系统判不了，
                       点名字段 → 进字段缺口台账 → 信息科补接口字段后重判
两个字段都齐               **不可判定**——字段齐备，但实质相符性判断依赖
                       药品说明书知识库，本轮不接入；**转人工复核**
=========================  =========================================================

两个分支都落「不可判定」，但**归因不同**：前者是**接口缺口**（可排期补字段），
后者是**知识库缺口**（补字段也没用，得接知识库或由药师判）。这个区别必须落在
归因文本里，否则信息科会把「知识库缺口」当成「接口少一列」去排期。

**为什么绝不能落「合规」**

字段齐备就判「合规」，等于替药师宣布「这药跟这个诊断相符」——而这一判断
本轮根本没有做过。《处方审核规则体系》§4 写明「药师是处方审核第一责任人；
信息系统可辅助，但系统筛选出的不合理处方及系统不能审核的部分**必须人工审核**」。
把「系统不能审核的部分」判成合规，就是把药师的职责悄悄接过来，这一条红线
本模块不许碰。

**结论不写死在代码里**

字段依赖由规则行在 ``required_fields`` 里显式声明（``drug_name`` + ``clinical_diag``），
本模块不猜「相符性要哪几个字段」；配置错误（依赖没声明全 / 把「字段没落地」
声明成「违规」）一律显式抛 :class:`IndicationConfigurationError`，不降级成
「不可判定」——配置问题伪装成业务缺口，规则体检就永远查不出真因。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from ..models import Conclusion, PrescriptionRow, RuleRow, RuleLayer
from .evidence import build_reason
from .sched import match_rule

# ---------------------------------------------------------------------------
# 口径
# ---------------------------------------------------------------------------

#: 诊断相符性判定所需的字段（医嘱落地列口径，spec.md §2.1）。
#:
#: 药品标识 + 临床诊断——这两样齐了才谈得上「相符」；缺任一样系统判不了。
INDICATION_FIELDS: tuple[str, ...] = ("drug_name", "clinical_diag")

#: 诊断相符性规则的依据出处（《处方审核规则体系》§1 适宜性审核第 2 项）。
INDICATION_BASIS = "处方审核规则体系 §1 适宜性审核第 2 项（处方用药与临床诊断的相符性）"

#: 知识库缺口的归因措辞（本轮不接药品说明书知识库的固定说法）。
KNOWLEDGE_GAP_DETAIL = (
    "药品名与临床诊断均已落地，但处方用药与临床诊断的相符性需药品说明书适应症"
    "（属厂商资产，本轮未接入），系统判不了，须药师人工复核"
)


class IndicationStatus(str, Enum):
    """诊断相符性判定的状态（结论只有「不适用」与「不可判定」两种）。"""

    NOT_APPLICABLE = "不适用"
    FIELD_GAP = "字段缺口"
    KNOWLEDGE_GAP = "知识库缺口"


class IndicationConfigurationError(RuntimeError):
    """规则行写法与诊断相符性口径冲突（**配置错误**，不得降级成「不可判定」）。"""


@dataclass(frozen=True)
class IndicationEvaluation:
    """一次诊断相符性判定的结果。"""

    status: IndicationStatus
    conclusion: Conclusion | None = None
    detail: str = ""
    reason: str = ""
    missing_fields: tuple[str, ...] = ()
    rule_id: str = ""

    @property
    def is_undeterminable(self) -> bool:
        """是否落「不可判定」（系统判不了，转人工）。"""
        return self.conclusion is Conclusion.UNDETERMINABLE

    @property
    def is_field_gap(self) -> bool:
        """是否为字段缺口（可排期补接口字段）。"""
        return self.status is IndicationStatus.FIELD_GAP

    @property
    def is_knowledge_gap(self) -> bool:
        """是否为知识库缺口（补接口字段没用，得接知识库或人工判）。"""
        return self.status is IndicationStatus.KNOWLEDGE_GAP

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照（CLI ``--json`` 输出用）。"""
        return {
            "rule_id": self.rule_id,
            "status": self.status.value,
            "conclusion": self.conclusion.value if self.conclusion else "",
            "detail": self.detail,
            "reason": self.reason,
            "missing_fields": list(self.missing_fields),
        }


# ---------------------------------------------------------------------------
# 规则行识别与配置校验
# ---------------------------------------------------------------------------


def is_indication_rule(rule: RuleRow) -> bool:
    """这条规则是不是诊断相符性规则。

    判据是**规则行显式声明了依赖**（``drug_name`` + ``clinical_diag``）且层次为
    「适宜性」——不是按规则号前缀猜。声明即「没有这两个字段我判不了」，
    这条边界由规则行自己声明，引擎不替它猜。

    排除两类走各自路径的规则：量限规则（``limit_ref``）与词表触发型规则
    （``forbidden_terms``）；皮试规则不声明这两个字段，自然也不落入本族。
    """
    if rule.layer != RuleLayer.APPROPRIATENESS.value:
        return False
    if rule.limit_ref or rule.forbidden_terms:
        return False
    return set(INDICATION_FIELDS) <= set(rule.required_fields)


def validate_indication_rule(rule: RuleRow) -> tuple[str, ...]:
    """校验一条诊断相符性规则行，返回配置问题清单（空清单 = 通过）。

    三条硬约束：

    #. 必须声明 ``drug_name`` + ``clinical_diag`` 两个依赖字段；
    #. ``missing_conclusion`` 必须是「不可判定」——字段没落地时系统判不了，
       落「违规」会把接口缺口伪装成处方不合格；
    #. 不带 ``limit_ref`` / ``forbidden_terms``——量限与词表走各自的判定路径。
    """
    issues: list[str] = []
    if not is_indication_rule(rule):
        issues.append(
            f"规则 {rule.rule_id} 不是诊断相符性规则（须为适宜性层并声明依赖 "
            f"{list(INDICATION_FIELDS)}）"
        )
    if rule.missing_conclusion is not Conclusion.UNDETERMINABLE:
        issues.append(
            f"规则 {rule.rule_id} 的 missing_conclusion 声明为「{rule.missing_conclusion.value}」，"
            "字段没落地只能落「不可判定」（系统判不了，不等于处方不合格）"
        )
    if rule.limit_ref:
        issues.append(
            f"规则 {rule.rule_id} 同时声明了 limit_ref，量限走矩阵路径，不走诊断相符性路径"
        )
    if rule.forbidden_terms:
        issues.append(
            f"规则 {rule.rule_id} 同时声明了 forbidden_terms，词表命中走词表路径，不走诊断相符性路径"
        )
    return tuple(issues)


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def _detail_for_missing(missing: Sequence[str]) -> str:
    """字段没落地时的归因说明（接口缺口，可排期补字段）。"""
    names = "、".join(missing)
    return (
        f"诊断相符性判定所需字段 {names} 未落地（医嘱没填写或接口未回传），"
        "系统判不了，须药师人工审核；字段补齐后可重判"
    )


def evaluate_indication(rule: RuleRow, row: PrescriptionRow) -> IndicationEvaluation:
    """按规则行判定一条医嘱的诊断相符性结论（不可判定 + 不适用）。

    判定链：

    #. **配置校验**：规则行不合诊断相符性口径 → 抛
       :class:`IndicationConfigurationError`（配置错误不降级）；
    #. **适用性**：``applies_when`` 不命中 → 不适用，不产生判定行；
    #. **字段完备性**：依赖字段缺任一项 → 不可判定并点名（接口缺口）；
    #. **知识库缺口**：字段齐备 → 仍不可判定，转人工复核（本轮不做实质判定）。
    """
    issues = validate_indication_rule(rule)
    if issues:
        raise IndicationConfigurationError("；".join(issues))

    schedule = match_rule(rule, row)
    if not schedule.is_candidate:
        return IndicationEvaluation(
            status=IndicationStatus.NOT_APPLICABLE,
            detail=schedule.reason,
            rule_id=rule.rule_id,
        )

    missing = row.missing_fields(rule.required_fields)
    if missing:
        return IndicationEvaluation(
            status=IndicationStatus.FIELD_GAP,
            conclusion=Conclusion.UNDETERMINABLE,
            detail=_detail_for_missing(missing),
            missing_fields=missing,
            rule_id=rule.rule_id,
        )

    return IndicationEvaluation(
        status=IndicationStatus.KNOWLEDGE_GAP,
        conclusion=Conclusion.UNDETERMINABLE,
        detail=KNOWLEDGE_GAP_DETAIL,
        missing_fields=(),
        rule_id=rule.rule_id,
    )


def render_indication_reason(
    evaluation: IndicationEvaluation,
    rule: RuleRow,
    row: PrescriptionRow | None = None,
) -> str:
    """把诊断相符性判定结果组装成归因文本（固定句式 + 隐私约束）。

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


__all__ = [
    "INDICATION_BASIS",
    "INDICATION_FIELDS",
    "KNOWLEDGE_GAP_DETAIL",
    "IndicationConfigurationError",
    "IndicationEvaluation",
    "IndicationStatus",
    "evaluate_indication",
    "is_indication_rule",
    "render_indication_reason",
    "validate_indication_rule",
]
