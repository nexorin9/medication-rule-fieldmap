"""规范性审核 —— 儿童实际年龄与婴幼儿日 / 月龄口径。

**对应源产品能力**：源仓库 ``contrib/database/field_rules/registries.py`` 的
``before_row_created`` / ``before_row_updated``（写入前逐行逐规则求值）与
``FieldRuleValidity.error_text``（归因位）——本轮把源产品的二值 ``is_valid``
扩成三态，并把**年龄口径**这条路径从通用表达式求值里单独拆出来。

**依据**：《处方审核规则体系》§1 规范性审核原文——「儿童处方写实际年龄，
婴幼儿写日/月龄或体重」。

**为什么年龄口径要单列一册**
「年龄」这一栏在医嘱落地文件里是两个字段（``age_value`` + ``age_unit``），
而判定要看的是**数值与单位合起来的口径**，不是单个标量：

====================  ==========================================  ==========
落地情况              判定                                       结论
====================  ==========================================  ==========
年龄两栏都没填        儿童处方未写实际年龄                        **违规**
年龄数值有、单位没有  判不出是岁 / 月 / 日                         **不可判定**
数值是 0、单位是岁    婴幼儿写成了「0 岁」，口径不对                **违规**
数值 ≥ 1、单位是岁    儿童写了实际年龄                            合规
数值有、单位是月 / 日 婴幼儿已按月龄 / 日龄写明                    合规
====================  ==========================================  ==========

这些分支用标量表达式表达不了（「0 岁」是**有值**而不是缺失，
把 ``age_value == ''`` 当成触发条件就会把 0 岁误判成缺失），所以本册按
分支表执行，结论不依赖通用表达式求值。

**0 岁不是缺失（本册最要紧的一条）**
``age_value`` 写成 ``0`` 是一个**真实的年龄值**（未满 1 周岁），不是「没填」。
判定链里凡是用 ``if not value`` 判缺失的地方都会把 ``"0"`` 当成假值——
本册一律用 ``row.is_missing()``（空串 / 纯空白才算缺失）与数值比较，
``0`` 走的是「婴幼儿写成岁」这一支，结论是违规（口径不对），
**不是**「年龄缺失」，也不是「判不了」。

**儿童人群怎么圈定（本轮的口径边界）**
《处方审核规则体系》§1 没有给出「儿童」的年龄界线，本轮**不自创数字**。
落地文件里也没有患者主数据年龄（``age_value`` / ``age_unit`` 是处方行上的
年龄），「这条处方是不是儿童处方」只能靠**科室口径**近似圈定：规则行的
``applies_when`` 声明开单科室（默认口径 ``dept_name`` 含「儿科」），
药事审核岗按本院科室命名改这一行即可。边界写在这里，不藏：

* 儿科门诊 / 儿科病区的处方 → 进本册判定；
* 儿童患者在综合科室就诊（年龄缺失且科室不含「儿科」）→ **本册不覆盖**，
  该患者年龄没写这一事实不会出现在本规则结论里；
* 若 HIS 落地文件后续补上患者主数据年龄，把规则行的适用条件改成按年龄
  圈定即可，本册的分支表不用动。

**「或体重」这一支本轮没有落地字段**
§1 说婴幼儿可写日/月龄**或体重**。本轮医嘱落地文件（spec.md §2.1）不含体重列，
因此按日龄 / 月龄口径判定；归因文本里写明这一边界。若 HIS 落地文件补上体重列，
本册再加一支即可（体重是数值 + kg 单位，判「已写明」）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ..expression.evaluator import FieldState
from ..expression.normalize import normalize_field
from ..models import Conclusion, PrescriptionRow, RuleLayer, RuleRow
from .evidence import build_reason
from .sched import match_rule

# ---------------------------------------------------------------------------
# 口径常量
# ---------------------------------------------------------------------------

#: 年龄数值列（spec.md §2.1）。
AGE_VALUE_FIELD = "age_value"

#: 年龄单位列（spec.md §2.1：岁 / 月 / 日）。
AGE_UNIT_FIELD = "age_unit"

#: 本册关心的两个医嘱列。
CHILD_AGE_FIELDS: tuple[str, ...] = (AGE_VALUE_FIELD, AGE_UNIT_FIELD)

#: 规范年龄单位（由 ``expression/normalize.py`` 的单位别名表归一得到）。
AGE_UNIT_YEAR = "岁"
AGE_UNIT_MONTH = "月"
AGE_UNIT_DAY = "日"
AGE_UNIT_WEEK = "周"

#: 已写明年龄口径的单位（§1「日/月龄」；周龄与日龄同属未满一月的口径）。
AGE_UNITS_ACCEPTED: frozenset[str] = frozenset(
    {AGE_UNIT_MONTH, AGE_UNIT_DAY, AGE_UNIT_WEEK}
)

#: 婴幼儿（未满 1 周岁）在「岁」口径下的数值上界（不含）。
#:
#: 口径来自 §1「婴幼儿写日/月龄或体重」：写成「岁」且不足 1 周岁即属
#: 婴幼儿用了成人口径。只取 1 这一档边界（不引入「儿童＝几周岁」的数字——
#: §1 未明列，本轮不自创）。
INFANT_YEAR_THRESHOLD = Decimal(1)


class ChildAgeConfigurationError(RuntimeError):
    """规则行写法与儿童年龄口径冲突（**配置错误**，不得降级成「不可判定」）。"""


class ChildAgeStatus(str, Enum):
    """儿童年龄口径判定的结论（与三态判定内核一致，多一个「不适用」）。"""

    NOT_APPLICABLE = "不适用"
    COMPLIANT = "合规"
    VIOLATION = "违规"
    UNDETERMINABLE = "不可判定"


#: 儿童年龄结论 → 判定内核的三态结论。
CHILD_AGE_CONCLUSIONS: dict[ChildAgeStatus, Conclusion] = {
    ChildAgeStatus.COMPLIANT: Conclusion.COMPLIANT,
    ChildAgeStatus.VIOLATION: Conclusion.VIOLATION,
    ChildAgeStatus.UNDETERMINABLE: Conclusion.UNDETERMINABLE,
}


# ---------------------------------------------------------------------------
# 年龄读取
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgeReading:
    """一条医嘱行上的年龄读取结果（数值 + 单位的合口径）。

    ``number`` 是 ``None`` 表示数值读不出来（写成「三」「不详」等）——
    这与「没填」是两回事：前者是口径不可辨（不可判定），后者是缺项。
    """

    value_raw: str = ""
    unit_raw: str = ""
    unit: str = ""
    number: Decimal | None = None
    value_state: FieldState = FieldState.MISSING
    unit_state: FieldState = FieldState.MISSING

    @property
    def value_missing(self) -> bool:
        """年龄数值缺失（空串 / 纯空白）。"""
        return self.value_state is FieldState.MISSING

    @property
    def unit_missing(self) -> bool:
        """年龄单位缺失（空串 / 纯空白）。"""
        return self.unit_state is FieldState.MISSING

    @property
    def number_readable(self) -> bool:
        """年龄数值能读成数字。"""
        return self.number is not None

    @property
    def is_zero(self) -> bool:
        """年龄数值是 0——**有值**，不是缺失。"""
        return self.number is not None and self.number == 0

    def describe(self) -> str:
        """人读的年龄描述（归因文本里回显用）。"""
        if self.value_missing and self.unit_missing:
            return "年龄未填写"
        if self.value_missing:
            return f"年龄单位「{self.unit_raw}」已填但数值为空"
        if self.unit_missing:
            return f"年龄数值「{self.value_raw}」已填但单位为空"
        if self.number is None:
            return f"年龄「{self.value_raw}{self.unit_raw}」数值读不出来"
        return f"年龄「{self.value_raw}{self.unit}」"


def read_age(row: PrescriptionRow) -> AgeReading:
    """读一条医嘱行上的年龄（数值 + 单位），不做任何猜测。

    归一口径复用 ``expression/normalize.py``：单位别名（周岁 / 月龄 / 天 …）
    归成规范单位；数值读不出就留 ``None``，绝不静默当成 0。
    """
    value_raw = row.value(AGE_VALUE_FIELD)
    unit_raw = row.value(AGE_UNIT_FIELD)
    value = normalize_field(AGE_VALUE_FIELD, value_raw)
    unit = normalize_field(AGE_UNIT_FIELD, unit_raw)
    return AgeReading(
        value_raw=value_raw,
        unit_raw=unit_raw,
        unit=unit.text if not unit.is_missing else "",
        number=value.number,
        value_state=value.state,
        unit_state=unit.state,
    )


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChildAgeEvaluation:
    """一次儿童年龄口径判定的结果。"""

    status: ChildAgeStatus
    conclusion: Conclusion | None = None
    detail: str = ""
    missing_fields: tuple[str, ...] = ()
    empty_fields: tuple[str, ...] = ()
    age: AgeReading = AgeReading()
    rule_id: str = ""

    @property
    def is_undeterminable(self) -> bool:
        """是否落「不可判定」（口径不可辨 / 接口缺列）。"""
        return self.status is ChildAgeStatus.UNDETERMINABLE

    @property
    def is_violation(self) -> bool:
        """是否落「违规」（年龄口径不合规，退回医师补写）。"""
        return self.status is ChildAgeStatus.VIOLATION

    @property
    def named_fields(self) -> tuple[str, ...]:
        """本条结论点名的医嘱列（缺列 + 空值，按判定顺序）。"""
        return self.missing_fields + self.empty_fields

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照（CLI ``--json`` 输出用）。"""
        return {
            "rule_id": self.rule_id,
            "status": self.status.value,
            "conclusion": self.conclusion.value if self.conclusion else "",
            "detail": self.detail,
            "missing_fields": list(self.missing_fields),
            "empty_fields": list(self.empty_fields),
            "age_value": self.age.value_raw,
            "age_unit": self.age.unit,
        }


