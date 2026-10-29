"""主干最窄路径的端到端回归：CSV → 处方行 → 三态判定 → 判定台账 + 批次摘要。

对应任务书 T3：一份样例医嘱 CSV 落地后，用 CLI 跑一次判定，产出
``out/judgement_ledger.csv``，并保证：

* 逐处方 × 逐规则**恰好一行**（无重复、无遗漏）；
* 缺字段的处方落「不可判定」并**点名缺哪个字段**；
* 合规 / 违规 / 不可判定三态在同一样例批次里同时出现；
* 表达式的配置错误**显式报错**，不得降级成「不可判定」把问题藏起来。
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from med_rule_fieldmap.cli import main
from med_rule_fieldmap.engine.judge import (
    ExpressionError,
    evaluate_expression,
    judge_batch,
    summarize_conclusions,
)
from med_rule_fieldmap.ingest.csv_reader import read_prescription_rows
from med_rule_fieldmap.ledger.ledger import (
    LEDGER_COLUMNS,
    load_judgement_ledger,
    write_judgement_ledger,
)
from med_rule_fieldmap.models import PRESCRIPTION_FIELDS, Conclusion, PrescriptionRow
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"
BATCH_FILE = BATCH_DIR / "demo_batch_001.csv"

AS_OF = date(2026, 10, 28)


@pytest.fixture(scope="module")
def rules():
    repository = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repository.load()
    assert repository.errors == (), "样例规则集不应有载入错误"
    return repository.effective_rules


@pytest.fixture(scope="module")
def prescription_rows():
    return read_prescription_rows(BATCH_FILE)


# ---------------------------------------------------------------------------
# 样例批次本身
# ---------------------------------------------------------------------------


def test_sample_batch_has_enough_rows_and_full_header(prescription_rows) -> None:
    rows = prescription_rows
    assert len(rows) >= 20, "样例批次至少 20 行"

    with BATCH_FILE.open(encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    assert len(header) == len(PRESCRIPTION_FIELDS), (
        "样例批次表头应与医嘱字段全集一致"
        f"（表头 {len(header)} 列 / 口径 {len(PRESCRIPTION_FIELDS)} 列）"
    )

    # 覆盖任务书要求的场景
    assert any(row.is_missing("rx_type") for row in rows), "需含缺 rx_type 的行"
    assert any(row.is_missing("skin_test_result") for row in rows), "需含缺皮试结果的行"
    assert any(row.is_missing("days_qty") for row in rows), "需含缺日量的行"
    assert any(row.value("drug_name") == "盐酸吗啡注射液" for row in rows), "需含中文药品名"
    assert any(row.value("dept_name") == "呼吸内科门诊" for row in rows), "需含中文科室名"


def test_csv_reader_is_column_order_independent(tmp_path: Path) -> None:
    source = read_prescription_rows(BATCH_FILE)

    with BATCH_FILE.open(encoding="utf-8", newline="") as handle:
        records = list(csv.reader(handle))
    header, first = records[0], records[1]
    reversed_header = list(reversed(header))
    reversed_first = list(reversed(first))
    shuffled = tmp_path / "shuffled.csv"
    with shuffled.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(reversed_header)
        writer.writerow(reversed_first)

    rows = read_prescription_rows(shuffled)
    assert len(rows) == 1
    assert rows[0].prescription_no == source[0].prescription_no
    assert rows[0].drug_name == source[0].drug_name
    assert rows[0].days_qty == source[0].days_qty


# ---------------------------------------------------------------------------
# 判定台账
# ---------------------------------------------------------------------------


def test_ledger_has_exactly_one_row_per_prescription_rule_pair(rules, prescription_rows) -> None:
    rows = prescription_rows
    judgement = judge_batch(rows, rules, batch_no="DEMO-BATCH-001")

    counts = summarize_conclusions(judgement.rows)
    assert sum(counts.values()) == len(judgement.rows)
    assert len(judgement.rows) == sum(judgement.candidate_rule_counts.values())

    per_prescription: dict[str, int] = {}
    for row in judgement.rows:
        per_prescription[row.prescription_no] = per_prescription.get(row.prescription_no, 0) + 1
    assert per_prescription == dict(judgement.candidate_rule_counts)

    # 同一 (处方, 规则) 不重复
    keys = [(row.prescription_no, row.rule_id) for row in judgement.rows]
    assert len(keys) == len(set(keys))


def test_three_conclusions_all_occur_in_one_batch(rules, prescription_rows) -> None:
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")
    counts = summarize_conclusions(judgement.rows)

    assert counts[Conclusion.COMPLIANT.value] > 0, "样例批次应含合规例"
    assert counts[Conclusion.VIOLATION.value] > 0, "样例批次应含违规例"
    assert counts[Conclusion.UNDETERMINABLE.value] > 0, "样例批次应含不可判定例"


def test_missing_rx_type_is_undeterminable_and_names_the_field(rules, prescription_rows) -> None:
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")
    undeterminable = [
        row for row in judgement.rows if row.conclusion is Conclusion.UNDETERMINABLE
    ]
    named_rx_type = [
        row for row in undeterminable if "rx_type" in row.missing_fields
    ]
    assert named_rx_type, "缺 rx_type 的处方必须落不可判定并点名 rx_type"
    for row in named_rx_type:
        assert row.prescription_no == "RX-0007"
        assert "rx_type" in row.reason

    # 缺处方类型时，档位相关的量限规则都判不了：门急诊一般档（麻醉药品注射剂）、
    # 门急诊癌症疼痛档（麻醉药品注射剂）与住院档（麻醉药品大类）三条都要出现在
    # 不可判定台账里——这正是"补一个字段能解锁几条规则"的原始信息。
    assert {row.rule_id for row in named_rx_type} == {
        "RX-LEGAL-MA-OUT-DAYS",
        "RX-LEGAL-MA-CANCER-OUT-DAYS",
        "RX-LEGAL-MA-INPATIENT-DAYS",
    }


def test_missing_skin_test_result_is_undeterminable_not_violation(rules, prescription_rows) -> None:
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")
    skin_rows = [
        row
        for row in judgement.rows
        if row.rule_id == "RX-APPR-SKIN-TEST-UNSPECIFIED"
    ]
    by_prescription = {row.prescription_no: row for row in skin_rows}
    assert by_prescription

    missing = by_prescription["RX-0020"]
    assert missing.conclusion is Conclusion.UNDETERMINABLE
    assert "skin_test_result" in missing.missing_fields

    explicit = by_prescription["RX-0021"]
    assert explicit.conclusion is Conclusion.VIOLATION, "显式『未注明』应为违规"
    assert explicit.missing_fields == ()

    negative = by_prescription["RX-0022"]
    assert negative.conclusion is Conclusion.COMPLIANT, "『阴性』应为合规"


def test_over_limit_prescription_is_violation(rules, prescription_rows) -> None:
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")
    rows = {
        (row.prescription_no, row.rule_id): row for row in judgement.rows
    }

    assert rows[("RX-0001", "RX-LEGAL-MA-OUT-DAYS")].conclusion is Conclusion.COMPLIANT
    assert rows[("RX-0002", "RX-LEGAL-MA-OUT-DAYS")].conclusion is Conclusion.VIOLATION
    assert rows[("RX-0009", "RX-LEGAL-CR-OUT-DAYS")].conclusion is Conclusion.VIOLATION
    assert rows[("RX-0011", "RX-LEGAL-SPIRIT2-OUT-DAYS")].conclusion is Conclusion.VIOLATION
    assert rows[("RX-0019", "RX-LEGAL-MA-CANCER-OUT-DAYS")].conclusion is Conclusion.VIOLATION
    assert "RX-LEGAL-MA-OUT-DAYS" in rows[("RX-0002", "RX-LEGAL-MA-OUT-DAYS")].reason


def test_vague_usage_text_is_violation_and_names_the_term(rules, prescription_rows) -> None:
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")
    rows = {
        (row.prescription_no, row.rule_id): row for row in judgement.rows
    }
    vague = rows[("RX-0004", "RX-NORM-USAGE-VAGUE")]
    assert vague.conclusion is Conclusion.VIOLATION
    assert "遵医嘱" in vague.reason


def test_missing_front_matter_diagnosis_is_violation(rules, prescription_rows) -> None:
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")
    rows = {
        (row.prescription_no, row.rule_id): row for row in judgement.rows
    }
    front = rows[("RX-0003", "RX-NORM-FRONT-DIAG")]
    assert front.conclusion is Conclusion.VIOLATION
    assert "clinical_diag" in front.missing_fields


def test_reason_text_carries_rule_id_basis_and_no_patient_identifier(rules, prescription_rows) -> None:
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")
    identifiers = {row.patient_ref for row in prescription_rows}
    for row in judgement.rows:
        assert row.rule_id in row.reason
        assert row.basis in row.reason
        for identifier in identifiers:
            assert identifier not in row.reason, "归因文本不得带出患者标识"


def test_ledger_file_has_fixed_columns_and_round_trips(tmp_path: Path, rules, prescription_rows) -> None:
    judgement = judge_batch(prescription_rows, rules, batch_no="DEMO-BATCH-001")
    path = write_judgement_ledger(judgement.rows, tmp_path / "out")

    assert path.exists()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle))
    assert tuple(header) == LEDGER_COLUMNS

    records = load_judgement_ledger(path)
    assert len(records) == len(judgement.rows)
    assert all(record["rule_id"] for record in records)
    assert all(record["basis"] for record in records)


# ---------------------------------------------------------------------------
# 表达式语义
# ---------------------------------------------------------------------------


def test_missing_field_yields_unknown_not_false() -> None:
    row = PrescriptionRow(days_qty="")
    evaluation = evaluate_expression("days_qty > 1", row)
    assert evaluation.truth.value == "unknown"
    assert evaluation.missing == ("days_qty",)


def test_non_comparable_value_is_unknown_not_violation() -> None:
    # T14 完整判定内核上线后，本例的取值口径有变化：
    # 「不详」按受限表达式的三档空值语义（``expression/evaluator.py`` 的
    # ``EXPLICIT_NO``）属**显式否定值**——已知没写明，不等于「不可比」，
    # 与正数比较时按「无法证明满足条件」处理（违规），不是不可判定。
    # 「口径不可比」要用真正无法转成数值的写法（如「适量」），此处改用它。
    row = PrescriptionRow(days_qty="适量")
    evaluation = evaluate_expression("days_qty > 1", row)
    assert evaluation.truth.value == "unknown"
    assert "口径不可比" in evaluation.detail


def test_unsupported_expression_raises_explicitly() -> None:
    # T14 完整判定内核接上受限表达式求值器后，``and`` / ``or`` / ``not``
    # 已是表达式语言的一部分（不再属于「不被支持」）；真正越界的是
    # **函数调用 / 属性访问 / 赋值**等能力，载入期与判定期都必须显式报错。
    row = PrescriptionRow(days_qty="3", rx_type="住院")
    with pytest.raises(ExpressionError):
        evaluate_expression("days_qty > abs(1)", row)


def test_logical_connection_is_supported_by_the_full_engine() -> None:
    row = PrescriptionRow(days_qty="3", rx_type="住院")
    evaluation = evaluate_expression("days_qty > 1 and rx_type == '住院'", row)
    assert evaluation.truth.value == "true"


def test_expression_referencing_unknown_field_raises_explicitly() -> None:
    row = PrescriptionRow()
    with pytest.raises(ExpressionError):
        evaluate_expression("no_such_field > 1", row)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_judge_writes_ledger_and_prints_batch_summary(tmp_path: Path, capsys) -> None:
    out_dir = tmp_path / "out"
    exit_code = main(
        [
            "judge",
            "--inbox",
            str(BATCH_DIR),
            "--out",
            str(out_dir),
            "--rules",
            str(SAMPLE_RULES),
        ]
    )
    assert exit_code == 0

    captured = capsys.readouterr().out
    assert "批次 DEMO-BATCH-001" in captured
    assert "判定台账 →" in captured

    ledger_path = out_dir / "judgement_ledger.csv"
    assert ledger_path.exists()
    records = load_judgement_ledger(ledger_path)
    assert len(records) >= 20
    assert any(record["conclusion"] == "不可判定" for record in records)
    assert any(record["conclusion"] == "违规" for record in records)


def test_cli_judge_json_output(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "judge",
            "--inbox",
            str(BATCH_DIR),
            "--out",
            str(tmp_path / "out"),
            "--rules",
            str(SAMPLE_RULES),
            "--json",
        ]
    )
    assert exit_code == 0

    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["batches"][0]["batch_no"] == "DEMO-BATCH-001"
    assert payload["batches"][0]["received_rows"] >= 20
    assert payload["batches"][0]["undeterminable"] > 0


def test_cli_judge_without_inbox_files_fails_explicitly(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    exit_code = main(
        ["judge", "--inbox", str(empty), "--out", str(tmp_path / "out"), "--rules", str(SAMPLE_RULES)]
    )
    assert exit_code == 2
