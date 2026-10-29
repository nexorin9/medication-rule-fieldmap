"""运维 CLI 子命令：规则校验 / 规则体检 / 台账导出 / 审计导出 / 批次重放 / 确认撤回 / 状态。

对应任务书 T35。CLI 是运维排障通道，也是药事审核岗不做界面操作时的快捷入口；
每个子命令都要有 ``--json`` 输出，便于科员拿脚本接（月度报表拼装、CI 预检）。

覆盖八组硬断言（任务书点名「每个子命令 ``--help`` 可用；``rules validate``
对非法规则行返回非 0 退出码并打印字段名与行列定位」）：

1. **``--help`` 全通**：12 个子命令（含两层子命令的第二层）都能打出帮助；
2. **规则校验**：合法规则集退出码 0；非法规则行退出码**非 0**，且输出里
   **点名字段名**与**行列定位**；
3. **规则体检**：落 ``out/rule_health.csv``，覆盖率的**分母是适用处方数**
   （不是批次里的全部处方数）；
4. **台账导出**：落判定台账；给了 ``--inbox`` 连不可判定台账与字段补齐顺序表
   一起导；
5. **审计导出**：落审计流水；``basis`` 为空时**显式报错且不落盘**；
6. **批次重放**：落 ``out/replay_<batch_no>.csv``，每行带依据出处；
7. **确认 / 撤回**：确认后状态推进到「药师已确认」，撤回后回到「已撤回」；
   **未确认永不自动回写**；
8. **``--json`` 可解析**：每个子命令的 JSON 输出都能被 ``json.loads`` 解析，
   键齐全（脚本能直接拼报表）。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from med_rule_fieldmap import cli

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"
BATCH_DIR = PROJECT_ROOT / "tests" / "data" / "batch"

#: 所有要能打出 --help 的子命令（两层子命令的第一层与第二层各算一个）
CLI_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("judge",),
    ("scan",),
    ("state",),
    ("status",),
    ("replay",),
    ("writeback",),
    ("confirm",),
    ("revoke",),
    ("rules", "validate"),
    ("rules", "health"),
    ("ledger", "export"),
    ("audit", "export"),
    ("batch", "replay"),
)

#: 非法规则行（缺依据出处 + 表达式越界调用函数）
INVALID_RULE = """\
rule_id: RX-BAD-EXAMPLE
layer: 合法性
basis: ""
version: "2026.10"
applies_when: {}
required_fields: [days_qty]
expression: "days_qty > abs(1)"
severity: 严重不合理用药
"""


@pytest.fixture()
def judged(tmp_path: Path) -> dict[str, Path]:
    """跑一遍 ``judge``，产出一个有判定行与审计行的状态库。

    用真实链路（CSV → 判定 → 状态库）而不是手工塞库：CLI 子命令读的就是
    这张库，塞出来的假数据测不出「导出件与库里的行对不对得上」。
    """
    out_dir = tmp_path / "out"
    state_dir = tmp_path / "state"
    out_dir.mkdir()
    state_dir.mkdir()
    db_path = state_dir / "state.sqlite3"
    code = cli.main(
        [
            "judge",
            "--inbox",
            str(BATCH_DIR),
            "--out",
            str(out_dir),
            "--rules",
            str(SAMPLE_RULES),
            "--state-db",
            str(db_path),
        ]
    )
    assert code == cli.EXIT_OK
    return {"db": db_path, "out": out_dir, "tmp": tmp_path}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(record) for record in csv.DictReader(handle)]


def _first_judgement_key(db_path: Path) -> str:
    """取一条判定行的幂等键（供确认 / 撤回用）。"""
    from med_rule_fieldmap.store import Store

    store = Store(db_path)
    try:
        rows = store.list_judgements("DEMO-BATCH-001")
    finally:
        store.close()
    assert rows, "状态库里应当有判定行"
    return rows[0].idempotency_key


# ---------------------------------------------------------------------------
# 1. --help 全通
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", CLI_COMMANDS, ids=lambda c: " ".join(c))
def test_every_subcommand_help_exits_zero(command, capsys) -> None:
    """每个子命令的 ``--help`` 都可用（退出码 0，且打出用法）。"""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(list(command) + ["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out


def test_top_level_help_lists_all_subcommands(capsys) -> None:
    """顶层帮助列出全部子命令（含两层子命令的第一层）。"""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for name in (
        "judge",
        "scan",
        "state",
        "status",
        "replay",
        "writeback",
        "confirm",
        "revoke",
        "rules",
        "ledger",
        "audit",
        "batch",
    ):
        assert name in out


# ---------------------------------------------------------------------------
# 2. rules validate
# ---------------------------------------------------------------------------


def test_rules_validate_accepts_sample_rules(capsys) -> None:
    """合法规则集：退出码 0，并报出载入条数与生效条数。"""
    code = cli.main(["rules", "validate", "--rules", str(SAMPLE_RULES)])
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "全部规则行校验通过" in out
    assert "被拒 0 条" in out


def test_rules_validate_rejects_invalid_rule_with_field_and_location(
    tmp_path: Path, capsys
) -> None:
    """非法规则行：退出码非 0，输出点名字段名与行列定位。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "bad.yaml").write_text(INVALID_RULE, encoding="utf-8")

    code = cli.main(["rules", "validate", "--rules", str(rules_dir)])
    assert code != cli.EXIT_OK

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # 字段名
    assert "basis" in combined
    assert "expression" in combined
    # 行列定位
    assert "第 1 行" in combined
    assert "第 12 列" in combined
    # 规则号与文件名要能被点名
    assert "RX-BAD-EXAMPLE" in combined
    assert "bad.yaml" in combined