# ---------------------------------------------------------------------------
# 规则行识别与配置校验
# ---------------------------------------------------------------------------


def is_child_age_rule(rule: RuleRow) -> bool:
    """这条规则是不是「儿童实际年龄」类规则。

    判据：层次是「规范性」，且声明了 ``age_value`` 与 ``age_unit`` 两个年龄列。
    不按规则号前缀猜。
    """
    if rule.layer != RuleLayer.NORMATIVITY.value:
        return False
    return all(name in rule.required_fields for name in CHILD_AGE_FIELDS)


def validate_child_age_rule(rule: RuleRow) -> tuple[str, ...]:
    """校验一条儿童年龄规则，返回配置问题清单（空清单 = 通过）。

    三条硬约束：

    #. 必须同时声明 ``age_value`` 与 ``age_unit``——年龄是数值 + 单位的合口径，
       少一个都判不了；
    #. 不得混入前记 / 后记栏位（``clinical_diag`` / ``prescriber_id`` 等）——
       儿童年龄与前记缺项是两种处置，必须各占一条规则，药师才看得出退给谁；
    #. 依赖字段只允许是年龄两列（多声明别的字段说明规则写串了）。
    """
    issues: list[str] = []
    missing = [name for name in CHILD_AGE_FIELDS if name not in rule.required_fields]
    if missing:
        issues.append(
            f"规则 {rule.rule_id} 未声明年龄字段 {missing}，"
            "儿童年龄口径须同时声明 age_value 与 age_unit"
        )
    extra = [
        name for name in rule.required_fields if name not in CHILD_AGE_FIELDS
    ]
    if extra:
        issues.append(
            f"规则 {rule.rule_id} 声明了年龄两列以外的依赖字段 {extra}，"
            "儿童年龄规则只核年龄口径，其余字段请另立规则"
        )
    return tuple(issues)


