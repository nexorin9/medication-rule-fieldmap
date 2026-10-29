"""运维 CLI：命令行入口。

本模块是药事审核岗与信息科**不打开界面**时的快捷通道，也是运维排障通道。
所有子命令都支持 ``--json``，便于科员拿脚本接（月度报表拼装、CI 预检）。

**判定侧**

* ``judge`` —— 把落地目录里的医嘱批次跑一遍判定，产出判定台账、不可判定台账
  （按缺失字段聚合）与批次摘要行；带 ``--state-db`` 时同时落状态库并按事件指纹
  去重（重复投递只补审计）；
* ``scan`` —— 监听落地目录：文件大小/mtime 连续两次不变才算写完，切出批次；
  截断 / 乱码 / 缺必需列的文件**整文件挂起**（挂起台账 + 告警），不做部分判定；
  ``--release`` 人工解除挂起后重投。

**规则侧**

* ``rules validate`` —— 规则行载入校验：必填项、依据出处、表达式语法与能力禁用、
  生效区间逐条核对；**有非法规则时退出码非 0**，并打印字段名与行列定位；
* ``rules health`` —— 规则集体检：僵尸规则 / 恒不可判定 / 覆盖率，
  落 ``out/rule_health.csv``。

**台账侧**

* ``ledger export`` —— 从状态库导出判定台账（可按批次与结论过滤），
  给了 ``--inbox`` 时连不可判定台账与字段补齐顺序表一起导；
* ``audit export`` —— 导出审计流水（判定事件 + 出站调用事件合成一张表），
  缺依据出处时显式报错且不落盘；
* ``batch replay`` —— 回放某批次的判定证据链，导出 ``out/replay_<batch_no>.csv``：
  每条结论带规则号、规则版本、依据出处、归因文本、事件文件指纹、当前状态
  与写入时间，可按规则号或结论类型过滤。

**状态与回写侧**

* ``state`` —— 查状态库：批次登记、判定行数、审计行数、重复投递情况；
* ``status`` —— 守护运行状态与运行摘要（守护在不在跑、判了多少、还有多少等着
  药师复核、表达式解析缓存命中率）；
* ``confirm`` / ``revoke`` —— 药师确认 / 撤回；**回写门禁**：只有已确认的结论
  才回写 HIS，撤回后排队中的回写作废；
* ``writeback`` —— 查看 / 重放退避超次后待重放的回写队列（``pending_writeback``）：
  HIS 抖动时已确认的结论不会凭空消失；``--replay`` 按原样再发一次，
  幂等键保证早已回写成功的结论不被重复回写。

用法::

    PYTHONPATH=src python -m med_rule_fieldmap.cli judge \\
        --inbox tests/data/batch --out out --rules rules/sample

    PYTHONPATH=src python -m med_rule_fieldmap.cli scan --inbox inbox --out out

    PYTHONPATH=src python -m med_rule_fieldmap.cli judge \\
        --inbox inbox --out out --rules rules/sample --state-db state/mrfm.sqlite3

    PYTHONPATH=src python -m med_rule_fieldmap.cli rules validate --rules rules/sample

    PYTHONPATH=src python -m med_rule_fieldmap.cli rules health \\
        --rules rules/sample --db state/mrfm.sqlite3 --inbox inbox --out out

    PYTHONPATH=src python -m med_rule_fieldmap.cli ledger export \\
        --db state/mrfm.sqlite3 --out out --batch DEMO-BATCH-001

    PYTHONPATH=src python -m med_rule_fieldmap.cli audit export \\
        --db state/mrfm.sqlite3 --out out

    PYTHONPATH=src python -m med_rule_fieldmap.cli batch replay \\
        --db state/mrfm.sqlite3 --batch DEMO-BATCH-001 --out out

    PYTHONPATH=src python -m med_rule_fieldmap.cli state --db state/mrfm.sqlite3

    PYTHONPATH=src python -m med_rule_fieldmap.cli status --db state/mrfm.sqlite3

    PYTHONPATH=src python -m med_rule_fieldmap.cli confirm \\
        --db state/mrfm.sqlite3 --key <判定幂等键> --by pharm-007

    PYTHONPATH=src python -m med_rule_fieldmap.cli revoke \\
        --db state/mrfm.sqlite3 --key <判定幂等键> --by pharm-007

    PYTHONPATH=src python -m med_rule_fieldmap.cli writeback --db state/mrfm.sqlite3 --out out

（``pip install -e .`` 后可省略 ``PYTHONPATH=src``。）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from .engine.judge import BatchJudgement, judge_batch, summarize_conclusions
from .engine.confirm import ConfirmError, ConfirmService
from .engine.severity import mark_grade, resolve_severity
from .config import Config
from .daemon import PENDING_REVIEW_STATES
from .daemon_lock import holder_of, lock_path_for
from .ingest.csv_reader import CsvReadError, list_csv_files, read_prescription_rows
from .ingest.hl7_orm import (
    HL7_SUFFIXES,
    Hl7ParseError,
    list_hl7_files,
    read_order_rows,
)
from .ingest.watcher import EVENT_BATCH, BatchFile, InboxWatcher
from .ledger.audit_export import (
    AuditExportError,
    assert_audit_invariant,
    build_audit_records,
    export_audit_ledger,
    summarize_audit,
    write_audit_ledger,
)
from .ledger.evidence_replay import (
    chain_health,
    check_evidence_chain,
    replay_from_store,
    summarize_replay,
    write_replay_csv,
)
from .ledger.gap import (
    build_gap_entries,
    index_prescriptions,
    summarize_by_field,
    write_undeterminable_ledger,
)
from .ledger.ledger import LEDGER_FILENAME, write_judgement_ledger
from .ledger.priority import (
    build_priority_table,
    select_interface_gaps,
    write_priority_table,
)
from .ledger.rule_health import (
    build_rule_health,
    summarize_rule_health,
    write_rule_health,
)
from .models import Conclusion, JudgementRow, JudgementState, PrescriptionRow
from .outbound.his_review import HisCredentialError, HisReviewWriter
from .outbound.idempotency import WritebackIdempotencyGuard
from .outbound.retry_queue import (
    PENDING_WRITEBACK_FILENAME,
    PendingWritebackQueue,
    summarize_queue,
)
from .rules.repository import RuleRepository
from .run_summary import current_summary, record_batch, summary_snapshot
from .store import Store, process_batch

#: 退出码：正常
EXIT_OK = 0
#: 退出码：参数 / 运行失败
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="med_rule_fieldmap",
        description="合理用药审方字段对照系统 · 运维命令",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    judge = subparsers.add_parser(
        "judge",
        help="对落地目录里的医嘱批次跑三态判定，产出判定台账",
    )
    judge.add_argument("--inbox", default="inbox", help="医嘱落地目录（只读）")
    judge.add_argument("--out", default="out", help="产物输出目录")
    judge.add_argument("--rules", default="rules", help="规则行目录")
    judge.add_argument(
        "--as-of",
        default=None,
        help="规则生效区间判定基准日（YYYY-MM-DD），缺省取当天",
    )
    judge.add_argument(
        "--state-db",
        default=None,
        metavar="PATH",
        help="状态库路径（SQLite）；给了就落库并按事件指纹去重（重复投递只补审计）",
    )
    judge.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出批次摘要（便于脚本消费）",
    )

    scan = subparsers.add_parser(
        "scan",
        help="扫描医嘱落地目录：写完的文件切批次，写坏的文件整文件挂起",
    )
    scan.add_argument("--inbox", default="inbox", help="医嘱落地目录（只读）")
    scan.add_argument("--out", default="out", help="产物输出目录（挂起台账落在这里）")
    scan.add_argument(
        "--wait-sec",
        type=float,
        default=5.0,
        help="等待文件写完的秒数（默认 5）",
    )
    scan.add_argument(
        "--settle-checks",
        type=int,
        default=2,
        help="连续几次观察大小/mtime 不变才算写完（默认 2）",
    )
    scan.add_argument(
        "--release",
        default=None,
        metavar="FILE_NAME",
        help="人工解除某份挂起文件（解除后该文件可重投），执行后退出",
    )
    scan.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出扫描结果（便于脚本消费）",
    )

    state = subparsers.add_parser(
        "state",
        help="查看状态库：批次登记、判定行数、审计行数与重复投递",
    )
    state.add_argument("--db", required=True, metavar="PATH", help="状态库路径（SQLite）")
    state.add_argument("--batch", default=None, help="只看某个批次号")
    state.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出状态快照（便于脚本消费）",
    )

    status = subparsers.add_parser(
        "status",
        help="查看守护运行状态与运行摘要（批次 / 三态计数 / 回写数 / 耗时）",
    )
    status.add_argument("--db", required=True, metavar="PATH", help="状态库路径（SQLite）")
    status.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出状态快照（便于脚本消费）",
    )

    replay = subparsers.add_parser(
        "replay",
        help="回放某批次的判定证据链（每条结论可追到规则版本与依据）",
    )
    _add_replay_arguments(replay)

    writeback = subparsers.add_parser(
        "writeback",
        help="查看 / 重放退避超次后待重放的回写队列（HIS 抖动时的兜底通道）",
    )
    writeback.add_argument("--db", required=True, metavar="PATH", help="状态库路径（SQLite）")
    writeback.add_argument("--out", default="out", help="导出目录（默认 out）")
    writeback.add_argument(
        "--replay",
        action="store_true",
        help="把待重放的回写按原样再发一次（需 HIS 凭据；幂等键保证不重复）",
    )
    writeback.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="最多重放几条（默认全部）",
    )
    writeback.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出队列快照（便于脚本消费）",
    )

    confirm = subparsers.add_parser(
        "confirm",
        help="药师确认 / 撤回 / 触发回写（回写门禁：只有已确认的结论才回写 HIS）",
    )
    _add_confirm_arguments(confirm)
    confirm.add_argument(
        "--withdraw",
        action="store_true",
        help="撤回而不是确认（排队中的回写作废；已发出的同步撤回通知到 HIS）",
    )

    revoke = subparsers.add_parser(
        "revoke",
        help="药师撤回一条已确认的结论（等价于 confirm --withdraw）",
    )
    _add_confirm_arguments(revoke)

    # -- 规则行相关：rules validate / rules health -------------------------
    rules_ns = subparsers.add_parser(
        "rules",
        aliases=["rule"],
        help="规则行相关操作：载入校验（validate）与集体检（health）",
    )
    rules_sub = rules_ns.add_subparsers(dest="rules_command", required=True)

    rules_validate = rules_sub.add_parser(
        "validate",
        help="校验规则行：必填项、依据出处、表达式语法与能力禁用、生效区间",
    )
    rules_validate.add_argument("--rules", default="rules", help="规则行目录")
    rules_validate.add_argument(
        "--as-of",
        default=None,
        help="规则生效区间判定基准日（YYYY-MM-DD），缺省取当天",
    )
    rules_validate.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出校验结果（便于脚本消费）",
    )

    rules_health = rules_sub.add_parser(
        "health",
        help="规则集体检：僵尸规则 / 恒不可判定 / 覆盖率",
    )
    rules_health.add_argument("--rules", default="rules", help="规则行目录")
    rules_health.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="状态库路径（读判定台账，算命中率与恒不可判定）",
    )
    rules_health.add_argument(
        "--inbox",
        default=None,
        metavar="DIR",
        help="医嘱落地目录（覆盖率分母的来源：这条规则本批该管多少张处方）",
    )
    rules_health.add_argument("--out", default="out", help="产物输出目录（体检表落在这里）")
    rules_health.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出体检结论（便于脚本消费）",
    )

    # -- 台账侧：ledger export / audit export / batch replay ----------------
    ledger_ns = subparsers.add_parser(
        "ledger",
        help="判定台账相关操作：导出（export）",
    )
    ledger_sub = ledger_ns.add_subparsers(dest="ledger_command", required=True)
    ledger_export = ledger_sub.add_parser(
        "export",
        help="从状态库导出判定台账（可按批次与结论过滤）",
    )
    _add_ledger_export_arguments(ledger_export)

    audit_ns = subparsers.add_parser(
        "audit",
        help="审计台账相关操作：导出（export）",
    )
    audit_sub = audit_ns.add_subparsers(dest="audit_command", required=True)
    audit_export = audit_sub.add_parser(
        "export",
        help="导出审计流水（判定事件 + 出站调用事件合成一张表）",
    )
    _add_audit_export_arguments(audit_export)

    batch_ns = subparsers.add_parser(
        "batch",
        help="批次相关操作：回放（replay）",
    )
    batch_sub = batch_ns.add_subparsers(dest="batch_command", required=True)
    batch_replay = batch_sub.add_parser(
        "replay",
        help="回放某批次的判定证据链（每条结论可追到规则版本与依据）",
    )
    _add_replay_arguments(batch_replay)

    return parser


def _add_confirm_arguments(parser: argparse.ArgumentParser) -> None:
    """给 ``confirm`` / ``revoke`` 两个子命令装同一套参数。"""
    parser.add_argument("--db", required=True, metavar="PATH", help="状态库路径（SQLite）")
    parser.add_argument(
        "--key",
        default=None,
        metavar="KEY",
        help="判定标识：判定幂等键（sha256）或业务键（batch_no｜prescription_no｜rule_id｜rule_version）",
    )
    parser.add_argument(
        "--by",
        default=None,
        metavar="PHARMACIST_ID",
        help="药师标识（reviewed_by / withdrawn_by）；系统不代签",
    )
    parser.add_argument("--note", default="", help="确认 / 撤回备注（写进留痕）")
    parser.add_argument(
        "--trigger",
        action="store_true",
        help="触发回写：把已确认的结论发到 HIS（需 HIS 凭据，缺则显式失败）",
    )
    parser.add_argument(
        "--batch",
        default=None,
        metavar="BATCH_NO",
        help="只看某个批次的判定行",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="本次最多处理几条（默认全部）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="as_list",
        help="列出待回写候选与确认 / 撤回留痕",
    )
    parser.add_argument(
        "--resync",
        action="store_true",
        help="重投未同步成功的撤回通知（需 HIS 凭据）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出结果（便于脚本消费）",
    )


def _add_replay_arguments(parser: argparse.ArgumentParser) -> None:
    """给 ``replay`` / ``batch replay`` 两个子命令装同一套参数。"""
    parser.add_argument("--db", required=True, metavar="PATH", help="状态库路径（SQLite）")
    parser.add_argument("--batch", required=True, metavar="BATCH_NO", help="要回放的批次号")
    parser.add_argument("--out", default="out", help="回放件输出目录（默认 out）")
    parser.add_argument(
        "--rules",
        default=None,
        metavar="DIR",
        help="规则行目录；给了就补出「严重度来源」与「点评分级」两列",
    )
    parser.add_argument(
        "--rule",
        action="append",
        default=None,
        metavar="RULE_ID",
        help="只看某条规则（可重复给多个）",
    )
    parser.add_argument(
        "--conclusion",
        action="append",
        default=None,
        metavar="结论",
        help="只看某类结论：合规 / 违规 / 不可判定（可重复给多个）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出回放摘要（便于脚本消费）",
    )


def _add_ledger_export_arguments(parser: argparse.ArgumentParser) -> None:
    """给 ``ledger export`` 装参数。"""
    parser.add_argument("--db", required=True, metavar="PATH", help="状态库路径（SQLite）")
    parser.add_argument("--out", default="out", help="产物输出目录（默认 out）")
    parser.add_argument("--batch", default=None, metavar="BATCH_NO", help="只导某个批次")
    parser.add_argument(
        "--conclusion",
        action="append",
        default=None,
        metavar="结论",
        help="只导某类结论：合规 / 违规 / 不可判定（可重复给多个）",
    )
    parser.add_argument(
        "--inbox",
        default=None,
        metavar="DIR",
        help="医嘱落地目录；给了就连不可判定台账与字段补齐顺序表一起导",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出导出结果（便于脚本消费）",
    )


def _add_audit_export_arguments(parser: argparse.ArgumentParser) -> None:
    """给 ``audit export`` 装参数。"""
    parser.add_argument("--db", required=True, metavar="PATH", help="状态库路径（SQLite）")
    parser.add_argument("--out", default="out", help="产物输出目录（默认 out）")
    parser.add_argument("--batch", default=None, metavar="BATCH_NO", help="只导某个批次")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出导出结果（便于脚本消费）",
    )


def _parse_as_of(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"--as-of 取值非法（期望 YYYY-MM-DD）：{raw!r}") from exc


def cmd_judge(args: argparse.Namespace) -> int:
    """``judge`` 子命令：跑判定、落台账、打批次摘要行。"""
    rules_dir = Path(args.rules)
    repository = RuleRepository(rules_dir, as_of=_parse_as_of(args.as_of))
    repository.load()
    for error in repository.errors:
        print(
            f"[judge] 规则被拒（不影响其余规则生效）：{error}",
            file=sys.stderr,
        )
    rules = repository.effective_rules
    if not rules:
        print(f"[judge] 规则目录 {rules_dir} 里没有生效规则，判定终止", file=sys.stderr)
        return EXIT_ERROR

    files = list_csv_files(args.inbox) + list_hl7_files(args.inbox)
    if not files:
        print(
            f"[judge] 入站目录 {args.inbox} 里没有 CSV / HL7 批次文件，判定终止",
            file=sys.stderr,
        )
        return EXIT_ERROR

    out_dir = Path(args.out)
    summaries: list[dict[str, object]] = []
    all_rows = []
    audit_path: Path | None = None
    audit_check = None
    # 处方索引：判定行 → 处方行的反查表，用来分辨「接口未回传」与
    # 「医嘱未填写」，并统计字段缺口牵涉多少个科室（见 ledger/gap.py）。
    prescription_index: dict[str, PrescriptionRow] = {}
    # 落状态库时，台账按**批次**从库里读回（同一批次换名重投不会让台账多出一份）
    processed_batch_nos: list[str] = []
    store = Store(args.state_db) if args.state_db else None
    try:
        for path in files:
            try:
                if path.suffix.lower() in HL7_SUFFIXES:
                    batch = read_order_rows(path)
                    for warning in batch.warnings:
                        print(f"[judge] {warning}", file=sys.stderr)
                    prescription_rows = list(batch.rows)
                    source_kind = "hl7"
                else:
                    prescription_rows = read_prescription_rows(path)
                    source_kind = "csv"
            except (CsvReadError, Hl7ParseError) as exc:
                print(f"[judge] {exc}", file=sys.stderr)
                return EXIT_ERROR
            if not prescription_rows:
                print(f"[judge] 批次文件 {path.name} 没有可判定的医嘱行，跳过", file=sys.stderr)
                continue
            batch_no = prescription_rows[0].batch_no or path.stem
            prescription_index.update(index_prescriptions(prescription_rows))
            duplicate = False
            fingerprint = ""
            new_rows = 0
            if store is not None:
                batch_file = BatchFile(
                    path=path,
                    batch_no=batch_no,
                    rows=tuple(prescription_rows),
                    source_kind=source_kind,
                )
                outcome = process_batch(store, batch_file, rules)
                duplicate = outcome.duplicate
                fingerprint = outcome.fingerprint
                new_rows = outcome.judgement_rows
                batch_rows = store.list_judgements(batch_no)
                counts = outcome.counts or summarize_conclusions(batch_rows)
                if batch_no not in processed_batch_nos:
                    processed_batch_nos.append(batch_no)
                # 运行摘要记「本次新增」，重复投递不把结论数重复累加
                record_batch(
                    received_rows=len(prescription_rows),
                    judgement_rows=outcome.judgement_rows,
                    counts=outcome.new_counts,
                )
            else:
                judgement: BatchJudgement = judge_batch(
                    prescription_rows,
                    rules,
                    batch_no=batch_no,
                    source_file=path.name,
                )
                batch_rows = list(judgement.rows)
                counts = summarize_conclusions(judgement.rows)
                all_rows.extend(batch_rows)
                record_batch(
                    received_rows=len(prescription_rows),
                    judgement_rows=len(batch_rows),
                    counts=counts,
                )
            summary: dict[str, object] = {
                "batch_no": batch_no,
                "source_file": path.name,
                "received_rows": len(prescription_rows),
                "judgement_rows": len(batch_rows),
                "compliant": counts.get(ConclusionKeys.compliant, 0),
                "violation": counts.get(ConclusionKeys.violation, 0),
                "undeterminable": counts.get(ConclusionKeys.undeterminable, 0),
            }
            if store is not None:
                summary["event_fingerprint"] = fingerprint
                summary["duplicate"] = duplicate
                summary["new_judgement_rows"] = new_rows
            summaries.append(summary)
        if store is not None:
            all_rows = [
                row
                for batch_no in processed_batch_nos
                for row in store.list_judgements(batch_no)
            ]
            # 审计台账：判定事件 + 出站调用事件合成一张审计流水（飞检自证用）。
            # 必须在状态库还开着的时候导——审计台账读的是库里的留痕。
            audit_path, audit_check = export_audit_ledger(store, out_dir)
    finally:
        if store is not None:
            store.close()

    if not all_rows:
        print("[judge] 没有可判定的医嘱行，判定终止", file=sys.stderr)
        return EXIT_ERROR

    ledger_path = write_judgement_ledger(all_rows, out_dir, filename=LEDGER_FILENAME)
    # 字段缺口台账：把「判不了」按缺失字段聚合，供信息科排接口补齐顺序
    gap_entries = build_gap_entries(all_rows, prescriptions=prescription_index)
    gap_path = write_undeterminable_ledger(gap_entries, out_dir)
    field_gaps = summarize_by_field(gap_entries)
    # 字段补齐顺序表：按「补一个字段能救多少张处方」排序，直接给信息科排期
    priority_items = build_priority_table(gap_entries)
    priority_path = write_priority_table(priority_items, out_dir)
    interface_gaps = select_interface_gaps(priority_items)

    if args.as_json:
        payload = {
            "ledger_path": str(ledger_path),
            "gap_ledger_path": str(gap_path),
            "gap_fields": [item.to_record() for item in field_gaps],
            "field_gap_priority_path": str(priority_path),
            "field_gap_priority": [item.to_record() for item in priority_items],
            "interface_gaps": [item.to_record() for item in interface_gaps],
            "batches": summaries,
            "run_summary": summary_snapshot(),
        }
        if args.state_db:
            payload["state_db"] = str(args.state_db)
        if audit_path is not None and audit_check is not None:
            payload["audit_ledger_path"] = str(audit_path)
            payload["audit_summary"] = audit_check.to_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    for summary in summaries:
        extra = ""
        if "duplicate" in summary:
            extra = (
                f" · 新增判定 {summary['new_judgement_rows']} 条"
                f" · 事件指纹 {str(summary['event_fingerprint'])[:12]}"
            )
            if summary["duplicate"]:
                extra += " · 重复投递（只补审计，未新增判定行）"
        print(
            f"[judge] 批次 {summary['batch_no']}（来源 {summary['source_file']}）："
            f"收到 {summary['received_rows']} 行 · "
            f"命中规则 {summary['judgement_rows']} 条 · "
            f"合规 {summary['compliant']} · 违规 {summary['violation']} · "
            f"不可判定 {summary['undeterminable']} · "
            f"判定台账 → {ledger_path}{extra}"
        )
    print(f"[judge] 运行摘要：{current_summary().render()}")
    if gap_entries:
        top = " · ".join(
            f"{item.missing_field} {item.affected_prescriptions} 张"
            for item in field_gaps[:3]
        )
        print(
            f"[judge] 不可判定台账 → {gap_path}"
            f"（{len(gap_entries)} 组字段缺口 · {len(field_gaps)} 个字段 · "
            f"缺口最多：{top}）"
        )
    else:
        print(f"[judge] 不可判定台账 → {gap_path}（本批没有字段缺口）")
    if priority_items:
        top_priority = " · ".join(
            f"{item.priority}) {item.missing_field} {item.affected_prescriptions} 张"
            for item in priority_items[:3]
        )
        print(
            f"[judge] 字段补齐顺序表 → {priority_path}"
            f"（{len(priority_items)} 个字段 · 先补：{top_priority} · "
            f"其中要动接口 {len(interface_gaps)} 个）"
        )
    else:
        print(f"[judge] 字段补齐顺序表 → {priority_path}（本批没有字段缺口）")
    if audit_path is not None and audit_check is not None:
        print(
            f"[judge] 审计台账 → {audit_path}"
            f"（判定事件 {audit_check.judgement_events} 条 · "
            f"出站调用 {audit_check.outbound_events} 条 · "
            f"依据缺失 {audit_check.basis_missing} 条）"
        )
    return EXIT_OK


class ConclusionKeys:
    """结论枚举的取值（避免在 CLI 里散落中文结论字面量）。"""

    compliant = Conclusion.COMPLIANT.value
    violation = Conclusion.VIOLATION.value
    undeterminable = Conclusion.UNDETERMINABLE.value


def cmd_scan(args: argparse.Namespace) -> int:
    """``scan`` 子命令：监听落地目录 —— 写完的切批次，写坏的整文件挂起。"""
    inbox = Path(args.inbox)
    out_dir = Path(args.out)
    watcher = InboxWatcher(
        inbox,
        out_dir=out_dir,
        settle_checks=args.settle_checks,
        poll_interval=0.2,
    )

    if args.release:
        if not watcher.release(args.release):
            print(
                f"[scan] 文件 {args.release} 当前没有挂起记录，无需解除",
                file=sys.stderr,
            )
            return EXIT_ERROR
        print(f"[scan] 已人工解除挂起：{args.release}，重新投递后即可重判")
        return EXIT_OK

    batches: list[dict[str, object]] = []
    suspended: list[dict[str, object]] = []
    deadline = time.monotonic() + max(0.0, args.wait_sec)
    while True:
        for event in watcher.poll_once():
            if event.kind == EVENT_BATCH and event.batch is not None:
                batch = event.batch
                batches.append(
                    {
                        "batch_no": batch.batch_no,
                        "source_file": batch.path.name,
                        "prescription_rows": batch.prescription_count,
                        "source_kind": batch.source_kind,
                    }
                )
                if not args.as_json:
                    print(
                        f"[scan] 批次 {batch.batch_no} 已切分："
                        f"来源 {batch.path.name} · {batch.prescription_count} 行 · "
                        f"格式 {batch.source_kind}"
                    )
            else:
                report = event.report
                suspended.append(
                    {
                        "source_file": event.path.name,
                        "kind": report.kind if report else "",
                        "reason": event.reason,
                        "missing_columns": list(report.missing_columns) if report else [],
                    }
                )
                if not args.as_json:
                    missing = report.missing_columns if report else ()
                    print(
                        f"[scan] 整文件挂起：{event.path.name} · "
                        f"{report.kind if report else '未知原因'}"
                        + (f" · 缺列 {'、'.join(missing)}" if missing else "")
                        + f"（{event.reason}）"
                    )
        if not watcher.pending_candidates():
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)

    pending = watcher.pending_suspended()
    if args.as_json:
        print(
            json.dumps(
                {
                    "batches": batches,
                    "suspended": suspended,
                    "pending_suspended": [
                        {
                            "file_name": record.file_name,
                            "kind": record.kind,
                            "reason": record.reason,
                            "missing_columns": list(record.missing_columns),
                            "suspended_at": record.suspended_at,
                        }
                        for record in pending
                    ],
                    "suspend_ledger": str(watcher.ledger.path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_OK

    print(
        f"[scan] 挂起台账：{watcher.ledger.path} · 当前挂起 {len(pending)} 份"
        + (f"（{'、'.join(record.file_name for record in pending)}）" if pending else "")
    )
    return EXIT_OK


def cmd_state(args: argparse.Namespace) -> int:
    """``state`` 子命令：查状态库（批次 / 判定 / 审计 / 重复投递）。"""
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[state] 状态库 {db_path} 不存在，尚未落过批次", file=sys.stderr)
        return EXIT_ERROR

    store = Store(db_path)
    try:
        batches = store.list_batches()
        if args.batch:
            batches = [item for item in batches if item.batch_no == args.batch]
        payload_batches = []
        for item in batches:
            record = item.to_dict()
            record["audit_rows"] = store.count_audit(item.batch_no)
            payload_batches.append(record)

        if args.as_json:
            print(
                json.dumps(
                    {
                        "state_db": str(db_path),
                        "batches": payload_batches,
                        "judgement_rows": store.count_judgements(args.batch),
                        "audit_rows": store.count_audit(args.batch),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return EXIT_OK

        print(f"[state] 状态库 {db_path} · 批次 {len(batches)} 个")
        for item in batches:
            print(
                f"[state] 批次 {item.batch_no}（来源 {item.source_file}）："
                f"处方 {item.prescription_count} 行 · 判定 {item.judgement_count} 条 · "
                f"审计 {store.count_audit(item.batch_no)} 行 · "
                f"状态 {item.status} · 指纹 {item.short_fingerprint}"
            )
        print(
            f"[state] 合计：判定 {store.count_judgements(args.batch)} 条 · "
            f"审计 {store.count_audit(args.batch)} 行"
        )
        return EXIT_OK
    finally:
        store.close()


def cmd_status(args: argparse.Namespace) -> int:
    """``status`` 子命令：守护运行状态 + 运行摘要。

    **这一屏回答三个问题**：守护在不在跑、这一阵子判了多少、有多少结论还等着
    药师复核。运行摘要落在状态库里，**跨进程重启也还在**（进程级摘要在重启后
    会清零，落库的这份不会）。
    """
    db_path = Path(args.db)
    lock_file = lock_path_for(db_path)
    holder = holder_of(lock_file)
    if not db_path.exists():
        payload = {
            "state_db": str(db_path),
            "daemon_running": holder is not None,
            "daemon_holder": holder.to_dict() if holder is not None else {},
            "batches": 0,
            "judgement_rows": 0,
            "pending_review": 0,
            "unfinished_batches": 0,
            "totals": {},
            "latest_batch": {},
        }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return EXIT_OK
        print(f"[status] 状态库 {db_path} 尚未落过批次")
        _print_status_running(holder)
        return EXIT_OK

    store = Store(db_path)
    try:
        rows = store.list_judgements()
        pending = sum(1 for row in rows if row.state in PENDING_REVIEW_STATES)
        totals = store.summarize_batch_runs()
        latest = store.latest_batch_run()
        payload = {
            "state_db": str(db_path),
            "daemon_running": holder is not None,
            "daemon_holder": holder.to_dict() if holder is not None else {},
            "batches": store.count_batches(),
            "judgement_rows": len(rows),
            "pending_review": pending,
            "unfinished_batches": store.count_unfinished_batches(),
            "totals": totals,
            "run_summary": summary_snapshot(),
            "latest_batch": latest.to_dict() if latest is not None else {},
        }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return EXIT_OK

        _print_status_running(holder)
        print(
            f"[status] 批次 {payload['batches']} 个 · 判定 {payload['judgement_rows']} 条 · "
            f"待药师复核 {pending} 条 · 没走完的批次 {payload['unfinished_batches']} 个"
        )
        if totals:
            print(
                f"[status] 累计：收到 {totals['received_rows']} 行 · "
                f"合规 {totals['compliant']} · 违规 {totals['violation']} · "
                f"不可判定 {totals['undeterminable']} · "
                f"回写 {totals['writeback_sent']} 条（失败 {totals['writeback_failed']} 条） · "
                f"推送 {totals['pushed']} 次 · 平均耗时 {totals['avg_elapsed_sec']}s"
            )
        if latest is not None:
            print(
                f"[status] 最近批次 {latest.batch_no}：收到 {latest.received_rows} 行 · "
                f"合规 {latest.compliant} · 违规 {latest.violation} · "
                f"不可判定 {latest.undeterminable} · 回写 {latest.writeback_sent} 条 · "
                f"耗时 {round(float(latest.elapsed_sec), 3)}s"
            )
        return EXIT_OK
    finally:
        store.close()


def _print_status_running(holder) -> None:
    """打印守护是否在跑（占用进程信息来自单实例锁文件）。"""
    if holder is None:
        print("[status] 守护进程：未在运行")
        return
    print(f"[status] 守护进程：运行中（{holder.describe()}）")


def cmd_replay(args: argparse.Namespace) -> int:
    """``replay`` 子命令：回放批次判定证据链并导出回放件。"""
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[replay] 状态库 {db_path} 不存在，没有可回放的批次", file=sys.stderr)
        return EXIT_ERROR

    # 给了规则行目录就补出「严重度来源」与「点评分级」两列：
    # 药师回看一条结论时要能同时看到「多严重、这严重度哪来的、属哪一类点评」
    severity_sources: dict[str, str] = {}
    grades: dict[str, str] = {}
    if args.rules:
        repository = RuleRepository(Path(args.rules))
        repository.load()
        for error in repository.errors:
            print(f"[replay] 规则被拒（不影响其余规则补列）：{error}", file=sys.stderr)
        for rule in repository.effective_rules:
            severity_sources[rule.rule_id] = resolve_severity(rule).source.value
            grades[rule.rule_id] = mark_grade(rule).value

    store = Store(db_path)
    try:
        entries = replay_from_store(
            store,
            args.batch,
            rule_ids=args.rule,
            conclusions=args.conclusion,
            severity_sources=severity_sources,
            grades=grades,
        )
    except ValueError as exc:
        print(f"[replay] 过滤条件非法：{exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        store.close()

    if not entries:
        print(
            f"[replay] 批次 {args.batch} 在状态库里没有匹配的判定行（检查批次号与过滤条件）",
            file=sys.stderr,
        )
        return EXIT_ERROR

    target = write_replay_csv(entries, args.out, batch_no=args.batch)
    counts = summarize_replay(entries)
    issues = check_evidence_chain(entries)
    health = chain_health(entries)

    if args.as_json:
        print(
            json.dumps(
                {
                    "batch_no": args.batch,
                    "replay_path": str(target),
                    "rows": len(entries),
                    "counts": counts,
                    "chain_health": health,
                    "issues": [str(issue) for issue in issues],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_OK

    print(
        f"[replay] 批次 {args.batch} 证据链回放：{len(entries)} 条 · "
        f"合规 {counts.get('合规', 0)} · 违规 {counts.get('违规', 0)} · "
        f"不可判定 {counts.get('不可判定', 0)} · 回放件 → {target}"
    )
    print(
        f"[replay] 证据链体检：链完整 {health['链完整条数']} 条 · "
        f"断链 {health['断链条数']} 条"
    )
    if severity_sources:
        print(
            f"[replay] 严重度来源与点评分级：已按规则集补出"
            f"（{len(severity_sources)} 条规则）"
        )
    for issue in issues[:10]:
        print(f"[replay] 断链：{issue}")
    if len(issues) > 10:
        print(f"[replay] ……另有 {len(issues) - 10} 条断链，详见回放件")
    return EXIT_OK


def cmd_writeback(args: argparse.Namespace) -> int:
    """``writeback`` 子命令：查看 / 重放待重放的回写队列。

    退避重试耗尽后，回写请求落进 ``pending_writeback`` 表（本命令看的就是它）。
    ``--replay`` 会把队列里的请求**按原样**再发一次；早已回写成功的结论由
    **幂等键**抑制，不会重复回写（药师端不会看到两条复核记录）。
    """
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"[writeback] 状态库 {db_path} 不存在，没有待重放的回写队列",
            file=sys.stderr,
        )
        return EXIT_ERROR

    store = Store(db_path)
    try:
        writer = None
        if args.replay:
            # 凭据缺失 → 显式失败（不静默跳过：重放要走真实出站通道）
            config = Config.from_env()
            try:
                writer = HisReviewWriter.from_config(
                    config,
                    guard=WritebackIdempotencyGuard(store),
                    ledger=store,
                )
            except HisCredentialError as exc:
                print(f"[writeback] {exc}", file=sys.stderr)
                return EXIT_ERROR

        queue = PendingWritebackQueue(
            store,
            writer=writer,
            csv_path=Path(args.out) / PENDING_WRITEBACK_FILENAME,
        )
        records = queue.records()
        counts = summarize_queue(records)
        report = queue.replay(limit=args.limit) if args.replay else None
        target = queue.export_csv()
    finally:
        store.close()

    if args.as_json:
        payload = {
            "state_db": str(db_path),
            "pending_csv": str(target),
            "counts": counts,
            "records": [record.to_dict() for record in records],
        }
        if report is not None:
            payload["replay"] = report.to_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    print(
        f"[writeback] 待重放队列：待重放 {counts.get('待重放', 0)} 条 · "
        f"已重放 {counts.get('已重放', 0)} 条 · 累计重试 {counts.get('累计重试次数', 0)} 次"
    )
    if report is not None:
        print(
            f"[writeback] 重放：尝试 {report.attempted} 条 · 已发出 {report.sent} 条 · "
            f"幂等抑制 {report.suppressed} 条 · 仍失败 {report.failed} 条"
        )
        for item in report.outcomes[:10]:
            print(
                f"[writeback] {item.status}：处方 {item.prescription_no} · "
                f"规则 {item.rule_id} · {item.detail}"
            )
        if len(report.outcomes) > 10:
            print(f"[writeback] ……另有 {len(report.outcomes) - 10} 条，详见队列文件")
    for record in records:
        if record.status.value != "待重放":
            continue
        print(
            f"[writeback] 待重放：批次 {record.batch_no} · 处方 {record.prescription_no} · "
            f"规则 {record.rule_id} · 重试 {record.retry_count} 次 · "
            f"最后错误 {record.last_error_code}"
        )
    print(f"[writeback] 队列文件 → {target}")
    return EXIT_OK


def _build_writer(store: Store, config: Config) -> HisReviewWriter:
    """按配置面构造出站回写通道（凭据缺失 → 显式失败，不静默跳过）。"""
    return HisReviewWriter.from_config(
        config,
        guard=WritebackIdempotencyGuard(store),
        ledger=store,
    )


def cmd_confirm(args: argparse.Namespace) -> int:
    """``confirm`` 子命令：药师确认 / 撤回 / 触发回写。

    **回写门禁在这里落地**：只有「药师已确认」的判定行会进回写请求体，
    「已判定」与「待补字段」永远不出现；撤回后排队中的回写作废、
    已发出的同步一条撤回通知到 HIS。
    """
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[confirm] 状态库 {db_path} 不存在，没有可确认的判定行", file=sys.stderr)
        return EXIT_ERROR

    config = Config.from_env()
    store = Store(db_path)
    try:
        # -- 出站通道：只有需要发请求时才构造（缺凭据 → 显式失败） --------
        writer = None
        needs_outbound = bool(args.trigger or args.resync)
        if needs_outbound:
            try:
                writer = _build_writer(store, config)
            except HisCredentialError as exc:
                print(f"[confirm] {exc}", file=sys.stderr)
                return EXIT_ERROR
        else:
            # 撤回一条**已经回写**的结论也需要出站通道（同步撤回通知）
            if args.withdraw and args.key:
                try:
                    row = store.get_judgement(args.key) or store.find_judgement_by_business_key(
                        args.key
                    )
                except Exception:  # pragma: no cover - 取不到行时按「无已回写记录」处理
                    row = None
                if row is not None and row.state is JudgementState.WRITTEN_BACK:
                    try:
                        writer = _build_writer(store, config)
                    except HisCredentialError as exc:
                        print(
                            f"[confirm] {exc}（该判定已回写，撤回必须同步到 HIS）",
                            file=sys.stderr,
                        )
                        return EXIT_ERROR

        queue = PendingWritebackQueue(store, writer=writer)
        service = ConfirmService(store, writer=writer, queue=queue if writer else None)

        # -- 列出 ----------------------------------------------------------
        if args.as_list:
            candidates = service.candidates(args.batch)
            records = service.confirmations()
            if args.as_json:
                print(
                    json.dumps(
                        {
                            "state_db": str(db_path),
                            "candidates": [
                                {
                                    "idempotency_key": item.idempotency_key,
                                    "batch_no": item.judgement.batch_no,
                                    "prescription_no": item.judgement.prescription_no,
                                    "rule_id": item.judgement.rule_id,
                                    "conclusion": item.judgement.conclusion.value,
                                    "pharmacist_id": item.confirmation.pharmacist_id,
                                    "confirmed_at": item.confirmation.acted_at,
                                }
                                for item in candidates
                            ],
                            "confirmations": [record.to_dict() for record in records],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return EXIT_OK
            print(f"[confirm] 待回写候选 {len(candidates)} 条（只有已确认的结论在这里）")
            for item in candidates:
                print(
                    f"[confirm] 待回写：批次 {item.judgement.batch_no} · "
                    f"处方 {item.judgement.prescription_no} · 规则 {item.judgement.rule_id} · "
                    f"结论 {item.judgement.conclusion.value} · "
                    f"确认人 {item.confirmation.pharmacist_id}"
                )
            print(f"[confirm] 确认 / 撤回留痕 {len(records)} 条")
            for record in records:
                print(
                    f"[confirm] {record.action}：处方 {record.prescription_no} · "
                    f"规则 {record.rule_id} · 药师 {record.pharmacist_id} · "
                    f"回写状态 {record.writeback_state}"
                )
            return EXIT_OK

        # -- 触发回写 ------------------------------------------------------
        if args.trigger:
            report = service.trigger_writeback(batch_no=args.batch, limit=args.limit)
            if args.as_json:
                print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
                return EXIT_OK
            print(
                f"[confirm] 回写触发：候选 {report.attempted} 条 · 已发出 {report.sent} 条 · "
                f"幂等抑制 {report.suppressed} 条 · 失败 {report.failed} 条 · "
                f"跳过 {report.skipped} 条"
            )
            for item in report.outcomes[:10]:
                print(
                    f"[confirm] {item.status}：处方 {item.prescription_no} · "
                    f"规则 {item.rule_id} · {item.detail}"
                )
            if len(report.outcomes) > 10:
                print(f"[confirm] ……另有 {len(report.outcomes) - 10} 条")
            return EXIT_OK if report.failed == 0 else EXIT_ERROR

        # -- 重投撤回通知 --------------------------------------------------
        if args.resync:
            report = service.resync_withdrawals()
            if args.as_json:
                print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
                return EXIT_OK
            print(
                f"[confirm] 撤回重投：尝试 {report.attempted} 条 · 成功 {report.sent} 条 · "
                f"失败 {report.failed} 条"
            )
            for item in report.outcomes[:10]:
                print(
                    f"[confirm] {item.status}：处方 {item.prescription_no} · "
                    f"规则 {item.rule_id} · {item.detail}"
                )
            return EXIT_OK if report.failed == 0 else EXIT_ERROR

        # -- 确认 / 撤回 ---------------------------------------------------
        if not args.key:
            print(
                "[confirm] 缺少 --key（判定幂等键或业务键）；"
                "或用 --list / --trigger / --resync 走其它动作",
                file=sys.stderr,
            )
            return EXIT_ERROR
        if not args.by:
            print("[confirm] 缺少 --by（药师标识）：系统不代签", file=sys.stderr)
            return EXIT_ERROR

        try:
            if args.withdraw:
                outcome = service.withdraw(args.key, pharmacist_id=args.by, note=args.note)
            else:
                outcome = service.confirm(args.key, pharmacist_id=args.by, note=args.note)
        except ConfirmError as exc:
            print(f"[confirm] {exc}", file=sys.stderr)
            return EXIT_ERROR

        if args.as_json:
            print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
            return EXIT_OK

        print(
            f"[confirm] {outcome.record.action}：批次 {outcome.judgement.batch_no} · "
            f"处方 {outcome.judgement.prescription_no} · 规则 {outcome.judgement.rule_id} · "
            f"药师 {outcome.record.pharmacist_id}"
        )
        print(
            f"[confirm] 状态 {outcome.state_before.value} → {outcome.state_after.value} · "
            f"回写状态 {outcome.record.writeback_state}"
        )
        if outcome.pending_withdrawn:
            print(f"[confirm] 排队中的回写已作废 {outcome.pending_withdrawn} 条（不再发出）")
        for message in outcome.alerts:
            print(f"[confirm] 告警：{message}")
        return EXIT_OK
    finally:
        store.close()


def cmd_rules_validate(args: argparse.Namespace) -> int:
    """``rules validate`` 子命令：规则行载入校验。

    **有非法规则时退出码非 0**——规则行是审方结论的唯一依据，配置写错了还
    照常跑，药师拿到的台账就不可信。每条问题打印**字段名**与**行列定位**，
    药事审核岗照着改哪一格一目了然。

    非法规则只被拒，**不影响其余规则生效**（与规则仓库的载入语义一致）。
    """
    rules_dir = Path(args.rules)
    repository = RuleRepository(rules_dir, as_of=_parse_as_of(args.as_of))
    report = repository.load()
    effective = repository.effective_rules

    payload = {
        "rules_dir": str(rules_dir),
        "as_of": repository.as_of.isoformat(),
        "files_scanned": report.files_scanned,
        "loaded": report.loaded,
        "effective": len(effective),
        "rejected": report.rejected,
        "ok": report.rejected == 0,
        "errors": [error.to_dict() for error in report.errors],
    }

    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK if report.rejected == 0 else EXIT_ERROR

    print(
        f"[rules validate] 规则目录 {rules_dir}：扫描 {report.files_scanned} 个文件 · "
        f"载入 {report.loaded} 条 · 生效 {len(effective)} 条 · 被拒 {report.rejected} 条"
    )
    for error in report.errors:
        target = f" [{error.rule_id}]" if error.rule_id else ""
        print(f"[rules validate] 被拒：{error.path}{target}")
        for issue in error.issues:
            location = ""
            if issue.line is not None:
                location = f"（第 {issue.line} 行"
                if issue.column is not None:
                    location += f" 第 {issue.column} 列"
                location += "）"
            field = issue.field if issue.field else "<规则行>"
            print(f"[rules validate]   · 字段 {field}：{issue.message}{location}")
    if report.rejected:
        print(
            f"[rules validate] 校验未通过：{report.rejected} 条规则被拒"
            "（其余规则照常生效；被拒的规则不会出现在判定台账里）",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print("[rules validate] 全部规则行校验通过")
    return EXIT_OK


def _read_prescription_rows(inbox: str | Path) -> list[PrescriptionRow]:
    """读落地目录里的 CSV / HL7 批次文件，返回全部处方行。

    读不出来就**显式失败**（返回 ``None`` 由调用方处理）——体检算覆盖率要
    用「适用处方数」当分母，处方行读不全会让覆盖率看起来很好，实际是错的。
    """
    rows: list[PrescriptionRow] = []
    directory = Path(inbox)
    if not directory.is_dir():
        return rows
    for path in list_csv_files(directory) + list_hl7_files(directory):
        if path.suffix.lower() in HL7_SUFFIXES:
            batch = read_order_rows(path)
            rows.extend(batch.rows)
        else:
            rows.extend(read_prescription_rows(path))
    return rows


def cmd_rules_health(args: argparse.Namespace) -> int:
    """``rules health`` 子命令：规则集体检（僵尸 / 恒不可判定 / 覆盖率）。

    覆盖率的分母是**适用处方数**（这条规则本批该管多少张处方），不是批次里的
    全部处方数——量限规则只管麻醉药品处方，拿全院处方当分母会让覆盖率永远是个
    位数，看着像规则没用，其实是口径错了。

    判定台账从 ``--db`` 读（状态库），适用面从 ``--inbox`` 读（落地文件）；
    两者都不给就无从体检，直接报错而不是给一张全是「僵尸规则」的表。
    """
    rules_dir = Path(args.rules)
    repository = RuleRepository(rules_dir)
    repository.load()
    rules = repository.effective_rules
    if not rules:
        print(
            f"[rules health] 规则目录 {rules_dir} 里没有生效规则，体检终止",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if not args.db and not args.inbox:
        print(
            "[rules health] 至少要给 --db（读判定台账）或 --inbox（读适用处方），"
            "否则无从体检",
            file=sys.stderr,
        )
        return EXIT_ERROR

    judgements: list[JudgementRow] = []
    if args.db:
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"[rules health] 状态库 {db_path} 不存在", file=sys.stderr)
            return EXIT_ERROR
        store = Store(db_path)
        try:
            judgements = store.list_judgements()
        finally:
            store.close()

    prescriptions: list[PrescriptionRow] = []
    if args.inbox:
        try:
            prescriptions = _read_prescription_rows(args.inbox)
        except (CsvReadError, Hl7ParseError) as exc:
            print(f"[rules health] 落地文件解析失败：{exc}", file=sys.stderr)
            return EXIT_ERROR

    items = build_rule_health(
        rules,
        judgements,
        prescriptions=prescriptions if prescriptions else None,
    )
    summary = summarize_rule_health(items)
    target = write_rule_health(items, args.out)

    if args.as_json:
        print(
            json.dumps(
                {
                    "rules_dir": str(rules_dir),
                    "state_db": str(args.db) if args.db else "",
                    "inbox": str(args.inbox) if args.inbox else "",
                    "rule_health_path": str(target),
                    "summary": summary,
                    "items": [item.to_record() for item in items],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_OK

    print(
        f"[rules health] 规则体检：{summary['规则总数']} 条规则 · "
        f"整体覆盖率 {summary['整体覆盖率']}% · "
        f"僵尸规则 {summary['僵尸规则数']} 条 · "
        f"恒不可判定 {summary['恒不可判定规则数']} 条 · "
        f"不在生效集 {summary['不在生效集规则数']} 条"
    )
    for item in items:
        if item.health in ("正常",):
            continue
        print(
            f"[rules health] {item.health}：{item.rule_id} · "
            f"适用 {item.applicable_prescriptions} 张 · "
            f"判出结论 {item.hit_prescriptions} 张 · "
            f"覆盖率 {item.coverage_pct}% · {item.reason}"
        )
    print(f"[rules health] 规则体检表 → {target}")
    return EXIT_OK


def cmd_ledger_export(args: argparse.Namespace) -> int:
    """``ledger export`` 子命令：从状态库导出判定台账。

    药师逐批复核用**判定台账**（逐处方 × 逐规则），信息科排接口改造用
    **不可判定台账 + 字段补齐顺序表**。给了 ``--inbox`` 就把三张表一起导；
    没给就只导判定台账（此时不可判定项的成因判不出来）。
    """
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[ledger export] 状态库 {db_path} 不存在，没有可导出的判定行", file=sys.stderr)
        return EXIT_ERROR

    wanted = _parse_conclusion_filter(args.conclusion)
    if wanted is None:
        return EXIT_ERROR

    store = Store(db_path)
    try:
        rows = store.list_judgements(args.batch)
    finally:
        store.close()

    if wanted:
        rows = [row for row in rows if row.conclusion.value in wanted]
    if not rows:
        print(
            "[ledger export] 没有匹配的判定行（检查批次号与结论过滤条件）",
            file=sys.stderr,
        )
        return EXIT_ERROR

    out_dir = Path(args.out)
    ledger_path = write_judgement_ledger(rows, out_dir, filename=LEDGER_FILENAME)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.conclusion.value] = counts.get(row.conclusion.value, 0) + 1

    gap_path = None
    priority_path = None
    gap_entries: list = []
    priority_items: list = []
    if args.inbox:
        try:
            prescriptions = _read_prescription_rows(args.inbox)
        except (CsvReadError, Hl7ParseError) as exc:
            print(f"[ledger export] 落地文件解析失败：{exc}", file=sys.stderr)
            return EXIT_ERROR
        prescription_index = index_prescriptions(prescriptions)
        gap_entries = build_gap_entries(rows, prescriptions=prescription_index)
        gap_path = write_undeterminable_ledger(gap_entries, out_dir)
        priority_items = build_priority_table(gap_entries)
        priority_path = write_priority_table(priority_items, out_dir)

    if args.as_json:
        payload: dict[str, object] = {
            "state_db": str(db_path),
            "ledger_path": str(ledger_path),
            "rows": len(rows),
            "counts": counts,
        }
        if args.batch:
            payload["batch_no"] = args.batch
        if gap_path is not None:
            payload["gap_ledger_path"] = str(gap_path)
            payload["gap_entries"] = len(gap_entries)
            payload["field_gap_priority_path"] = str(priority_path)
            payload["field_gap_priority"] = [item.to_record() for item in priority_items]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    print(
        f"[ledger export] 判定台账：{len(rows)} 条 · "
        f"合规 {counts.get(ConclusionKeys.compliant, 0)} · "
        f"违规 {counts.get(ConclusionKeys.violation, 0)} · "
        f"不可判定 {counts.get(ConclusionKeys.undeterminable, 0)} · "
        f"判定台账 → {ledger_path}"
    )
    if gap_path is not None:
        print(
            f"[ledger export] 不可判定台账 → {gap_path}"
            f"（{len(gap_entries)} 组字段缺口）"
        )
        print(
            f"[ledger export] 字段补齐顺序表 → {priority_path}"
            f"（{len(priority_items)} 个字段）"
        )
    else:
        print(
            "[ledger export] 未给 --inbox：不可判定台账与字段补齐顺序表未导出"
            "（成因要靠落地文件才判得出来）"
        )
    return EXIT_OK


def cmd_audit_export(args: argparse.Namespace) -> int:
    """``audit export`` 子命令：导出审计流水。

    审计流水 = **判定事件**（审了什么）+ **出站调用事件**（发出去什么、
    成没成）合成一张表。缺依据出处（``basis`` 为空）时显式报错且**不落盘**——
    不导出一份看着正常、实则漏了依据的表。
    """
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[audit export] 状态库 {db_path} 不存在，没有可导出的审计流水", file=sys.stderr)
        return EXIT_ERROR

    store = Store(db_path)
    try:
        expected = store.count_judgements(args.batch)
        records = build_audit_records(store, args.batch)
        try:
            check = assert_audit_invariant(records, expected)
        except AuditExportError as exc:
            print(f"[audit export] {exc}", file=sys.stderr)
            return EXIT_ERROR
        target = write_audit_ledger(records, Path(args.out))
        counts = summarize_audit(records)
    finally:
        store.close()

    if args.as_json:
        print(
            json.dumps(
                {
                    "state_db": str(db_path),
                    "audit_ledger_path": str(target),
                    "rows": len(records),
                    "counts": counts,
                    "check": check.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_OK

    print(
        f"[audit export] 审计流水：判定事件 {check.judgement_events} 条 · "
        f"出站调用 {check.outbound_events} 条 · "
        f"依据缺失 {check.basis_missing} 条 · 审计台账 → {target}"
    )
    if not check.judgement_rows_match:
        print(
            f"[audit export] 判定事件 {check.judgement_events} 条 ≠ 判定行 "
            f"{check.expected_judgements} 条：重复投递与重判只补审计不新增判定，"
            "属正常现象"
        )
    return EXIT_OK


def cmd_batch_replay(args: argparse.Namespace) -> int:
    """``batch replay`` 子命令：批次重放（与顶层 ``replay`` 同一实现）。"""
    return cmd_replay(args)


def cmd_revoke(args: argparse.Namespace) -> int:
    """``revoke`` 子命令：药师撤回一条已确认的结论。

    与 ``confirm --withdraw`` 同一实现——顶层 ``confirm`` 保留 ``--withdraw``
    开关是为了脚本里一条命令走两条路；``revoke`` 是给「撤回」单独开一条入口，
    免得科员记错开关方向把撤回做成确认。
    """
    args.withdraw = True
    return cmd_confirm(args)


def _parse_conclusion_filter(values: list[str] | None) -> set[str] | None:
    """解析 ``--conclusion`` 过滤值；取值非法时返回 ``None`` 并报错。

    返回空集合表示「不过滤」。
    """
    if not values:
        return set()
    allowed = {
        Conclusion.COMPLIANT.value,
        Conclusion.VIOLATION.value,
        Conclusion.UNDETERMINABLE.value,
    }
    unknown = [value for value in values if value not in allowed]
    if unknown:
        print(
            f"[ledger export] 结论过滤取值非法：{'、'.join(unknown)}"
            f"（合法取值：{'、'.join(sorted(allowed))}）",
            file=sys.stderr,
        )
        return None
    return set(values)


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "judge":
        return cmd_judge(args)
    if args.command == "scan":
        return cmd_scan(args)
    if args.command == "state":
        return cmd_state(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "replay":
        return cmd_replay(args)
    if args.command == "writeback":
        return cmd_writeback(args)
    if args.command == "confirm":
        return cmd_confirm(args)
    if args.command == "revoke":
        return cmd_revoke(args)
    # 规则行 / 台账 / 审计 / 批次这四组是「两层子命令」（rules validate 这种）
    if args.command in ("rules", "rule"):
        if args.rules_command == "validate":
            return cmd_rules_validate(args)
        if args.rules_command == "health":
            return cmd_rules_health(args)
    if args.command == "ledger" and args.ledger_command == "export":
        return cmd_ledger_export(args)
    if args.command == "audit" and args.audit_command == "export":
        return cmd_audit_export(args)
    if args.command == "batch" and args.batch_command == "replay":
        return cmd_batch_replay(args)
    parser.error(f"未知子命令：{args.command}")
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
