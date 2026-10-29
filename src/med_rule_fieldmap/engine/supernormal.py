"""超常处方（《处方审核规则体系》§3 尾段）：跨行计数，**只出提示不自动限权**。

**对应源产品能力**：源仓库 ``contrib/database/field_rules/registries.py`` 的
``before_row_created`` / ``before_row_updated``（写入前逐行逐规则求值）——
本轮把「逐行」扩成「逐行 × 同一医师的多次开立」：判定键从行内字段扩成
**跨行计数键**（同一开方医师）。

**依据**：§3 尾段「超常处方 3 次以上且无正当理由：警告并限制处方权；
限权后仍连续 2 次以上：取消处方权」。官方案例 rx-009（医师张三、超常处方
4 次、无正当理由）应拦截。

**「超常处方」的口径（本轮唯一有法规原文可依的那一支）**
§3 没有给「超常处方」下定义，只给了它的**处置**。本轮把判据锚在 §3 里
唯一可确定性引擎化的口径上：

* 一张处方落在 §3 量限矩阵的某个单元内，**且日量超过该单元上限** → 这张处方
  是一次「超常处方」（数量超出正常诊疗需要）；
* 且**未注明正当理由**（延长理由字段为空，或写「未注明」这类显式否定）→
  计入「无正当理由的超常处方次数」。

普通药品的「正常诊疗需要用量」属临床判断，§3 未给数字，**本轮不自创阈值**；
该缺口由药事审核岗按本院口径补进规则行后再引擎化。边界写在这里，不藏。

**正当理由字段**：``extend_reason``（spec.md §2.1 已含该列，与 §3 精二
「延长须注明理由」同源）。三档语义与全产品一致：

=========================  ==================================================
落地情况                   口径
=========================  ==================================================
整列未落地（接口没回传）    **不可判定**——系统判不了有没有写理由
栏位在但内容为空            无正当理由
写成「未注明」等显式否定      无正当理由
有实际内容                  有正当理由
=========================  ==================================================

**次数口径与触发条件**

* **次数** = 同一开方医师（``prescriber_id``）在**本批**内「超 §3 量限且未注明
  正当理由」的处方张数，**按开立先后累计**（第 1 次、第 2 次……）。
* **触发** = 累计次数**超过** :data:`SUPERNNORMAL_THRESHOLD`（= 3，即第 4 次起）
  → 出提示并标记「应限制处方权」。

口径说明（写在这里，不藏）：§3 原文是「3 次以上」，中文法规文书中「以上」通常
含本数；本轮按**「超 3 次」**实现为**严格大于 3**（官方案例 rx-009 为 4 次，
恰好 3 次不提示）。两条读法的差异常量在 :data:`SUPERNNORMAL_THRESHOLD` 一处，
本院药事委员会若按「3 次以上含本数」执行，改这一处比较即可，不必翻遍代码。

**只出提示，不自动限权（这一条是本模块的底线）**
限权是**药事委员会的处置动作**，不是信息系统能自行推进的状态。系统只做两件事：

#. 出提示（判定结论落「违规」，归因文本写明须提请药事委员会处置）；
#. 标记「应限制处方权」（:attr:`SupernormalEvaluation.should_restrict_prescribing_right`）。

系统**不**改医师权限、**不**停其开方——那是药事系统的动作。§3 尾段后半句
「限权后仍连续 2 次以上：取消处方权」本轮**不实现**：限权状态不在医嘱落地
文件里，引擎没有这个事实来源，不自造。

**规则行怎么写**：``expression`` 声明判定前提所依赖的字段（开方医师），
``required_fields`` 声明 ``prescriber_id`` 与 ``extend_reason``。载入期校验
（``rules/schema.py`` 的 ``_supernormal_rule_issues``）要求两者都在位——
理由与 §3 量限规则、重复给药规则同源：跨行聚合口径用行内标量表达式表达不了，
口径由本模块给，规则行负责声明依赖，对不上就是配置错误，载入期即拒。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ..expression.normalize import normalize_field
from ..models import Conclusion, PrescriptionRow, RuleLayer, RuleRow
from .context import BatchContext, order_key, row_identity
from .evidence import build_reason
from .limits import LIMIT_MATRIX, check_quantity_semantics, classify_limit
from .sched import match_rule

# ---------------------------------------------------------------------------
# 口径
# ---------------------------------------------------------------------------

#: 超常处方提示的次数阈值（累计次数**严格大于**该值时提示）。
#:
#: §3 尾段原文「超常处方 3 次以上且无正当理由」，本轮按「超 3 次」实现为
#: 第 4 次起提示（官方案例 rx-009 为 4 次）。口径集中在这一个常量上。
SUPERNNORMAL_THRESHOLD = 3

#: 正当理由字段（医嘱落地列口径，spec.md §2.1；与 §3 精二「延长须注明理由」同源）。
SUPERNNORMAL_REASON_FIELD = "extend_reason"

#: 开方医师字段（跨行计数的分组键）。
SUPERNNORMAL_PRESCRIBER_FIELD = "prescriber_id"

#: 超常处方规则的依据出处（《处方审核规则体系》§3 尾段）。
SUPERNNORMAL_BASIS = (
    "处方审核规则体系 §3 尾段（超常处方 3 次以上且无正当理由：警告并限制处方权）"
)

#: 发生清单的口径标识（同一批数据里只算一次）。
SUPERNNORMAL_OCCURRENCE_TAG = "supernormal-occurrences"


class SupernormalStatus(str, Enum):
    """超常处方判定结论（与三态判定内核一致，多一个「不适用」）。"""

    NOT_APPLICABLE = "不适用"
    COMPLIANT = "合规"
    VIOLATION = "违规"
    UNDETERMINABLE = "不可判定"


#: 超常处方结论 → 判定内核的三态结论。
SUPERNNORMAL_CONCLUSIONS: Mapping[SupernormalStatus, Conclusion] = {
    SupernormalStatus.COMPLIANT: Conclusion.COMPLIANT,
    SupernormalStatus.VIOLATION: Conclusion.VIOLATION,
    SupernormalStatus.UNDETERMINABLE: Conclusion.UNDETERMINABLE,
}


class SupernormalConfigurationError(RuntimeError):
    """规则行写法与超常处方口径冲突（**配置错误**，不得降级成「不可判定」）。"""


# ---------------------------------------------------------------------------
# 发生（一次超常处方）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LimitExceeded:
    """一张处方超了 §3 量限矩阵的某个单元上限。"""

    limit_ref: str
    limit_phrase: str
    quantity_text: str


@dataclass(frozen=True)
class SupernormalOccurrence:
    """一次「超 §3 量限且未注明正当理由」的超常处方。"""

    row: PrescriptionRow
    prescriber_id: str
    limit_ref: str
    limit_phrase: str
    quantity_text: str
    reason_text: str = ""
    reason_state: str = ""

    @property
    def identity(self) -> str:
        """该次发生的处方身份标识。"""
        return row_identity(self.row)

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照。"""
        return {
            "prescription_no": self.identity,
            "prescriber_id": self.prescriber_id,
            "limit_ref": self.limit_ref,
            "limit_phrase": self.limit_phrase,
            "quantity_text": self.quantity_text,
            "reason_text": self.reason_text,
            "reason_state": self.reason_state,
        }


