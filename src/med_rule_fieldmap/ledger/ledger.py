"""判定台账：逐处方 × 逐规则的三态结论落盘。

**对应源产品能力**：源仓库 ``contrib/database/webhooks/models.py`` 的 ``TableWebhookCall``
（请求 / 响应副本留痕，可导出复核）与 ``core/workflow_actions/handler.py`` 的
``create_workflow_action()``（动作实例按序创建、每条留一条记录）。

**改写**：源对象的"出站调用留痕"改写成"审方判定留痕"——本文件落的是
**逐处方 × 逐规则**的结论副本，药师按这张表逐批复核，医务质控按这张表抽查。

**文件口径**

* 列固定为 :data:`LEDGER_COLUMNS`，顺序不变（下游脚本按列名取值）。
* 编码用 ``utf-8-sig``：Excel 直接双击打开不乱码，pandas 读 ``utf-8-sig`` 亦可。
* ``missing_fields`` 用 ``|`` 分隔多个字段名（不用逗号，避免与 CSV 分隔符混淆）。
* 落盘前按「批次号 → 处方号 → 规则号 → 规则版本」稳定排序：台账与输入顺序
  无关，同一批数据重跑产出逐字相同的文件。

**不可判定项的聚合不在本文件**：本文件只落**逐处方 × 逐规则**的原始结论，
「按缺失字段聚合」是 ``ledger/gap.py`` 的事（产出不可判定台账与字段补齐
顺序表）。两者的分工：台账是药师逐批复核的底稿，聚合表是信息科排接口改造
顺序的清单。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from pathlib import Path

from ..models import JudgementRow

#: 判定台账列（顺序固定）
LEDGER_COLUMNS: tuple[str, ...] = (
    "batch_no",
    "prescription_no",
    "rule_id",
    "rule_version",
    "conclusion",
    "severity",
    "reason",
    "basis",
    "missing_fields",
)

#: 台账文件名
LEDGER_FILENAME = "judgement_ledger.csv"

#: 多字段名分隔符
MISSING_FIELDS_SEPARATOR = "|"


def _row_to_record(row: JudgementRow) -> dict[str, str]:
    """判定行 → 台账行（字符串口径）。"""
    return {
        "batch_no": row.batch_no,
        "prescription_no": row.prescription_no,
        "rule_id": row.rule_id,
        "rule_version": row.rule_version,
        "conclusion": row.conclusion.value,
        "severity": row.severity or "",
        "reason": row.reason,
        "basis": row.basis,
        "missing_fields": MISSING_FIELDS_SEPARATOR.join(row.missing_fields),
    }


def write_judgement_ledger(
    rows: Iterable[JudgementRow],
    out_dir: str | Path,
    *,
    filename: str = LEDGER_FILENAME,
    sort: bool = True,
) -> Path:
    """把**全部**判定结论写成判定台账 CSV，返回落盘路径。

    三态结论（合规 / 违规 / 不可判定）**一条不落**地进台账——药师逐批复核
    靠的就是这张全量表，缺一条就等于漏审一张处方。

    ``out_dir`` 不存在时自动创建；已有文件按批次覆盖写（同一批次重跑得到同一份台账）。

    ``sort=True``（默认）时按「批次号 → 处方号 → 规则号 → 规则版本」稳定排序，
    使台账与输入顺序无关：同一批数据换个投递顺序重跑，产出的文件逐字相同，
    科室才能逐批对账。
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    materialised = list(rows)
    if sort:
        materialised.sort(
            key=lambda row: (
                row.batch_no,
                row.prescription_no,
                row.rule_id,
                row.rule_version,
            )
        )
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(LEDGER_COLUMNS))
        writer.writeheader()
        for row in materialised:
            writer.writerow(_row_to_record(row))
    return target


def load_judgement_ledger(path: str | Path) -> list[dict[str, str]]:
    """读回判定台账（供回归与下游台账聚合使用）。"""
    target = Path(path)
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(record) for record in csv.DictReader(handle)]


def count_by_conclusion(records: Sequence[dict[str, str]]) -> dict[str, int]:
    """按结论统计台账行数。"""
    counts: dict[str, int] = {}
    for record in records:
        conclusion = record.get("conclusion", "")
        counts[conclusion] = counts.get(conclusion, 0) + 1
    return counts


__all__ = [
    "LEDGER_COLUMNS",
    "LEDGER_FILENAME",
    "MISSING_FIELDS_SEPARATOR",
    "count_by_conclusion",
    "load_judgement_ledger",
    "write_judgement_ledger",
]
