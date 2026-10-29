"""受限表达式载入期预检（validator）的回归测试。

覆盖任务书要求的四组主线：

1. **表达式引用了未声明字段 → 被拒**：字段名、规则号、行列定位都在报错里；
2. **表达式含函数调用等禁用能力 → 被拒**：报「表达式不允许的能力」并定位到行列；
3. **合法表达式通过**：现行 13 条样例规则的 ``expression`` 全部预检通过；
4. **错误信息含字段名与行列**。

另附两条边界断言：

* **配置错误与「数据缺字段」分列** —— 载入期拒绝的是规则行本身，
  运行期的数据缺字段仍落「不可判定」，两者的类型互不相干；
* 表达式子包里**不存在** eval / exec / compile 一类的动态执行。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from med_rule_fieldmap.expression import (
    CAPABILITY_BAN_LIST,
    CONFIG_ERROR_KINDS,
    CapabilityError,
    ExpressionConfigError,
    LexError,
    ParseError,
    assert_valid_expression,
    capability_ban_table,
    validate_expression,
    validate_expression_syntax,
    validate_field_declaration,
)
from med_rule_fieldmap.expression.validator import (
    KIND_CAPABILITY,
    KIND_LEX,
    KIND_SYNTAX,
    KIND_UNDECLARED_FIELD,
    KIND_UNKNOWN_FIELD,
    collect_field_refs,
)
from med_rule_fieldmap.rules.schema import RuleValidationError, validate_rule

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
EXPRESSION_PACKAGE = PROJECT_ROOT / "src" / "med_rule_fieldmap" / "expression"


# ---------------------------------------------------------------------------
# 1. 未声明字段被拒
# ---------------------------------------------------------------------------


def test_expression_referencing_undeclared_field_is_rejected() -> None:
    issues = validate_expression(
        "days_qty > 1",
        required_fields=["rx_type", "drug_category"],
        rule_id="RX-LEGAL-MA-OUT-DAYS",
    )

    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == KIND_UNDECLARED_FIELD
    assert issue.field == "days_qty"
    assert issue.rule_id == "RX-LEGAL-MA-OUT-DAYS"
    assert issue.is_config_error


def test_undeclared_field_error_mentions_field_name_and_location() -> None:
    issues = validate_expression(
        "days_qty > 1",
        required_fields=["rx_type"],
        rule_id="RX-LEGAL-MA-OUT-DAYS",
    )

    rendered = issues[0].render()
    assert "days_qty" in rendered, "报错必须点名是哪个字段没声明"
    assert "RX-LEGAL-MA-OUT-DAYS" in rendered, "报错必须点名是哪条规则"
    assert "第 1 行第 1 列" in rendered, "报错必须定位到行列"
    assert "required_fields" in rendered


def test_expression_referencing_unknown_prescription_field_is_rejected() -> None:
    issues = validate_expression(
        "no_such_field > 1",
        required_fields=["no_such_field"],
        rule_id="RX-X-001",
    )

    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == KIND_UNKNOWN_FIELD
    assert issue.field == "no_such_field"
    assert "不存在的医嘱字段" in issue.message


def test_dotted_field_path_is_not_a_prescription_column() -> None:
    """医嘱落地文件是平铺列，点号路径没有数据来源 → 按未知字段拒。"""
    issues = validate_expression(
        "order.drug_code == 'X'",
        required_fields=["order.drug_code"],
        rule_id="RX-X-002",
    )

    assert [issue.kind for issue in issues] == [KIND_UNKNOWN_FIELD]
    assert issues[0].field == "order.drug_code"


def test_all_referenced_fields_must_be_declared() -> None:
    """表达式里每个字段都要声明过，漏一个就拒。"""
    issues = validate_expression(
        "days_qty > 1 and drug_category == '麻醉药品注射剂'",
        required_fields=["days_qty"],
        rule_id="RX-LEGAL-MA-OUT-DAYS",
    )

    assert [issue.field for issue in issues] == ["drug_category"]
    assert all(issue.kind == KIND_UNDECLARED_FIELD for issue in issues)


def test_field_declaration_check_reports_every_undeclared_field() -> None:
    issues = validate_field_declaration(
        "days_qty > 1 and drug_category == '麻醉药品注射剂'",
        required_fields=(),
        rule_id="RX-LEGAL-MA-OUT-DAYS",
    )

    assert [issue.field for issue in issues] == ["days_qty", "drug_category"]


def test_declared_field_passes_field_check() -> None:
    assert validate_field_declaration("days_qty > 1", ["days_qty"]) == []


def test_syntax_problem_is_not_reported_twice_as_field_problem() -> None:
    """表达式本身站不住时，不应该再报「未声明字段」。"""
    assert validate_field_declaration("(days_qty > 1", ["days_qty"]) == []


# ---------------------------------------------------------------------------
# 2. 能力禁用清单
# ---------------------------------------------------------------------------


def test_function_call_is_rejected_with_capability_and_location() -> None:
    issues = validate_expression(
        "days_qty > abs(1)",
        required_fields=["days_qty"],
        rule_id="RX-X-003",
    )

    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == KIND_CAPABILITY
    assert issue.capability == "函数调用"
    assert "表达式不允许的能力" in issue.message
    assert issue.line == 1
    assert issue.column == 12, "报错应定位到函数名所在的列（abs 的 a）"


def test_attribute_access_is_rejected() -> None:
    issues = validate_expression(
        "days_qty. > 1",
        required_fields=["days_qty"],
        rule_id="RX-X-004",
    )

    assert [issue.capability for issue in issues] == ["属性访问"]


def test_assignment_is_rejected() -> None:
    issues = validate_expression(
        "days_qty = 1",
        required_fields=["days_qty"],
        rule_id="RX-X-005",
    )

    assert [issue.capability for issue in issues] == ["赋值"]


def test_import_statement_is_rejected_and_named_explicitly() -> None:
    issues = validate_expression(
        "import os",
        required_fields=[],
        rule_id="RX-X-006",
    )

    assert [issue.capability for issue in issues] == ["导入语句"]
    assert "导入语句" in issues[0].message


def test_multiple_statement_separator_is_rejected() -> None:
    issues = validate_expression(
        "days_qty > 1; days_qty < 9",
        required_fields=["days_qty"],
        rule_id="RX-X-007",
    )

    assert [issue.capability for issue in issues] == ["多语句分隔符"]


def test_reserved_word_is_rejected() -> None:
    issues = validate_expression(
        "days_qty > 1 and lambda > 0",
        required_fields=["days_qty"],
        rule_id="RX-X-008",
    )

    assert [issue.capability for issue in issues] == ["保留字"]


def test_dynamic_execution_word_is_rejected_and_named_explicitly() -> None:
    issues = validate_expression(
        "eval > 0",
        required_fields=[],
        rule_id="RX-X-009",
    )

    assert [issue.capability for issue in issues] == ["动态执行"]


def test_all_banned_capabilities_are_reported_at_once() -> None:
    """一次改干净：整条表达式里命中的禁用能力要全部列出来。"""
    issues = validate_expression_syntax(
        "import os; days_qty = abs(1)"
    )

    capabilities = {issue.capability for issue in issues}
    assert capabilities == {"导入语句", "多语句分隔符", "赋值", "函数调用"}


def test_capability_ban_list_is_explicit_and_rendered() -> None:
    names = {ban.name for ban in CAPABILITY_BAN_LIST}
    assert {
        "函数调用",
        "属性访问",
        "赋值",
        "多语句分隔符",
        "导入语句",
        "动态执行",
        "保留字",
        "参数列表",
    } <= names

    table = capability_ban_table()
    assert ("函数调用", "规则表达式只能是比较式，不得调用任意函数") in table
    assert all(reason for _, reason in table), "每条禁用能力都要写明理由"


# ---------------------------------------------------------------------------
# 3. 合法表达式通过
# ---------------------------------------------------------------------------


def test_valid_expression_passes_validation() -> None:
    issues = validate_expression(
        "days_qty > 1",
        required_fields=["days_qty"],
        rule_id="RX-LEGAL-MA-OUT-DAYS",
    )

    assert issues == []
    assert_valid_expression(
        "days_qty > 1",
        required_fields=["days_qty"],
        rule_id="RX-LEGAL-MA-OUT-DAYS",
    )


def test_logical_expression_with_declared_fields_passes() -> None:
    issues = validate_expression(
        "days_qty > 1 and drug_category == '麻醉药品注射剂'",
        required_fields=["days_qty", "drug_category"],
        rule_id="RX-LEGAL-MA-OUT-DAYS",
    )

    assert issues == []


def test_sample_rules_all_pass_expression_validation() -> None:
    """现行 15 条样例规则必须全部预检通过（否则样例集会被载入期拒掉）。"""
    checked = 0
    for path in sorted(SAMPLE_RULES.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(document, dict) or "rules" not in document:
                documents = [document]
            else:
                documents = document["rules"]
            for raw in documents:
                if not isinstance(raw, dict):
                    continue
                issues = validate_expression(
                    str(raw.get("expression", "")),
                    required_fields=raw.get("required_fields") or [],
                    rule_id=raw.get("rule_id"),
                )
                assert issues == [], f"{raw.get('rule_id')} 预检未通过：{issues}"
                checked += 1

    assert checked == 19


def test_assert_valid_expression_raises_config_error() -> None:
    with pytest.raises(ExpressionConfigError) as excinfo:
        assert_valid_expression(
            "days_qty > 1", required_fields=["rx_type"], rule_id="RX-X-010"
        )

    assert excinfo.value.issues
    assert "days_qty" in str(excinfo.value)


def test_field_refs_are_collected_in_order_without_duplicates() -> None:
    from med_rule_fieldmap.expression import parse

    tree = parse("days_qty > 1 and days_qty < 9 and drug_category == 'x'")
    assert [ref.name for ref in collect_field_refs(tree)] == [
        "days_qty",
        "drug_category",
    ]


# ---------------------------------------------------------------------------
# 4. 载入期接线：非法规则被拒，其余规则照常生效
# ---------------------------------------------------------------------------


def _rule_dict(**overrides: object) -> dict[str, object]:
    rule: dict[str, object] = {
        "rule_id": "RX-TEST-VALIDATOR",
        "layer": "合法性",
        "basis": "处方审核规则体系 §3 特殊药品处方量限",
        "version": "2026.10",
        "required_fields": ["days_qty"],
        "expression": "days_qty > 1",
        "severity": "严重不合理用药",
    }
    rule.update(overrides)
    return rule


def test_rule_with_undeclared_field_reference_is_rejected_at_load_time() -> None:
    with pytest.raises(RuleValidationError) as excinfo:
        validate_rule(
            _rule_dict(
                rule_id="RX-TEST-UNDECLARED",
                required_fields=["rx_type"],
                expression="days_qty > 1",
            )
        )

    fields = {issue.field for issue in excinfo.value.issues}
    assert "required_fields" in fields
    rendered = str(excinfo.value)
    assert "days_qty" in rendered
    assert "RX-TEST-UNDECLARED" in rendered


def test_rule_with_banned_capability_is_rejected_at_load_time() -> None:
    with pytest.raises(RuleValidationError) as excinfo:
        validate_rule(
            _rule_dict(
                rule_id="RX-TEST-CALL",
                expression="days_qty > abs(1)",
            )
        )

    fields = {issue.field for issue in excinfo.value.issues}
    assert "expression" in fields
    assert "表达式不允许的能力" in str(excinfo.value)


def test_valid_rule_still_passes_validate_rule() -> None:
    rule = validate_rule(_rule_dict())
    assert rule.rule_id == "RX-TEST-VALIDATOR"
    assert rule.expression == "days_qty > 1"


# ---------------------------------------------------------------------------
# 5. 配置错误与「数据缺字段」分列
# ---------------------------------------------------------------------------


def test_config_error_kinds_are_disjoint_from_runtime_conclusions() -> None:
    """载入期的问题类型不与判定结论同名，避免台账里混作一谈。"""
    from med_rule_fieldmap.models import Conclusion

    runtime_values = {member.value for member in Conclusion}
    assert not (CONFIG_ERROR_KINDS & runtime_values)
    assert KIND_UNDECLARED_FIELD in CONFIG_ERROR_KINDS
    assert KIND_CAPABILITY in CONFIG_ERROR_KINDS


def test_syntax_issue_kinds_are_distinct_from_field_issue_kinds() -> None:
    assert KIND_UNDECLARED_FIELD not in {KIND_CAPABILITY, KIND_LEX, KIND_SYNTAX}
    assert KIND_UNKNOWN_FIELD not in {KIND_CAPABILITY, KIND_LEX, KIND_SYNTAX}


# ---------------------------------------------------------------------------
# 6. 静态检查：不得出现动态执行
# ---------------------------------------------------------------------------


def test_expression_package_has_no_dynamic_execution_calls() -> None:
    offenders = []
    for path in EXPRESSION_PACKAGE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in ("eval(", "exec(", "compile("):
            if marker in text:
                offenders.append(f"{path.name}:{marker}")
    assert offenders == []


def test_parser_errors_are_still_raised_as_before() -> None:
    """预检层不吞掉解析层的异常类型。"""
    from med_rule_fieldmap.expression import parse

    with pytest.raises(LexError):
        parse("days_qty > '未闭合")
    with pytest.raises(ParseError):
        parse("days_qty > ")
    with pytest.raises(CapabilityError):
        parse("days_qty > abs(1)")
