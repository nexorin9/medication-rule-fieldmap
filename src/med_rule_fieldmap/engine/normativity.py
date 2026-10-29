"""规范性审核 —— 前记缺项与后记签章缺项。

**对应源产品能力**：源仓库 ``contrib/database/field_rules/registries.py`` 的
``before_row_created`` / ``before_row_updated``（写入前逐行逐规则求值）与
``FieldRuleValidity.error_text``（归因位）——本轮把源产品的二值 ``is_valid``
扩成三态，并把**规范性缺项**这条路径从通用表达式求值里单独拆出来。

**依据**：《处方审核规则体系》§1 规范性审核原文——「前记（机构、患者身份、科别、
临床诊断、日期）、正文（药品名称、剂型、数量、用法用量）、后记（签章）完整清晰；
用法用量表述清楚，不得用『遵医嘱』『自用』含糊字句；儿童处方写实际年龄，婴幼儿
写日/月龄或体重」。

本模块管两段：

* **前记与后记**——缺项判定（缺项与空值分流，见下）；
* **正文段的用法用量含糊字句**——词表命中即违规并回显命中的字句。

正文段的**儿童实际年龄**分支另立 ``engine/children_age.py``（判定内核 6/10）：
年龄口径的分支（缺实际年龄 / 口径不可辨 / 婴幼儿写成岁 / 已按日·月龄写明）
与含糊字句查表是两类判定，混在一个模块里会让归因文本互相干扰。

**规范性缺项为什么单列一册**
通用判定链里，「依赖字段没落地」一律落「不可判定」（系统判不了，等接口补字段）。
规范性缺项不适用这条口径：缺前记往往是**不合格处方**，药师要退回医师补写，
不是等信息科改接口。因此「栏位在但没填」的结论由规则行显式声明
（``missing_conclusion: 违规`` = 不合格处方、退回医师补写；``不可判定`` =
进字段对照台账、等接口补齐），本模块据此落结论，并**点名缺哪一项**。

**缺项与空值必须分开（本模块最要紧的一条）**

同样是「这一栏没有值」，有两种完全不同的成因，结论与处置也不同：

===========================  ==================  ==========================
落地情况                      判定结论             处置
===========================  ==================  ==========================
**栏位在，内容为空**          **违规**              处方本身不合格 → 药师退回医师补写
**整列未落地（接口没回传）**   **不可判定**          系统判不了 → 信息科补接口字段后重判
===========================  ==================  ==========================

前者是 HIS 接口的字段缺口——医嘱落地文件里根本没有「临床诊断」这一列，
说明接口没回传，系统无从判起；后者是医师没填——栏位在，值是空的，
处方笺确实不完整，归药学部判。这条区分是本轮 10x 楔子
（按缺失字段聚合的字段对照表）的地基：只有分清「接口缺列」与「医师漏填」，
字段补齐顺序表才不会把「医师没填」当成「接口没接」排到信息科的改造清单上。

落地记录里哪些列真实存在，由入站层登记（``PrescriptionRow.present_columns``）：
CSV 按表头名登记、HL7 按通道覆盖的字段登记；未登记时一律视为全部存在
（手工构造的行保持旧行为）。

**结论不写死在代码里**
本模块只做三件事：把规则行声明的依赖字段映射到前记 / 后记栏位、区分缺列与空值、
把结论与归因文本组装出来。结论由规则行的 ``missing_conclusion`` 声明，
换一条规则改配置即可，不改代码。

**后记只判缺项，不判签章真伪**
本轮只核「签章栏位有没有落地」，**不判断签名真伪、不判断签章人与开方医师是否一致**
——那属电子签名与身份核验，是 HIS 与 CA 侧的职责。药师签章也不在医嘱落地文件的
口径内（签章发生在审方复核与调配环节，属回写侧字段），本模块据此把
「后记签章」核到开立医师签章标识这一支，并在归因文本里写明这一边界。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ..models import Conclusion, PrescriptionRow, RuleLayer, RuleRow
from .evidence import build_reason
from .sched import match_rule

# ---------------------------------------------------------------------------
# 栏位口径
# ---------------------------------------------------------------------------


class NormativityStatus(str, Enum):
    """规范性缺项判定的结论（与三态判定内核一致，多一个「不适用」）。"""

    NOT_APPLICABLE = "不适用"
    COMPLIANT = "合规"
    VIOLATION = "违规"
    UNDETERMINABLE = "不可判定"


#: 规范性结论 → 判定内核的三态结论。
NORMATIVITY_CONCLUSIONS: Mapping[NormativityStatus, Conclusion] = {
    NormativityStatus.COMPLIANT: Conclusion.COMPLIANT,
    NormativityStatus.VIOLATION: Conclusion.VIOLATION,
    NormativityStatus.UNDETERMINABLE: Conclusion.UNDETERMINABLE,
}


class NormativityConfigurationError(RuntimeError):
    """规则行写法与规范性口径冲突（**配置错误**，不得降级成「不可判定」）。

    与「数据缺字段」严格分列：规则行写错时若静默降级，就会把一条配置问题
    伪装成业务上的「审不了」，规则体检永远查不出真因。
    """


@dataclass(frozen=True)
class NormativityItem:
    """前记 / 后记的一项（栏位口径）。

    ``key`` 是段落名（"前记" / "后记"），``name`` 是栏位中文名（归因文本里
    点名用），``field`` 是医嘱落地列名（spec.md §2.1）。
    """

    key: str
    name: str
    field: str

    def describe(self) -> str:
        """人读的一项（"前记「临床诊断」"）。"""
        return f"{self.key}「{self.name}」"

    def to_dict(self) -> dict[str, str]:
        """可 JSON 序列化的快照。"""
        return {"key": self.key, "name": self.name, "field": self.field}


#: 前记五项（机构 / 患者身份 / 科别 / 临床诊断 / 日期）。
#:
#: 「机构」落到 ``dept_name``（申请科室名称）：医嘱落地文件是**本院单一机构**的
#: 批次文件，机构本身是批次级常量，行级真正能核的是「开单科室有没有落地」；
#: 「科别」落到 ``dept_code``（科别的机器可读标识）。两栏在 HIS 文件里是各自
#: 独立的列，可以各自缺失，缺失时结论也不同。
FRONT_MATTER_ITEMS: tuple[NormativityItem, ...] = (
    NormativityItem("前记", "机构（开方科室）", "dept_name"),
    NormativityItem("前记", "患者身份", "patient_ref"),
    NormativityItem("前记", "科别", "dept_code"),
    NormativityItem("前记", "临床诊断", "clinical_diag"),
    NormativityItem("前记", "日期", "event_time"),
)

#: 后记签章（只判缺项，不判签章真伪）。
BACK_MATTER_ITEMS: tuple[NormativityItem, ...] = (
    NormativityItem("后记", "签章", "prescriber_id"),
)

#: 前记 + 后记的栏位全集。
NORMATIVITY_ITEMS: tuple[NormativityItem, ...] = FRONT_MATTER_ITEMS + BACK_MATTER_ITEMS

#: 医嘱列名 → 栏位口径（唯一真源）。
NORMATIVITY_ITEM_BY_FIELD: Mapping[str, NormativityItem] = {
    item.field: item for item in NORMATIVITY_ITEMS
}

#: 前记栏位对应的医嘱列。
FRONT_MATTER_FIELDS: tuple[str, ...] = tuple(item.field for item in FRONT_MATTER_ITEMS)

#: 后记栏位对应的医嘱列。
BACK_MATTER_FIELDS: tuple[str, ...] = tuple(item.field for item in BACK_MATTER_ITEMS)


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormativityEvaluation:
    """一次规范性缺项判定的结果。

    ``missing_fields`` 与 ``empty_fields`` 分列两种「没有值」：

    * ``missing_fields``：**整列未落地**（接口没回传这一列）→ 不可判定，
      点名字段进字段对照台账；
    * ``empty_fields``：**栏位在但内容为空**（医师没填）→ 违规，
      点名栏位退回医师补写。
    """

    status: NormativityStatus
    conclusion: Conclusion | None = None
    detail: str = ""
    missing_fields: tuple[str, ...] = ()
    empty_fields: tuple[str, ...] = ()
    items: tuple[NormativityItem, ...] = ()
    rule_id: str = ""

    @property
    def is_undeterminable(self) -> bool:
        """是否落「不可判定」（接口缺列，系统判不了）。"""
        return self.status is NormativityStatus.UNDETERMINABLE

    @property
    def is_violation(self) -> bool:
        """是否落「违规」（栏位在但没填，处方本身不合格）。"""
        return self.status is NormativityStatus.VIOLATION

    @property
    def named_fields(self) -> tuple[str, ...]:
        """本条结论点名的医嘱列（缺列 + 空值，按判定顺序）。"""
        return self.missing_fields + self.empty_fields

    @property
    def named_items(self) -> tuple[NormativityItem, ...]:
        """本条结论点名的栏位（缺列 + 空值，按判定顺序）。"""
        return tuple(
            NORMATIVITY_ITEM_BY_FIELD[name]
            for name in self.named_fields
            if name in NORMATIVITY_ITEM_BY_FIELD
        )

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照（CLI ``--json`` 输出用）。"""
        return {
            "rule_id": self.rule_id,
            "status": self.status.value,
            "conclusion": self.conclusion.value if self.conclusion else "",
            "detail": self.detail,
            "missing_fields": list(self.missing_fields),
            "empty_fields": list(self.empty_fields),
            "items": [item.to_dict() for item in self.items],
        }