def _reason_state(row: PrescriptionRow) -> tuple[str, str]:
    """正当理由的落地状态：``(状态, 落地值)``。

    状态取值：**未落地**（接口没回传这一列）/ **未注明**（栏位在但为空或写了
    显式否定）/ **已注明**（有实际内容）。
    """
    raw = row.value(SUPERNNORMAL_REASON_FIELD)
    if not row.has_column(SUPERNNORMAL_REASON_FIELD):
        return ("未落地", raw.strip())
    normalized = normalize_field(SUPERNNORMAL_REASON_FIELD, raw)
    if normalized.is_missing or normalized.is_explicit_no:
        return ("未注明", normalized.text)
    return ("已注明", normalized.text)


def detect_exceedance(row: PrescriptionRow) -> LimitExceeded | None:
    """判一条医嘱是否超了 §3 量限矩阵的某个单元上限。

    不超限返回 ``None``。三种「判不出来」的情形都不算超限，理由各自不同：

    * **不在 §3 量限矩阵内**（普通药品、档位判不出）——§3 没给这类药的
      「正常诊疗需要用量」，本轮不自创阈值；
    * **日量口径不可比**（写成 ``3mg`` / ``适量``）——量限规则会把这条落
      「不可判定」并点名 ``days_qty``，此处不重复计入超常次数。
    """
    normalized = {
        "rx_type": normalize_field("rx_type", row.value("rx_type")),
        "drug_category": normalize_field("drug_category", row.value("drug_category")),
    }
    key = classify_limit(row, normalized)
    if key is None:
        return None
    cell = LIMIT_MATRIX.get(key.ref)
    if cell is None:
        return None
    quantity = check_quantity_semantics(row)
    if not quantity.comparable or quantity.number is None:
        return None
    if quantity.number <= cell.max_quantity:
        return None
    return LimitExceeded(
        limit_ref=cell.ref,
        limit_phrase=cell.unit_phrase,
        quantity_text=quantity.display,
    )