def test_rules_validate_json_payload_reports_rejected_rules(tmp_path: Path, capsys) -> None:
    """``--json`` 载荷里逐条给出字段名与行列定位，退出码同样非 0。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "bad.yaml").write_text(INVALID_RULE, encoding="utf-8")

    code = cli.main(["rules", "validate", "--rules", str(rules_dir), "--json"])
    assert code != cli.EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["rejected"] == 1
    assert payload["loaded"] == 0
    assert payload["ok"] is False
    issues = payload["errors"][0]["issues"]
    assert any(issue["field"] == "basis" for issue in issues)
    located = [issue for issue in issues if issue["line"] is not None]
    assert located, "表达式问题必须带行列定位"
    assert located[0]["column"] == 12


def test_rules_validate_rejects_invalid_rule_but_keeps_valid_ones(
    tmp_path: Path, capsys
) -> None:
    """非法规则只被拒，其余规则照常生效（与规则仓库的载入语义一致）。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "bad.yaml").write_text(INVALID_RULE, encoding="utf-8")
    (rules_dir / "good.yaml").write_text(
        "rule_id: RX-GOOD-EXAMPLE\n"
        "layer: 合法性\n"
        "basis: 处方审核规则体系 §3 特殊药品处方量限\n"
        'version: "2026.10"\n'
        "effective_from: \"2026-01-01\"\n"
        "applies_when: {}\n"
        "required_fields: [days_qty]\n"
        'expression: "days_qty > 1"\n'
        "severity: 严重不合理用药\n",
        encoding="utf-8",
    )

    code = cli.main(["rules", "validate", "--rules", str(rules_dir), "--json"])
    assert code != cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["loaded"] == 1
    assert payload["effective"] == 1
    assert payload["rejected"] == 1


# ---------------------------------------------------------------------------
# 3. rules health
# ---------------------------------------------------------------------------