# ---------------------------------------------------------------------------
# 规则行识别与配置校验
# ---------------------------------------------------------------------------


def is_normativity_rule(rule: RuleRow) -> bool:
    """这条规则是不是规范性缺项类规则。

    两个判据同时成立才算：

    #. 层次是「规范性」；
    #. 声明的依赖字段全部落在前记 / 后记的栏位口径表内。

    不按规则号前缀猜：规则号是药事审核岗自己起的，靠前缀识别等于把口径
    焊死在命名约定上。

    **「栏位在但没填」的结论由规则行决定**（``missing_conclusion``：违规 =
    不合格处方、退回医师补写；不可判定 = 进字段对照台账、等接口补齐），
    不参与识别——识别只回答「这条规则走哪条判定路径」。
    """
    if rule.layer != RuleLayer.NORMATIVITY.value:
        return False
    if not rule.required_fields:
        return False
    return all(name in NORMATIVITY_ITEM_BY_FIELD for name in rule.required_fields)


def validate_normativity_rule(rule: RuleRow) -> tuple[str, ...]:
    """校验一条规范性缺项类规则，返回配置问题清单（空清单 = 通过）。

    三条硬约束：

    #. 必须声明依赖字段，否则无从判起；
    #. 依赖字段必须落在前记 / 后记栏位口径表内；
    #. **不得跨段混用**——后记签章必须单独成条（前记缺项与后记缺签章是两种
       处置：前者退回医师补写前记，后者退回补签），混在一条规则里药师
       看不出该退给谁。
    """
    issues: list[str] = []
    if not rule.required_fields:
        issues.append(
            f"规则 {rule.rule_id} 未声明 required_fields，不能作为规范性缺项规则判定"
        )
    unknown = [name for name in rule.required_fields if name not in NORMATIVITY_ITEM_BY_FIELD]
    if unknown:
        issues.append(
            f"规则 {rule.rule_id} 声明的依赖字段 {unknown} 不在前记 / 后记栏位口径表内，"
            "不能作为规范性缺项规则判定"
        )
    front = [name for name in rule.required_fields if name in FRONT_MATTER_FIELDS]
    back = [name for name in rule.required_fields if name in BACK_MATTER_FIELDS]
    if front and back:
        issues.append(
            f"规则 {rule.rule_id} 同时声明了前记字段 {front} 与后记字段 {back}，"
            "前记缺项与后记缺签章必须单独成条"
        )
    return tuple(issues)