def child_age_condition_text(rule: RuleRow) -> str:
    """人读的「判定条件」措辞（替换归因文本里的阈值表达式那一格）。"""
    return "儿童实际年龄与婴幼儿日 / 月龄口径核查"


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def evaluate_child_age(
    rule: RuleRow, row: PrescriptionRow
) -> ChildAgeEvaluation:
    """按规则行判定一条医嘱的儿童年龄口径结论（三态 + 不适用）。

    判定链（分支表见模块文档）：

    #. **配置校验**：规则行不合儿童年龄口径 → 抛
       :class:`ChildAgeConfigurationError`（配置错误不降级）；
    #. **适用性**：``applies_when`` 不命中 → 不适用，不产生判定行；
    #. **年龄两列整列未落地** → 不可判定（接口缺口，进字段对照台账）；
    #. **两栏都没填** → 按规则行声明的缺项结论（样例规则为违规：儿童处方
       未写实际年龄即不合格处方）；
    #. **数值有、单位空** → 不可判定（口径不可辨，点名 ``age_unit``）；
    #. **单位是岁、数值不足 1 周岁** → 违规（婴幼儿写成了「岁」）；
    #. 其余（岁且 ≥1 / 月 / 日 / 周） → 合规。
    """
    issues = validate_child_age_rule(rule)
    if issues:
        raise ChildAgeConfigurationError("；".join(issues))

    schedule = match_rule(rule, row)
    if not schedule.is_candidate:
        return ChildAgeEvaluation(
            status=ChildAgeStatus.NOT_APPLICABLE,
            detail=schedule.reason,
            rule_id=rule.rule_id,
        )

    age = read_age(row)

    missing = tuple(name for name in CHILD_AGE_FIELDS if not row.has_column(name))
    if missing:
        detail = (
            "；".join(
                f"字段 {name} 在本批落地记录里整列未落地（接口未回传）" for name in missing
            )
            + "。系统判不了该处方的年龄口径，须信息科补齐年龄字段后重判。"
        )
        return ChildAgeEvaluation(
            status=ChildAgeStatus.UNDETERMINABLE,
            conclusion=CHILD_AGE_CONCLUSIONS[ChildAgeStatus.UNDETERMINABLE],
            detail=detail,
            missing_fields=missing,
            age=age,
            rule_id=rule.rule_id,
        )

    if age.value_missing and age.unit_missing:
        # 两栏都没填：缺实际年龄。结论由规则行声明（样例规则为违规——
        # 儿童处方未写实际年龄即不合格处方，退回医师补写）。
        conclusion = rule.missing_conclusion
        status = (
            ChildAgeStatus.VIOLATION
            if conclusion is Conclusion.VIOLATION
            else ChildAgeStatus.UNDETERMINABLE
        )
        detail = (
            "儿童处方未写实际年龄（字段 age_value 栏位在但内容为空、"
            "字段 age_unit 栏位在但内容为空）。须退回医师补写实际年龄；"
            "婴幼儿按日龄 / 月龄写明。"
        )
        return ChildAgeEvaluation(
            status=status,
            conclusion=conclusion,
            detail=detail,
            empty_fields=CHILD_AGE_FIELDS,
            age=age,
            rule_id=rule.rule_id,
        )

    if age.unit_missing:
        # 数值有、单位空：判不出是岁 / 月 / 日——口径不可辨。
        return ChildAgeEvaluation(
            status=ChildAgeStatus.UNDETERMINABLE,
            conclusion=CHILD_AGE_CONCLUSIONS[ChildAgeStatus.UNDETERMINABLE],
            detail=(
                f"年龄数值「{age.value_raw}」已填但字段 age_unit 为空，"
                "判不出是周岁还是月龄 / 日龄，口径不可比。"
                "须信息科补齐 age_unit 后重判。"
            ),
            missing_fields=(AGE_UNIT_FIELD,),
            age=age,
            rule_id=rule.rule_id,
        )

    if age.value_missing:
        # 单位有、数值空：单位写了但没写数值，属缺项。
        conclusion = rule.missing_conclusion
        status = (
            ChildAgeStatus.VIOLATION
            if conclusion is Conclusion.VIOLATION
            else ChildAgeStatus.UNDETERMINABLE
        )
        detail = (
            f"年龄单位「{age.unit_raw}」已填但字段 age_value 的数值为空，"
            "年龄未写明。须退回医师补写实际年龄。"
        )
        return ChildAgeEvaluation(
            status=status,
            conclusion=conclusion,
            detail=detail,
            empty_fields=(AGE_VALUE_FIELD,),
            age=age,
            rule_id=rule.rule_id,
        )

    if not age.number_readable:
        return ChildAgeEvaluation(
            status=ChildAgeStatus.UNDETERMINABLE,
            conclusion=CHILD_AGE_CONCLUSIONS[ChildAgeStatus.UNDETERMINABLE],
            detail=(
                f"年龄「{age.value_raw}{age.unit_raw}」的数值读不出来，口径不可比。"
                "须信息科按数值口径补齐 age_value 后重判。"
            ),
            missing_fields=(AGE_VALUE_FIELD,),
            age=age,
            rule_id=rule.rule_id,
        )

    if age.unit == AGE_UNIT_YEAR:
        if age.number < INFANT_YEAR_THRESHOLD:
            # 婴幼儿写成了「岁」：0 岁是有值的年龄，不是缺失——但口径不对。
            return ChildAgeEvaluation(
                status=ChildAgeStatus.VIOLATION,
                conclusion=CHILD_AGE_CONCLUSIONS[ChildAgeStatus.VIOLATION],
                detail=(
                    f"年龄已填写为「{age.value_raw}{age.unit}」，"
                    "属未满 1 周岁的婴幼儿，却写成了周岁口径。"
                    "婴幼儿处方应写日龄 / 月龄（或体重），须退回医师改写。"
                ),
                age=age,
                rule_id=rule.rule_id,
            )
        return ChildAgeEvaluation(
            status=ChildAgeStatus.COMPLIANT,
            conclusion=CHILD_AGE_CONCLUSIONS[ChildAgeStatus.COMPLIANT],
            detail=(
                f"儿童处方已写实际年龄「{age.value_raw}{age.unit}」，"
                "符合「儿童处方写实际年龄」的要求。"
            ),
            age=age,
            rule_id=rule.rule_id,
        )

    if age.unit in AGE_UNITS_ACCEPTED:
        return ChildAgeEvaluation(
            status=ChildAgeStatus.COMPLIANT,
            conclusion=CHILD_AGE_CONCLUSIONS[ChildAgeStatus.COMPLIANT],
            detail=(
                f"婴幼儿处方已按「{age.unit}」龄写明年龄「{age.value_raw}{age.unit}」，"
                "符合「婴幼儿写日 / 月龄」的要求。"
            ),
            age=age,
            rule_id=rule.rule_id,
        )

    return ChildAgeEvaluation(
        status=ChildAgeStatus.UNDETERMINABLE,
        conclusion=CHILD_AGE_CONCLUSIONS[ChildAgeStatus.UNDETERMINABLE],
        detail=(
            f"年龄单位「{age.unit_raw}」不在可判定的口径内（岁 / 月 / 日 / 周），"
            "口径不可比。须信息科补齐 age_unit 后重判"
        ),
        missing_fields=(AGE_UNIT_FIELD,),
        age=age,
        rule_id=rule.rule_id,
    )