def test_rules_health_writes_table_and_reports_coverage(judged, capsys) -> None:
    """规则体检：落体检表，覆盖率分母是**适用处方数**而非全部处方数。"""
    code = cli.main(
        [
            "rules",
            "health",
            "--rules",
            str(SAMPLE_RULES),
            "--db",
            str(judged["db"]),
            "--inbox",
            str(BATCH_DIR),
            "--out",
            str(judged["out"]),
        ]
    )
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "规则体检表 →" in out

    table = judged["out"] / "rule_health.csv"
    assert table.exists()
    records = _read_csv(table)
    assert records, "体检表不应为空"
    # 分母口径：适用处方数 ≤ 批次处方数（26 张），绝不能拿 26 当分母
    for record in records:
        applicable = int(record["applicable_prescriptions"])
        assert 0 <= applicable <= 26
    # 至少有一条规则的适用面小于批次总数（量限规则只管麻醉药品处方）
    assert any(int(record["applicable_prescriptions"]) < 26 for record in records)


def test_rules_health_json_payload_has_summary(judged, capsys) -> None:
    """``--json`` 载荷含体检小结与逐条结论。"""
    code = cli.main(
        [
            "rules",
            "health",
            "--rules",
            str(SAMPLE_RULES),
            "--db",
            str(judged["db"]),
            "--inbox",
            str(BATCH_DIR),
            "--out",
            str(judged["out"]),
            "--json",
        ]
    )
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["规则总数"] > 0
    assert "整体覆盖率" in payload["summary"]
    assert payload["items"]
    assert payload["rule_health_path"].endswith("rule_health.csv")


def test_rules_health_without_sources_fails_explicitly(tmp_path: Path, capsys) -> None:
    """既不给 ``--db`` 也不给 ``--inbox``：显式失败，不给一张全是僵尸规则的表。"""
    code = cli.main(
        ["rules", "health", "--rules", str(SAMPLE_RULES), "--out", str(tmp_path)]
    )
    assert code == cli.EXIT_ERROR
    assert "至少要给" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 4. ledger export
# ---------------------------------------------------------------------------


def test_ledger_export_writes_judgement_ledger(judged, capsys) -> None:
    """台账导出：落判定台账，行数与状态库里的判定行一致。"""
    out_dir = judged["tmp"] / "ledger_out"
    code = cli.main(
        [
            "ledger",
            "export",
            "--db",
            str(judged["db"]),
            "--out",
            str(out_dir),
        ]
    )
    assert code == cli.EXIT_OK
    assert "判定台账 →" in capsys.readouterr().out

    ledger = out_dir / "judgement_ledger.csv"
    assert ledger.exists()
    records = _read_csv(ledger)
    assert len(records) == 192
    assert {record["conclusion"] for record in records} <= {"合规", "违规", "不可判定"}


def test_ledger_export_with_inbox_writes_gap_and_priority(judged, capsys) -> None:
    """给了 ``--inbox``：不可判定台账与字段补齐顺序表一起导。"""
    out_dir = judged["tmp"] / "ledger_out2"
    code = cli.main(
        [
            "ledger",
            "export",
            "--db",
            str(judged["db"]),
            "--out",
            str(out_dir),
            "--inbox",
            str(BATCH_DIR),
        ]
    )
    assert code == cli.EXIT_OK
    assert (out_dir / "judgement_ledger.csv").exists()
    assert (out_dir / "undeterminable_ledger.csv").exists()
    assert (out_dir / "field_gap_priority.csv").exists()


def test_ledger_export_filters_by_conclusion(judged, capsys) -> None:
    """按结论过滤：只导不可判定的那部分。"""
    out_dir = judged["tmp"] / "ledger_out3"
    code = cli.main(
        [
            "ledger",
            "export",
            "--db",
            str(judged["db"]),
            "--out",
            str(out_dir),
            "--conclusion",
            "不可判定",
        ]
    )
    assert code == cli.EXIT_OK
    records = _read_csv(out_dir / "judgement_ledger.csv")
    assert records
    assert {record["conclusion"] for record in records} == {"不可判定"}


def test_ledger_export_rejects_unknown_conclusion(judged, capsys) -> None:
    """结论过滤取值非法：显式失败，不静默导出一份空表。"""
    code = cli.main(
        [
            "ledger",
            "export",
            "--db",
            str(judged["db"]),
            "--out",
            str(judged["tmp"] / "ledger_out4"),
            "--conclusion",
            "大概合规",
        ]
    )
    assert code == cli.EXIT_ERROR
    assert "取值非法" in capsys.readouterr().err


