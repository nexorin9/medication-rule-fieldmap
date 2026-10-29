"""规则行仓库与载入期校验的回归测试。

覆盖任务书要求的三条主线：

1. 合法规则集载入成功；
2. 缺 ``basis`` 的规则被拒，且**不影响其余规则生效**；
3. 生效区间外的规则不进入生效集；改动规则文件后热重载可观察到新版本。

另附重复规则号、未知键、非法依赖字段、严重度缺失等边界用例。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from med_rule_fieldmap.models import Conclusion, RuleLayer
from med_rule_fieldmap.rules.repository import RuleRepository
from med_rule_fieldmap.rules.schema import RuleValidationError, validate_rule

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"

AS_OF = date(2026, 10, 28)


def write_rule(path: Path, **overrides: object) -> Path:
    """写一条规则行到 YAML 文件（供临时目录用例使用）。"""
    rule: dict[str, object] = {
        "rule_id": "RX-TEST-001",
        "layer": "合法性",
        "basis": "处方审核规则体系 §3 特殊药品处方量限",
        "version": "2026.10",
        "effective_from": "2026-01-01",
        "effective_to": None,
        "applies_when": {"rx_type": "门急诊一般"},
        "required_fields": ["rx_type", "drug_category", "days_qty"],
        "expression": "days_qty > 1",
        "severity": "严重不合理用药",
    }
    rule.update(overrides)
    lines = ["---"]
    for key, value in rule.items():
        if value is None:
            continue
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value)
            lines.append(f"{key}: [{rendered}]")
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"  {sub_key}: {sub_value}")
        else:
            lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 样例规则集
# ---------------------------------------------------------------------------


def test_sample_rules_all_load_without_errors() -> None:
    repo = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    report = repo.load()

    assert repo.errors == (), f"样例规则集不应有载入错误：{[str(e) for e in repo.errors]}"
    assert report.files_scanned == 3
    assert report.loaded == 19
    assert report.rejected == 0
    assert len(repo.rules) == 19


def test_sample_rules_cover_three_layers_with_basis_and_severity() -> None:
    repo = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repo.load()

    layers = {RuleLayer.from_value(rule.layer) for rule in repo.rules}
    assert layers == {RuleLayer.LEGALITY, RuleLayer.NORMATIVITY, RuleLayer.APPROPRIATENESS}

    for rule in repo.rules:
        assert rule.basis, f"{rule.rule_id} 缺依据出处"
        assert rule.severity, f"{rule.rule_id} 缺严重度"
        assert rule.version
        assert rule.rule_id == rule.rule_id.strip()


def test_sample_rules_include_quantity_limit_rule_with_required_fields() -> None:
    repo = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repo.load()

    quantity_rules = [
        rule
        for rule in repo.rules
        if {"rx_type", "drug_category", "days_qty"} <= set(rule.required_fields)
    ]
    assert quantity_rules, "至少要有一条量限规则声明 rx_type / drug_category / days_qty 依赖"
    assert any("days_qty" in rule.expression for rule in quantity_rules)


def test_sample_rules_include_skin_test_rule() -> None:
    repo = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repo.load()

    skin_rules = [rule for rule in repo.rules if "skin_test_result" in rule.required_fields]
    assert skin_rules, "至少要有一条皮试规则声明 skin_test_result 依赖"
    assert any(rule.layer == RuleLayer.APPROPRIATENESS.value for rule in skin_rules)


def test_normativity_missing_field_rule_uses_violation_conclusion() -> None:
    repo = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repo.load()

    rule = repo.by_rule_id("RX-NORM-FRONT-DIAG")
    assert rule is not None
    assert rule.missing_conclusion is Conclusion.VIOLATION

    vague = repo.by_rule_id("RX-NORM-USAGE-VAGUE")
    assert vague is not None
    assert "遵医嘱" in vague.forbidden_terms


# ---------------------------------------------------------------------------
# 非法规则被拒，且不影响其余规则生效
# ---------------------------------------------------------------------------


def test_rule_without_basis_is_rejected_and_others_still_effective(tmp_path: Path) -> None:
    write_rule(tmp_path / "ok.yaml", rule_id="RX-OK-001")
    write_rule(tmp_path / "bad.yaml", rule_id="RX-BAD-001", basis=None)

    repo = RuleRepository(tmp_path, as_of=AS_OF)
    report = repo.load()

    assert report.loaded == 1
    assert report.rejected == 1
    assert len(repo.errors) == 1
    error = repo.errors[0]
    assert error.rule_id == "RX-BAD-001"
    assert any(issue.field == "basis" for issue in error.issues)
    assert "basis" in str(error)

    # 其余规则照常进入生效集
    assert [rule.rule_id for rule in repo.effective_rules] == ["RX-OK-001"]
    assert repo.by_rule_id("RX-BAD-001") is None


def test_rule_missing_expression_is_rejected(tmp_path: Path) -> None:
    write_rule(tmp_path / "bad.yaml", expression=None)
    repo = RuleRepository(tmp_path, as_of=AS_OF)
    repo.load()

    assert repo.rules == ()
    assert any(issue.field == "expression" for issue in repo.errors[0].issues)


def test_rule_with_unknown_key_is_rejected(tmp_path: Path) -> None:
    write_rule(tmp_path / "typo.yaml", required_field=["days_qty"])
    repo = RuleRepository(tmp_path, as_of=AS_OF)
    repo.load()

    assert repo.rules == ()
    assert any(issue.field == "required_field" for issue in repo.errors[0].issues)


def test_duplicate_rule_id_is_rejected_and_first_wins(tmp_path: Path) -> None:
    write_rule(tmp_path / "a.yaml", rule_id="RX-DUP-001", version="2026.01")
    write_rule(tmp_path / "b.yaml", rule_id="RX-DUP-001", version="2026.02")

    repo = RuleRepository(tmp_path, as_of=AS_OF)
    repo.load()

    assert len(repo.rules) == 1
    assert repo.by_rule_id("RX-DUP-001").version == "2026.01"
    assert len(repo.errors) == 1
    assert "重复" in str(repo.errors[0])


def test_rule_with_unknown_field_dependency_is_rejected(tmp_path: Path) -> None:
    write_rule(tmp_path / "bad.yaml", required_fields=["rx_type", "no_such_field"])
    repo = RuleRepository(tmp_path, as_of=AS_OF)
    repo.load()

    assert repo.rules == ()
    assert any(issue.field == "required_fields" for issue in repo.errors[0].issues)


def test_rule_with_unknown_applies_when_field_is_rejected(tmp_path: Path) -> None:
    write_rule(tmp_path / "bad.yaml", applies_when={"nope": "x"})
    repo = RuleRepository(tmp_path, as_of=AS_OF)
    repo.load()

    assert repo.rules == ()
    assert any(issue.field == "applies_when" for issue in repo.errors[0].issues)


def test_rule_with_inverted_effective_window_is_rejected(tmp_path: Path) -> None:
    write_rule(
        tmp_path / "bad.yaml",
        effective_from="2026-10-01",
        effective_to="2026-01-01",
    )
    repo = RuleRepository(tmp_path, as_of=AS_OF)
    repo.load()

    assert repo.rules == ()
    assert any(issue.field == "effective_to" for issue in repo.errors[0].issues)


def test_rule_with_unbalanced_expression_is_rejected(tmp_path: Path) -> None:
    write_rule(tmp_path / "bad.yaml", expression="(days_qty > 1")
    repo = RuleRepository(tmp_path, as_of=AS_OF)
    repo.load()

    assert repo.rules == ()
    assert any(issue.field == "expression" for issue in repo.errors[0].issues)


def test_rule_without_severity_rejected_by_default_and_tolerated_when_lenient(
    tmp_path: Path,
) -> None:
    write_rule(tmp_path / "sev.yaml", severity=None)

    strict = RuleRepository(tmp_path, as_of=AS_OF)
    strict.load()
    assert strict.rules == ()
    assert any(issue.field == "severity" for issue in strict.errors[0].issues)

    lenient = RuleRepository(tmp_path, as_of=AS_OF, require_severity=False)
    lenient.load()
    assert len(lenient.rules) == 1
    assert lenient.rules[0].severity is None


def test_validate_rule_raises_with_field_names() -> None:
    with pytest.raises(RuleValidationError) as excinfo:
        validate_rule({"rule_id": "RX-X-001", "layer": "合法性", "expression": "1 == 1"})

    fields = {issue.field for issue in excinfo.value.issues}
    assert "basis" in fields
    assert "version" in fields
    assert "severity" in fields


# ---------------------------------------------------------------------------
# 生效区间
# ---------------------------------------------------------------------------


def test_rules_outside_effective_window_are_not_in_effective_set(tmp_path: Path) -> None:
    write_rule(
        tmp_path / "expired.yaml",
        rule_id="RX-EXPIRED",
        effective_from="2026-01-01",
        effective_to="2026-02-01",
    )
    write_rule(
        tmp_path / "future.yaml",
        rule_id="RX-FUTURE",
        effective_from="2027-01-01",
        effective_to=None,
    )
    write_rule(tmp_path / "current.yaml", rule_id="RX-CURRENT")

    repo = RuleRepository(tmp_path, as_of=AS_OF)
    repo.load()

    assert len(repo.rules) == 3, "三条规则都应载入成功"
    assert [rule.rule_id for rule in repo.effective_rules] == ["RX-CURRENT"]
    assert repo.by_rule_id("RX-EXPIRED") is not None
    assert repo.by_rule_id("RX-FUTURE") is not None


# ---------------------------------------------------------------------------
# 热重载
# ---------------------------------------------------------------------------


def test_reload_detects_rule_file_change(tmp_path: Path) -> None:
    rule_file = write_rule(tmp_path / "hot.yaml", version="2026.01")

    repo = RuleRepository(tmp_path, as_of=AS_OF)
    repo.load()
    assert repo.by_rule_id("RX-TEST-001").version == "2026.01"
    assert repo.reload_if_changed() is False, "文件未变时不应触发重载"

    write_rule(rule_file, version="2026.02")

    assert repo.reload_if_changed() is True
    assert repo.by_rule_id("RX-TEST-001").version == "2026.02"
    assert repo.reload_if_changed() is False


def test_reload_keeps_valid_rules_when_one_file_becomes_invalid(tmp_path: Path) -> None:
    write_rule(tmp_path / "ok.yaml", rule_id="RX-OK-001")
    broken = write_rule(tmp_path / "bad.yaml", rule_id="RX-BAD-001")

    repo = RuleRepository(tmp_path, as_of=AS_OF)
    repo.load()
    assert len(repo.rules) == 2

    broken.write_text("rule_id: RX-BAD-001\nlayer: 合法性\n", encoding="utf-8")
    assert repo.reload_if_changed() is True

    assert [rule.rule_id for rule in repo.rules] == ["RX-OK-001"]
    assert len(repo.errors) == 1
    assert repo.errors[0].rule_id == "RX-BAD-001"


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------


def test_by_rule_id_and_by_version_lookup() -> None:
    repo = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repo.load()

    rule = repo.by_rule_id("RX-LEGAL-MA-OUT-DAYS")
    assert rule is not None
    assert repo.by_version("RX-LEGAL-MA-OUT-DAYS", rule.version) is rule
    assert repo.by_version("RX-LEGAL-MA-OUT-DAYS", "1999.01") is None
    assert repo.by_rule_id("RX-NOT-EXIST") is None


def test_repository_reads_rules_recursively(tmp_path: Path) -> None:
    nested = tmp_path / "sample"
    nested.mkdir()
    write_rule(nested / "nested.yaml", rule_id="RX-NESTED")

    repo = RuleRepository(tmp_path, as_of=AS_OF)
    repo.load()

    assert [rule.rule_id for rule in repo.rules] == ["RX-NESTED"]
