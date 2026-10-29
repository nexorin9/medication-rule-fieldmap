"""受限表达式的 AST 节点。

节点集合刻意保持最小：**Compare / BoolOp / UnaryOp / Literal / FieldRef**。
每类节点都带 ``line`` / ``column``，规则行由药事审核岗手工维护，
定位信息决定他能不能自己把表达式改对。

**对应源产品能力**：源仓库 ``web-frontend/modules/core/formula/index.js`` 的
``resolveFormula()`` 把公式文本解析成解析树，再交给求值访问器遍历；
本轮把「单元格公式树」改写成「审方规则表达式树」，求值由后续
``evaluator.py`` 承担，本模块只负责**结构**。

三种节点语义要点：

* :class:`Literal` —— 字面量，区分字符串与数字（``kind``）。
* :class:`FieldRef` —— 字段引用，``path`` 逐段保存（``order.drug_code`` → 两个段）。
* :class:`Compare` —— 比较式，运算符取 ``== != > >= < <=`` 之一。
* :class:`BoolOp` —— ``and`` / ``or`` 的多元连接（``a and b and c`` 收成一棵）。
* :class:`UnaryOp` —— ``not`` 一元否定。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal


class Node:
    """AST 节点基类（仅用于 isinstance 分派与遍历，不承载数据）。"""

    line: int
    column: int

    @property
    def children(self) -> tuple["Node", ...]:
        """子节点；叶子节点返回空元组。"""
        return ()

    def walk(self) -> Iterator["Node"]:
        """按先序遍历本节点及全部子节点（``validator.py`` 逐节点检查用）。"""
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True)
class Literal(Node):
    """字面量：字符串或数字。"""

    value: str | Decimal
    kind: str = "string"
    line: int = 1
    column: int = 1

    @property
    def text(self) -> str:
        """字面量的文本形式。"""
        return str(self.value)


@dataclass(frozen=True)
class FieldRef(Node):
    """字段引用；``path`` 逐段保存，``name`` 是拼接后的完整字段名。"""

    path: tuple[str, ...]
    line: int = 1
    column: int = 1

    @property
    def name(self) -> str:
        """完整字段名（``order.drug_code`` 形式）。"""
        return ".".join(self.path)


@dataclass(frozen=True)
class Compare(Node):
    """比较式：``left <operator> right``。"""

    operator: str
    left: Node
    right: Node
    line: int = 1
    column: int = 1

    @property
    def children(self) -> tuple[Node, ...]:
        return (self.left, self.right)


@dataclass(frozen=True)
class BoolOp(Node):
    """逻辑连接：``and`` / ``or``（多元）。"""

    operator: str
    operands: tuple[Node, ...]
    line: int = 1
    column: int = 1

    @property
    def children(self) -> tuple[Node, ...]:
        return self.operands


@dataclass(frozen=True)
class UnaryOp(Node):
    """一元运算：``not``。"""

    operator: str
    operand: Node
    line: int = 1
    column: int = 1

    @property
    def children(self) -> tuple[Node, ...]:
        return (self.operand,)


__all__ = [
    "BoolOp",
    "Compare",
    "FieldRef",
    "Literal",
    "Node",
    "UnaryOp",
]