def test_ledger_export_json_payload(judged, capsys) -> None:
    """``--json`` 载荷含行数与三态计数。"""
    code = cli.main(
        [
            "ledger",
            "export",
            "--db",
            str(judged["db"]),
            "--out",
            str(judged["tmp"] / "ledger_out5"),
            "--json",
        ]
    )
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == 192
    assert payload["counts"]["合规"] + payload["counts"]["违规"] + payload["counts"][
        "不可判定"
    ] == 192


# ---------------------------------------------------------------------------
# 5. audit export
# ---------------------------------------------------------------------------


def test_audit_export_writes_audit_ledger(judged, capsys) -> None:
    """审计导出：落审计流水，判定事件行数与判定行数一致。"""
    out_dir = judged["tmp"] / "audit_out"
    code = cli.main(
        ["audit", "export", "--db", str(judged["db"]), "--out", str(out_dir)]
    )
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "审计台账 →" in out

    ledger = out_dir / "audit_ledger.csv"
    assert ledger.exists()
    records = _read_csv(ledger)
    assert len(records) == 192
    # 每条判定事件都要说得出依据出处
    assert all(record["basis"].strip() for record in records)
    assert {record["action"] for record in records} == {"判定"}


def test_audit_export_json_payload(judged, capsys) -> None:
    """``--json`` 载荷含行数、动作分布与自检结论。"""
    code = cli.main(
        [
            "audit",
            "export",
            "--db",
            str(judged["db"]),
            "--out",
            str(judged["tmp"] / "audit_out2"),
            "--json",
        ]
    )
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == 192
    assert payload["check"]["basis_missing"] == 0
    assert payload["check"]["basis_ok"] is True


def test_audit_export_fails_when_basis_missing(judged, capsys) -> None:
    """依据出处缺失：显式报错且**不落盘**（不导出一份看着正常的表）。"""
    import sqlite3

    # 直接改库模拟「历史数据里有一条结论说不出依据」——走状态库的公开方法
    # 改不了 ``basis``（写入路径总会带上依据）。
    conn = sqlite3.connect(judged["db"])
    try:
        conn.execute(
            "UPDATE audit SET basis = '' WHERE batch_no = ?", ("DEMO-BATCH-001",)
        )
        conn.commit()
    finally:
        conn.close()

    out_dir = judged["tmp"] / "audit_out3"
    code = cli.main(
        ["audit", "export", "--db", str(judged["db"]), "--out", str(out_dir)]
    )
    assert code == cli.EXIT_ERROR
    assert "缺依据出处" in capsys.readouterr().err
    assert not (out_dir / "audit_ledger.csv").exists()


# ---------------------------------------------------------------------------
# 6. batch replay
# ---------------------------------------------------------------------------


def test_batch_replay_writes_replay_file(judged, capsys) -> None:
    """批次重放：落回放件，每行带规则号、规则版本与依据出处。"""
    out_dir = judged["tmp"] / "replay_out"
    code = cli.main(
        [
            "batch",
            "replay",
            "--db",
            str(judged["db"]),
            "--batch",
            "DEMO-BATCH-001",
            "--out",
            str(out_dir),
        ]
    )
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "回放件 →" in out

    target = out_dir / "replay_DEMO-BATCH-001.csv"
    assert target.exists()
    records = _read_csv(target)
    assert len(records) == 192
    assert all(record["basis"].strip() for record in records)
    assert all(record["rule_version"].strip() for record in records)


def test_batch_replay_filters_by_rule(judged, capsys) -> None:
    """按规则号过滤：只回放某条规则。"""
    out_dir = judged["tmp"] / "replay_out2"
    code = cli.main(
        [
            "batch",
            "replay",
            "--db",
            str(judged["db"]),
            "--batch",
            "DEMO-BATCH-001",
            "--out",
            str(out_dir),
            "--rule",
            "RX-NORM-FRONT-DIAG",
        ]
    )
    assert code == cli.EXIT_OK
    records = _read_csv(out_dir / "replay_DEMO-BATCH-001.csv")
    assert records
    assert {record["rule_id"] for record in records} == {"RX-NORM-FRONT-DIAG"}


