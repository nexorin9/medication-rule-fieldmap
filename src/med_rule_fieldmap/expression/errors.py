"""受限表达式的错误类型。

分三层，**都带行列定位**：

* :class:`LexError` —— 词法层：非法字符、引号未闭合、字符串跨行。
* :class:`ParseError` —— 语法层：缺少操作数、括号不配对、叶子不是比较式。
* :class:`CapabilityError` —— 能力层：函数调用、属性访问、赋值、多语句、保留字。

为什么定位信息是硬要求：规则行的 ``expression`` 由药事审核岗手工维护，
一句「表达式不合法」他没法自己改对；「第 1 行第 12 列缺少右操作数」才能。

错误基类是 :class:`ExpressionLanguageError`，与 ``engine/judge.py`` 里的
``ExpressionError``（判定期「表达式不被支持或引用了不存在的字段」）不是一回事，
两者互不继承，避免调用方误 catch。
"""

from __future__ import annotations


class ExpressionLanguageError(ValueError):
    """受限表达式语言错误的基类；带行列定位。"""

    def __init__(
        self,
        message: str,
        line: int = 1,
        column: int = 1,
        *,
        source: str = "",
    ) -> None:
        self.message = message
        self.line = line
        self.column = column
        self.source = source
        super().__init__(self.render())

    def render(self) -> str:
        """渲染成带定位的可读文本（回写规则行报错时直接用）。"""
        prefix = f"表达式 {self.source!r}：" if self.source else ""
        return f"{prefix}{self.message}（第 {self.line} 行第 {self.column} 列）"

    @property
    def location(self) -> str:
        """定位短句，供 CLI 表格化输出。"""
        return f"第 {self.line} 行第 {self.column} 列"


class LexError(ExpressionLanguageError):
    """词法错误：非法字符、引号未闭合、字符串跨行。"""


class ParseError(ExpressionLanguageError):
    """语法错误：结构不对（缺少操作数、括号不配对、叶子不是比较式）。"""


class CapabilityError(ExpressionLanguageError):
    """能力越界：函数调用、属性访问、赋值、多语句、保留字等本轮禁用的语法。

    ``capability`` 给出被拒的能力名（函数调用 / 赋值 / …），便于后续
    ``validator.py`` 归类成统一口径的「配置错误」。
    """

    def __init__(
        self,
        message: str,
        line: int = 1,
        column: int = 1,
        *,
        source: str = "",
        capability: str = "",
    ) -> None:
        self.capability = capability
        super().__init__(message, line, column, source=source)


__all__ = [
    "CapabilityError",
    "ExpressionLanguageError",
    "LexError",
    "ParseError",
]