def detect_occurrence(row: PrescriptionRow) -> SupernormalOccurrence | None:
    """判一条医嘱是不是一次「超 §3 量限且未注明正当理由」的超常处方。

    不属于超常处方时返回 ``None``（不产生判定行）：

    * **未超 §3 量限**——不发生；
    * **已注明正当理由**——按 §3「延长须注明理由」予以放行，不计入
      「无正当理由」的超常次数（量限结论仍由量限规则给出）；
    * **理由整列未落地**——判不出有没有写理由，交给不可判定结论与字段
      补齐台账，不猜。
    """
    exceeded = detect_exceedance(row)
    if exceeded is None:
        return None

    reason_state, reason_text = _reason_state(row)
    if reason_state in ("未落地", "已注明"):
        return None

    return SupernormalOccurrence(
        row=row,
        prescriber_id=row.value(SUPERNNORMAL_PRESCRIBER_FIELD).strip(),
        limit_ref=exceeded.limit_ref,
        limit_phrase=exceeded.limit_phrase,
        quantity_text=exceeded.quantity_text,
        reason_text=reason_text,
        reason_state=reason_state,
    )


def detect_occurrences(rows: Sequence[PrescriptionRow]) -> tuple[SupernormalOccurrence, ...]:
    """一批医嘱里的全部超常处方发生（按开立先后排序）。"""
    occurrences = [
        occurrence
        for occurrence in (detect_occurrence(row) for row in rows)
        if occurrence is not None
    ]
    occurrences.sort(key=lambda item: order_key(item.row))
    return tuple(occurrences)


def occurrences_of(context: BatchContext) -> tuple[SupernormalOccurrence, ...]:
    """取本批的超常处方发生清单（同一批只算一次）。"""
    return context.memo(
        SUPERNNORMAL_OCCURRENCE_TAG,
        lambda: detect_occurrences(context.rows),
    )


