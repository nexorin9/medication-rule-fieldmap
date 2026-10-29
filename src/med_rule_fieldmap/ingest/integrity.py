"""入站文件完整性校验：**先校验、后切批次**。

**对应源产品能力**：源仓库 ``backend/src/advocate/connection.py`` 的
``validating_create_connection()`` —— 出站请求**先校验目标地址**，校验不过就
显式抛错，绝不带着不合法地址去连；配套的 ``attempt_timeout(timeout, deadline)``
把单次连接的预算压到「剩余 deadline」而不是一个固定超时。

**改写**：本模块把「先校验、再动作」从**出站连接**搬到**入站文件**——
落地文件必须先过完整性校验（可解码 / 有表头 / 未截断 / 必需列齐备），
校验不过就**整文件挂起**（挂起台账 + 告警），**绝不部分判定**。

**为什么整文件挂起**：半份数据会让药师拿到一份看起来正常、实则漏判的台账，
而漏判在审方场景里比「明确判不了」危险得多。挂起不是丢数据——文件留在
``inbox/`` 原地，挂起台账记下原因与缺列清单，人工解除后重投即可。

**校验口径**

============  ==========================================================
问题分类      判据
============  ==========================================================
空文件        文件字节全空或全是空白
编码不可识别  UTF-8（含 BOM）与 GB18030 都解不开，且不是尾部多字节截断
缺少表头      解开了但没有表头行（CSV）/ 没有 MSH 段（HL7）
文件截断      最后一行字段数少于表头（CSV）/ 末段字段数少于同段兄弟（HL7）
缺少必需列    CSV 表头缺 ``REQUIRED_COLUMNS`` 中任意一列
============  ==========================================================

**注意**：``rx_type`` / ``drug_category`` / ``days_qty`` / ``skin_test_result``
这四个 ★ 常缺字段**不在必需列里**——它们缺了是「判不了」的来源，由判定层落
第三态「不可判定」并点名字段，而不是把整份文件挂起（口径同
``ingest/csv_reader.py``）。
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .csv_reader import REQUIRED_COLUMNS, build_column_map

logger = logging.getLogger("med_rule_fieldmap.ingest")

# ---------------------------------------------------------------------------
# 问题分类（口径唯一真源）
# ---------------------------------------------------------------------------

#: 校验通过
KIND_OK = "通过"
#: 文件为空
KIND_EMPTY_FILE = "空文件"
#: 编码不可识别（既不是 UTF-8 也不是 GBK）
KIND_UNDECODABLE = "编码不可识别"
#: 缺少表头（CSV 无表头行 / HL7 无 MSH 段）
KIND_MISSING_HEADER = "缺少表头"
#: 文件截断（写入未完成）
KIND_TRUNCATED = "文件截断"
#: 缺少必需列
KIND_MISSING_COLUMNS = "缺少必需列"

#: 判定为「尾部多字节被截断」的最小字节数：太短的片段无法区分「乱码」与「截断」。
_TRUNCATED_TAIL_MIN_BYTES = 8
#: 尾部多字节字符的最大长度（UTF-8 最多 4 字节，GB18030 最多 4 字节）。
_TRUNCATED_TAIL_MAX_BYTES = 4


@dataclass(frozen=True)
class IntegrityReport:
    """一份落地文件的完整性结论。

    ``ok`` 为真时该文件可以切分成批次；为假时**整文件挂起**，
    ``kind`` / ``reason`` 给出挂起原因，``missing_columns`` 给出缺列清单
    （信息科据此排接口补齐顺序）。
    """

    path: Path
    kind: str = KIND_OK
    reason: str = ""
    missing_columns: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """是否通过完整性校验。"""
        return self.kind == KIND_OK

    def summary(self) -> str:
        """一句话结论（挂起台账与告警日志共用）。"""
        if self.ok:
            return f"{self.path.name} 完整性校验通过"
        return f"{self.path.name} 挂起：{self.kind} · {self.reason}"


@dataclass(frozen=True)
class _Decoded:
    """解码结果；``kind != KIND_OK`` 时 ``text`` 不可用。"""

    text: str
    kind: str = KIND_OK
    reason: str = ""


def _decode_for_integrity(raw: bytes, *, source: str = "") -> _Decoded:
    """按入站口径解码文件字节，并区分「乱码」与「尾部截断」。

    * UTF-8（含 BOM）优先，GB18030 兜底——与 ``ingest/csv_reader.py`` 同口径；
    * UTF-8 解码失败且**错误点在文件尾部** → 判为**文件截断**（多字节字符被
      写了一半），不是乱码；
    * 两种编码都解不开 → 判为**编码不可识别**。
    """
    if not raw.strip():
        return _Decoded("", KIND_EMPTY_FILE, f"文件 {source} 是空的（没有任何内容）")

    utf8_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return _Decoded(raw.decode(encoding))
        except UnicodeDecodeError as exc:
            utf8_error = exc
            if (
                len(raw) >= _TRUNCATED_TAIL_MIN_BYTES
                and exc.start >= len(raw) - _TRUNCATED_TAIL_MAX_BYTES
            ):
                return _Decoded(
                    "",
                    KIND_TRUNCATED,
                    f"文件 {source} 末尾的多字节字符被写了一半（{exc}），写入未完成",
                )

    try:
        return _Decoded(raw.decode("gb18030"))
    except UnicodeDecodeError:
        pass

    detail = f"（{utf8_error}）" if utf8_error is not None else ""
    return _Decoded(
        "",
        KIND_UNDECODABLE,
        f"文件 {source} 既不是 UTF-8 也不是 GBK 文本{detail}",
    )


def _split_csv_records(text: str) -> tuple[list[str], list[str]]:
    """把 CSV 文本拆成「表头 + 数据行」。

    返回 ``(header, records)``；``records`` 里是**非空**行（保留原始字段数，
    字段数不足的行也要留着——那正是截断的信号）。
    """
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = list(reader)
    if not rows:
        return [], []
    header = [str(name) for name in rows[0]]
    records = []
    for record in rows[1:]:
        if not record or all(not cell.strip() for cell in record):
            continue  # 全空行跳过
        records.append(record)
    return header, records


def check_csv_integrity(path: str | Path) -> IntegrityReport:
    """校验一份 CSV 医嘱落地文件的完整性。

    判据顺序：空文件 → 编码 → 表头 → 必需列 → 截断。
    必需列缺失与文件截断是**两种不同的挂起原因**：前者要信息科改接口，
    后者要 HIS 重新导出，两者的处置动作不同，不能混成一个提示。
    """
    file_path = Path(path)
    raw = file_path.read_bytes()
    decoded = _decode_for_integrity(raw, source=file_path.name)
    if decoded.kind != KIND_OK:
        return IntegrityReport(file_path, decoded.kind, decoded.reason)

    try:
        header, records = _split_csv_records(decoded.text)
    except csv.Error as exc:
        return IntegrityReport(
            file_path,
            KIND_TRUNCATED,
            f"CSV 解析中断，疑似文件截断：{exc}",
        )

    if not header:
        return IntegrityReport(
            file_path, KIND_MISSING_HEADER, f"文件 {file_path.name} 没有表头行"
        )

    columns = build_column_map(header)
    missing = tuple(name for name in REQUIRED_COLUMNS if name not in columns)
    if missing:
        return IntegrityReport(
            file_path,
            KIND_MISSING_COLUMNS,
            f"文件 {file_path.name} 缺少必需列：{'、'.join(missing)}；"
            "整文件挂起，不做部分判定",
            missing_columns=missing,
        )

    if records:
        last = records[-1]
        if len(last) < len(header) and not decoded.text.endswith(("\n", "\r")):
            return IntegrityReport(
                file_path,
                KIND_TRUNCATED,
                f"文件 {file_path.name} 末行只有 {len(last)} 列（表头 {len(header)} 列）"
                "且行尾未闭合，写入未完成，整文件挂起",
            )

    return IntegrityReport(file_path, KIND_OK)


def check_hl7_integrity(path: str | Path) -> IntegrityReport:
    """校验一份 HL7 v2 医嘱落地文件的完整性。

    * 没有 ``MSH`` 段 → 缺少表头（文件不是 HL7 消息，或开头就被截断）；
    * 末段没有以段分隔符 ``\\r`` 收尾，且末段字段数少于同一消息里**同段名**的
      兄弟段 → 文件截断（写入未完成）。

    同段名兄弟段的比较是 HL7 侧的等价口径：CSV 看「末行字段数 vs 表头列数」，
    HL7 看「末段字段数 vs 同段兄弟段字段数」。
    """
    file_path = Path(path)
    raw = file_path.read_bytes()
    decoded = _decode_for_integrity(raw, source=file_path.name)
    if decoded.kind != KIND_OK:
        return IntegrityReport(file_path, decoded.kind, decoded.reason)

    text = decoded.text
    segments = [seg for seg in text.split("\r") if seg.strip()]
    if not segments:
        return IntegrityReport(
            file_path, KIND_MISSING_HEADER, f"文件 {file_path.name} 没有任何 HL7 段"
        )

    first_field = segments[0].split("|", 1)[0].strip()
    if first_field != "MSH":
        return IntegrityReport(
            file_path,
            KIND_MISSING_HEADER,
            f"文件 {file_path.name} 首个段是 {first_field or '<空>'} 而不是 MSH，"
            "文件开头即不完整",
        )

    warnings: list[str] = []
    if not text.endswith("\r"):
        tail = segments[-1]
        siblings = [
            seg
            for seg in segments[:-1]
            if seg.split("|", 1)[0].strip() == tail.split("|", 1)[0].strip()
        ]
        if "|" not in tail:
            reason = (
                f"文件 {file_path.name} 末段 {tail!r} 没有字段分隔符且未以段分隔符收尾，"
                "写入未完成，整文件挂起"
            )
            return IntegrityReport(file_path, KIND_TRUNCATED, reason)
        if siblings and len(tail.split("|")) < max(len(s.split("|")) for s in siblings):
            reason = (
                f"文件 {file_path.name} 末段字段数少于同段兄弟段（{len(tail.split('|'))} < "
                f"{max(len(s.split('|')) for s in siblings)}）且未以段分隔符收尾，"
                "写入未完成，整文件挂起"
            )
            return IntegrityReport(file_path, KIND_TRUNCATED, reason)
        warnings.append(
            f"文件 {file_path.name} 末段未以段分隔符收尾，按完整段处理"
        )

    return IntegrityReport(file_path, KIND_OK, warnings=tuple(warnings))


def check_file_integrity(path: str | Path) -> IntegrityReport:
    """按后缀分派到 CSV / HL7 的完整性校验。

    未知后缀按 CSV 处理（落地目录里的批次文件以 CSV 为主格式，spec.md §2.1）。
    """
    file_path = Path(path)
    if file_path.suffix.lower() == ".hl7":
        return check_hl7_integrity(file_path)
    return check_csv_integrity(file_path)


__all__ = [
    "KIND_EMPTY_FILE",
    "KIND_MISSING_COLUMNS",
    "KIND_MISSING_HEADER",
    "KIND_OK",
    "KIND_TRUNCATED",
    "KIND_UNDECODABLE",
    "IntegrityReport",
    "check_csv_integrity",
    "check_file_integrity",
    "check_hl7_integrity",
]
