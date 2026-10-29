"""受限表达式求值器：AST → 三态结论（真 / 假 / 不可判定）。

**对应源产品能力**：源仓库 ``web-frontend/modules/core/formula/index.js`` 的
``resolveFormula()``（解析树在行上下文里求值）与
``contrib/database/field_rules/registries.py`` 的 ``FieldRuleValidity.error_text``
（求值不成立时给出可读的归因位）。本轮把「单元格公式求值」改写成
「审方规则表达式求值」，并把源产品的二值语义扩为三态。

**三态空值语义（本模块的核心）**

字段取值的空值语义分三档，少一档结论就会错：

======================  ======================================================
状态                    含义
======================  ======================================================
``MISSING``             字段没有落地（缺列 / 空串 / 纯空白）
``EXPLICIT_NO``         字段有值，但值是显式否定标记（未注明 / 未做 / 否 …）
``VALUE``               字段有正常取值
======================  ======================================================

为什么要把 ``EXPLICIT_NO`` 从 ``VALUE`` 里单列：皮试结果留空与皮试结果写「未注明」
在《处方审核规则体系》§1 适宜性审核第 1 项下是**两个不同结论**——留空是「系统审不了」
（不可判定，字段补齐后可重判），写「未注明」是「已知不合格」（违规）。两者若混成
「非空即有值」，最常见的现场错误就是「没填皮试结果」被当成「皮试阴性」放过。

**比较语义**

* ``MISSING`` 与任何值比较一律返回 ``MISSING``——不抛错、不静默判假；
* ``EXPLICIT_NO`` 与正数（或其他正值）比较时，条件**不成立即违规**，不静默判合规；
* 两侧都有值才做真正的比较；数值优先，比不了落「口径不可比」（仍归不可判定）。

**逻辑连接的缺失传播**

* ``and``：任一侧缺失 → 整体不可判定（不做短路求值）；
* ``or``：一侧为真 → 整体为真；否则有缺失 → 整体不可判定。

``and`` 不做短路是刻意的：Kleene 逻辑里「假 且 未知」= 假，但审方场景不能这样——
一侧已命中违规条件、另一侧字段还没落地时，若按 Kleene 短路判成合规，
就会把一条待核的处方放过去。

**不使用动态执行**：本模块只按 AST 遍历求值，不解释字符串、不调用任意函数；
规则行由药事审核岗维护，表达式必须可复算、可审计、可定位。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from ..models import NUMERIC_FIELDS, PRESCRIPTION_FIELDS, PrescriptionRow
from .ast import BoolOp, Compare, FieldRef, Literal, Node, UnaryOp
from .cache import ParseTreeCache, default_cache

# ---------------------------------------------------------------------------
# 显式否定标记
# ---------------------------------------------------------------------------

#: 字段有值但值表示「没有 / 未做 / 不详」的标记（皮试「未注明」「未做」、
#: 处方权「否」等）。这类值**不是**缺失：它是一条已知的否定信息。
#:
#: 词表只收院内医嘱落地文件里真实出现的写法，不自造药品知识；
#: 归一后的别名由 ``expression/normalize.py`` 负责，本表是求值层的兜底口径。
DEFAULT_EXPLICIT_NO_MARKERS: frozenset[str] = frozenset(
    {
        "未注明",
        "未做",
        "未行",
        "未查",
        "未填",
        "未标注",
        "未做皮试",
        "未知",
        "不详",
        "否",
        "无",
        "没有",
        "na",
        "n/a",
        "null",
        "none",
        "-",
        "—",
        "/",
    }
)

#: 标记集的小写折叠形式（比对时统一折叠，避免大小写写法漏判）。
_DEFAULT_MARKERS_FOLDED: frozenset[str] = frozenset(
    marker.casefold() for marker in DEFAULT_EXPLICIT_NO_MARKERS
)

#: 数值字段的单位后缀容错（``3日量`` → ``3``），与 ``models.PrescriptionRow`` 同口径。
#: 这里手写扫描而不引正则：表达式子包禁止出现任何动态执行相关的调用形态，
#: 保持「整个子包只做静态处理」的观感一致，便于静态扫描一刀切。
def _leading_number(text: str) -> Decimal | None:
    """取文本开头的数值（容忍单位后缀）；取不到返回 ``None``。"""
    body = text.lstrip()
    sign = ""
    if body[:1] in ("+", "-"):
        sign = body[0]
        body = body[1:]
    collected: list[str] = []
    dot_seen = False
    for char in body:
        if char.isdigit():
            collected.append(char)
        elif char == "." and collected and not dot_seen:
            dot_seen = True
            collected.append(char)
        else:
            break
    number_text = "".join(collected).rstrip(".")
    if not number_text:
        return None
    try:
        value = Decimal(number_text)
    except InvalidOperation:
        return None
    return -value if sign == "-" else value


def _fold_markers(markers: Iterable[str] | None) -> frozenset[str]:
    """标记集归一（去空白 + 大小写折叠）；``None`` 表示用默认词表。"""
    if markers is None:
        return _DEFAULT_MARKERS_FOLDED
    return frozenset(
        text.strip().casefold() for text in (str(m) for m in markers) if text.strip()
    )


# ---------------------------------------------------------------------------
# 值域与结果
# ---------------------------------------------------------------------------


class FieldState(str, Enum):
    """字段取值的空值语义三档。"""

    MISSING = "缺失"
    EXPLICIT_NO = "显式否"
    VALUE = "有值"


class EvalState(str, Enum):
    """表达式求值的三态结论。

    ``TRUE`` / ``FALSE`` 是布尔结论，``MISSING`` 是「判不了」——它必须与
    「判为假」严格分开，否则缺字段会被静默判成合规。
    """

    TRUE = "真"
    FALSE = "假"
    MISSING = "不可判定"


@dataclass(frozen=True)
class EvaluationResult:
    """一次表达式求值的结果：三态结论 + 归因 + 缺值诊断。"""

    state: EvalState
    reason: str = ""
    missing_fields: tuple[str, ...] = ()
    explicit_no_fields: tuple[str, ...] = ()
    uncomparable_fields: tuple[str, ...] = ()
    expression: str = ""

    @property
    def is_true(self) -> bool:
        """条件成立。"""
        return self.state is EvalState.TRUE

    @property
    def is_false(self) -> bool:
        """条件不成立。"""
        return self.state is EvalState.FALSE

    @property
    def is_missing(self) -> bool:
        """判不了（字段缺失 / 口径不可比）。"""
        return self.state is EvalState.MISSING


class EvaluationError(RuntimeError):
    """求值期的**配置错误**（如表达式引用了不存在的医嘱字段）。

    与「数据缺字段」严格分列：配置错误不得降级成不可判定，
    否则规则行写错会伪装成业务上的「审不了」。
    """

    def __init__(self, message: str, *, field: str = "", line: int = 1, column: int = 1) -> None:
        self.field = field
        self.line = line
        self.column = column
        location = f"（第 {line} 行第 {column} 列）"
        super().__init__(f"{message}{location}")


# ---------------------------------------------------------------------------
# 内部结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldResolution:
    """一个字段引用在求值上下文里的解析结果。"""

    name: str
    state: FieldState
    raw: str = ""
    number: Decimal | None = None


@dataclass(frozen=True)
class _Side:
    """比较式的一侧：字段引用或字面量。"""

    is_field: bool
    name: str = ""
    state: FieldState = FieldState.VALUE
    raw: str = ""
    number: Decimal | None = None
    is_marker: bool = False


@dataclass(frozen=True)
class _Outcome:
    """求值过程中的中间结论（尚未汇总成对外文案）。"""

    state: EvalState
    reason: str = ""
    missing_fields: tuple[str, ...] = ()
    explicit_no_fields: tuple[str, ...] = ()
    uncomparable_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Context:
    """求值上下文：字段取值 + 显式否定标记词表。"""

    data: Mapping[str, str]
    markers: frozenset[str]


# ---------------------------------------------------------------------------
# 字段解析
# ---------------------------------------------------------------------------


def _numeric_view(name: str, text: str) -> Decimal | None:
    """字段取值的数值视图；不可解析返回 ``None``。

    数值字段（``dosage_value`` / ``quantity`` / ``days_qty`` / ``age_value``）
    容忍单位后缀（``3日量`` → ``3``）；其余字段按整串解析。
    """
    if name in NUMERIC_FIELDS:
        return _leading_number(text)
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def resolve_field(
    name: str,
    context: Mapping[str, str],
    *,
    markers: Iterable[str] | None = None,
) -> FieldResolution:
    """把字段引用解析成 :class:`FieldResolution`（三档空值语义）。

    * 字段名不在医嘱列全集内 → 抛 :class:`EvaluationError`（配置错误）；
    * 取值为空串 / 纯空白 / 缺列 → ``MISSING``；
    * 取值是显式否定标记 → ``EXPLICIT_NO``；
    * 其余 → ``VALUE``。
    """
    if name not in PRESCRIPTION_FIELDS:
        raise EvaluationError(
            f"表达式引用了不存在的医嘱字段 {name!r}"
            f"（落地文件共 {len(PRESCRIPTION_FIELDS)} 列）",
            field=name,
        )
    raw = context.get(name, "")
    text = "" if raw is None else str(raw).strip()
    if not text:
        return FieldResolution(name, FieldState.MISSING, "")
    if text.casefold() in _fold_markers(markers):
        return FieldResolution(name, FieldState.EXPLICIT_NO, text)
    return FieldResolution(name, FieldState.VALUE, text, _numeric_view(name, text))


def context_from_row(row: PrescriptionRow) -> dict[str, str]:
    """把一条医嘱行摊平成求值上下文（列名 → 原始文本）。"""
    return {name: row.value(name) for name in PRESCRIPTION_FIELDS}


# ---------------------------------------------------------------------------
# 比较
# ---------------------------------------------------------------------------


def _apply_numeric(operator: str, left: Decimal, right: Decimal) -> bool:
    """数值比较。"""
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    raise EvaluationError(f"不支持的比较运算符 {operator!r}")


def _apply_text(operator: str, left: str, right: str) -> bool:
    """文本比较（等值类）。"""
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    raise EvaluationError(
        f"比较运算符 {operator!r} 需要两侧都是数值，本次比较的是文本"
    )


def _side_of(node: Node, context: _Context) -> _Side:
    """把比较式的一侧解析成 :class:`_Side`。"""
    if isinstance(node, FieldRef):
        resolution = resolve_field(
            node.name, context.data, markers=context.markers
        )
        return _Side(
            is_field=True,
            name=resolution.name,
            state=resolution.state,
            raw=resolution.raw,
            number=resolution.number,
            is_marker=resolution.state is FieldState.EXPLICIT_NO,
        )
    if isinstance(node, Literal):
        text = node.text
        number = node.value if node.kind == "number" else None
        if number is None:
            number = _numeric_view("", text)
        return _Side(
            is_field=False,
            state=FieldState.VALUE,
            raw=text,
            number=number,
            is_marker=bool(text.strip()) and text.strip().casefold() in context.markers,
        )
    raise EvaluationError(
        f"比较式的操作数类型不受支持：{type(node).__name__}",
        line=node.line,
        column=node.column,
    )


def _evaluate_compare(node: Compare, context: _Context) -> _Outcome:
    """求值一条比较式。"""
    left = _side_of(node.left, context)
    right = _side_of(node.right, context)

    # 1) 任一侧缺失 → 不可判定（不抛错、不静默判假）
    missing = tuple(
        side.name for side in (left, right) if side.is_field and side.state is FieldState.MISSING
    )
    if missing:
        names = "、".join(missing)
        return _Outcome(
            EvalState.MISSING,
            reason=f"字段 {names} 缺失，无法完成判定",
            missing_fields=missing,
        )

    # 2) 字段侧是显式否定值 → 条件不成立即违规（不静默判合规）
    explicit_no = tuple(
        side.name
        for side in (left, right)
        if side.is_field and side.state is FieldState.EXPLICIT_NO
    )
    if explicit_no:
        marker_side = left if left.is_field and left.state is FieldState.EXPLICIT_NO else right
        other = right if marker_side is left else left
        if other.is_marker:
            # 两侧都是显式否定语义 → 按文本比较（规则若写的就是这个标记）
            result = _compare_text(node.operator, marker_side.raw, other.raw)
            return _Outcome(
                EvalState.TRUE if result else EvalState.FALSE,
                explicit_no_fields=explicit_no,
            )
        names = "、".join(explicit_no)
        return _Outcome(
            EvalState.TRUE,
            reason=(
                f"字段 {names} 为显式否定值「{marker_side.raw}」，"
                "无法证明满足条件，按违规处理"
            ),
            explicit_no_fields=explicit_no,
        )

    # 3) 两侧都有值 → 正常比较
    if left.number is not None and right.number is not None:
        result = _apply_numeric(node.operator, left.number, right.number)
    elif node.operator in ("==", "!="):
        result = _apply_text(node.operator, left.raw.strip(), right.raw.strip())
    else:
        # 序比较但两侧不是数值 → 口径不可比，仍归不可判定
        field_sides = tuple(side for side in (left, right) if side.is_field)
        detail = field_sides[0].raw if field_sides else f"{left.raw} 与 {right.raw}"
        label = "、".join(side.name for side in field_sides)
        subject = f"字段 {label} 的取值" if field_sides else "比较两侧的取值"
        return _Outcome(
            EvalState.MISSING,
            reason=f"{subject}「{detail}」与比较口径不可比，无法完成判定",
            uncomparable_fields=tuple(side.name for side in field_sides),
        )
    return _Outcome(EvalState.TRUE if result else EvalState.FALSE)


def _compare_text(operator: str, left: str, right: str) -> bool:
    """显式否定语义之间的文本比较。"""
    if operator in ("==", "!="):
        return _apply_text(operator, left.strip(), right.strip())
    raise EvaluationError(
        f"比较运算符 {operator!r} 需要两侧都是数值，本次比较的是文本"
    )


# ---------------------------------------------------------------------------
# 逻辑连接与一元否定
# ---------------------------------------------------------------------------


def _evaluate_boolop(node: BoolOp, context: _Context) -> _Outcome:
    """求值逻辑连接（``and`` / ``or``）。"""
    outcomes = [_evaluate_node(operand, context) for operand in node.operands]

    missing = _merge(outcome.missing_fields for outcome in outcomes)
    explicit_no = _merge(outcome.explicit_no_fields for outcome in outcomes)
    uncomparable = _merge(outcome.uncomparable_fields for outcome in outcomes)

    if node.operator == "and":
        # 刻意不做短路：任一侧缺失 → 整体不可判定
        if missing:
            names = "、".join(missing)
            return _Outcome(
                EvalState.MISSING,
                reason=f"字段 {names} 缺失，无法完成判定",
                missing_fields=missing,
                explicit_no_fields=explicit_no,
                uncomparable_fields=uncomparable,
            )
        state = (
            EvalState.TRUE
            if all(outcome.state is EvalState.TRUE for outcome in outcomes)
            else EvalState.FALSE
        )
        return _Outcome(
            state, explicit_no_fields=explicit_no, uncomparable_fields=uncomparable
        )

    # or：一侧为真 → 整体为真；否则有缺失 → 整体不可判定
    if any(outcome.state is EvalState.TRUE for outcome in outcomes):
        return _Outcome(
            EvalState.TRUE, explicit_no_fields=explicit_no, uncomparable_fields=uncomparable
        )
    if missing:
        names = "、".join(missing)
        return _Outcome(
            EvalState.MISSING,
            reason=f"字段 {names} 缺失，无法完成判定",
            missing_fields=missing,
            explicit_no_fields=explicit_no,
            uncomparable_fields=uncomparable,
        )
    if any(outcome.state is EvalState.MISSING for outcome in outcomes):
        return _Outcome(
            EvalState.MISSING,
            reason="存在无法完成判定的条件，整体不可判定",
            explicit_no_fields=explicit_no,
            uncomparable_fields=uncomparable,
        )
    return _Outcome(EvalState.FALSE, explicit_no_fields=explicit_no)


def _evaluate_unary(node: UnaryOp, context: _Context) -> _Outcome:
    """求值一元否定（``not``）；缺失照原样传播。"""
    outcome = _evaluate_node(node.operand, context)
    if outcome.state is EvalState.MISSING:
        return outcome
    return _Outcome(
        EvalState.FALSE if outcome.state is EvalState.TRUE else EvalState.TRUE,
        missing_fields=outcome.missing_fields,
        explicit_no_fields=outcome.explicit_no_fields,
        uncomparable_fields=outcome.uncomparable_fields,
    )


def _merge(groups: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    """合并多组字段名（去重、保持首次出现顺序）。"""
    seen: dict[str, None] = {}
    for group in groups:
        for name in group:
            seen.setdefault(name, None)
    return tuple(seen)


def _evaluate_node(node: Node, context: _Context) -> _Outcome:
    """按 AST 分派求值。"""
    if isinstance(node, Compare):
        return _evaluate_compare(node, context)
    if isinstance(node, BoolOp):
        return _evaluate_boolop(node, context)
    if isinstance(node, UnaryOp):
        return _evaluate_unary(node, context)
    if isinstance(node, FieldRef):
        raise EvaluationError(
            f"表达式叶子必须是比较式（{node.name} 缺少比较运算符）",
            field=node.name,
            line=node.line,
            column=node.column,
        )
    raise EvaluationError(
        "表达式叶子必须是比较式",
        line=node.line,
        column=node.column,
    )


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def evaluate_ast(
    node: Node,
    context: Mapping[str, str],
    *,
    source: str = "",
    markers: Iterable[str] | None = None,
) -> EvaluationResult:
    """对已解析的 AST 求值。

    ``context`` 是「列名 → 原始文本」的映射；缺列视为该字段缺失。
    解析树缓存（``expression/cache.py``）命中时走本函数，不必重复解析。
    """
    resolved = _Context(data=context, markers=_fold_markers(markers))
    outcome = _evaluate_node(node, resolved)
    reason = outcome.reason
    if not reason:
        if outcome.state is EvalState.TRUE:
            reason = f"判定条件「{source}」成立" if source else "判定条件成立"
        elif outcome.state is EvalState.FALSE:
            reason = f"判定条件「{source}」不成立" if source else "判定条件不成立"
    return EvaluationResult(
        state=outcome.state,
        reason=reason,
        missing_fields=outcome.missing_fields,
        explicit_no_fields=outcome.explicit_no_fields,
        uncomparable_fields=outcome.uncomparable_fields,
        expression=source,
    )


def evaluate_expression(
    expression: str,
    context: Mapping[str, str],
    *,
    markers: Iterable[str] | None = None,
    cache: ParseTreeCache | None = None,
) -> EvaluationResult:
    """求值一条规则表达式（文本 → 解析 → 求值）。

    语法错误 / 能力越界由 ``parser.py`` 与 ``validator.py`` 负责，
    本函数只处理「表达式站得住但数据有三种空值形态」的情形。

    解析树走 ``cache``（缺省用进程级默认缓存）：同一段表达式文本只解析一次，
    后面每条医嘱行都直接复用。缓存不存求值结果——求值依赖行上下文，
    缓存结果会引入过期语义。
    """
    tree = (cache if cache is not None else default_cache()).get(expression)
    return evaluate_ast(tree, context, source=expression, markers=markers)


__all__ = [
    "DEFAULT_EXPLICIT_NO_MARKERS",
    "EvalState",
    "EvaluationError",
    "EvaluationResult",
    "FieldResolution",
    "FieldState",
    "context_from_row",
    "evaluate_ast",
    "evaluate_expression",
    "resolve_field",
]