def render_child_age_reason(
    evaluation: ChildAgeEvaluation,
    rule: RuleRow,
    row: PrescriptionRow | None = None,
) -> str:
    """把儿童年龄判定结果组装成归因文本（固定句式 + 隐私约束）。"""
    if evaluation.conclusion is None:
        return evaluation.detail
    return build_reason(
        evaluation.conclusion,
        rule,
        detail=evaluation.detail,
        missing_fields=evaluation.named_fields,
        fields=row,
        condition=child_age_condition_text(rule),
    )


__all__ = [
    "AGE_UNITS_ACCEPTED",
    "AGE_UNIT_DAY",
    "AGE_UNIT_FIELD",
    "AGE_UNIT_MONTH",
    "AGE_UNIT_WEEK",
    "AGE_UNIT_YEAR",
    "AGE_VALUE_FIELD",
    "AgeReading",
    "CHILD_AGE_CONCLUSIONS",
    "CHILD_AGE_FIELDS",
    "ChildAgeConfigurationError",
    "ChildAgeEvaluation",
    "ChildAgeStatus",
    "INFANT_YEAR_THRESHOLD",
    "child_age_condition_text",
    "evaluate_child_age",
    "is_child_age_rule",
    "read_age",
    "render_child_age_reason",
    "validate_child_age_rule",
]
