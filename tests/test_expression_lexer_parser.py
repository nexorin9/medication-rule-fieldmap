"""受限表达式 lexer / parser / AST 的回归测试。

覆盖任务书要求的四组主线：

1. **全部 token 类型**都能切出来：字段路径标识符、数字、字符串、六个比较运算符、
   ``and`` / ``or`` / ``not``、括号；
2. **优先级**：``and`` 高于 ``or``，括号改变分组，``not`` 作用于比较式；
3. **语法错误定位到具体行列**（缺右操作数、括号不配对、缺比较运算符、叶子不是比较式）；
4. **能力越界显式拒绝**（函数调用 / 属性访问 / 赋值 / 多语句 / 保留字）并带定位。

另附两条兼容性断言：现行 13 条样例规则的 ``expression`` 全部能被新语法解析；
表达式子包里**不存在** eval / exec / compile 一类的动态执行。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from med_rule_fieldmap.expression import (
    BoolOp,
    CapabilityError,
    Compare,
    FieldRef,
    LexError,
    Literal,
    ParseError,
    TokenType,
    UnaryOp,
    parse,
    tokenize,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
EXPRESSION_PACKAGE = PROJECT_ROOT / "src" / "med_rule_fieldmap" / "expression"


# ---------------------------------------------------------------------------
# 1. 词法：token 类型覆盖
# ---------------------------------------------------------------------------


def test_tokenize_covers_every_token_type() -> None:
    source = "days_qty > 1 and (rx_type == '住院' or not days_qty <= 7)"
    tokens = tokenize(source)

    assert [token.type for token in tokens] == [
        TokenType.IDENT,
        TokenType.OP,
        TokenType.NUMBER,
        TokenType.AND,
        TokenType.LPAREN,
        TokenType.IDENT,
        TokenType.OP,
        TokenType.STRING,
        TokenType.OR,
        TokenType.NOT,
        TokenType.IDENT,
        TokenType.OP,
        TokenType.NUMBER,
        TokenType.RPAREN,
        TokenType.EOF,
    ]
    assert [token.value for token in tokens][:4] == ["days_qty", ">", "1", "and"]
    assert tokens[7].value == "住院"
    assert tokens[-1].type is TokenType.EOF


def test_tokenize_covers_all_six_comparison_operators() -> None:
    tokens = tokenize("a == b != c > d >= e < f <= g")
    operators = [token.value for token in tokens if token.type is TokenType.OP]
    assert operators == ["==", "!=", ">", ">=", "<", "<="]


def test_tokenize_keeps_dotted_field_path_in_one_identifier() -> None:
    tokens = tokenize("order.drug_code")
    assert len(tokens) == 2  # IDENT + EOF
    assert tokens[0].type is TokenType.IDENT
    assert tokens[0].value == "order.drug_code"


def test_tokenize_empty_source_yields_only_eof() -> None:
    tokens = tokenize("")
    assert len(tokens) == 1
    assert tokens[0].type is TokenType.EOF


# ---------------------------------------------------------------------------
# 2. 词法错误：非法字符 / 引号未闭合 / 行列定位
# ---------------------------------------------------------------------------


def test_illegal_character_reports_line_and_column() -> None:
    source = "days_qty @ 1"
    with pytest.raises(LexError) as excinfo:
        tokenize(source)
    error = excinfo.value
    assert error.line == 1
    assert error.column == source.index("@") + 1
    assert "非法字符" in error.message


def test_illegal_character_on_second_line_reports_second_line() -> None:
    source = "days_qty >\n1 @ 2"
    with pytest.raises(LexError) as excinfo:
        tokenize(source)
    error = excinfo.value
    assert error.line == 2
    assert error.column == source.index("@") - source.index("\n")


def test_unterminated_string_reports_quote_position() -> None:
    source = "skin_test_result == '未注明"
    with pytest.raises(LexError) as excinfo:
        tokenize(source)
    error = excinfo.value
    assert "字符串未闭合" in error.message
    assert error.column == source.index("'") + 1


# ---------------------------------------------------------------------------
# 3. 语法：AST 结构与优先级
# ---------------------------------------------------------------------------


def test_parse_builds_compare_ast() -> None:
    node = parse("days_qty > 1")
    assert isinstance(node, Compare)
    assert node.operator == ">"
    assert isinstance(node.left, FieldRef)
    assert node.left.name == "days_qty"
    assert isinstance(node.right, Literal)
    assert node.right.kind == "number"
    assert str(node.right.value) == "1"


def test_parse_builds_string_literal_and_dotted_field_path() -> None:
    node = parse("order.drug_code == '未注明'")
    assert isinstance(node, Compare)
    assert isinstance(node.left, FieldRef)
    assert node.left.path == ("order", "drug_code")
    assert node.left.name == "order.drug_code"
    assert isinstance(node.right, Literal)
    assert node.right.kind == "string"
    assert node.right.value == "未注明"


def test_and_binds_tighter_than_or() -> None:
    node = parse("a == 1 or b > 2 and c != 3")
    assert isinstance(node, BoolOp)
    assert node.operator == "or"
    assert len(node.operands) == 2
    right = node.operands[1]
    assert isinstance(right, BoolOp)
    assert right.operator == "and"


def test_parentheses_change_grouping() -> None:
    node = parse("(a == 1 or b > 2) and c != 3")
    assert isinstance(node, BoolOp)
    assert node.operator == "and"
    left = node.operands[0]
    assert isinstance(left, BoolOp)
    assert left.operator == "or"


def test_not_applies_to_comparison() -> None:
    node = parse("not a == 1")
    assert isinstance(node, UnaryOp)
    assert node.operator == "not"
    assert isinstance(node.operand, Compare)


def test_ast_nodes_carry_position() -> None:
    source = "days_qty > 1"
    node = parse(source)
    assert node.line == 1
    assert node.column == 1
    assert node.left.column == 1
    assert node.right.column == source.index("1") + 1


def test_walk_visits_every_node() -> None:
    node = parse("a == 1 and b > 2 or c != 3")
    names = [type(item).__name__ for item in node.walk()]
    assert names[0] == "BoolOp"
    assert "Compare" in names
    assert "Literal" in names
    assert "FieldRef" in names


# ---------------------------------------------------------------------------
# 4. 语法错误：行列定位
# ---------------------------------------------------------------------------


def test_missing_right_operand_reports_operator_position() -> None:
    source = "days_qty >"
    with pytest.raises(ParseError) as excinfo:
        parse(source)
    error = excinfo.value
    assert "缺少右操作数" in error.message
    assert error.line == 1
    assert error.column == source.index(">") + 1


def test_unclosed_parenthesis_reports_open_bracket_position() -> None:
    with pytest.raises(ParseError) as excinfo:
        parse("(a == 1")
    error = excinfo.value
    assert "括号未闭合" in error.message
    assert error.column == 1


def test_extra_right_parenthesis_is_rejected() -> None:
    source = "a == 1)"
    with pytest.raises(ParseError) as excinfo:
        parse(source)
    error = excinfo.value
    assert "多余" in error.message
    assert error.column == source.index(")") + 1


def test_bare_literal_is_not_a_valid_expression() -> None:
    with pytest.raises(ParseError) as excinfo:
        parse("1")
    assert "必须是比较式" in excinfo.value.message


def test_missing_comparison_operator_is_reported() -> None:
    source = "a == 1 b == 2"
    with pytest.raises(ParseError) as excinfo:
        parse(source)
    error = excinfo.value
    assert "缺少比较运算符" in error.message
    assert error.column == source.index("b") + 1


def test_empty_expression_is_rejected() -> None:
    with pytest.raises(ParseError) as excinfo:
        parse("")
    assert "表达式为空" in excinfo.value.message


# ---------------------------------------------------------------------------
# 5. 能力越界：函数调用 / 属性访问 / 赋值 / 多语句 / 保留字
# ---------------------------------------------------------------------------


def test_function_call_is_rejected() -> None:
    source = "days_qty > abs(1)"
    with pytest.raises(CapabilityError) as excinfo:
        parse(source)
    error = excinfo.value
    assert error.capability == "函数调用"
    assert "不允许的能力" in error.message
    assert error.column == source.index("abs") + 1


def test_attribute_access_is_rejected() -> None:
    source = "(a == 1).value"
    with pytest.raises(CapabilityError) as excinfo:
        parse(source)
    error = excinfo.value
    assert error.capability == "属性访问"
    assert error.column == source.index(".") + 1


def test_assignment_is_rejected() -> None:
    source = "x = 1"
    with pytest.raises(CapabilityError) as excinfo:
        parse(source)
    error = excinfo.value
    assert error.capability == "赋值"
    assert error.column == source.index("=") + 1


def test_multiple_statements_are_rejected() -> None:
    source = "a == 1; b == 2"
    with pytest.raises(CapabilityError) as excinfo:
        parse(source)
    error = excinfo.value
    assert error.capability == "多语句分隔符"
    assert error.column == source.index(";") + 1


def test_reserved_word_is_rejected() -> None:
    source = "import os"
    with pytest.raises(CapabilityError) as excinfo:
        parse(source)
    error = excinfo.value
    assert error.capability == "保留字"
    assert "import" in error.message


# ---------------------------------------------------------------------------
# 6. 兼容性：现行样例规则全部可解析 + 无动态执行
# ---------------------------------------------------------------------------


def test_every_sample_rule_expression_parses() -> None:
    parsed = 0
    for path in sorted(SAMPLE_RULES.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not document:
                continue
            expression = document["expression"]
            node = parse(expression)
            # 规则行表达式顶层可以是比较式，也可以是逻辑连接式
            #（规范性前记缺项规则 RX-NORM-FRONT-HEADER 用 or 连接多个栏位）；
            # 两种形态的叶子都必须是比较式——受限表达式不允许裸字面量。
            assert isinstance(node, (Compare, BoolOp)), f"{path.name} :: {expression}"
            leaves = list(node.children) if isinstance(node, BoolOp) else [node]
            assert all(isinstance(leaf, Compare) for leaf in leaves), (
                f"{path.name} :: {expression} 的叶子必须是比较式"
            )
            parsed += 1
    assert parsed == 19


def test_expression_package_has_no_dynamic_execution() -> None:
    forbidden = ("eval(", "exec(", "compile(")
    for path in sorted(EXPRESSION_PACKAGE.glob("*.py")):
        content = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern not in content, f"{path.name} 出现动态执行调用 {pattern}"
