"""重复给药（《处方审核规则体系》§1 适宜性审核第 5 项）：跨行判定。

**对应源产品能力**：源仓库 ``contrib/database/field_rules/registries.py`` 的
``before_row_created`` / ``before_row_updated``（写入前逐行逐规则求值并产出
可校验结论）——本轮把「逐行」扩成「逐行 × 同组」：判定键从行内字段扩成
**跨行聚合键**（同一次就诊 · 同一个药 · 同一天）。

**依据**：§1 适宜性审核七项第 5 项「是否有重复给药现象」。官方案例 rx-005
（同一患者同日两张氨氯地平 5mg qd）应拦截。

**重复的判定键**

==================  ==========================================================
键的组成             缺了就判不了的原因
==================  ==========================================================
``visit_no``        分不清是不是**同一次就诊**
``drug_code``       分不清是不是**同一个药品**
``event_time`` 的   分不清是不是**同一天**（同日不同天算两次开立）
**日期部分**
==================  ==========================================================

三样缺一即落「不可判定」并**点名该字段**，进字段补齐顺序表——
而不是猜一个「大概重复」。这正是本轮相对二元审方结论的核心增量：
「审不了」要能说清卡在哪个字段上。

**同日不同药不误判、跨日不误判**：键里三样都要一样才算重复。

**首张不算重复**：同一组里的第一张是正常开立，后续几张才是重复给药。
组内按（事件时间、处方号）定先后，保证同一批数据的判定结果可复现——
台账里同一张处方两次跑出不同结论，是不可接受的。

**规则行怎么写**：本条规则的 ``expression`` 声明「判定前提所依赖的字段」，
载入期校验要求三个键字段都被显式引用（见 ``rules/schema.py`` 的
``_duplicate_rule_issues``）。理由与 §3 量限规则同源：**跨行聚合口径用行内
标量表达式表达不了**（表达式看不到同组的其他行），阈值与口径由本模块给，
规则行负责声明依赖，两者对不上就是配置错误，载入期即拒。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from ..expression.normalize import CHINA_STANDARD_TIME, normalize_field
from ..models import Conclusion, PrescriptionRow, RuleLayer, RuleRow
from .context import BatchContext, order_key, row_identity
from .evidence import build_reason
from .sched import match_rule

# ---------------------------------------------------------------------------
# 口径
# ---------------------------------------------------------------------------

#: 重复给药的判定键字段（医嘱落地列口径，spec.md §2.1）。
DUPLICATE_KEY_FIELDS: tuple[str, ...] = ("visit_no", "drug_code", "event_time")

#: 分组索引的口径标识（同一批数据里只建一次）。
DUPLICATE_GROUP_TAG = "duplicate-administration"

#: 重复给药规则的依据出处（《处方审核规则体系》§1 适宜性审核第 5 项）。
DUPLICATE_BASIS = "处方审核规则体系 §1 适宜性审核第 5 项（是否有重复给药现象）"


class DuplicateStatus(str, Enum):
    """重复给药判定结论（与三态判定内核一致，多一个「不适用」）。"""

    NOT_APPLICABLE = "不适用"
    COMPLIANT = "合规"
    VIOLATION = "违规"
    UNDETERMINABLE = "不可判定"


#: 重复给药结论 → 判定内核的三态结论。
DUPLICATE_CONCLUSIONS: Mapping[DuplicateStatus, Conclusion] = {
    DuplicateStatus.COMPLIANT: Conclusion.COMPLIANT,
    DuplicateStatus.VIOLATION: Conclusion.VIOLATION,
    DuplicateStatus.UNDETERMINABLE: Conclusion.UNDETERMINABLE,
}


class DuplicateConfigurationError(RuntimeError):
    """规则行写法与重复给药口径冲突（**配置错误**，不得降级成「不可判定」）。

    与「数据缺字段」严格分列：规则行写错时若静默降级成「不可判定」，
    就会把一条配置问题伪装成业务上的「审不了」，规则体检永远查不出真因。
    """


# ---------------------------------------------------------------------------
# 判定键
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateKey:
    """重复给药的判定键：同一次就诊 · 同一个药品 · 同一天。"""

    visit_no: str
    drug_code: str
    event_date: str

    @property
    def text(self) -> str:
        """键的文本形态（分组索引用）。"""
        return f"{self.visit_no}｜{self.drug_code}｜{self.event_date}"

    def describe(self) -> str:
        """人读的键描述（归因文本回显用）。"""
        return f"同一就诊 {self.visit_no} · 同一药品 {self.drug_code} · 同一日 {self.event_date}"


def event_date_part(row: PrescriptionRow) -> str:
    """事件时间的**日期部分**（``YYYY-MM-DD``）；读不出返回空串。

    取的是**本地日历日**（中国标准时间），不是 UTC 日期——落地文件写
    ``2026-10-28 07:30`` 属 10 月 28 日，若按 UTC 折算会落到 10 月 27 日，
    同一天的两张处方就会被判成「跨日不重复」。口径写在这里，不藏。
    """
    raw = row.value("event_time").strip()
    if not raw:
        return ""
    normalized = normalize_field("event_time", raw)
    if normalized.timestamp is None:
        return ""
    return normalized.timestamp.astimezone(CHINA_STANDARD_TIME).date().isoformat()


def duplicate_key(row: PrescriptionRow) -> DuplicateKey | None:
    """算一条医嘱的重复给药判定键；键不全返回 ``None``。

    返回 ``None`` 的行由判定层落「不可判定」并点名缺哪个字段——
    键不全的行**不进任何分组**，免得半个键把不相干的处方凑成一组。
    """
    visit_no = row.value("visit_no").strip()
    drug_code = row.value("drug_code").strip()
    if not visit_no or not drug_code:
        return None
    event_date = event_date_part(row)
    if not event_date:
        return None
    return DuplicateKey(visit_no=visit_no, drug_code=drug_code, event_date=event_date)


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateEvaluation:
    """一次重复给药判定的结果。"""

    status: DuplicateStatus
    conclusion: Conclusion | None = None
    detail: str = ""
    missing_fields: tuple[str, ...] = ()
    key: DuplicateKey | None = None
    group_size: int = 0
    earlier_prescriptions: tuple[str, ...] = ()
    later_prescriptions: tuple[str, ...] = ()
    rule_id: str = ""

    @property
    def is_duplicate(self) -> bool:
        """是否落「违规」（本张是同组的重复给药）。"""
        return self.status is DuplicateStatus.VIOLATION

    @property
    def is_undeterminable(self) -> bool:
        """是否落「不可判定」（键字段不全，判不了）。"""
        return self.status is DuplicateStatus.UNDETERMINABLE

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照（CLI ``--json`` 输出用）。"""
        return {
            "rule_id": self.rule_id,
            "status": self.status.value,
            "conclusion": self.conclusion.value if self.conclusion else "",
            "detail": self.detail,
            "missing_fields": list(self.missing_fields),
            "key": self.key.text if self.key else "",
            "group_size": self.group_size,
            "earlier_prescriptions": list(self.earlier_prescriptions),
            "later_prescriptions": list(self.later_prescriptions),
        }


