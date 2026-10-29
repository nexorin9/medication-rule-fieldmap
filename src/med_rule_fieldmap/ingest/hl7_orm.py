"""入站 HL7 v2 ``ORM^O01`` 解析：把 HIS 落地的 HL7 医嘱消息读成 :class:`PrescriptionRow`。

**字段口径**：spec.md §2.1 B 段 —— 医嘱落地目录里的 HL7 v2 医嘱消息，取
``MSH-9``（消息类型）、``MSH-10``（消息控制号 → ``batch_no``）、``PID-3``（患者标识）、
``PV1-1``（就诊号）、``PV1-7``（主治医生）、``ORC-2``（医嘱号）、``ORC-7``（开立时间）、
``ORC-9``（开立医师）、``OMG-4``（医嘱描述）、``RXO-1/2/4/11``（数量 / 给药途径 / 剂量 /
单位）、``RXE-4``（药品名称）、``RXE-10/11``（给药剂量 / 单位），映射到与 CSV
**等价**的处方行字段。

**对应源产品能力**：源仓库 ``integrations/zapier/index.js`` 的三段式对外接口 ——
``triggers``（事件到达即处理）、``searches``（读侧取数处理器）、``creates``（出站写入）。
**改写**：入站触发源从「行数据库变更」换成 HIS 医嘱落地目录里的 HL7 消息文件；读侧
取数从「按 key 注册的 search 处理器」换成「按段名 + 字段号取值的 HL7 解析器」。
两种入站格式（CSV / HL7）产出**同一个** :class:`PrescriptionRow` 对象，判定链只有一套口径。

**本轮落地四件事**

1. **HL7 v2 基础解析**：段分隔回车、字段分隔竖线、组件分隔 ``^``、重复符 ``~``、
   子组件分隔 ``&``；编码字符以消息自带的 ``MSH-2`` 为准（默认 ``^~\\&``）；
2. **段字段 → 口径字段映射**：映射表 :data:`HL7_FIELD_MAP` 是**唯一真源**，
   院内接口口径不同只改这张表一行，不改解析器；
3. **缺项置空**：HL7 侧字段缺项与 CSV 等价 —— **置空而非报错**，由后续判定层归入
   第三态「不可判定」并点名缺失字段；
4. **非医嘱消息显式跳过并告警**：消息类型不是 ``ORM^O01`` 的（结果、检验、ADT 等）
   一律跳过，记入 ``skipped`` 并打告警日志，不静默丢弃。

**已知口径缺口（落字段缺口台账，不在本轮猜测补齐）**

本项目的 HL7 落地口径（spec.md §2.1 B）**不携带** ``prescription_no`` /
``drug_code`` / ``rx_type`` / ``drug_category`` / ``days_qty`` / ``skin_test_result`` /
``dept_code`` / ``dept_name`` / ``clinical_diag`` / ``age_value`` / ``spec`` /
``dosage_form`` / ``freq``。这些字段在 HL7 行上**一律置空**，由判定层落「不可判定」
并点名，字段缺口台账（``ledger/gap.py``）按缺失字段聚合后交给信息科排接口补齐顺序
—— 这正是本产品「把审不了变成可交付结论」的落点，**不在入站层编造数据**。

**衔接提醒**：``prescription_no`` 置空时，判定层回落用 ``order_no``（见
``engine/judge.py``）。同一条 HL7 消息若含多条 ``RXE`` 明细（一个医嘱组多味药），
这些明细共用同一个 ``ORC-2``，业务键会撞键；幂等键的实现（``store.py`` /
``outbound/idempotency.py``）需并入明细序号，或要求院内接口补处方号。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..models import PRESCRIPTION_FIELDS, PrescriptionRow
from .csv_reader import CsvReadError, decode_bytes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 口径常量
# ---------------------------------------------------------------------------

#: HL7 落地文件后缀。
HL7_SUFFIXES: tuple[str, ...] = (".hl7",)

#: 期望的医嘱消息类型（``MSH-9`` 的消息码 / 触发事件）。
ORDER_MESSAGE_CODE = "ORM"
ORDER_TRIGGER_EVENT = "O01"

#: 字段分隔符（竖线）。
FIELD_SEPARATOR = "|"

#: 默认编码字符（``MSH-2``）；实际以消息自带的 ``MSH-2`` 为准。
DEFAULT_ENCODING_CHARACTERS = "^~\\&"

#: 段分隔符：HL7 v2 用回车（CR）分隔段，落盘文件常被转成换行（LF）。
_SEGMENT_SPLIT = re.compile(r"[\r\n]+")

#: 时间戳尾部时区偏移（``+0800`` / ``-0500``）。
_OFFSET_PATTERN = re.compile(r"([+-]\d{4})$")

#: ``MSH`` 段字段号与分段后下标的偏移：``MSH-2`` 落在下标 0。
_MSH_FIELD_OFFSET = 2


@dataclass(frozen=True)
class EncodingCharacters:
    """HL7 编码字符（``MSH-2`` 声明）。"""

    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"

    @classmethod
    def parse(cls, text: str) -> "EncodingCharacters":
        """从 ``MSH-2`` 原文解析编码字符；长度不足时逐位补默认值。"""
        body = text or ""
        defaults = cls()
        return cls(
            component=body[0] if len(body) > 0 else defaults.component,
            repetition=body[1] if len(body) > 1 else defaults.repetition,
            escape=body[2] if len(body) > 2 else defaults.escape,
            subcomponent=body[3] if len(body) > 3 else defaults.subcomponent,
        )


@dataclass(frozen=True)
class Hl7Segment:
    """一个 HL7 段。

    ``fields[0]`` 是该段的**第 1 个数据字段**；``MSH`` 段例外 —— 它的 ``fields[0]``
    是 ``MSH-2``（编码字符声明），因此 ``MSH`` 的字段号下标偏移与其它段不同。
    """

    name: str
    fields: tuple[str, ...] = ()

    def raw(self, number: int) -> str:
        """按字段号取原始文本（未拆重复、未拆组件）；越界返回空串。"""
        index = number - _MSH_FIELD_OFFSET if self.name == "MSH" else number - 1
        if index < 0 or index >= len(self.fields):
            return ""
        return self.fields[index]


@dataclass(frozen=True)
class Hl7Message:
    """一条 HL7 消息（段序列 + 编码字符）。"""

    segments: tuple[Hl7Segment, ...] = ()
    encoding: EncodingCharacters = field(default_factory=EncodingCharacters)

    def first(self, name: str) -> Hl7Segment | None:
        """取第一个同名段；没有返回 ``None``。"""
        for segment in self.segments:
            if segment.name == name:
                return segment
        return None

    def find_all(self, name: str) -> list[Hl7Segment]:
        """取全部同名段（按出现顺序）。"""
        return [segment for segment in self.segments if segment.name == name]

    def field(self, segment: Hl7Segment, number: int) -> str:
        """字段原文（只取第一个重复，去首尾空白）。"""
        raw = segment.raw(number)
        if not raw:
            return ""
        return raw.split(self.encoding.repetition)[0].strip()

    def components(self, segment: Hl7Segment, number: int) -> list[str]:
        """字段的组件列表（第一个重复按组件分隔符拆开）。"""
        raw = self.field(segment, number)
        if not raw:
            return []
        return [part.strip() for part in raw.split(self.encoding.component)]

    def component(self, segment: Hl7Segment, number: int, index: int = 0) -> str:
        """字段的第 ``index`` 个组件；越界返回空串。"""
        parts = self.components(segment, number)
        if index < 0 or index >= len(parts):
            return ""
        return parts[index]

    @property
    def message_type(self) -> str:
        """``MSH-9`` 原文（消息类型 + 触发事件 + 消息结构）。"""
        msh = self.first("MSH")
        if msh is None:
            return ""
        return self.field(msh, 9)

    @property
    def message_code(self) -> str:
        """``MSH-9`` 第 1 个组件（消息码，如 ``ORM``）。"""
        msh = self.first("MSH")
        if msh is None:
            return ""
        return self.component(msh, 9, 0).upper()

    @property
    def trigger_event(self) -> str:
        """``MSH-9`` 第 2 个组件（触发事件，如 ``O01``）。"""
        msh = self.first("MSH")
        if msh is None:
            return ""
        return self.component(msh, 9, 1).upper()

    @property
    def control_id(self) -> str:
        """``MSH-10``（消息控制号 → 批次号）。"""
        msh = self.first("MSH")
        if msh is None:
            return ""
        return self.field(msh, 10)


class Hl7ParseError(ValueError):
    """HL7 消息的**整消息级**问题：没有 ``MSH`` 段、编码不可识别等。

    整消息级问题一律不做部分解析 —— 半份医嘱会让药师拿到一份看起来正常、
    实则漏判的台账。挂起台账与告警由入站完整性任务接管。
    """


@dataclass(frozen=True)
class SkippedMessage:
    """被显式跳过的一条消息（非医嘱消息 / 结构损坏）。"""

    source: str
    message_type: str
    reason: str


@dataclass(frozen=True)
class Hl7OrderBatch:
    """一批 HL7 医嘱消息的解析结果。"""

    rows: tuple[PrescriptionRow, ...] = ()
    skipped: tuple[SkippedMessage, ...] = ()
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 段字段 → 口径字段映射（唯一真源）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldRule:
    """一条「HL7 段字段 → 口径字段」的映射规则。

    ``pick`` 决定取法：

    * ``field`` —— 字段原文（不拆组件）；
    * ``component`` —— 第 ``index`` 个组件；
    * ``xcn_id`` / ``xcn_name`` —— 人名字段（``XCN``）的标识 / 姓名；
    * ``timestamp`` —— HL7 ``TS`` 时间戳，按口径时间写法归一。
    """

    segment: str
    number: int
    target: str
    pick: str = "component"
    index: int = 0
    priority: int = 50
    note: str = ""


#: HL7 v2 ``ORM^O01`` → 医嘱落地口径字段的映射表（spec.md §2.1 B 段）。
#:
#: 优先级数字小者优先：同名字段有多个来源时，先取优先级数字小的**非空**值，
#: 取不到再回落（例：``RXO-4`` 缺剂量时回落 ``RXE-10`` 的给药剂量）。
HL7_FIELD_MAP: tuple[FieldRule, ...] = (
    FieldRule("MSH", 10, "batch_no", pick="field", priority=10,
              note="消息控制号 → 批次号"),
    FieldRule("PID", 3, "patient_ref", pick="component", index=0, priority=10,
              note="患者标识 CX 第 1 分量"),
    FieldRule("PV1", 1, "visit_no", pick="field", priority=10,
              note="就诊号"),
    FieldRule("ORC", 2, "order_no", pick="field", priority=10,
              note="医嘱号"),
    FieldRule("ORC", 7, "event_time", pick="timestamp", priority=10,
              note="开立时间"),
    FieldRule("ORC", 9, "prescriber_id", pick="xcn_id", priority=10,
              note="开立医师标识"),
    FieldRule("ORC", 9, "prescriber_name", pick="xcn_name", priority=20,
              note="开立医师姓名（XCN 第 2 分量；缺项时回落第 1 分量）"),
    FieldRule("PV1", 7, "prescriber_name", pick="xcn_name", priority=30,
              note="主治医生（开立医师未给姓名时回落）"),
    FieldRule("OMG", 4, "dosage_text", pick="field", priority=40,
              note="医嘱描述 → 用法用量原文"),
    FieldRule("RXO", 1, "quantity", pick="field", priority=40,
              note="数量"),
    FieldRule("RXO", 2, "route", pick="component", index=0, priority=40,
              note="给药途径"),
    FieldRule("RXO", 4, "dosage_value", pick="component", index=0, priority=40,
              note="剂量"),
    FieldRule("RXO", 11, "dosage_unit", pick="component", index=0, priority=40,
              note="剂量单位"),
    FieldRule("RXE", 4, "drug_name", pick="component", index=0, priority=40,
              note="药品名称"),
    FieldRule("RXE", 10, "dosage_value", pick="field", priority=60,
              note="给药剂量（RXO-4 缺项时回落）"),
    FieldRule("RXE", 11, "dosage_unit", pick="component", index=0, priority=60,
              note="给药剂量单位（RXO-11 缺项时回落）"),
)

#: HL7 落地口径**覆盖到**的口径字段全集（等价性断言的字段范围）。
HL7_COVERED_FIELDS: tuple[str, ...] = tuple(
    dict.fromkeys(rule.target for rule in HL7_FIELD_MAP)
)


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------


def _split_segments(text: str) -> list[str]:
    """按段分隔符切开（回车 / 换行），丢掉空段。"""
    return [piece.strip() for piece in _SEGMENT_SPLIT.split(text or "") if piece.strip()]


def _split_messages(text: str) -> list[str]:
    """把文本切成若干条消息（每个 ``MSH`` 段开一条新消息）。"""
    messages: list[str] = []
    current: list[str] = []
    for segment in _split_segments(text):
        name = segment[:3].upper()
        if name == "MSH" and current:
            messages.append("\r".join(current))
            current = []
        current.append(segment)
    if current:
        messages.append("\r".join(current))
    return messages


def parse_message(text: str, *, source: str = "") -> Hl7Message:
    """把一段 HL7 文本解析成消息对象。

    * 段分隔回车 / 换行，字段分隔竖线；
    * 编码字符以 ``MSH-2`` 声明为准（缺省 ``^~\\&``）；
    * 没有 ``MSH`` 段 → 抛 :class:`Hl7ParseError`（整消息级错误，不做部分解析）。
    """
    segments: list[Hl7Segment] = []
    encoding = EncodingCharacters.parse(DEFAULT_ENCODING_CHARACTERS)

    for raw in _split_segments(text):
        name, separator, remainder = raw.partition(FIELD_SEPARATOR)
        name = name[:3].strip().upper()
        if not name:
            continue
        if not separator:
            segments.append(Hl7Segment(name=name, fields=()))
            continue
        fields = tuple(remainder.split(FIELD_SEPARATOR))
        if name == "MSH":
            encoding = EncodingCharacters.parse(fields[0] if fields else "")
        segments.append(Hl7Segment(name=name, fields=fields))

    if not segments or segments[0].name != "MSH":
        raise Hl7ParseError(
            f"消息 {source or '<未知>'} 缺少 MSH 段，不是有结构的 HL7 消息，整消息跳过"
        )
    return Hl7Message(segments=tuple(segments), encoding=encoding)


def is_order_message(message: Hl7Message) -> bool:
    """是否医嘱消息（``MSH-9`` = ``ORM^O01``）。"""
    return (
        message.message_code == ORDER_MESSAGE_CODE
        and message.trigger_event == ORDER_TRIGGER_EVENT
    )


# ---------------------------------------------------------------------------
# 时间戳归一
# ---------------------------------------------------------------------------


def parse_hl7_timestamp(raw: str) -> str:
    """HL7 ``TS`` 时间戳 → 口径时间写法 ``YYYY-MM-DD HH:MM:SS``。

    * 支持 ``YYYYMMDD`` / ``YYYYMMDDHHMM`` / ``YYYYMMDDHHMMSS``；
    * 支持小数秒（``.S``）与尾部时区偏移（``+0800`` → ``+08:00``）；
    * 认不出来的写法**保留原文**，不猜时刻（交判定层按不可比处理）。
    """
    body = (raw or "").strip()
    if not body:
        return ""

    offset = ""
    match = _OFFSET_PATTERN.search(body)
    if match:
        offset_text = match.group(1)
        body = body[: match.start()].strip()
        hours = int(offset_text[1:3])
        minutes = int(offset_text[3:5])
        offset = f"{offset_text[0]}{hours:02d}:{minutes:02d}"

    fraction = ""
    if "." in body:
        body, _, tail = body.partition(".")
        fraction = tail

    digits = body.strip()
    if not digits.isdigit():
        return raw.strip()

    if len(digits) == 8:
        date_text = f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
        time_text = ""
    elif len(digits) == 12:
        date_text = f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
        time_text = f"{digits[8:10]}:{digits[10:12]}"
    elif len(digits) == 14:
        date_text = f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
        time_text = f"{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
    else:
        return raw.strip()

    result = f"{date_text} {time_text}".strip()
    if fraction:
        result = f"{result}.{fraction}"
    return f"{result}{offset}"


# ---------------------------------------------------------------------------
# 段字段 → 口径字段
# ---------------------------------------------------------------------------


def _pick_value(message: Hl7Message, segment: Hl7Segment, rule: FieldRule) -> str:
    """按映射规则取一个字段值。"""
    if rule.pick == "field":
        return message.field(segment, rule.number)
    if rule.pick == "component":
        return message.component(segment, rule.number, rule.index)
    if rule.pick == "xcn_id":
        return message.component(segment, rule.number, 0)
    if rule.pick == "xcn_name":
        parts = message.components(segment, rule.number)
        if not parts:
            return ""
        if len(parts) > 1 and parts[1]:
            return parts[1]
        return parts[0]
    if rule.pick == "timestamp":
        return parse_hl7_timestamp(message.field(segment, rule.number))
    return ""


def _collect_candidates(
    message: Hl7Message,
    segments: Sequence[Hl7Segment],
    candidates: dict[str, list[tuple[int, int, str]]],
) -> None:
    """把 ``segments`` 里命中映射规则的字段值收进候选表。"""
    for rule in HL7_FIELD_MAP:
        for segment in segments:
            if segment.name != rule.segment:
                continue
            value = _pick_value(message, segment, rule)
            if not value:
                continue
            bucket = candidates.setdefault(rule.target, [])
            bucket.append((rule.priority, len(bucket), value))


def _resolve(candidates: dict[str, list[tuple[int, int, str]]]) -> dict[str, str]:
    """候选表 → 最终取值：优先级小者优先，同优先级先出现者优先，均取非空值。"""
    values: dict[str, str] = {}
    for target, bucket in candidates.items():
        best_key: tuple[int, int] | None = None
        best_value = ""
        for priority, order, value in bucket:
            if not value:
                continue
            key = (priority, order)
            if best_key is None or key < best_key:
                best_key = key
                best_value = value
        if best_value:
            values[target] = best_value
    return values


def _extract_rows(message: Hl7Message) -> list[PrescriptionRow]:
    """把一条医嘱消息拆成处方行（每个 ``RXE`` 明细一行）。

    段归属：``PID`` / ``PV1`` 是消息级，``ORC`` 是医嘱级，``OMG`` / ``RXO`` 是
    医嘱组级，``RXE`` 是明细级。一条消息里出现新的 ``ORC`` 即开启新的医嘱组。
    """
    rows: list[PrescriptionRow] = []
    committed: list[Hl7Segment] = []
    pending: list[Hl7Segment] = []
    order: list[Hl7Segment] = []
    group: list[Hl7Segment] = []

    msh = message.first("MSH")
    if msh is not None:
        committed.append(msh)

    for segment in message.segments:
        if segment.name in ("PID", "PV1"):
            pending.append(segment)
        elif segment.name == "ORC":
            committed = committed + pending
            pending = []
            order = [segment]
            group = []
        elif segment.name in ("OMG", "RXO"):
            group.append(segment)
        elif segment.name == "RXE":
            candidates: dict[str, list[tuple[int, int, str]]] = {}
            _collect_candidates(message, committed + pending, candidates)
            _collect_candidates(message, order, candidates)
            _collect_candidates(message, group, candidates)
            _collect_candidates(message, [segment], candidates)
            values = _resolve(candidates)
            rows.append(
                PrescriptionRow(
                    **{name: values.get(name, "") for name in PRESCRIPTION_FIELDS},
                    # HL7 通道能承载的口径字段 = 该通道「有这一栏」；
                    # 通道本身不承载的字段（如临床诊断）属整列未落地，
                    # 由判定层落不可判定并点名字段。
                    present_columns=frozenset(HL7_COVERED_FIELDS),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def parse_order_messages(text: str, *, source: str = "") -> Hl7OrderBatch:
    """解析一批 HL7 消息，返回处方行 + 被跳过的消息 + 告警。

    * 非 ``ORM^O01`` 消息（结果、检验、ADT 等）→ **显式跳过并告警**，不静默丢弃；
    * 结构损坏（没有 ``MSH`` 段）→ 整消息跳过并告警；
    * 字段缺项 → 置空（由判定层落第三态），不报错。
    """
    rows: list[PrescriptionRow] = []
    skipped: list[SkippedMessage] = []
    warnings: list[str] = []

    for raw_message in _split_messages(text):
        try:
            message = parse_message(raw_message, source=source)
        except Hl7ParseError as exc:
            reason = str(exc)
            skipped.append(SkippedMessage(source=source, message_type="", reason=reason))
            warnings.append(reason)
            logger.warning("[hl7] %s", reason)
            continue

        if not is_order_message(message):
            kind = message.message_type or "<未知>"
            reason = (
                f"消息类型 {kind} 不是 {ORDER_MESSAGE_CODE}^{ORDER_TRIGGER_EVENT} "
                "医嘱消息，已显式跳过"
            )
            skipped.append(SkippedMessage(source=source, message_type=kind, reason=reason))
            warnings.append(reason)
            logger.warning("[hl7] %s：%s", source or "<未知>", reason)
            continue

        message_rows = _extract_rows(message)
        if not message_rows:
            reason = "医嘱消息里没有 RXE 明细行，落不成处方行，已显式跳过"
            skipped.append(
                SkippedMessage(source=source, message_type=message.message_type, reason=reason)
            )
            warnings.append(reason)
            logger.warning("[hl7] %s：%s", source or "<未知>", reason)
            continue

        rows.extend(message_rows)

    return Hl7OrderBatch(rows=tuple(rows), skipped=tuple(skipped), warnings=tuple(warnings))


def read_order_rows(path: str | Path) -> Hl7OrderBatch:
    """读取一份 HL7 落地文件，返回处方行 + 被跳过的消息 + 告警。

    编码兜底与 CSV 读取同口径（UTF-8 优先、GBK 兜底）；解码失败 → 整文件错误
    （不做部分解析）。
    """
    file_path = Path(path)
    try:
        text = decode_bytes(file_path.read_bytes(), source=str(file_path))
    except CsvReadError as exc:
        raise Hl7ParseError(
            f"文件 {file_path} 解码失败，整文件挂起，不做部分解析：{exc}"
        ) from exc
    return parse_order_messages(text, source=file_path.name)


def list_hl7_files(inbox_dir: str | Path) -> list[Path]:
    """列出落地目录下的 HL7 文件（按文件名排序，保证批次处理顺序稳定）。"""
    directory = Path(inbox_dir)
    if not directory.is_dir():
        return []
    files = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in HL7_SUFFIXES
    ]
    return sorted(files, key=lambda item: item.name)


__all__ = [
    "DEFAULT_ENCODING_CHARACTERS",
    "FIELD_SEPARATOR",
    "HL7_COVERED_FIELDS",
    "HL7_FIELD_MAP",
    "HL7_SUFFIXES",
    "ORDER_MESSAGE_CODE",
    "ORDER_TRIGGER_EVENT",
    "EncodingCharacters",
    "FieldRule",
    "Hl7Message",
    "Hl7OrderBatch",
    "Hl7ParseError",
    "Hl7Segment",
    "SkippedMessage",
    "is_order_message",
    "list_hl7_files",
    "parse_hl7_timestamp",
    "parse_message",
    "parse_order_messages",
    "read_order_rows",
]