def items_for_rule(rule: RuleRow) -> tuple[NormativityItem, ...]:
    """规则行声明的依赖字段 → 前记 / 后记栏位（按声明顺序）。"""
    return tuple(
        NORMATIVITY_ITEM_BY_FIELD[name]
        for name in rule.required_fields
        if name in NORMATIVITY_ITEM_BY_FIELD
    )


def condition_text(items: Sequence[NormativityItem]) -> str:
    """人读的「判定条件」措辞（替换归因文本里的阈值表达式那一格）。"""
    if not items:
        return "规范性缺项"
    key = items[0].key
    names = " / ".join(item.name for item in items)
    return f"{key}缺项（{names}）"


# ---------------------------------------------------------------------------
# 归因文本
# ---------------------------------------------------------------------------


def _describe_missing(items: Sequence[NormativityItem]) -> str:
    """整列未落地时的归因说明（点名栏位与医嘱列）。"""
    parts = [
        f"{item.describe()}对应的字段 {item.field} 在本批落地记录里整列未落地"
        "（接口未回传）"
        for item in items
    ]
    return (
        "；".join(parts)
        + "。系统判不了，须信息科补齐该字段后重判；该字段不计入处方本身的不合格"
    )


def _describe_empty(items: Sequence[NormativityItem]) -> str:
    """栏位在但没填时的归因说明（点名栏位与医嘱列）。"""
    parts = [
        f"{item.describe()}缺项（字段 {item.field} 栏位在但内容为空）" for item in items
    ]
    return "；".join(parts) + "。栏位已落地而内容为空，属处方本身不合格，须退回医师补写"