def test_batch_replay_unknown_batch_fails(judged, capsys) -> None:
    """批次号不存在：显式失败，不产出空回放件。"""
    code = cli.main(
        [
            "batch",
            "replay",
            "--db",
            str(judged["db"]),
            "--batch",
            "NO-SUCH-BATCH",
            "--out",
            str(judged["tmp"] / "replay_out3"),
        ]
    )
    assert code == cli.EXIT_ERROR
    assert "没有匹配的判定行" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 7. confirm / revoke
# ---------------------------------------------------------------------------


def test_confirm_then_revoke_moves_state(judged, capsys) -> None:
    """确认 → 药师已确认；撤回 → 已撤回。留痕带药师标识。"""
    from med_rule_fieldmap.models import JudgementState
    from med_rule_fieldmap.store import Store

    key = _first_judgement_key(judged["db"])

    code = cli.main(
        [
            "confirm",
            "--db",
            str(judged["db"]),
            "--key",
            key,
            "--by",
            "pharm-007",
            "--note",
            "药师已复核",
        ]
    )
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "确认" in out
    assert "pharm-007" in out

    store = Store(judged["db"])
    try:
        row = store.get_judgement(key)
        assert row is not None
        assert row.state is JudgementState.CONFIRMED
    finally:
        store.close()

    code = cli.main(
        [
            "revoke",
            "--db",
            str(judged["db"]),
            "--key",
            key,
            "--by",
            "pharm-007",
            "--note",
            "复核有误",
        ]
    )
    assert code == cli.EXIT_OK
    assert "撤回" in capsys.readouterr().out

    store = Store(judged["db"])
    try:
        row = store.get_judgement(key)
        assert row is not None
        assert row.state is JudgementState.WITHDRAWN
        # 留痕是追加式的：确认与撤回各留一条
        records = store.list_confirmations(key)
        assert len(records) == 2
        assert {record.action for record in records} == {"确认", "撤回"}
    finally:
        store.close()


def test_confirm_requires_pharmacist_id(judged, capsys) -> None:
    """不给药师标识：显式失败（系统不代签）。"""
    key = _first_judgement_key(judged["db"])
    code = cli.main(["confirm", "--db", str(judged["db"]), "--key", key])
    assert code == cli.EXIT_ERROR
    assert "不代签" in capsys.readouterr().err


def test_confirm_json_payload(judged, capsys) -> None:
    """``--json`` 载荷含状态迁移与回写状态。"""
    key = _first_judgement_key(judged["db"])
    code = cli.main(
        [
            "confirm",
            "--db",
            str(judged["db"]),
            "--key",
            key,
            "--by",
            "pharm-007",
            "--json",
        ]
    )
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["state_before"] == "已判定"
    assert payload["state_after"] == "药师已确认"


# ---------------------------------------------------------------------------
# 8. status
# ---------------------------------------------------------------------------


def test_status_json_payload_has_daemon_and_run_summary(judged, capsys) -> None:
    """状态：守护是否在跑 + 批次 / 判定 / 待复核 + 运行摘要。"""
    code = cli.main(["status", "--db", str(judged["db"]), "--json"])
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["daemon_running"] is False
    assert payload["batches"] == 1
    assert payload["judgement_rows"] == 192
    assert payload["pending_review"] > 0
    assert "totals" in payload
    assert "run_summary" in payload


def test_status_on_missing_db_reports_cleanly(tmp_path: Path, capsys) -> None:
    """状态库还没建：报「尚未落过批次」，不报错。"""
    code = cli.main(["status", "--db", str(tmp_path / "nope.sqlite3"), "--json"])
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["batches"] == 0
    assert payload["daemon_running"] is False
