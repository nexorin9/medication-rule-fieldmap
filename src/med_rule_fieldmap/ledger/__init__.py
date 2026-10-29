"""台账层：把判定结论落成可交付文件。

已落地：

* 判定台账 ``out/judgement_ledger.csv``（逐处方 × 逐规则的三态结论）；
* 不可判定台账 ``out/undeterminable_ledger.csv``（按缺失字段 × 规则聚合，
  分清「接口未回传」与「医嘱未填写」两种成因）；
* 判定证据链回放 ``out/replay_<batch_no>.csv``（每条结论可追到规则版本与依据）；
* 字段补齐顺序表 ``out/field_gap_priority.csv``（按「补一个字段能救多少张
  处方」排序，给信息科排接口改造顺序）；
* 规则体检表 ``out/rule_health.csv``（僵尸规则 / 恒不可判定 / 规则覆盖率，
  给药事审核岗做月度规则体检）。

审计导出在后续台账任务补齐。
"""

from .evidence_replay import (
    REPLAY_COLUMNS,
    REPLAY_FILENAME_PREFIX,
    EvidenceIssue,
    ReplayEntry,
    ReplayError,
    build_replay_entries,
    chain_health,
    check_evidence_chain,
    load_replay_csv,
    replay_filename,
    replay_from_store,
    summarize_replay,
    write_replay_csv,
)
from .gap import (
    GAP_COLUMNS,
    UNDETERMINABLE_FILENAME,
    FieldGap,
    GapCause,
    GapEntry,
    build_gap_entries,
    index_prescriptions,
    load_undeterminable_ledger,
    summarize_by_field,
    write_undeterminable_ledger,
)
from .ledger import (
    LEDGER_COLUMNS,
    LEDGER_FILENAME,
    MISSING_FIELDS_SEPARATOR,
    load_judgement_ledger,
    write_judgement_ledger,
)
from .priority import (
    PRIORITY_COLUMNS,
    PRIORITY_FILENAME,
    ROUTE_BOTH,
    ROUTE_INTERFACE,
    ROUTE_PHYSICIAN,
    ROUTE_UNKNOWN,
    FieldPriority,
    build_priority_table,
    load_priority_table,
    select_interface_gaps,
    write_priority_table,
)
from .rule_health import (
    HEALTH_ALWAYS_UNDETERMINABLE,
    HEALTH_INCONSISTENT,
    HEALTH_NORMAL,
    HEALTH_PARTIAL,
    HEALTH_RETIRED,
    HEALTH_ZOMBIE,
    RULE_HEALTH_COLUMNS,
    RULE_HEALTH_FILENAME,
    RuleHealth,
    build_rule_health,
    load_rule_health,
    summarize_rule_health,
    write_rule_health,
)

__all__ = [
    "GAP_COLUMNS",
    "HEALTH_ALWAYS_UNDETERMINABLE",
    "HEALTH_INCONSISTENT",
    "HEALTH_NORMAL",
    "HEALTH_PARTIAL",
    "HEALTH_RETIRED",
    "HEALTH_ZOMBIE",
    "LEDGER_COLUMNS",
    "LEDGER_FILENAME",
    "MISSING_FIELDS_SEPARATOR",
    "PRIORITY_COLUMNS",
    "PRIORITY_FILENAME",
    "REPLAY_COLUMNS",
    "REPLAY_FILENAME_PREFIX",
    "ROUTE_BOTH",
    "ROUTE_INTERFACE",
    "ROUTE_PHYSICIAN",
    "ROUTE_UNKNOWN",
    "RULE_HEALTH_COLUMNS",
    "RULE_HEALTH_FILENAME",
    "UNDETERMINABLE_FILENAME",
    "EvidenceIssue",
    "FieldGap",
    "FieldPriority",
    "GapCause",
    "GapEntry",
    "ReplayEntry",
    "ReplayError",
    "RuleHealth",
    "build_gap_entries",
    "build_priority_table",
    "build_replay_entries",
    "build_rule_health",
    "chain_health",
    "check_evidence_chain",
    "index_prescriptions",
    "load_judgement_ledger",
    "load_priority_table",
    "load_replay_csv",
    "load_rule_health",
    "load_undeterminable_ledger",
    "replay_filename",
    "replay_from_store",
    "select_interface_gaps",
    "summarize_by_field",
    "summarize_replay",
    "summarize_rule_health",
    "write_judgement_ledger",
    "write_priority_table",
    "write_replay_csv",
    "write_rule_health",
    "write_undeterminable_ledger",
]