def _describe_present(items: Sequence[NormativityItem]) -> str:
    """全部落地时的归因说明。"""
    names = " / ".join(item.name for item in items)
    return f"{items[0].key}核对的栏位（{names}）均已落地"


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def evaluate_normativity(rule: RuleRow, row: PrescriptionRow) -> NormativityEvaluation:
    """按规则行判定一条医嘱的规范性缺项结论（三态 + 不适用）。

    判定链：

    #. **配置校验**：规则行不合规范性口径 → 抛
       :class:`NormativityConfigurationError`（配置错误不降级）；
    #. **适用性**：``applies_when`` 不命中 → 不适用，不产生判定行；
    #. **逐栏位核对**：每个声明依赖的栏位分两档——
       整列未落地 → **缺列**；栏位在但内容为空 → **空值**；
    #. **结论**：有缺列 → 不可判定（点名缺列的字段）；否则有空值 → 违规
       （按规则行声明的 ``missing_conclusion``）；否则合规。
    """
    issues = validate_normativity_rule(rule)
    if issues:
        raise NormativityConfigurationError("；".join(issues))

    schedule = match_rule(rule, row)
    if not schedule.is_candidate:
        return NormativityEvaluation(
            status=NormativityStatus.NOT_APPLICABLE,
            detail=schedule.reason,
            rule_id=rule.rule_id,
        )

    items = items_for_rule(rule)
    if not items:
        return NormativityEvaluation(
            status=NormativityStatus.NOT_APPLICABLE,
            detail="规则未声明任何前记 / 后记栏位，本规则不适用",
            rule_id=rule.rule_id,
        )

    missing: list[str] = []
    empty: list[str] = []
    for item in items:
        if not row.has_column(item.field):
            missing.append(item.field)
        elif row.is_missing(item.field):
            empty.append(item.field)

    if missing:
        missing_items = [
            NORMATIVITY_ITEM_BY_FIELD[name]
            for name in missing
            if name in NORMATIVITY_ITEM_BY_FIELD
        ]
        return NormativityEvaluation(
            status=NormativityStatus.UNDETERMINABLE,
            conclusion=NORMATIVITY_CONCLUSIONS[NormativityStatus.UNDETERMINABLE],
            detail=_describe_missing(missing_items),
            missing_fields=tuple(missing),
            empty_fields=tuple(empty),
            items=items,
            rule_id=rule.rule_id,
        )

    if empty:
        empty_items = [
            NORMATIVITY_ITEM_BY_FIELD[name]
            for name in empty
            if name in NORMATIVITY_ITEM_BY_FIELD
        ]
        # 「栏位在但没填」的结论由规则行声明：违规 = 不合格处方、退回医师补写；
        # 不可判定 = 进字段对照台账、等接口补齐。引擎不替规则作者决定。
        conclusion = rule.missing_conclusion
        status = (
            NormativityStatus.VIOLATION
            if conclusion is Conclusion.VIOLATION
            else NormativityStatus.UNDETERMINABLE
        )
        return NormativityEvaluation(
            status=status,
            conclusion=conclusion,
            detail=_describe_empty(empty_items),
            empty_fields=tuple(empty),
            items=items,
            rule_id=rule.rule_id,
        )

    return NormativityEvaluation(
        status=NormativityStatus.COMPLIANT,
        conclusion=NORMATIVITY_CONCLUSIONS[NormativityStatus.COMPLIANT],
        detail=_describe_present(items),
        items=items,
        rule_id=rule.rule_id,
    )


