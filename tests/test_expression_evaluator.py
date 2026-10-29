"""受限表达式求值器（evaluator）的回归测试。

覆盖任务书要求的四组主线：

1. **正常求值**：数值比较、文本比较、单位后缀容错；
2. **MISSING 与 EXPLICIT_NO 严格区分**：字段留空 → 不可判定；
   字段写「未注明 / 未做 / 否」→ 已知否定，按违规处理，不静默判合规；
3. **逻辑连接的 MISSING 传播**：`and` 任一侧缺失即整体不可判定（不短路），
   `or` 一侧为真即整体为真；
4. **源码内不存在动态执行**：表达式子包里不得出现 eval / exec / compile 调用。

另附两条边界断言：

* **未知字段属配置错误**（抛 :class:`EvaluationError`），不得降级成不可判定；
* 现行 13 条样例规则的 ``expression`` 在求值器下全部可求值、不抛异常。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from med_rule_fieldmap.expression import (
    EvalState,
    EvaluationError,
    FieldState,
    evaluate_ast,
    evaluate_expression,
    parse,
    resolve_field,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
EXPRESSION_PACKAGE = PROJECT_ROOT / "src" / "med_rule_fieldmap" / "expression"


def ctx(**values: object) -> dict[str, str]:
    """构造求值上下文（列名 → 原始文本）。"""
    return {name: str(value) for name, value in values.items()}


# ---------------------------------------------------------------------------
# 1. 正常求值
# ---------------------------------------------------------------------------


def test_numeric_comparison_yields_true_and_false() -> None:
    """数值比较：超量为真、未超量为假。"""
    hit = evaluate_expression("days_qty > 3", ctx(days_qty="5"))
    miss = evaluate_expression("days_qty > 3", ctx(days_qty="2"))

    assert hit.state is EvalState.TRUE
    assert hit.is_true
    assert miss.state is EvalState.FALSE
    assert miss.is_false
    assert not miss.is_missing


def test_numeric_field_tolerates_unit_suffix() -> None:
    """数值字段容忍单位后缀（``3日量`` → ``3``），与入站口径一致。"""
    assert evaluate_expression("days_qty > 3", ctx(days_qty="5日量")).is_true
    assert evaluate_expression("days_qty > 3", ctx(days_qty="3日量")).is_false


def test_text_equality_comparison() -> None:
    """文本比较：皮试结果为「阴性」时条件成立，为「阳性」时不成立。"""
    negative = evaluate_expression(
        "skin_test_result == '阴性'", ctx(skin_test_result="阴性")
    )
    positive = evaluate_expression(
        "skin_test_result == '阴性'", ctx(skin_test_result="阳性")
    )

    assert negative.is_true
    assert positive.is_false


def test_equality_between_number_and_text_field() -> None:
    """数值字段与数字字面量按数值比较（``0 <= 0`` 成立）。"""
    result = evaluate_expression("dosage_value <= 0", ctx(dosage_value="0"))
    assert result.is_true


def test_reason_text_mentions_the_expression() -> None:
    """归因文本带条件原文，便于药师在台账上直接看懂判了什么。"""
    result = evaluate_expression("days_qty > 3", ctx(days_qty="5"))
    assert "days_qty > 3" in result.reason


# ---------------------------------------------------------------------------
# 2. MISSING 与 EXPLICIT_NO 的严格区分
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_blank_field_is_missing_not_explicit_no(value: str) -> None:
    """空串 / 纯空白 → MISSING（不可判定），不是显式否。"""
    resolution = resolve_field("skin_test_result", ctx(skin_test_result=value))
    assert resolution.state is FieldState.MISSING

    result = evaluate_expression(
        "skin_test_result == '未注明'", ctx(skin_test_result=value)
    )
    assert result.state is EvalState.MISSING
    assert result.is_missing
    assert result.missing_fields == ("skin_test_result",)


def test_absent_column_is_missing() -> None:
    """上下文里根本没有这一列 → 同样按缺失处理。"""
    result = evaluate_expression("days_qty > 1", ctx(drug_name="某药"))
    assert result.state is EvalState.MISSING
    assert result.missing_fields == ("days_qty",)


def test_missing_field_never_raises_and_never_judges_false() -> None:
    """MISSING 与任何值比较一律返回 MISSING：不抛错、不静默判假。"""
    result = evaluate_expression("days_qty > 1", ctx(days_qty=""))
    assert result.state is EvalState.MISSING
    assert result.is_missing is True
    assert result.is_false is False


def test_explicit_no_marker_is_distinguished_from_missing() -> None:
    """显式否定标记 → EXPLICIT_NO，与 MISSING 分属不同语义。"""
    resolution = resolve_field("skin_test_result", ctx(skin_test_result="未注明"))
    assert resolution.state is FieldState.EXPLICIT_NO
    assert resolution.raw == "未注明"


def test_explicit_no_equal_to_marker_literal_is_violation() -> None:
    """皮试结果写「未注明」→ 命中违规条件（真）。"""
    result = evaluate_expression(
        "skin_test_result == '未注明'", ctx(skin_test_result="未注明")
    )
    assert result.is_true
    assert result.explicit_no_fields == ("skin_test_result",)


def test_explicit_no_against_positive_literal_is_violation_not_compliance() -> None:
    """显式否与正值比较默认判违规，不得静默判成合规。

    ``skin_test_result != '阴性'``：字段写「未注明」时皮试结果并未达要求，
    结论必须是违规（真），不能因为「不等于阴性」就判成合规。
    """
    result = evaluate_expression(
        "skin_test_result != '阴性'", ctx(skin_test_result="未注明")
    )
    assert result.is_true
    assert "未注明" in result.reason
    assert result.explicit_no_fields == ("skin_test_result",)


def test_explicit_no_against_positive_number_is_violation() -> None:
    """显式否与正数比较默认判违规（任务书硬要求）。"""
    result = evaluate_expression("days_qty > 1", ctx(days_qty="未注明"))
    assert result.is_true
    assert result.explicit_no_fields == ("days_qty",)


def test_explicit_no_alias_is_recognised() -> None:
    """别名写法（未做 / 否）同样归一为显式否。"""
    for marker in ("未做", "否", "未注明"):
        result = evaluate_expression(
            "skin_test_result != '阴性'", ctx(skin_test_result=marker)
        )
        assert result.is_true, marker
        assert result.explicit_no_fields == ("skin_test_result",)


def test_positive_value_against_marker_literal_is_compliant() -> None:
    """字段有正常值时按普通文本比较：皮试「阴性」不等于「未注明」→ 不成立。"""
    result = evaluate_expression(
        "skin_test_result == '未注明'", ctx(skin_test_result="阴性")
    )
    assert result.is_false


def test_blank_and_explicit_no_produce_different_conclusions() -> None:
    """留空与写「未注明」必须产出不同结论（本条是本任务的语义底线）。"""
    blank = evaluate_expression("skin_test_result == '未注明'", ctx(skin_test_result=""))
    explicit = evaluate_expression(
        "skin_test_result == '未注明'", ctx(skin_test_result="未注明")
    )

    assert blank.state is EvalState.MISSING
    assert explicit.state is EvalState.TRUE
    assert blank.state is not explicit.state


# ---------------------------------------------------------------------------
# 3. 逻辑连接的 MISSING 传播
# ---------------------------------------------------------------------------


def test_and_propagates_missing_from_either_side() -> None:
    """and：任一侧缺失 → 整体不可判定。"""
    left_missing = evaluate_expression(
        "days_qty > 1 and rx_type == '门急诊一般'", ctx(rx_type="门急诊一般")
    )
    right_missing = evaluate_expression(
        "days_qty > 1 and rx_type == '门急诊一般'", ctx(days_qty="5")
    )

    assert left_missing.state is EvalState.MISSING
    assert right_missing.state is EvalState.MISSING
    assert right_missing.missing_fields == ("rx_type",)


def test_and_does_not_short_circuit_on_missing() -> None:
    """and 不做短路：一侧为真、另一侧缺失 → 仍判不可判定。

    Kleene 逻辑会把它算成「真」，但审方场景不能这样——另一侧字段还没落地，
    判成合规等于把一条待核的处方放过去。
    """
    result = evaluate_expression(
        "days_qty > 1 and rx_type == '门急诊一般'", ctx(days_qty="5")
    )
    assert result.state is EvalState.MISSING


def test_and_with_all_fields_present_evaluates_normally() -> None:
    """两侧都有值时 and 正常求值。"""
    both_true = evaluate_expression(
        "days_qty > 1 and rx_type == '门急诊一般'",
        ctx(days_qty="5", rx_type="门急诊一般"),
    )
    one_false = evaluate_expression(
        "days_qty > 1 and rx_type == '门急诊一般'",
        ctx(days_qty="5", rx_type="住院"),
    )

    assert both_true.is_true
    assert one_false.is_false


def test_or_is_true_when_one_side_is_true() -> None:
    """or：一侧为真 → 整体为真（不受另一侧缺失影响）。"""
    result = evaluate_expression(
        "days_qty > 1 or rx_type == '门急诊一般'", ctx(days_qty="5")
    )
    assert result.is_true


def test_or_is_missing_when_no_side_is_true_and_one_is_missing() -> None:
    """or：一侧为假、另一侧缺失 → 整体不可判定。"""
    result = evaluate_expression(
        "days_qty > 1 or rx_type == '门急诊一般'", ctx(days_qty="1")
    )
    assert result.state is EvalState.MISSING
    assert result.missing_fields == ("rx_type",)


def test_not_propagates_missing() -> None:
    """not 不吃掉缺失：缺失照原样传播为不可判定。"""
    result = evaluate_expression("not (days_qty > 1)", ctx(days_qty=""))
    assert result.state is EvalState.MISSING


def test_not_flips_a_known_value() -> None:
    """not 对已知结论正常取反。"""
    assert evaluate_expression("not (days_qty > 1)", ctx(days_qty="1")).is_true
    assert evaluate_expression("not (days_qty > 1)", ctx(days_qty="5")).is_false


# ---------------------------------------------------------------------------
# 4. 边界：未知字段属配置错误
# ---------------------------------------------------------------------------


def test_unknown_field_raises_configuration_error() -> None:
    """引用不存在的医嘱字段是配置错误，不得降级成不可判定。"""
    with pytest.raises(EvaluationError) as excinfo:
        evaluate_expression("no_such_field > 1", ctx(days_qty="5"))
    assert "no_such_field" in str(excinfo.value)


def test_evaluate_ast_reuses_parsed_tree() -> None:
    """已解析的 AST 可直接求值（解析树缓存走这条路径）。"""
    tree = parse("days_qty > 3")
    result = evaluate_ast(tree, ctx(days_qty="5"), source="days_qty > 3")
    assert result.is_true


# ---------------------------------------------------------------------------
# 5. 静态检查：不得出现动态执行
# ---------------------------------------------------------------------------


def test_expression_package_has_no_dynamic_execution_calls() -> None:
    """表达式子包里不得出现 eval / exec / compile 一类的动态执行。"""
    offenders = []
    for path in EXPRESSION_PACKAGE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in ("eval(", "exec(", "compile("):
            if marker in text:
                offenders.append(f"{path.name}:{marker}")
    assert offenders == []


def test_evaluator_module_uses_no_dynamic_execution() -> None:
    """求值器自身只按 AST 遍历，不解释字符串。"""
    source = (EXPRESSION_PACKAGE / "evaluator.py").read_text(encoding="utf-8")
    for marker in ("eval(", "exec(", "compile("):
        assert marker not in source


# ---------------------------------------------------------------------------
# 6. 与样例规则集对接
# ---------------------------------------------------------------------------


def _sample_rules() -> list[dict[str, object]]:
    rules: list[dict[str, object]] = []
    for path in sorted(SAMPLE_RULES.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            rules.append(document)
    return rules


def test_all_sample_rule_expressions_are_evaluable() -> None:
    """现行 19 条样例规则的表达式在求值器下都能求出结论，不抛异常。"""
    rules = _sample_rules()
    assert len(rules) == 19

    row = ctx(
        days_qty="5",
        dosage_value="5",
        dosage_text="5mg 每日三次",
        clinical_diag="上呼吸道感染",
        skin_test_result="未注明",
        rx_type="门急诊一般",
        drug_category="麻醉药品注射剂",
    )
    for rule in rules:
        result = evaluate_expression(str(rule["expression"]), row)
        assert result.state in {EvalState.TRUE, EvalState.FALSE, EvalState.MISSING}
        assert result.reason
