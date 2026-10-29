"""受限表达式的递归下降解析器：token 序列 → AST。

**对应源产品能力**：源仓库 ``web-frontend/modules/core/formula/index.js`` 的
``getCachedParseTree()`` → ``parseBaserowFormula()``（切词结果交给语法解析器
产出解析树，再交给求值访问器）。本轮把「公式解析树」改写成「审方规则表达式树」，
并在语法层就把能力边界卡死。

语法（优先级由低到高）::

    expr       := or_expr
    or_expr    := and_expr ( 'or' and_expr )*
    and_expr   := not_expr ( 'and' not_expr )*
    not_expr   := 'not' not_expr | comparison
    comparison := primary [ COMP_OP primary ]
    primary    := '(' expr ')' | NUMBER | STRING | IDENT

两条口径：

* **叶子必须是比较式** —— 表达式求值出来是一个布尔结论；"字段非空"这类真值判断
  不在本轮口径内，否则除了归一层（``normalize.py``）之外又多一套隐式语义。
* **能力越界显式拒绝** —— 函数调用、属性访问、赋值、多语句、保留字一律抛
  :class:`~med_rule_fieldmap.expression.errors.CapabilityError`，由 ``validator.py``
  统一翻译成带行列的配置错误，不在这里"顺手支持"。
"""

from __future__ import annotations

from decimal import Decimal

from .ast import BoolOp, Compare, FieldRef, Literal, Node, UnaryOp
from .errors import CapabilityError, ParseError
from .lexer import Token, TokenType, tokenize

#: 记号类型 → 被禁用的能力名（本轮禁用的语法一律在此登记，便于统一报错口径）。
_CAPABILITY_BY_TOKEN: dict[TokenType, str] = {
    TokenType.ASSIGN: "赋值",
    TokenType.COMMA: "参数列表（函数调用）",
    TokenType.SEMICOLON: "多语句分隔符",
    TokenType.DOT: "属性访问",
    TokenType.KEYWORD: "保留字",
}