def render_normativity_reason(
    evaluation: NormativityEvaluation,
    rule: RuleRow,
    row: PrescriptionRow | None = None,
) -> str:
    """把规范性缺项判定结果组装成归因文本（固定句式 + 隐私约束）。

    与通用路径共用 ``engine/evidence.py`` 的句式：句中带出规则号、规则版本、
    依据出处；违规句式点名缺哪一项，不可判定句式点名缺哪一列（接口缺口）。
    """
    if evaluation.conclusion is None:
        return evaluation.detail
    return build_reason(
        evaluation.conclusion,
        rule,
        detail=evaluation.detail,
        missing_fields=evaluation.named_fields,
        fields=row,
        condition=condition_text(evaluation.items),
    )


# ---------------------------------------------------------------------------
# 正文段：用法用量含糊字句
# ---------------------------------------------------------------------------

#: 正文段栏位（《处方审核规则体系》§1 规范性审核：正文药品名称、剂型、数量、用法用量）。
#:
#: 用途是**识别**用法用量含糊字句类规则（结构判据），不是数据字典。
BODY_MATTER_FIELDS: tuple[str, ...] = (
    "drug_name",
    "spec",
    "dosage_form",
    "quantity",
    "dosage_text",
    "dosage_value",
    "dosage_unit",
    "route",
    "freq",
    "days_qty",
)

#: §1 规范性审核**明列**的含糊字句——唯一内置词表来源。
#:
#: 原文：「用法用量表述清楚，不得用『遵医嘱』『自用』含糊字句」。
#:
#: 生效词表是**规则行**的 ``forbidden_terms``（药事审核岗可增补，须带 ``basis``）；
#: 本常量只作**核对基准**：规则词表漏掉 §1 明列的字句即判配置错误，避免有人
#: 把「遵医嘱」从词表里删掉后，系统反过来把这类处方判成合规。
VAGUE_USAGE_BASELINE_TERMS: tuple[str, ...] = ("遵医嘱", "自用")


def is_usage_vagueness_rule(rule: RuleRow) -> bool:
    """这条规则是不是「用法用量含糊字句」类规则。

    三个结构判据同时成立才算：

    #. 层次是「规范性」；
    #. 声明了 ``forbidden_terms``（含糊字句词表——词表命中无法用标量比较表达，
       故单列为规则行配置项）；
    #. 依赖字段全部落在正文段栏位内（药品名称、剂型、数量、用法用量……）。

    不按规则号前缀猜：规则号是药事审核岗自己起的，靠前缀识别等于把口径
    焊死在命名约定上。
    """
    if rule.layer != RuleLayer.NORMATIVITY.value:
        return False
    if not rule.forbidden_terms:
        return False
    return all(name in BODY_MATTER_FIELDS for name in rule.required_fields)


def baseline_terms_missing(rule: RuleRow) -> tuple[str, ...]:
    """规则词表未覆盖的 §1 基线字句（空元组 = 基线全覆盖）。"""
    effective = set(rule.forbidden_terms)
    return tuple(term for term in VAGUE_USAGE_BASELINE_TERMS if term not in effective)


