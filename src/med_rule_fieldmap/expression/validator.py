"""受限表达式的载入期预检：能力禁用清单 + 字段依赖声明核对。

**对应源产品能力**：源仓库 ``core/workflow_actions/registries.py`` 的
``WorkflowActionType.prepare_values()``（落库前把配置值统一转换并做校验）与
``raise_if_misconfigured()``（拒绝跑不起来的已保存配置）。本轮把「工作流动作」
改写成「审方规则行」：规则行在**载入期**就被检查两件事——

1. **能力禁用清单**（:data:`CAPABILITY_BAN_LIST`）：表达式只允许比较式；
   函数调用 / 参数列表 / 属性访问 / 赋值 / 多语句分隔符 / 导入语句 / 动态执行 /
   保留字一律显式拒绝，并定位到行列。
2. **字段依赖声明核对**：表达式引用的每个字段都必须在规则行的
   ``required_fields`` 里声明过，否则判为**配置错误**。

为什么要这条核对：引擎靠 ``required_fields`` 判断「这条处方缺哪个字段 →
落不可判定」。如果表达式读了某个没声明的字段，该字段缺失时规则仍会去比一个
空值，结论就从「不可判定」退化成误判。**配置错误必须在载入期炸掉**，
而不是等到某个批次才在台账里冒出来。

两类错误必须分列（本轮 10x 楔子的前提）：

* **配置错误**（本模块）—— 规则行本身写错了，规则**不进入生效集**，
  错误落在规则载入报告里（``rules/repository.py`` 的 ``errors``）；
* **数据缺字段**（``engine/judge.py``）—— 规则写对了但这条医嘱缺字段，
  结论落「不可判定」并进判定台账的 ``missing_fields`` 列。

本模块只做**静态**检查：不求值、不访问数据、不改状态、不使用
``eval`` / ``exec`` / ``compile``。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..models import PRESCRIPTION_FIELDS
from .ast import FieldRef, Node
from .cache import ParseTreeCache, default_cache
from .errors import CapabilityError, LexError, ParseError
from .lexer import Token, TokenType, tokenize

# ---------------------------------------------------------------------------
# 能力禁用清单
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityBan:
    """一条能力禁用条目：能力名 + 为什么禁。"""

    name: str
    reason: str


CAPABILITY_BAN_LIST: tuple[CapabilityBan, ...] = (
    CapabilityBan("函数调用", "规则表达式只能是比较式，不得调用任意函数"),
    CapabilityBan("参数列表", "比较式不接受参数列表"),
    CapabilityBan("属性访问", "只能引用医嘱落地文件的列名，不得访问对象属性"),
    CapabilityBan("赋值", "规则表达式是只读的求值式，不是赋值语句"),
    CapabilityBan("多语句分隔符", "一条规则行只能有一个表达式"),
    CapabilityBan("导入语句", "不得在规则表达式里引入模块"),
    CapabilityBan("动态执行", "不得在规则表达式里执行动态代码"),
    CapabilityBan("保留字", "该关键字不在表达式语法内"),
)

#: 能力名 → 禁用理由（报错文案直接引用，避免各说各话）。
_BAN_REASONS: dict[str, str] = {ban.name: ban.reason for ban in CAPABILITY_BAN_LIST}

#: 保留字 → 更精确的能力名（import / eval 这类要指名道姓，不能笼统说「保留字」）。
_KEYWORD_CAPABILITIES: dict[str, str] = {
    "import": "导入语句",
    "from": "导入语句",
    "exec": "动态执行",
    "eval": "动态执行",
}


def capability_ban_table() -> tuple[tuple[str, str], ...]:
    """能力禁用清单（名称, 理由），供 CLI ``rules validate`` 展示。"""
    return tuple((ban.name, ban.reason) for ban in CAPABILITY_BAN_LIST)


# ---------------------------------------------------------------------------
# 问题类型
# ---------------------------------------------------------------------------

#: 命中禁用能力。
KIND_CAPABILITY = "能力越界"
#: 词法层不成立（非法字符、引号未闭合）。
KIND_LEX = "词法错误"
#: 语法层不成立（缺操作数、括号不配对、叶子不是比较式）。
KIND_SYNTAX = "语法错误"
#: 表达式引用了规则行 ``required_fields`` 未声明的字段。
KIND_UNDECLARED_FIELD = "未声明字段"
#: 表达式引用了医嘱落地文件里不存在的列。
KIND_UNKNOWN_FIELD = "未知字段"

#: 属于「配置错误」的问题类型；与运行期的「数据缺字段」严格分列。
CONFIG_ERROR_KINDS: frozenset[str] = frozenset(
    {
        KIND_CAPABILITY,
        KIND_LEX,
        KIND_SYNTAX,
        KIND_UNDECLARED_FIELD,
        KIND_UNKNOWN_FIELD,
    }
)

#: 语法类问题（不含字段声明问题）——用来判断表达式本身是否还站得住。
_SYNTAX_KINDS: frozenset[str] = frozenset({KIND_CAPABILITY, KIND_LEX, KIND_SYNTAX})


@dataclass(frozen=True)
class ExpressionIssue:
    """一条表达式预检问题；带行列定位、字段名与规则号。"""

    kind: str
    message: str
    line: int = 1
    column: int = 1
    field: str | None = None
    rule_id: str | None = None
    capability: str | None = None

    @property
    def is_config_error(self) -> bool:
        """是否属于「配置错误」（规则行本身写错了）。"""
        return self.kind in CONFIG_ERROR_KINDS

    def render(self) -> str:
        """渲染成带定位的可读文本（回写规则行报错时直接用）。"""
        prefix = f"规则 {self.rule_id}：" if self.rule_id else ""
        return f"{prefix}{self.message}（第 {self.line} 行第 {self.column} 列）"


class ExpressionConfigError(ValueError):
    """表达式配置错误（载入期）；``issues`` 逐条给出定位信息。"""

    def __init__(self, issues: Sequence[ExpressionIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("；".join(issue.render() for issue in self.issues))


# ---------------------------------------------------------------------------
# 能力扫描
# ---------------------------------------------------------------------------


def _keyword_capability(word: str) -> str:
    """保留字 → 精确的能力名。"""
    return _KEYWORD_CAPABILITIES.get(word, "保留字")


def _scan_capabilities(tokens: list[Token]) -> list[ExpressionIssue]:
    """静态扫描 token 流，列出命中的禁用能力。

    为什么在解析器之外再扫一遍：解析器遇到第一个越界语法就抛错，
    只报出**第一处**；本函数把整条表达式里命中的禁用能力**全部**列出来，
    药事审核岗改一次就能改干净。
    """
    issues: list[ExpressionIssue] = []
    for index, token in enumerate(tokens):
        capability: str | None = None
        if (
            token.type is TokenType.IDENT
            and index + 1 < len(tokens)
            and tokens[index + 1].type is TokenType.LPAREN
        ):
            capability = "函数调用"
        elif token.type is TokenType.COMMA:
            capability = "参数列表"
        elif token.type is TokenType.DOT:
            capability = "属性访问"
        elif token.type is TokenType.ASSIGN:
            capability = "赋值"
        elif token.type is TokenType.SEMICOLON:
            capability = "多语句分隔符"
        elif token.type is TokenType.KEYWORD:
            capability = _keyword_capability(token.value)
        if capability is None:
            continue
        reason = _BAN_REASONS.get(capability, "")
        issues.append(
            ExpressionIssue(
                kind=KIND_CAPABILITY,
                message=f"表达式不允许的能力：{capability}（{reason}）",
                line=token.line,
                column=token.column,
                capability=capability,
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 解析（词法 + 能力 + 语法）
# ---------------------------------------------------------------------------


def _parse_expression(
    text: str,
    *,
    rule_id: str | None = None,
    cache: ParseTreeCache | None = None,
) -> tuple[Node | None, list[ExpressionIssue]]:
    """切词 → 能力扫描 → 语法解析；返回 (AST, 问题列表)。

    AST 为 ``None`` 表示表达式本身站不住（词法 / 能力 / 语法有问题）。

    语法解析走 ``cache``（缺省用进程级默认缓存）：规则文件反复热重载时，
    表达式文本没变的那些规则不必重新解析。切词仍在这里做——能力扫描需要
    token 列表，缓存只存解析树不存 token。
    """
    try:
        tokens = tokenize(text)
    except LexError as exc:
        return None, [
            ExpressionIssue(
                kind=KIND_LEX,
                message=exc.message,
                line=exc.line,
                column=exc.column,
                rule_id=rule_id,
            )
        ]

    capability_issues = _scan_capabilities(tokens)
    if capability_issues:
        return None, [
            ExpressionIssue(
                kind=issue.kind,
                message=issue.message,
                line=issue.line,
                column=issue.column,
                capability=issue.capability,
                rule_id=rule_id,
            )
            for issue in capability_issues
        ]

    try:
        tree = (cache if cache is not None else default_cache()).get(text, tokens=tokens)
    except ParseError as exc:
        return None, [
            ExpressionIssue(
                kind=KIND_SYNTAX,
                message=exc.message,
                line=exc.line,
                column=exc.column,
                rule_id=rule_id,
            )
        ]
    except CapabilityError as exc:  # pragma: no cover - 能力扫描已先行拦截
        return None, [
            ExpressionIssue(
                kind=KIND_CAPABILITY,
                message=exc.message,
                line=exc.line,
                column=exc.column,
                capability=exc.capability,
                rule_id=rule_id,
            )
        ]
    return tree, []


def validate_expression_syntax(
    expression: str,
    *,
    rule_id: str | None = None,
    cache: ParseTreeCache | None = None,
) -> list[ExpressionIssue]:
    """只做词法 + 能力 + 语法三层检查（不碰字段声明）。

    返回空列表表示表达式本身站得住。
    """
    text = expression.strip()
    if not text:
        return [
            ExpressionIssue(kind=KIND_SYNTAX, message="表达式为空", rule_id=rule_id)
        ]
    _, issues = _parse_expression(text, rule_id=rule_id, cache=cache)
    return issues


# ---------------------------------------------------------------------------
# 字段依赖声明核对
# ---------------------------------------------------------------------------


def collect_field_refs(tree: Node) -> tuple[FieldRef, ...]:
    """按先序遍历收集 AST 里的字段引用（去重，保持首次出现顺序）。"""
    seen: dict[str, FieldRef] = {}
    for node in tree.walk():
        if isinstance(node, FieldRef) and node.name not in seen:
            seen[node.name] = node
    return tuple(seen.values())


def _field_issues(
    tree: Node,
    required_fields: Iterable[str],
    *,
    rule_id: str | None,
    known_fields: Iterable[str],
) -> list[ExpressionIssue]:
    """核对表达式引用的字段：既要在医嘱列全集内，也要在 ``required_fields`` 内声明。"""
    declared = {str(name).strip() for name in required_fields}
    known = {str(name).strip() for name in known_fields}
    issues: list[ExpressionIssue] = []
    for ref in collect_field_refs(tree):
        name = ref.name
        if name not in known:
            issues.append(
                ExpressionIssue(
                    kind=KIND_UNKNOWN_FIELD,
                    message=(
                        f"表达式引用了不存在的医嘱字段 {name!r}"
                        f"（落地文件共 {len(known)} 列，列名不含点号）"
                    ),
                    line=ref.line,
                    column=ref.column,
                    field=name,
                    rule_id=rule_id,
                )
            )
        elif name not in declared:
            issues.append(
                ExpressionIssue(
                    kind=KIND_UNDECLARED_FIELD,
                    message=(
                        f"表达式引用了未声明的字段 {name!r}"
                        "（规则行 required_fields 未声明该字段，"
                        "该字段缺失时无法落不可判定）"
                    ),
                    line=ref.line,
                    column=ref.column,
                    field=name,
                    rule_id=rule_id,
                )
            )
    return issues


def validate_field_declaration(
    expression: str,
    required_fields: Iterable[str] = (),
    *,
    rule_id: str | None = None,
    known_fields: Iterable[str] = PRESCRIPTION_FIELDS,
    cache: ParseTreeCache | None = None,
) -> list[ExpressionIssue]:
    """核对表达式引用的字段是否都在规则行 ``required_fields`` 内声明过。

    表达式本身语法不成立时返回空列表——语法问题由
    :func:`validate_expression_syntax` 报告，避免同一个问题报两遍。
    """
    text = expression.strip()
    if not text:
        return []
    tree, _ = _parse_expression(text, rule_id=rule_id, cache=cache)
    if tree is None:
        return []
    return _field_issues(
        tree, required_fields, rule_id=rule_id, known_fields=known_fields
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def validate_expression(
    expression: str,
    *,
    required_fields: Iterable[str] = (),
    rule_id: str | None = None,
    known_fields: Iterable[str] = PRESCRIPTION_FIELDS,
    check_declared_fields: bool = True,
    cache: ParseTreeCache | None = None,
) -> list[ExpressionIssue]:
    """载入期预检一条规则行的 ``expression``。

    返回空列表表示这条表达式可以直接生效；非空时每条问题都带
    **行列定位**，字段声明类问题还带**字段名**与**规则号**。

    ``check_declared_fields=False`` 时只查语法与能力（规则行自身的
    ``required_fields`` 声明已经非法时用，避免同一个字段报两遍）。
    """
    text = expression.strip()
    if not text:
        return [
            ExpressionIssue(kind=KIND_SYNTAX, message="表达式为空", rule_id=rule_id)
        ]
    tree, issues = _parse_expression(text, rule_id=rule_id, cache=cache)
    if tree is None:
        return issues
    if check_declared_fields:
        issues.extend(
            _field_issues(
                tree, required_fields, rule_id=rule_id, known_fields=known_fields
            )
        )
    return issues


def assert_valid_expression(
    expression: str,
    *,
    required_fields: Iterable[str] = (),
    rule_id: str | None = None,
    known_fields: Iterable[str] = PRESCRIPTION_FIELDS,
    check_declared_fields: bool = True,
    cache: ParseTreeCache | None = None,
) -> None:
    """校验不通过就抛 :class:`ExpressionConfigError`。"""
    issues = validate_expression(
        expression,
        required_fields=required_fields,
        rule_id=rule_id,
        known_fields=known_fields,
        check_declared_fields=check_declared_fields,
        cache=cache,
    )
    if issues:
        raise ExpressionConfigError(issues)


__all__ = [
    "CAPABILITY_BAN_LIST",
    "CONFIG_ERROR_KINDS",
    "KIND_CAPABILITY",
    "KIND_LEX",
    "KIND_SYNTAX",
    "KIND_UNDECLARED_FIELD",
    "KIND_UNKNOWN_FIELD",
    "CapabilityBan",
    "ExpressionConfigError",
    "ExpressionIssue",
    "assert_valid_expression",
    "capability_ban_table",
    "collect_field_refs",
    "validate_expression",
    "validate_expression_syntax",
    "validate_field_declaration",
]