# ---------------------------------------------------------------------------
# 规则行识别与配置校验
# ---------------------------------------------------------------------------


def is_duplicate_rule(rule: RuleRow) -> bool:
    """这条规则是不是「重复给药」规则。

    判据是**规则行显式声明了三个键字段**——不是按规则号前缀猜。
    声明即「没有这些字段我判不了」，这条边界由规则行自己声明，引擎不替它猜。
    """
    if rule.layer != RuleLayer.APPROPRIATENESS.value:
        return False
    return all(name in rule.required_fields for name in DUPLICATE_KEY_FIELDS)


def validate_duplicate_rule(rule: RuleRow) -> tuple[str, ...]:
    """校验一条重复给药规则行，返回配置问题清单（空清单 = 通过）。

    四条硬约束：

    #. 层次必须是「适宜性」——重复给药属 §1 适宜性审核第 5 项；
    #. 必须声明全部三个键字段——少一个都判不了；
    #. ``expression`` 必须显式引用三个键字段——判定前提写在规则行里，
       载入期就核对，不靠代码里的隐含约定；
    #. ``missing_conclusion`` 必须是「不可判定」——键字段没落地时系统判不了，
       落「违规」会把接口缺口伪装成处方不合格。
    """
    issues: list[str] = []
    if rule.layer != RuleLayer.APPROPRIATENESS.value:
        issues.append(
            f"规则 {rule.rule_id} 的层次是「{rule.layer}」，"
            "重复给药属适宜性审核（§1 第 5 项），层次须为「适宜性」"
        )
    missing = [name for name in DUPLICATE_KEY_FIELDS if name not in rule.required_fields]
    if missing:
        issues.append(
            f"规则 {rule.rule_id} 未在 required_fields 里声明键字段 {missing}，"
            "重复给药判定键须同时声明 visit_no / drug_code / event_time"
        )
    unreferenced = [name for name in DUPLICATE_KEY_FIELDS if name not in rule.expression]
    if unreferenced:
        issues.append(
            f"规则 {rule.rule_id} 的 expression 未引用键字段 {unreferenced}，"
            "判定前提须在规则行里写明（跨行聚合口径不由表达式给，"
            "但依赖字段必须由规则行显式声明）"
        )
    if rule.missing_conclusion is not Conclusion.UNDETERMINABLE:
        issues.append(
            f"规则 {rule.rule_id} 的 missing_conclusion 声明为「{rule.missing_conclusion.value}」，"
            "键字段未落地只能落「不可判定」（系统判不了，不等于处方不合格）"
        )
    return tuple(issues)


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def duplicate_groups(
    context: BatchContext,
) -> dict[str, tuple[PrescriptionRow, ...]]:
    """按判定键分组（键不全的行不进任何组）。"""
    return context.group_by(
        DUPLICATE_GROUP_TAG,
        lambda row: (key.text if (key := duplicate_key(row)) is not None else None),
    )