def validate_usage_vagueness_rule(rule: RuleRow) -> tuple[str, ...]:
    """校验一条用法用量含糊字句规则，返回配置问题清单（空清单 = 通过）。

    三条硬约束：

    #. 必须声明 ``forbidden_terms``——没有词表的含糊字句规则无从判起；
    #. 词表不得含空项；
    #. **词表必须覆盖 §1 明列的字句**——漏掉即配置错误。这条是防止「悄悄把
       禁止项从词表里删掉、让系统反过来判合规」：删词表属于改规则，须走规则
       改版流程并留版本号与依据，不该由配置缺口静默生效。
    """
    issues: list[str] = []
    if not rule.forbidden_terms:
        issues.append(
            f"规则 {rule.rule_id} 未声明 forbidden_terms，不能作为用法用量含糊字句规则判定"
        )
    blanks = [term for term in rule.forbidden_terms if not term.strip()]
    if blanks:
        issues.append(f"规则 {rule.rule_id} 的含糊字句词表含空项 {blanks}")
    missing = baseline_terms_missing(rule)
    if missing:
        issues.append(
            f"规则 {rule.rule_id} 的含糊字句词表未覆盖 §1 明列字句："
            f"{'、'.join(missing)}；删词表属规则改版，须走版本与依据流程"
        )
    return tuple(issues)


def usage_condition_text(rule: RuleRow) -> str:
    """人读的「判定条件」措辞（替换归因文本里的阈值表达式那一格）。"""
    terms = "、".join(rule.forbidden_terms) if rule.forbidden_terms else "（词表为空）"
    return f"用法用量含糊字句核查（词表：{terms}）"


def find_usage_vagueness_hits(
    rule: RuleRow, row: PrescriptionRow
) -> tuple[tuple[str, str], ...]:
    """在规则声明的字段里查含糊字句词表，返回命中的 ``(字段名, 字句)``。

    命中顺序 = 字段声明顺序 → 词表声明顺序，保证同一份数据每次跑出来的
    归因文本完全一致（台账要能对得上行）。
    """
    hits: list[tuple[str, str]] = []
    for name in rule.required_fields:
        value = row.value(name)
        if not value:
            continue
        for term in rule.forbidden_terms:
            if term in value and (name, term) not in hits:
                hits.append((name, term))
    return tuple(hits)


@dataclass(frozen=True)
class UsageVaguenessEvaluation:
    """一次用法用量含糊字句判定的结果。

    ``hit_terms`` 是命中的 ``(字段名, 字句)``——归因文本要**回显命中的字句**，
    药师才知道退回去要改哪句；``missing_fields`` 与 ``empty_fields`` 沿用
    前记 / 后记那套分流：整列未落地 → 不可判定（接口缺口），栏位在但没填 →
    按规则行声明的 ``missing_conclusion``。
    """

    status: NormativityStatus
    conclusion: Conclusion | None = None
    detail: str = ""
    hits: tuple[tuple[str, str], ...] = ()
    missing_fields: tuple[str, ...] = ()
    empty_fields: tuple[str, ...] = ()
    rule_id: str = ""

    @property
    def is_undeterminable(self) -> bool:
        """是否落「不可判定」（接口缺列，系统判不了）。"""
        return self.status is NormativityStatus.UNDETERMINABLE

    @property
    def hit_terms(self) -> tuple[str, ...]:
        """命中的字句（按命中顺序）。"""
        return tuple(term for _field, term in self.hits)

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
            "hits": [{"field": field, "term": term} for field, term in self.hits],
            "missing_fields": list(self.missing_fields),
            "empty_fields": list(self.empty_fields),
        }