class Parser:
    """递归下降解析器；一次解析一棵表达式树。"""

    def __init__(self, tokens: list[Token], *, source: str = "") -> None:
        self._tokens = tokens
        self._index = 0
        self._source = source

    # -- 记号操作 ---------------------------------------------------------

    def _peek(self, offset: int = 0) -> Token:
        index = self._index + offset
        if index < len(self._tokens):
            return self._tokens[index]
        return self._tokens[-1]

    def _advance(self) -> Token:
        token = self._peek()
        if token.type is not TokenType.EOF:
            self._index += 1
        return token

    def _guard(self, token: Token) -> None:
        """能力越界检查：命中即抛错并定位到行列。"""
        capability = _CAPABILITY_BY_TOKEN.get(token.type)
        if capability is None:
            return
        detail = f"{capability} {token.value!r}" if token.type is TokenType.KEYWORD else capability
        raise CapabilityError(
            f"表达式不允许的能力：{detail}",
            token.line,
            token.column,
            source=self._source,
            capability=capability,
        )

    # -- 文法 -------------------------------------------------------------

    def parse(self) -> Node:
        """解析整条表达式；成功后返回 AST 根节点。"""
        if self._peek().type is TokenType.EOF:
            raise ParseError("表达式为空", 1, 1, source=self._source)
        node = self._parse_or()
        token = self._peek()
        self._guard(token)
        if token.type is not TokenType.EOF:
            raise ParseError(
                f"表达式结尾有多余的记号 {token.value!r}",
                token.line,
                token.column,
                source=self._source,
            )
        return node

    def _parse_or(self) -> Node:
        operands = [self._parse_and()]
        while self._peek().type is TokenType.OR:
            operator = self._advance()
            self._require_operand(operator)
            operands.append(self._parse_and())
        if len(operands) == 1:
            return operands[0]
        return BoolOp(
            "or", tuple(operands), line=operands[0].line, column=operands[0].column
        )

    def _parse_and(self) -> Node:
        operands = [self._parse_not()]
        while self._peek().type is TokenType.AND:
            operator = self._advance()
            self._require_operand(operator)
            operands.append(self._parse_not())
        if len(operands) == 1:
            return operands[0]
        return BoolOp(
            "and", tuple(operands), line=operands[0].line, column=operands[0].column
        )

    def _parse_not(self) -> Node:
        token = self._peek()
        if token.type is TokenType.NOT:
            self._advance()
            self._require_operand(token)
            operand = self._parse_not()
            return UnaryOp("not", operand, line=token.line, column=token.column)
        return self._parse_comparison()

    def _parse_comparison(self) -> Node:
        token = self._peek()
        if token.type is TokenType.LPAREN:
            # 括号组本身就是一个完整的布尔表达式，不再要求后面跟比较运算符
            self._advance()
            node = self._parse_or()
            closing = self._peek()
            if closing.type is not TokenType.RPAREN:
                raise ParseError(
                    "括号未闭合（缺少右括号）",
                    token.line,
                    token.column,
                    source=self._source,
                )
            self._advance()
            self._reject_adjacent_operand()
            return node

        left = self._parse_primary("左操作数")
        operator = self._peek()
        self._guard(operator)
        if operator.type is TokenType.OP:
            self._advance()
            if self._peek().type is TokenType.EOF:
                # 定位到运算符本身：报错指向 ">" 比指向行尾更好改
                raise ParseError(
                    "缺少右操作数",
                    operator.line,
                    operator.column,
                    source=self._source,
                )
            right = self._parse_primary("右操作数")
            node = Compare(
                operator.value, left, right, line=left.line, column=left.column
            )
        elif operator.type in (
            TokenType.NUMBER,
            TokenType.STRING,
            TokenType.IDENT,
            TokenType.LPAREN,
        ):
            raise ParseError(
                f"缺少比较运算符（{operator.value!r} 之前需要 == / != / > / >= / < / <=）",
                operator.line,
                operator.column,
                source=self._source,
            )
        else:
            raise ParseError(
                "表达式叶子必须是比较式（形如 days_qty > 1）",
                left.line,
                left.column,
                source=self._source,
            )
        self._reject_adjacent_operand()
        return node

    def _reject_adjacent_operand(self) -> None:
        """两个操作数紧挨着（中间没有运算符）属语法错误，点名后一个操作数。"""
        token = self._peek()
        if token.type in (TokenType.NUMBER, TokenType.STRING, TokenType.IDENT):
            raise ParseError(
                f"缺少比较运算符（{token.value!r} 之前需要 == / != / > / >= / < / <=）",
                token.line,
                token.column,
                source=self._source,
            )

    def _parse_primary(self, role: str = "操作数") -> Node:
        token = self._peek()
        if token.type is TokenType.EOF:
            raise ParseError(
                f"缺少{role}", token.line, token.column, source=self._source
            )
        self._guard(token)

        if token.type is TokenType.NUMBER:
            self._advance()
            return Literal(
                value=Decimal(token.value),
                kind="number",
                line=token.line,
                column=token.column,
            )

        if token.type is TokenType.STRING:
            self._advance()
            return Literal(
                value=token.value,
                kind="string",
                line=token.line,
                column=token.column,
            )

        if token.type is TokenType.IDENT:
            self._advance()
            if self._peek().type is TokenType.LPAREN:
                raise CapabilityError(
                    "表达式不允许的能力：函数调用",
                    token.line,
                    token.column,
                    source=self._source,
                    capability="函数调用",
                )
            return FieldRef(
                path=tuple(token.value.split(".")),
                line=token.line,
                column=token.column,
            )

        if token.type is TokenType.LPAREN:
            self._advance()
            node = self._parse_or()
            closing = self._peek()
            if closing.type is not TokenType.RPAREN:
                raise ParseError(
                    "括号未闭合（缺少右括号）",
                    token.line,
                    token.column,
                    source=self._source,
                )
            self._advance()
            return node

        raise ParseError(
            f"无法解析的记号 {token.value!r}",
            token.line,
            token.column,
            source=self._source,
        )

    def _require_operand(self, operator: Token) -> None:
        """逻辑连接词后面必须还有操作数。"""
        if self._peek().type is TokenType.EOF:
            raise ParseError(
                f"运算符 {operator.value!r} 后缺少操作数",
                operator.line,
                operator.column,
                source=self._source,
            )


def parse(source: str, *, tokens: list[Token] | None = None) -> Node:
    """把表达式文本解析成 AST。

    词法错误抛 :class:`~med_rule_fieldmap.expression.errors.LexError`，
    语法错误抛 :class:`~med_rule_fieldmap.expression.errors.ParseError`，
    能力越界抛 :class:`~med_rule_fieldmap.expression.errors.CapabilityError`；
    三者都带行列定位。
    """
    resolved = tokenize(source) if tokens is None else tokens
    return Parser(resolved, source=source).parse()


__all__ = ["Parser", "parse"]