def occurrences_by_prescriber(
    occurrences: Sequence[SupernormalOccurrence],
) -> dict[str, tuple[SupernormalOccurrence, ...]]:
    """按开方医师分组（组内按开立先后）。"""
    grouped: dict[str, list[SupernormalOccurrence]] = {}
    for occurrence in occurrences:
        grouped.setdefault(occurrence.prescriber_id, []).append(occurrence)
    return {key: tuple(items) for key, items in grouped.items()}


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupernormalEvaluation:
    """一次超常处方判定的结果。"""

    status: SupernormalStatus
    conclusion: Conclusion | None = None
    detail: str = ""
    missing_fields: tuple[str, ...] = ()
    prescriber_id: str = ""
    occurrence_count: int = 0
    threshold: int = SUPERNNORMAL_THRESHOLD
    occurrence: SupernormalOccurrence | None = None
    should_restrict_prescribing_right: bool = False
    rule_id: str = ""

    @property
    def is_undeterminable(self) -> bool:
        """是否落「不可判定」（理由字段没落地，系统判不了）。"""
        return self.status is SupernormalStatus.UNDETERMINABLE

    @property
    def exceeds_threshold(self) -> bool:
        """是否已越过提示阈值（累计次数 > 阈值）。"""
        return self.occurrence_count > self.threshold

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照（CLI ``--json`` 输出用）。"""
        return {
            "rule_id": self.rule_id,
            "status": self.status.value,
            "conclusion": self.conclusion.value if self.conclusion else "",
            "detail": self.detail,
            "missing_fields": list(self.missing_fields),
            "prescriber_id": self.prescriber_id,
            "occurrence_count": self.occurrence_count,
            "threshold": self.threshold,
            "should_restrict_prescribing_right": self.should_restrict_prescribing_right,
            "occurrence": self.occurrence.to_dict() if self.occurrence else {},
        }


# ---------------------------------------------------------------------------
# 规则行识别与配置校验
# ---------------------------------------------------------------------------


def is_supernormal_rule(rule: RuleRow) -> bool:
    """这条规则是不是「超常处方」规则。

    判据是**规则行显式声明了跨行计数的两个依赖**——``prescriber_id``（分组键）
    与 ``extend_reason``（正当理由）——不是按规则号前缀猜。
    """
    if rule.layer != RuleLayer.APPROPRIATENESS.value:
        return False
    return all(
        name in rule.required_fields
        for name in (SUPERNNORMAL_PRESCRIBER_FIELD, SUPERNNORMAL_REASON_FIELD)
    )


def validate_supernormal_rule(rule: RuleRow) -> tuple[str, ...]:
    """校验一条超常处方规则行，返回配置问题清单（空清单 = 通过）。

    四条硬约束：

    #. 层次必须是「适宜性」——超常处方属 §1 适宜性审核第 7 项与 §3 尾段；
    #. 必须声明 ``prescriber_id``（跨行分组键）与 ``extend_reason``（正当理由）；
    #. ``expression`` 必须引用 ``prescriber_id``——判定前提写在规则行里；
    #. ``missing_conclusion`` 必须是「不可判定」——理由字段没落地时系统判不了，
       落「违规」会把接口缺口伪装成医师超常处方。
    """
    issues: list[str] = []
    if rule.layer != RuleLayer.APPROPRIATENESS.value:
        issues.append(
            f"规则 {rule.rule_id} 的层次是「{rule.layer}」，"
            "超常处方属适宜性审核与 §3 尾段，层次须为「适宜性」"
        )
    missing = [
        name
        for name in (SUPERNNORMAL_PRESCRIBER_FIELD, SUPERNNORMAL_REASON_FIELD)
        if name not in rule.required_fields
    ]
    if missing:
        issues.append(
            f"规则 {rule.rule_id} 未在 required_fields 里声明依赖字段 {missing}，"
            "超常处方判定须同时声明 prescriber_id（分组键）与 extend_reason（正当理由）"
        )
    if SUPERNNORMAL_PRESCRIBER_FIELD not in rule.expression:
        issues.append(
            f"规则 {rule.rule_id} 的 expression 未引用 {SUPERNNORMAL_PRESCRIBER_FIELD}，"
            "跨行计数的分组键须在规则行里写明"
        )
    if rule.missing_conclusion is not Conclusion.UNDETERMINABLE:
        issues.append(
            f"规则 {rule.rule_id} 的 missing_conclusion 声明为「{rule.missing_conclusion.value}」，"
            "理由字段未落地只能落「不可判定」（系统判不了，不等于医师超常开方）"
        )
    return tuple(issues)


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def evaluate_supernormal(
    rule: RuleRow,
    row: PrescriptionRow,
    *,
    context: BatchContext | None = None,
) -> SupernormalEvaluation:
    """按规则行判定一条医嘱是否触发超常处方提示（三态 + 不适用）。

    判定链：

    #. **配置校验**：规则行不合超常处方口径 → 抛
       :class:`SupernormalConfigurationError`（配置错误不降级）；
    #. **适用性**：``applies_when`` 不命中 → 不适用，不产生判定行；
    #. **不是超常处方**（未超 §3 量限 / 已注明正当理由 / 判不出来）→ 不适用，
       不产生判定行——超量本身的结论由量限规则给出，不重复出一条；
    #. **理由整列未落地** → 不可判定并点名 ``extend_reason``（接口缺口）；
    #. **累计次数**：该医师在本批内「超量且无正当理由」的开立次数（含本张）；
    #. **越过阈值**（次数 > 3）→ 违规（提示 + 标记应限制处方权）；
       未越过 → 合规（未达提示阈值）。
    """
    issues = validate_supernormal_rule(rule)
    if issues:
        raise SupernormalConfigurationError("；".join(issues))

    schedule = match_rule(rule, row)
    if not schedule.is_candidate:
        return SupernormalEvaluation(
            status=SupernormalStatus.NOT_APPLICABLE,
            detail=schedule.reason,
            rule_id=rule.rule_id,
        )

    prescriber_id = row.value(SUPERNNORMAL_PRESCRIBER_FIELD).strip()
    if not prescriber_id:
        return SupernormalEvaluation(
            status=SupernormalStatus.UNDETERMINABLE,
            conclusion=SUPERNNORMAL_CONCLUSIONS[SupernormalStatus.UNDETERMINABLE],
            detail=(
                f"开方医师字段 {SUPERNNORMAL_PRESCRIBER_FIELD} 未落地，"
                "系统判不了这一张处方属不属于同一医师的超常开方，"
                "须信息科补齐该字段后重判"
            ),
            missing_fields=(SUPERNNORMAL_PRESCRIBER_FIELD,),
            rule_id=rule.rule_id,
        )

    resolved = context or BatchContext()

    exceeded = detect_exceedance(row)
    if exceeded is None:
        # 未超 §3 量限 → 本规则不适用（超量本身的结论由量限规则给出）。
        return SupernormalEvaluation(
            status=SupernormalStatus.NOT_APPLICABLE,
            detail=(
                "本张处方未超 §3 量限，不计入无正当理由的超常处方次数"
                "（超量结论由量限规则给出）"
            ),
            prescriber_id=prescriber_id,
            rule_id=rule.rule_id,
        )

    if not row.has_column(SUPERNNORMAL_REASON_FIELD):
        return SupernormalEvaluation(
            status=SupernormalStatus.UNDETERMINABLE,
            conclusion=SUPERNNORMAL_CONCLUSIONS[SupernormalStatus.UNDETERMINABLE],
            detail=(
                f"正当理由字段 {SUPERNNORMAL_REASON_FIELD} 整列未落地（接口未回传），"
                "系统判不了本次超常开方有没有正当理由，须信息科补齐该字段后重判"
            ),
            missing_fields=(SUPERNNORMAL_REASON_FIELD,),
            prescriber_id=prescriber_id,
            rule_id=rule.rule_id,
        )

    reason_state, reason_text = _reason_state(row)
    if reason_state == "已注明":
        # 已注明正当理由 → 按 §3「延长须注明理由」予以放行，不计入
        # 「无正当理由」的超常次数。
        return SupernormalEvaluation(
            status=SupernormalStatus.NOT_APPLICABLE,
            detail=(
                f"本张处方已注明正当理由「{reason_text}」，"
                "按 §3「延长须注明理由」予以放行，不计入无正当理由的超常处方次数"
            ),
            prescriber_id=prescriber_id,
            rule_id=rule.rule_id,
        )

    occurrence = next(
        (item for item in occurrences_of(resolved) if item.identity == row_identity(row)),
        None,
    )
    scope_note = ""
    if occurrence is None:
        # 本行不在本批上下文里（单行判定等场景）：按「本次判定范围内只见到本张」
        # 处理，只说可见范围内的情况，不替别的批次下判断。
        scope_note = "（本次判定范围内只见到本张，超常次数按 1 计）"
        occurrence = SupernormalOccurrence(
            row=row,
            prescriber_id=prescriber_id,
            limit_ref=exceeded.limit_ref,
            limit_phrase=exceeded.limit_phrase,
            quantity_text=exceeded.quantity_text,
            reason_text=reason_text,
            reason_state=reason_state,
        )

    grouped = occurrences_by_prescriber(occurrences_of(resolved))
    same_prescriber = grouped.get(prescriber_id, ())
    count = 0
    located = False
    for item in same_prescriber:
        count += 1
        if item.identity == occurrence.identity:
            located = True
            break
    if not located:
        # 本行不在本批的超常发生清单里（单行判定等场景）：按「本批可见范围内
        # 第 1 次」计，不替别的批次下判断。
        count = 1

    threshold = SUPERNNORMAL_THRESHOLD
    if count > threshold:
        return SupernormalEvaluation(
            status=SupernormalStatus.VIOLATION,
            conclusion=SUPERNNORMAL_CONCLUSIONS[SupernormalStatus.VIOLATION],
            detail=(
                f"开方医师 {prescriber_id} 本批内第 {count} 次开具超量处方"
                f"（开具日量 {occurrence.quantity_text}，超过「{occurrence.limit_phrase}」上限，"
                f"落在 {occurrence.limit_ref}）且未注明正当理由，"
                f"累计次数已超过 {threshold} 次阈值。{scope_note}"
                "按 §3 尾段应提出警告并限制处方权——本系统只出提示并标记，"
                "限制处方权属药事委员会的处置动作，系统不自动执行。"
            ),
            prescriber_id=prescriber_id,
            occurrence_count=count,
            threshold=threshold,
            occurrence=occurrence,
            should_restrict_prescribing_right=True,
            rule_id=rule.rule_id,
        )

    return SupernormalEvaluation(
        status=SupernormalStatus.COMPLIANT,
        conclusion=SUPERNNORMAL_CONCLUSIONS[SupernormalStatus.COMPLIANT],
        detail=(
            f"开方医师 {prescriber_id} 本批内第 {count} 次开具超量处方"
            f"（开具日量 {occurrence.quantity_text}，超过「{occurrence.limit_phrase}」上限）"
            f"且未注明正当理由，累计次数未超过 {threshold} 次阈值，"
            f"未达超常处方提示线（超量本身的结论由量限规则给出）。{scope_note}"
        ),
        prescriber_id=prescriber_id,
        occurrence_count=count,
        threshold=threshold,
        occurrence=occurrence,
        rule_id=rule.rule_id,
    )


def render_supernormal_reason(
    evaluation: SupernormalEvaluation,
    rule: RuleRow,
    row: PrescriptionRow | None = None,
) -> str:
    """把超常处方判定结果组装成归因文本（固定句式 + 隐私约束）。"""
    if evaluation.conclusion is None:
        return evaluation.detail
    return build_reason(
        evaluation.conclusion,
        rule,
        detail=evaluation.detail,
        missing_fields=evaluation.missing_fields,
        fields=row,
        condition="同一开方医师的超常处方次数核查（超 §3 量限且未注明正当理由）",
    )


__all__ = [
    "LimitExceeded",
    "SUPERNNORMAL_BASIS",
    "SUPERNNORMAL_CONCLUSIONS",
    "SUPERNNORMAL_OCCURRENCE_TAG",
    "SUPERNNORMAL_PRESCRIBER_FIELD",
    "SUPERNNORMAL_REASON_FIELD",
    "SUPERNNORMAL_THRESHOLD",
    "SupernormalConfigurationError",
    "SupernormalEvaluation",
    "SupernormalOccurrence",
    "SupernormalStatus",
    "detect_exceedance",
    "detect_occurrence",
    "detect_occurrences",
    "evaluate_supernormal",
    "is_supernormal_rule",
    "occurrences_by_prescriber",
    "occurrences_of",
    "render_supernormal_reason",
    "validate_supernormal_rule",
]