def evaluate_usage_vagueness(
    rule: RuleRow, row: PrescriptionRow
) -> UsageVaguenessEvaluation:
    """按规则行判定一条医嘱的用法用量含糊字句结论（三态 + 不适用）。

    判定链：

    #. **配置校验**：词表缺失 / 漏掉 §1 基线字句 → 抛
       :class:`NormativityConfigurationError`（配置错误不降级）；
    #. **适用性**：``applies_when`` 不命中 → 不适用，不产生判定行；
    #. **依赖字段缺列** → 不可判定（接口没回传这一列，进字段对照台账）；
    #. **依赖字段为空** → 按规则行声明的 ``missing_conclusion``；
    #. **查词表**：命中任一字句 → 违规并回显命中的字句；否则合规。
    """
    issues = validate_usage_vagueness_rule(rule)
    if issues:
        raise NormativityConfigurationError("；".join(issues))

    schedule = match_rule(rule, row)
    if not schedule.is_candidate:
        return UsageVaguenessEvaluation(
            status=NormativityStatus.NOT_APPLICABLE,
            detail=schedule.reason,
            rule_id=rule.rule_id,
        )

    missing = tuple(name for name in rule.required_fields if not row.has_column(name))
    if missing:
        detail = (
            "；".join(
                f"字段 {name} 在本批落地记录里整列未落地（接口未回传）" for name in missing
            )
            + "。系统判不了，须信息科补齐该字段后重判。"
        )
        return UsageVaguenessEvaluation(
            status=NormativityStatus.UNDETERMINABLE,
            conclusion=NORMATIVITY_CONCLUSIONS[NormativityStatus.UNDETERMINABLE],
            detail=detail,
            missing_fields=missing,
            rule_id=rule.rule_id,
        )

    empty = tuple(name for name in rule.required_fields if row.is_missing(name))
    if empty:
        # 栏位在但没填：结论由规则行声明（违规 = 不合格处方、退回医师补写；
        # 不可判定 = 进字段对照台账、等接口补齐）。引擎不替规则作者决定。
        conclusion = rule.missing_conclusion
        status = (
            NormativityStatus.VIOLATION
            if conclusion is Conclusion.VIOLATION
            else NormativityStatus.UNDETERMINABLE
        )
        detail = (
            "；".join(
                f"字段 {name} 栏位在但内容为空，用法用量未写明" for name in empty
            )
            + "。须退回医师按具体用法用量补写。"
        )
        return UsageVaguenessEvaluation(
            status=status,
            conclusion=conclusion,
            detail=detail,
            empty_fields=empty,
            rule_id=rule.rule_id,
        )

    hits = find_usage_vagueness_hits(rule, row)
    if hits:
        detail = "用法用量命中含糊字句" + "".join(
            f"「{term}」（出现在字段 {field}）" for field, term in hits
        ) + "。须退回医师按具体用法用量重新开具。"
        return UsageVaguenessEvaluation(
            status=NormativityStatus.VIOLATION,
            conclusion=NORMATIVITY_CONCLUSIONS[NormativityStatus.VIOLATION],
            detail=detail,
            hits=hits,
            rule_id=rule.rule_id,
        )

    return UsageVaguenessEvaluation(
        status=NormativityStatus.COMPLIANT,
        conclusion=NORMATIVITY_CONCLUSIONS[NormativityStatus.COMPLIANT],
        detail="用法用量表述清楚，未命中含糊字句词表。",
        rule_id=rule.rule_id,
    )


def render_usage_vagueness_reason(
    evaluation: UsageVaguenessEvaluation,
    rule: RuleRow,
    row: PrescriptionRow | None = None,
) -> str:
    """把用法用量含糊字句判定结果组装成归因文本（固定句式 + 隐私约束）。"""
    if evaluation.conclusion is None:
        return evaluation.detail
    return build_reason(
        evaluation.conclusion,
        rule,
        detail=evaluation.detail,
        missing_fields=evaluation.named_fields,
        fields=row,
        condition=usage_condition_text(rule),
    )


__all__ = [
    "BACK_MATTER_FIELDS",
    "BACK_MATTER_ITEMS",
    "BODY_MATTER_FIELDS",
    "FRONT_MATTER_FIELDS",
    "FRONT_MATTER_ITEMS",
    "NORMATIVITY_CONCLUSIONS",
    "NORMATIVITY_ITEM_BY_FIELD",
    "NORMATIVITY_ITEMS",
    "NormativityConfigurationError",
    "NormativityEvaluation",
    "NormativityItem",
    "NormativityStatus",
    "UsageVaguenessEvaluation",
    "VAGUE_USAGE_BASELINE_TERMS",
    "baseline_terms_missing",
    "condition_text",
    "evaluate_normativity",
    "evaluate_usage_vagueness",
    "find_usage_vagueness_hits",
    "is_normativity_rule",
    "is_usage_vagueness_rule",
    "items_for_rule",
    "render_normativity_reason",
    "render_usage_vagueness_reason",
    "usage_condition_text",
    "validate_normativity_rule",
    "validate_usage_vagueness_rule",
]