def _group_for(context: BatchContext, key: DuplicateKey) -> tuple[PrescriptionRow, ...]:
    """取某个判定键对应的组内医嘱行（按事件时间、处方号定先后）。"""
    group = duplicate_groups(context).get(key.text, ())
    return tuple(sorted(group, key=order_key))


def evaluate_duplicate(
    rule: RuleRow,
    row: PrescriptionRow,
    *,
    context: BatchContext | None = None,
) -> DuplicateEvaluation:
    """按规则行判定一条医嘱是否属重复给药（三态 + 不适用）。

    判定链：

    #. **配置校验**：规则行不合重复给药口径 → 抛
       :class:`DuplicateConfigurationError`（配置错误不降级）；
    #. **适用性**：``applies_when`` 不命中 → 不适用，不产生判定行；
    #. **键字段缺失**：``visit_no`` / ``drug_code`` / ``event_time`` 任一为空 →
       不可判定并点名该字段（接口缺口，进字段补齐顺序表）；
    #. **日期读不出**：``event_time`` 不是时间文本 → 不可判定并点名
       ``event_time``（口径不可比）；
    #. **分组**：同键组只有本行 → 合规；本行是组内首张 → 合规；
       本行不是首张 → 违规（重复给药），归因文本点名同组更早的处方号。
    """
    issues = validate_duplicate_rule(rule)
    if issues:
        raise DuplicateConfigurationError("；".join(issues))

    schedule = match_rule(rule, row)
    if not schedule.is_candidate:
        return DuplicateEvaluation(
            status=DuplicateStatus.NOT_APPLICABLE,
            detail=schedule.reason,
            rule_id=rule.rule_id,
        )

    missing = row.missing_fields(DUPLICATE_KEY_FIELDS)
    if missing:
        names = "、".join(missing)
        return DuplicateEvaluation(
            status=DuplicateStatus.UNDETERMINABLE,
            conclusion=DUPLICATE_CONCLUSIONS[DuplicateStatus.UNDETERMINABLE],
            detail=(
                f"重复给药判定键字段 {names} 未落地，"
                "系统判不了这一张处方是否与同次就诊的同一药品重复，"
                "须信息科补齐该字段后重判"
            ),
            missing_fields=missing,
            rule_id=rule.rule_id,
        )

    key = duplicate_key(row)
    if key is None:
        return DuplicateEvaluation(
            status=DuplicateStatus.UNDETERMINABLE,
            conclusion=DUPLICATE_CONCLUSIONS[DuplicateStatus.UNDETERMINABLE],
            detail=(
                f"事件时间「{row.value('event_time').strip()}」读不出日期部分，"
                "判不了是否与同日处方重复，口径不可比。"
                "须信息科按时间口径补齐 event_time 后重判"
            ),
            missing_fields=("event_time",),
            rule_id=rule.rule_id,
        )

    group = _group_for(context or BatchContext(), key)
    identity = row_identity(row)
    position = next(
        (index for index, item in enumerate(group) if row_identity(item) == identity),
        None,
    )
    if position is None:
        # 本行不在上下文里（单行判定等场景）：按组内仅本行处理，
        # 结论只说「本行可见范围内未发现重复」，不替别的批次下判断。
        group = (row,)
        position = 0

    earlier = tuple(row_identity(item) for item in group[:position])
    later = tuple(row_identity(item) for item in group[position + 1 :])
    size = len(group)

    if size == 1:
        return DuplicateEvaluation(
            status=DuplicateStatus.COMPLIANT,
            conclusion=DUPLICATE_CONCLUSIONS[DuplicateStatus.COMPLIANT],
            detail=(
                f"本批内未发现{key.describe()}的重复给药，"
                "同一次就诊的同一药品在本批内只开立了本张。"
            ),
            key=key,
            group_size=size,
            rule_id=rule.rule_id,
        )

    if position == 0:
        return DuplicateEvaluation(
            status=DuplicateStatus.COMPLIANT,
            conclusion=DUPLICATE_CONCLUSIONS[DuplicateStatus.COMPLIANT],
            detail=(
                f"{key.describe()}在本批内共开立 {size} 张，"
                f"本张为组内首张（其余 {size - 1} 张落在重复给药结论上："
                f"{'、'.join(later)}）。"
            ),
            key=key,
            group_size=size,
            later_prescriptions=later,
            rule_id=rule.rule_id,
        )

    return DuplicateEvaluation(
        status=DuplicateStatus.VIOLATION,
        conclusion=DUPLICATE_CONCLUSIONS[DuplicateStatus.VIOLATION],
        detail=(
            f"{key.describe()}在本批内共开立 {size} 张，"
            f"本张为组内第 {position + 1} 张，与同组已开立的 "
            f"{'、'.join(earlier)} 构成重复给药，须退回医师合并。"
        ),
        key=key,
        group_size=size,
        earlier_prescriptions=earlier,
        later_prescriptions=later,
        rule_id=rule.rule_id,
    )


def render_duplicate_reason(
    evaluation: DuplicateEvaluation,
    rule: RuleRow,
    row: PrescriptionRow | None = None,
) -> str:
    """把重复给药判定结果组装成归因文本（固定句式 + 隐私约束）。"""
    if evaluation.conclusion is None:
        return evaluation.detail
    return build_reason(
        evaluation.conclusion,
        rule,
        detail=evaluation.detail,
        missing_fields=evaluation.missing_fields,
        fields=row,
        condition="同一就诊 · 同一药品 · 同日的重复给药核查",
    )


__all__ = [
    "DUPLICATE_BASIS",
    "DUPLICATE_CONCLUSIONS",
    "DUPLICATE_GROUP_TAG",
    "DUPLICATE_KEY_FIELDS",
    "DuplicateConfigurationError",
    "DuplicateEvaluation",
    "DuplicateKey",
    "DuplicateStatus",
    "duplicate_groups",
    "duplicate_key",
    "evaluate_duplicate",
    "event_date_part",
    "is_duplicate_rule",
    "order_key",
    "render_duplicate_reason",
    "row_identity",
    "validate_duplicate_rule",
]
