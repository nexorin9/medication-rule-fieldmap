"""受限表达式的词法分析：表达式文本 → token 序列。

**对应源产品能力**：源仓库 ``web-frontend/modules/core/formula/index.js`` 的
``getCachedParseTree()``（同一段公式文本共享一棵解析树，先切词再组树）与
``parseBaserowFormula()`` 的两段式流程。本轮把「单元格公式」改写成
「审方规则行的 ``expression``」，把「公式能干什么」收窄成审方场景真正需要的那几种。

本模块只做**切分**，不做语义判断：哪些能力被禁用、哪些字段未声明，
分别由 ``parser.py``（结构解析）与 ``validator.py``（载入期预检）负责。

切得出来的记号分两类：

* **本轮可用** —— 字段路径标识符、数字、字符串、六个比较运算符、
  ``and`` / ``or`` / ``not``、括号；
* **切得出来但本轮禁用** —— 逗号、点号（属性访问）、分号、赋值号、保留字。

第二类**不在这里悄悄丢弃**：它们各自成 token，由 parser 显式拒绝并定位到行列。
悄悄丢弃会让「写成 `days_qty > abs(1)`」这种表达式退化成一条看不懂的报错，
而报错必须能直接指出「这里写了不该写的东西」。

字符串支持转义（``\\'`` / ``\\"`` / ``\\\\`` / ``\\n`` / ``\\t``），
单行字符串跨行按词法错误处理（规则表达式不允许多行）。
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from .errors import LexError

# ---------------------------------------------------------------------------
# token 定义
# ---------------------------------------------------------------------------


class TokenType(Enum):
    """记号类型。"""

    IDENT = "标识符"
    NUMBER = "数字"
    STRING = "字符串"
    OP = "比较运算符"
    LPAREN = "左括号"
    RPAREN = "右括号"
    DOT = "点号"
    COMMA = "逗号"
    SEMICOLON = "分号"
    ASSIGN = "赋值号"
    AND = "and"
    OR = "or"
    NOT = "not"
    KEYWORD = "保留字"
    EOF = "结束"


@dataclass(frozen=True)
class Token:
    """一个记号；带行列定位。"""

    type: TokenType
    value: str
    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.type.value}({self.value!r})"


#: 比较运算符全集（本轮口径：== != > >= < <=）。
COMPARISON_OPERATORS: tuple[str, ...] = ("==", "!=", ">=", "<=", ">", "<")

#: 逻辑连接词 → 记号类型。
_LOGICAL_KEYWORDS: dict[str, TokenType] = {
    "and": TokenType.AND,
    "or": TokenType.OR,
    "not": TokenType.NOT,
}

#: 保留字：出现即属能力越界（import / def / lambda / …），切出来是为了能定位并拒绝。
RESERVED_WORDS: frozenset[str] = frozenset(
    {
        "import",
        "from",
        "def",
        "class",
        "lambda",
        "global",
        "nonlocal",
        "pass",
        "return",
        "raise",
        "try",
        "except",
        "finally",
        "with",
        "as",
        "del",
        "assert",
        "break",
        "continue",
        "for",
        "while",
        "if",
        "elif",
        "else",
        "in",
        "is",
        "None",
        "True",
        "False",
        "exec",
        "eval",
        "await",
        "yield",
    }
)

_IDENT_START = frozenset(string.ascii_letters + "_")
_IDENT_CHARS = frozenset(string.ascii_letters + string.digits + "_")
_DIGITS = frozenset(string.digits)
_QUOTES = ("'", '"')
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}


# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------


def tokenize(source: str) -> list[Token]:
    """把表达式文本切成 token 序列；末尾带一个 EOF 记号。

    非法字符、引号未闭合等词法错误抛 :class:`~med_rule_fieldmap.expression.errors.LexError`，
    并带上行列定位。
    """
    tokens: list[Token] = []
    line = 1
    column = 1
    index = 0
    length = len(source)

    while index < length:
        char = source[index]

        # 空白与换行
        if char == "\n":
            line += 1
            column = 1
            index += 1
            continue
        if char in " \t\r":
            index += 1
            column += 1
            continue

        # 比较运算符 / 赋值号
        if char in "=<>!":
            pair = source[index : index + 2]
            if pair in COMPARISON_OPERATORS:
                tokens.append(Token(TokenType.OP, pair, line, column))
                index += 2
                column += 2
                continue
            if char == "=":
                tokens.append(Token(TokenType.ASSIGN, "=", line, column))
                index += 1
                column += 1
                continue
            if char in "<>":
                tokens.append(Token(TokenType.OP, char, line, column))
                index += 1
                column += 1
                continue
            raise LexError(
                f"表达式含非法字符 {char!r}（'!' 只能出现在 '!=' 中）",
                line,
                column,
                source=source,
            )

        # 数字（允许前导正负号）
        if char in _DIGITS or (
            char in "+-" and index + 1 < length and source[index + 1] in _DIGITS
        ):
            start = index
            start_line = line
            start_column = column
            if char in "+-":
                index += 1
                column += 1
            while index < length and source[index] in _DIGITS:
                index += 1
                column += 1
            if (
                index + 1 < length
                and source[index] == "."
                and source[index + 1] in _DIGITS
            ):
                index += 1
                column += 1
                while index < length and source[index] in _DIGITS:
                    index += 1
                    column += 1
            text = source[start:index]
            try:
                Decimal(text)
            except InvalidOperation:  # pragma: no cover - 上面的扫描保证可解析
                raise LexError(
                    f"数字字面量不合法：{text!r}", start_line, start_column, source=source
                )
            tokens.append(Token(TokenType.NUMBER, text, start_line, start_column))
            continue

        # 字符串
        if char in _QUOTES:
            quote = char
            quote_line = line
            quote_column = column
            index += 1
            column += 1
            buffer: list[str] = []
            closed = False
            while index < length:
                current = source[index]
                if current == "\\":
                    if index + 1 >= length:
                        raise LexError(
                            "字符串以孤立的转义符结尾", line, column, source=source
                        )
                    buffer.append(_ESCAPES.get(source[index + 1], source[index + 1]))
                    index += 2
                    column += 2
                    continue
                if current == quote:
                    index += 1
                    column += 1
                    closed = True
                    break
                if current == "\n":
                    raise LexError(
                        "字符串未闭合（字符串不允许跨行）",
                        quote_line,
                        quote_column,
                        source=source,
                    )
                buffer.append(current)
                index += 1
                column += 1
            if not closed:
                raise LexError(
                    "字符串未闭合", quote_line, quote_column, source=source
                )
            tokens.append(
                Token(TokenType.STRING, "".join(buffer), quote_line, quote_column)
            )
            continue

        # 标识符与字段路径（形如 order.drug_code）
        if char in _IDENT_START:
            segments: list[str] = []
            start_line = line
            start_column = column
            while True:
                segment_start = index
                while index < length and source[index] in _IDENT_CHARS:
                    index += 1
                    column += 1
                segments.append(source[segment_start:index])
                if (
                    index < length
                    and source[index] == "."
                    and index + 1 < length
                    and source[index + 1] in _IDENT_START
                ):
                    index += 1
                    column += 1
                    continue
                break
            text = ".".join(segments)
            head = segments[0]
            if head in _LOGICAL_KEYWORDS:
                tokens.append(
                    Token(_LOGICAL_KEYWORDS[head], text, start_line, start_column)
                )
            elif head in RESERVED_WORDS:
                tokens.append(Token(TokenType.KEYWORD, head, start_line, start_column))
            else:
                tokens.append(Token(TokenType.IDENT, text, start_line, start_column))
            continue

        # 括号与标点
        if char == "(":
            tokens.append(Token(TokenType.LPAREN, char, line, column))
        elif char == ")":
            tokens.append(Token(TokenType.RPAREN, char, line, column))
        elif char == ",":
            tokens.append(Token(TokenType.COMMA, char, line, column))
        elif char == ".":
            tokens.append(Token(TokenType.DOT, char, line, column))
        elif char == ";":
            tokens.append(Token(TokenType.SEMICOLON, char, line, column))
        else:
            raise LexError(
                f"表达式含非法字符 {char!r}", line, column, source=source
            )
        index += 1
        column += 1

    tokens.append(Token(TokenType.EOF, "", line, column))
    return tokens


__all__ = [
    "COMPARISON_OPERATORS",
    "RESERVED_WORDS",
    "Token",
    "TokenType",
    "tokenize",
]
