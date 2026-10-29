"""三态判定内核：处方行 × 规则行 → 合规 / 违规 / 不可判定。

**对应源产品能力**：源仓库 ``contrib/database/field_rules/registries.py`` 的
``before_row_created`` / ``before_row_updated``（写入前逐行逐规则求值，
产出可校验结论并挂一条 ``error_text`` 归因）与
``web-frontend/modules/core/formula/index.js`` 的 ``resolveFormula()`` /
``getCachedParseTree()``（公式文本 → 解析树 → 在行上下文里求值，解析树共享）。

**改写**：源对象的「公式」改写成「审方规则行的 ``expression``」，源对象的
「单元格行上下文」改写成「HIS 医嘱落地行上下文」；源产品求不出值时返回
``null``（二值语义里的"失败"），本轮把"求不出值"显式升级为第三态
**不可判定**，并要求**点名缺哪个字段、卡在哪条规则**——这正是本轮相对
Excel 台账与二元审方结论的核心增量。

**判定链（一条处方 × 一条规则）**

#. **调度**（``engine/sched.py``）：适用条件命中 → 候选规则；不适用 → 不产生
   判定行，不进台账。
#. **依赖检查**：``required_fields`` 里任一字段缺失或为空 → 结论「不可判定」
   并在 ``missing_fields`` 里点名该字段（规则行可把缺项类规则的结论定为
   「违规」，如规范性前记缺项）。
#. **口径检查**：依赖字段落地了但口径不可辨（``days_qty`` 写成 ``3mg``）→
   同样落「不可判定」，点名该字段并说明「口径不可比」，不猜、不静默判违规。
#. **表达式求值**：字段齐备且口径可比 → 按受限表达式求值得「合规 / 违规」。

**量限规则走另一条判定路径**：规则行声明了 ``limit_ref``（落在 §3 量限矩阵的哪个
单元）时，阈值与口径由 ``engine/limits.py`` 的矩阵给，不靠规则行里的阈值表达式
——矩阵单元还带**条件依赖**（精二延长须注明理由）与**口径核对**（日量写成 3mg
不可比），这两样表达式语法表达不了。矩阵单元与这条医嘱的档位 / 类别不一致时，
本规则对该医嘱**不适用**，不产生判定行。

**判定规则形态（两种，由规则行字段区分）**

* **违规条件型**（无 ``forbidden_terms``）：``expression`` 为真 → 违规，为假 → 合规。
* **词表触发型**（有 ``forbidden_terms``）：``expression`` 为真表示本规则适用，
  再到规则声明的字段里查词表；命中任一字句 → 违规，否则合规。
  （词表命中无法用标量比较表达，故单列为规则行配置项。）

**皮试规则走专用路径**（``engine/skin_test.py``）：规则行在 ``required_fields`` 里
声明了 ``skin_test_result`` 即视为皮试规则，由该模块判定——它把
「字段没落地」（不可判定）与「写明了没做皮试」（违规）分成两个结论，
是本轮最容易出错的一处边界。结论仍由规则行的受限表达式给出，代码里不写死分支。

**规范性缺项类规则走专用路径**（``engine/normativity.py``）：层次为「规范性」且
``missing_conclusion`` 声明为「违规」的规则由该模块判定——它把
「栏位在但内容为空」（违规，处方本身不合格，退回医师补写）与
「整列未落地」（不可判定，接口没回传这一列，进字段对照台账）分成两个结论，
并点名缺的是前记 / 后记的哪一项。

**用法用量含糊字句走专用路径**（``engine/normativity.py`` 同册）：层次为
「规范性」且声明了 ``forbidden_terms`` 词表的规则由该模块判定——词表命中即违规
并**回显命中的字句**（药师要知道退回去改哪句）；生效词表是规则行配置项，
药事审核岗可增补，但漏掉 §1 明列的字句属配置错误（载入期拒、判定期显式报错）。

**儿童实际年龄走专用路径**（``engine/children_age.py``）：层次为「规范性」且
声明了 ``age_value`` + ``age_unit`` 的规则由该模块判定——按分支表落三态：
缺实际年龄 → 违规；数值有而单位缺 → 不可判定（口径不可辨）；「0 岁」是**有值**
不是缺失，走「婴幼儿写成岁」这一支落违规（口径不对），不落「年龄缺失」。

**跨行判定走专用路径**（``engine/duplicate.py`` / ``engine/supernormal.py``）：
重复给药（§1 适宜性第 5 项）与超常处方（§3 尾段）单看一行永远判不出来——
前者要看「同一就诊 · 同一药品 · 同日」这一组里有几张，后者要看「同一开方
医师」在本批超常了几次。这两条规则由 ``engine/context.py`` 的
:class:`BatchContext` 提供同批聚合视图（分组索引与超常发生清单各只算一次），
判定链里因此多一个 ``context`` 参数，而不是多一层隐式全局状态。键字段不全
时落「不可判定」并点名该字段——「审不了」要能说清卡在哪个字段上。

**空值语义**：字段空值分三档——**缺失**（没落地，判不了）、**显式否**
（写「未注明」，是已知不合格，违规）、**有值**（正常参与比较）。
三者若混成「非空即有值」，最常见的现场错误就是「没填皮试结果」被当成
「皮试阴性」放过。三档语义由 ``expression/evaluator.py`` 落地，本模块只做
结论映射。

**严重度与点评分级**：判定行的 ``severity`` 取自
``engine/severity.py`` 的解析结果——规则行写了就用规则行的，没写才按审核层次
回落，并在归因文本里注明回落来源（回落值必须看得出来，不能让药师误以为是
审核岗给的严重度）。点评分级（不适宜处方 / 不规范处方 / 超常处方）由同一模块
给，**只打标不改结论**。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from ..expression.cache import ParseTreeCache, default_cache
from ..expression.errors import ExpressionLanguageError
from ..expression.evaluator import (
    EvalState,
    EvaluationError,
    evaluate_expression as evaluate_expression_text,
)
from ..expression.normalize import normalize_context, normalize_row
from ..models import Conclusion, JudgementRow, PrescriptionRow, RuleRow
from .children_age import (
    ChildAgeConfigurationError,
    ChildAgeStatus,
    evaluate_child_age,
    is_child_age_rule,
    render_child_age_reason,
)
from .context import BatchContext
from .duplicate import (
    DuplicateConfigurationError,
    DuplicateStatus,
    evaluate_duplicate,
    is_duplicate_rule,
    render_duplicate_reason,
)
from .evidence import build_reason
from .limits import LimitStatus, evaluate_limit_rule
from .normativity import (
    NormativityConfigurationError,
    NormativityStatus,
    evaluate_normativity,
    evaluate_usage_vagueness,
    is_normativity_rule,
    is_usage_vagueness_rule,
    render_normativity_reason,
    render_usage_vagueness_reason,
)
from .sched import RuleSchedule, match_rule, schedule_rules, select_candidates
from .indication import (
    IndicationConfigurationError,
    IndicationStatus,
    evaluate_indication,
    is_indication_rule,
    render_indication_reason,
)
from .severity import (
    SeverityResolution,
    render_severity_note,
    resolve_severity,
)
from .skin_test import (
    SkinTestConfigurationError,
    SkinTestStatus,
    evaluate_skin_test,
    is_skin_test_rule,
    render_skin_test_reason,
)
from .supernormal import (
    SupernormalConfigurationError,
    SupernormalStatus,
    evaluate_supernormal,
    is_supernormal_rule,
    render_supernormal_reason,
)

#: 判定结论结构的对外字段口径（逐处方 × 逐规则一行）。
#:
#: 台账、审计与回放都按这一组字段取数，避免各处口径漂移。
JUDGEMENT_FIELDS: tuple[str, ...] = (
    "batch_no",
    "prescription_no",
    "rule_id",
    "rule_version",
    "conclusion",
    "severity",
    "reason",
    "basis",
    "missing_fields",
    "event_fingerprint",
)


class JudgeError(RuntimeError):
    """判定内核的显式失败（配置问题，不是数据问题）。"""


class ExpressionError(JudgeError):
    """表达式不被支持或引用了不存在的字段——属**配置错误**，不得降级成不可判定。"""


class Truth(Enum):
    """求值三态：真 / 假 / 未知（未知即"字段缺了，判不了"）。"""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Evaluation:
    """一次表达式求值的结果。"""

    truth: Truth
    missing: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class Applicability:
    """规则适用性判定结果（兼容入口；调度细节见 ``engine/sched.py``）。"""

    applicable: bool
    unknown_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class BatchJudgement:
    """一个批次的判定结果。"""

    batch_no: str
    source_file: str = ""
    prescription_count: int = 0
    rows: tuple[JudgementRow, ...] = ()
    #: 每张处方命中的候选规则数（用于核对"逐处方 × 逐规则恰好一行"）
    candidate_rule_counts: Mapping[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 表达式求值
# ---------------------------------------------------------------------------


def _evaluate(
    expression: str,
    context: Mapping[str, str],
    *,
    cache: ParseTreeCache | None = None,
) -> Evaluation:
    """受限表达式求值 → 判定层的三态形态。

    语法 / 能力 / 字段越界属**配置错误**，显式抛 :class:`ExpressionError`；
    数据缺字段与口径不可比属**业务上的判不了**，落 :attr:`Truth.UNKNOWN`
    并点名字段。
    """
    try:
        result = evaluate_expression_text(expression, context, cache=cache)
    except (EvaluationError, ExpressionLanguageError) as exc:
        raise ExpressionError(str(exc)) from exc

    if result.state is EvalState.TRUE:
        return Evaluation(Truth.TRUE, detail=result.reason)
    if result.state is EvalState.FALSE:
        return Evaluation(Truth.FALSE, detail=result.reason)
    missing = tuple(result.missing_fields) + tuple(result.uncomparable_fields)
    return Evaluation(Truth.UNKNOWN, missing=missing, detail=result.reason)


def evaluate_expression(
    expression: str,
    row: PrescriptionRow,
    *,
    cache: ParseTreeCache | None = None,
) -> Evaluation:
    """在一条医嘱行的上下文里求值规则表达式（三态）。

    归一后的上下文（``3日量`` → ``3``、``门诊一般患者`` → ``门急诊一般``）
    才是可比较的形态，故求值前先过归一层；归一不动的留原文，不猜。
    """
    return _evaluate(expression, normalize_context(row), cache=cache)


def evaluate_applicability(rule: RuleRow, row: PrescriptionRow) -> Applicability:
    """判定规则对这条医嘱是否适用（兼容入口，内部走调度层）。"""
    schedule = match_rule(rule, row)
    return Applicability(schedule.is_candidate, schedule.unknown_fields)


# ---------------------------------------------------------------------------
# 依赖与口径检查
# ---------------------------------------------------------------------------


def _incomparable_fields(
    rule: RuleRow, normalized: Mapping[str, object]
) -> tuple[str, ...]:
    """依赖字段里「落地了但口径不可辨」的字段名。

    典型例子：``days_qty`` 写成 ``3mg``——数值是 3，但日量口径不可比，
    拿它去比「日量上限」必然出错。规则命中这类字段时落「不可判定」，
    不静默判违规（量限矩阵见 ``engine/limits.py``）。
    """
    names: list[str] = []
    for name in rule.required_fields:
        value = normalized.get(name)
        if value is None:
            continue
        if not getattr(value, "comparable", True):
            names.append(name)
    return tuple(names)


def _incomparable_detail(
    names: Sequence[str], normalized: Mapping[str, object]
) -> str:
    """口径不可比的归因说明（点名字段与落地值）。"""
    parts: list[str] = []
    for name in names:
        value = normalized.get(name)
        raw = getattr(value, "raw", "") if value is not None else ""
        parts.append(f"字段 {name} 的落地值「{raw}」口径不可比")
    return "；".join(parts) + "，无法完成判定"


def find_forbidden_term(rule: RuleRow, row: PrescriptionRow) -> str | None:
    """在规则声明的字段里查含糊字句词表，返回命中的字句（未命中返回 ``None``）。"""
    for name in rule.required_fields:
        value = row.value(name)
        if not value:
            continue
        for term in rule.forbidden_terms:
            if term in value:
                return term
    return None


# ---------------------------------------------------------------------------
# 归因文本
# ---------------------------------------------------------------------------


def _reason_compliant(rule: RuleRow, row: PrescriptionRow | None = None) -> str:
    """合规归因文本（固定句式，见 ``engine/evidence.py``）。"""
    return build_reason(Conclusion.COMPLIANT, rule, fields=row)


def _reason_violation(
    rule: RuleRow, detail: str, row: PrescriptionRow | None = None
) -> str:
    """违规归因文本（固定句式，见 ``engine/evidence.py``）。"""
    return build_reason(Conclusion.VIOLATION, rule, detail=detail, fields=row)


def _reason_undeterminable(
    rule: RuleRow,
    missing: Sequence[str],
    detail: str,
    row: PrescriptionRow | None = None,
) -> str:
    """不可判定归因文本：点名缺哪个字段、卡在哪条规则。"""
    return build_reason(
        Conclusion.UNDETERMINABLE,
        rule,
        detail=detail,
        missing_fields=missing,
        fields=row,
    )


def _reason_missing_conclusion(
    rule: RuleRow,
    missing: Sequence[str],
    conclusion: Conclusion,
    row: PrescriptionRow | None = None,
) -> str:
    """缺必填内容时的归因文本。

    规则行可把缺项类规则的结论定为「违规」（规范性前记缺项即不合格处方），
    此时缺项写进 ``detail``；其余规则保持「不可判定」并点名缺失字段。
    """
    names = "、".join(missing)
    if conclusion is Conclusion.VIOLATION:
        return build_reason(
            Conclusion.VIOLATION,
            rule,
            detail=f"缺少必填内容 {names}。",
            missing_fields=missing,
            fields=row,
        )
    return _reason_undeterminable(rule, missing, "该规则依赖的字段未落地", row)


def _with_severity_note(reason: str, resolution: SeverityResolution) -> str:
    """严重度按层次回落时，在归因文本末尾注明回落来源。

    规则行显式给了严重度 → 原样返回（不制造噪音）；回落 → 补一句说明，
    让药师看得出这个严重度不是审核岗给的，而是引擎按层次推的。
    """
    note = render_severity_note(resolution)
    if not note:
        return reason
    separator = "" if reason.endswith("。") else "。"
    return f"{reason}{separator}{note}"


# ---------------------------------------------------------------------------
# 判定主流程
# ---------------------------------------------------------------------------


def judge_prescription(
    row: PrescriptionRow,
    rules: Sequence[RuleRow],
    *,
    batch_no: str = "",
    event_fingerprint: str = "",
    cache: ParseTreeCache | None = None,
    context: BatchContext | None = None,
) -> list[JudgementRow]:
    """对一条医嘱逐规则求值，返回该处方命中的判定行（每条规则至多一行）。

    **分流**：规则不适用 → 不产生判定行（不进台账）；规则适用但判不了 →
    产生带 ``missing_fields`` 的「不可判定」行。

    ``context`` 是**跨行判定上下文**（重复给药、超常处方需要同批的其他行）。
    缺省按「只看本行」处理——跨行规则在单行视角下得不出重复或超常次数。
    """
    eval_context = normalize_context(row)
    normalized = normalize_row(row)
    results: list[JudgementRow] = []
    for schedule in select_candidates(rules, row):
        rule = schedule.rule
        # 严重度：规则行给了就用规则行的；没给则按审核层次回落，
        # 回落来源由 ``engine/severity.py`` 写进归因文本（见 ``_with_severity_note``）。
        resolution = resolve_severity(rule)

        missing = row.missing_fields(rule.required_fields)
        gaps: tuple[str, ...] = ()
        if is_skin_test_rule(rule):
            # 皮试规则走专用路径（``engine/skin_test.py``）：「空值 ≠ 显式否」
            # 这条边界由该模块保证——字段没落地落不可判定并点名，写「未注明」
            # 落违规；结论本身仍由规则行的受限表达式给出，代码里不写死分支。
            try:
                skin = evaluate_skin_test(rule, row, cache=cache)
            except SkinTestConfigurationError as exc:
                raise ExpressionError(str(exc)) from exc
            if skin.status is SkinTestStatus.NOT_APPLICABLE:
                continue
            conclusion = skin.conclusion or Conclusion.UNDETERMINABLE
            gaps = skin.missing_fields
            reason = render_skin_test_reason(skin, rule, row)
        elif is_normativity_rule(rule):
            # 规范性缺项类规则走专用路径（``engine/normativity.py``）：
            # 「栏位在但没填」落违规（处方本身不合格，退回医师补写），
            # 「整列未落地」落不可判定（接口没回传这一列，系统判不了，
            # 进字段对照台账等信息科补字段）。两种「没有值」结论不同，
            # 处置也不同——这是本模块存在的全部理由。
            try:
                normativity = evaluate_normativity(rule, row)
            except NormativityConfigurationError as exc:
                raise ExpressionError(str(exc)) from exc
            if normativity.status is NormativityStatus.NOT_APPLICABLE:
                continue
            conclusion = normativity.conclusion or Conclusion.UNDETERMINABLE
            gaps = normativity.named_fields
            reason = render_normativity_reason(normativity, rule, row)
        elif is_usage_vagueness_rule(rule):
            # 用法用量含糊字句（正文段）：词表命中 → 违规并**回显命中的字句**，
            # 药师才知道退回去要改哪句；词表本身是规则行配置项，药事审核岗可增补。
            try:
                usage = evaluate_usage_vagueness(rule, row)
            except NormativityConfigurationError as exc:
                raise ExpressionError(str(exc)) from exc
            if usage.status is NormativityStatus.NOT_APPLICABLE:
                continue
            conclusion = usage.conclusion or Conclusion.UNDETERMINABLE
            gaps = usage.named_fields
            reason = render_usage_vagueness_reason(usage, rule, row)
        elif is_child_age_rule(rule):
            # 儿童实际年龄 / 婴幼儿日·月龄口径（``engine/children_age.py``）：
            # 缺实际年龄 → 违规；数值有而单位缺 → 不可判定（口径不可辨）；
            # 「0 岁」是**有值**不是缺失，走「婴幼儿写成岁」这一支落违规。
            try:
                child = evaluate_child_age(rule, row)
            except ChildAgeConfigurationError as exc:
                raise ExpressionError(str(exc)) from exc
            if child.status is ChildAgeStatus.NOT_APPLICABLE:
                continue
            conclusion = child.conclusion or Conclusion.UNDETERMINABLE
            gaps = child.named_fields
            reason = render_child_age_reason(child, rule, row)
        elif is_duplicate_rule(rule):
            # 重复给药（``engine/duplicate.py``）：**跨行**判定——同一就诊 ·
            # 同一药品 · 同日这一组里有几张。键字段不全 → 不可判定并点名；
            # 组内首张合规，后续几张落违规。
            try:
                duplicate = evaluate_duplicate(rule, row, context=context)
            except DuplicateConfigurationError as exc:
                raise ExpressionError(str(exc)) from exc
            if duplicate.status is DuplicateStatus.NOT_APPLICABLE:
                continue
            conclusion = duplicate.conclusion or Conclusion.UNDETERMINABLE
            gaps = duplicate.missing_fields
            reason = render_duplicate_reason(duplicate, rule, row)
        elif is_supernormal_rule(rule):
            # 超常处方（``engine/supernormal.py``）：**跨行**计数——同一开方
            # 医师在本批内超 §3 量限且未注明正当理由的次数。越过阈值 → 提示
            # 并标记应限制处方权（限权是药事委员会的处置动作，系统不自动执行）。
            try:
                supernormal = evaluate_supernormal(rule, row, context=context)
            except SupernormalConfigurationError as exc:
                raise ExpressionError(str(exc)) from exc
            if supernormal.status is SupernormalStatus.NOT_APPLICABLE:
                continue
            conclusion = supernormal.conclusion or Conclusion.UNDETERMINABLE
            gaps = supernormal.missing_fields
            reason = render_supernormal_reason(supernormal, rule, row)
        elif is_indication_rule(rule):
            # 诊断相符性（``engine/indication.py``）：§1 适宜性审核第 2 项。
            # 本轮**不做实质判定**（药品说明书适应症属厂商资产，知识库未接入）——
            # 字段缺 → 不可判定并点名（接口缺口，可排期补字段）；字段齐 →
            # 仍不可判定，转药师人工复核（知识库缺口，补接口字段也没用）。
            # 两个分支都不落「合规」：系统不能审核的部分必须人工审核。
            try:
                indication = evaluate_indication(rule, row)
            except IndicationConfigurationError as exc:
                raise ExpressionError(str(exc)) from exc
            if indication.status is IndicationStatus.NOT_APPLICABLE:
                continue
            conclusion = indication.conclusion or Conclusion.UNDETERMINABLE
            gaps = indication.missing_fields
            reason = render_indication_reason(indication, rule, row)
        elif missing:
            conclusion = rule.missing_conclusion
            reason = _reason_missing_conclusion(rule, missing, conclusion, row)
            gaps = missing
        elif rule.limit_ref:
            # 量限规则：阈值与口径由 §3 量限矩阵给（见 engine/limits.py），
            # 矩阵单元与这条医嘱的档位 / 类别不一致 → 本规则不适用，不产生判定行。
            limit_result = evaluate_limit_rule(rule, row, normalized=normalized)
            if limit_result.status is LimitStatus.NOT_APPLICABLE:
                continue
            conclusion = limit_result.conclusion or Conclusion.UNDETERMINABLE
            gaps = limit_result.missing_fields
            reason = build_reason(
                conclusion,
                rule,
                detail=limit_result.detail,
                missing_fields=gaps,
                fields=row,
                condition=(
                    limit_result.cell.condition_text if limit_result.cell else ""
                ),
            )
        else:
            incomparable = _incomparable_fields(rule, normalized)
            if incomparable:
                conclusion = Conclusion.UNDETERMINABLE
                reason = _reason_undeterminable(
                    rule,
                    incomparable,
                    _incomparable_detail(incomparable, normalized),
                    row,
                )
                gaps = incomparable
            else:
                evaluation = _evaluate(rule.expression, eval_context, cache=cache)
                if evaluation.truth is Truth.UNKNOWN:
                    conclusion = Conclusion.UNDETERMINABLE
                    gaps = evaluation.missing
                    reason = _reason_undeterminable(rule, gaps, evaluation.detail, row)
                elif rule.forbidden_terms:
                    hit = find_forbidden_term(rule, row)
                    if hit is None:
                        conclusion = Conclusion.COMPLIANT
                        reason = _reason_compliant(rule, row)
                    else:
                        conclusion = Conclusion.VIOLATION
                        reason = _reason_violation(
                            rule, f"用法用量命中含糊字句「{hit}」。", row
                        )
                elif evaluation.truth is Truth.TRUE:
                    conclusion = Conclusion.VIOLATION
                    reason = _reason_violation(
                        rule, f"判定条件「{rule.expression}」命中。", row
                    )
                else:
                    conclusion = Conclusion.COMPLIANT
                    reason = _reason_compliant(rule, row)

        results.append(
            JudgementRow(
                batch_no=batch_no or row.batch_no,
                prescription_no=row.prescription_no or row.order_no,
                rule_id=rule.rule_id,
                rule_version=rule.version,
                conclusion=conclusion,
                severity=resolution.value,
                reason=_with_severity_note(reason, resolution),
                basis=rule.basis,
                missing_fields=tuple(gaps),
                event_fingerprint=event_fingerprint,
            )
        )
    return results


def judge_batch(
    rows: Sequence[PrescriptionRow],
    rules: Sequence[RuleRow],
    *,
    batch_no: str = "",
    source_file: str = "",
    event_fingerprint: str = "",
    cache: ParseTreeCache | None = None,
) -> BatchJudgement:
    """对一个批次的处方行逐条判定。

    返回的 ``candidate_rule_counts`` 给出每张处方命中的候选规则数，
    用于核对"逐处方 × 逐规则恰好一行、无重复无遗漏"。

    ``cache`` 是表达式解析树缓存（缺省用进程级默认缓存）：同一段表达式文本
    只解析一次，2000 行 × 12 条规则不会重复解析。

    **跨行判定**：本批的处方行会被组织成一个 :class:`BatchContext` 传给判定链，
    重复给药与超常处方据此做同批聚合——单看一行永远判不出「重复」与「超常」。
    """
    resolved_cache = cache if cache is not None else default_cache()
    resolved_batch = batch_no or (rows[0].batch_no if rows else "")
    context = BatchContext.of(rows)
    judgements: list[JudgementRow] = []
    counts: dict[str, int] = {}
    for row in rows:
        row_judgements = judge_prescription(
            row,
            rules,
            batch_no=resolved_batch,
            event_fingerprint=event_fingerprint,
            cache=resolved_cache,
            context=context,
        )
        judgements.extend(row_judgements)
        key = row.prescription_no or row.order_no
        counts[key] = len(row_judgements)
    return BatchJudgement(
        batch_no=resolved_batch,
        source_file=source_file,
        prescription_count=len(rows),
        rows=tuple(judgements),
        candidate_rule_counts=counts,
    )


def summarize_conclusions(rows: Sequence[JudgementRow]) -> dict[str, int]:
    """三态结论计数（缺失的结论计 0）。"""
    counts = {member.value: 0 for member in Conclusion}
    for row in rows:
        counts[row.conclusion.value] += 1
    return counts


__all__ = [
    "Applicability",
    "BatchJudgement",
    "Conclusion",
    "Evaluation",
    "ExpressionError",
    "JUDGEMENT_FIELDS",
    "JudgementRow",
    "JudgeError",
    "RuleSchedule",
    "Truth",
    "evaluate_applicability",
    "evaluate_expression",
    "find_forbidden_term",
    "judge_batch",
    "judge_prescription",
    "match_rule",
    "schedule_rules",
    "select_candidates",
    "summarize_conclusions",
]
