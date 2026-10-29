"""归一层：医嘱落地原文 → 可比较的规范值。

**对应源产品能力**：源仓库 ``backend/src/baserow/core/workflow_actions/registries.py``
的 ``WorkflowActionType.prepare_values()``——它的注释写得很直白：「落库前把提供的值
转换掉，比如把一个 ID 转成模型实例、把某个日期字符串转成日期对象，同时也是在同一次
钩子里做校验」。本轮把这条「落库前统一转换」的思路改写成**判定前的归一层**：
规则求值之前先把医嘱原文转成可比较的规范值，转不动的**留痕而不是猜**。

**为什么这一层不能省**：HIS 医嘱落地文件里同一个量有多种写法——
``3日量`` / ``3 日常用量`` / ``1次常用量`` / ``5mg`` / ``5毫克`` / ``qd`` / ``每日一次`` /
``门诊一般患者`` / ``住院患者``。不归一就拿字符串比数字，「3日量 > 1」会落成
「口径不可比」，或者更糟——被当成字符串相等比较后判成合规。

**四条归一纪律**

1. **空值 ≠ 显式否**：字段留空是「系统审不了」（不可判定），
   字段写「未注明 / 未做」是「已知不合格」（违规）。归一层绝不能把空串
   归一成「未注明」——那会把待补字段的处方静默变成违规处方。
2. **量值转不动就留原文**：``适量`` / ``遵医嘱`` 这类文本不是 0，
   归一层保留原文并把 ``kind`` 标成文本，由判定内核按「口径不可比」落不可判定。
3. **单位不匹配必须可见**：``days_qty`` 写成 ``3mg`` 时数值是 3，但口径不可比。
   归一层同时给出 ``number`` 与 ``unit``，并把 ``comparable`` 置否，
   由判定内核（T15）据此落「不可判定」，**不得**在此静默当成 3。
4. **别名只来自依据文件**：单位、频次、枚举别名表的口径只取自
   《处方审核规则体系》§3（特殊药品处方量限）与该案例集里的真实写法，
   不自造药品知识（药品说明书属厂商资产）。

**日期口径**：院内医嘱落地文件的本地时间按中国标准时间（UTC+8）解释，
带时区的 ISO8601 串按其偏移换算，统一收敛到 UTC 时刻表示。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum

from ..models import PRESCRIPTION_FIELDS, PrescriptionRow
from .evaluator import DEFAULT_EXPLICIT_NO_MARKERS, FieldState

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 中国标准时间（UTC+8）。院内医嘱落地文件里的本地时间按此解释。
CHINA_STANDARD_TIME = timezone(timedelta(hours=8), name="CST")


def _alias_key(text: str) -> str:
    """别名查找键：去空白并大小写折叠。"""
    return "".join(str(text).split()).casefold()


# ---------------------------------------------------------------------------
# 单位别名表
# ---------------------------------------------------------------------------

#: 单位别名表：规范单位 → 落地文件里的写法。
#:
#: 口径来自《处方审核规则体系》§3 出现的计量单位（日量 / 次 / 常用量）
#: 与 spec.md §2.1 声明的字段语义（``days_qty`` 单位「日」、``age_unit`` 岁 / 月 / 日）。
#: 只做**写法**归一，不换算单位（mg 与 g 的换算属药品数据资产，不在本层）。
UNIT_ALIASES: dict[str, tuple[str, ...]] = {
    "日": ("日", "天", "日量", "日用量", "日常量", "日常用量", "days"),
    "次": ("次", "回", "次量", "次用量", "次常用量"),
    "片": ("片", "片剂", "tablet"),
    "支": ("支", "支装", "vial"),
    "袋": ("袋", "包", "袋装"),
    "粒": ("粒",),
    "贴": ("贴",),
    "瓶": ("瓶",),
    "滴": ("滴", "gtt"),
    "mg": ("mg", "毫克"),
    "g": ("g", "克"),
    "kg": ("kg", "千克", "公斤"),
    "ug": ("ug", "μg", "µg", "微克"),
    "ml": ("ml", "毫升", "cc"),
    "l": ("l", "升"),
    "iu": ("iu", "单位"),
    "岁": ("岁", "周岁", "years"),
    "月": ("月", "月龄", "months"),
    "周": ("周", "周龄", "weeks"),
}


def _build_unit_index() -> dict[str, str]:
    """别名 → 规范单位（键做大小写折叠，拉丁字母单位大小写写法不敏感）。"""
    index: dict[str, str] = {}
    for canonical, aliases in UNIT_ALIASES.items():
        for alias in aliases:
            index[alias.casefold()] = canonical
    return index


#: 别名 → 规范单位。
UNIT_INDEX: dict[str, str] = _build_unit_index()

#: 日量字段可接受的口径单位。``次`` 在《处方审核规则体系》§3 里是合法口径
#: （麻醉药品注射剂「每张处方 1 次常用量」），``""`` 表示原始文件没写单位。
DAY_QUANTITY_UNITS: frozenset[str] = frozenset({"日", "次", ""})

#: 量值后缀 → 规范单位。命中这里的按「日 / 次」口径直接判定，不走通用单位表。
QUANTITY_SUFFIX_UNITS: dict[str, str] = {
    "日常用量": "日",
    "日常量": "日",
    "日用量": "日",
    "日量": "日",
    "日": "日",
    "天量": "日",
    "天": "日",
    "次常用量": "次",
    "次用量": "次",
    "次量": "次",
    "次": "次",
    "回": "次",
    "常用量": "",
}


# ---------------------------------------------------------------------------
# 频次枚举
# ---------------------------------------------------------------------------


class Frequency(str, Enum):
    """给药频次枚举（规范值为院内通用代码，``label`` 给中文显示名）。"""

    QD = "qd"
    BID = "bid"
    TID = "tid"
    QID = "qid"
    Q6H = "q6h"
    Q8H = "q8h"
    Q12H = "q12h"
    PRN = "prn"
    PO = "po"
    IV = "iv"
    IM = "im"
    SC = "sc"
    QW = "qw"
    QN = "qn"
    HS = "hs"

    @property
    def label(self) -> str:
        """中文显示名（仅用于台账展示，判定比较一律用规范代码）。"""
        return _FREQUENCY_LABELS[self]


_FREQUENCY_LABELS: dict[Frequency, str] = {
    Frequency.QD: "每日一次",
    Frequency.BID: "每日两次",
    Frequency.TID: "每日三次",
    Frequency.QID: "每日四次",
    Frequency.Q6H: "每 6 小时一次",
    Frequency.Q8H: "每 8 小时一次",
    Frequency.Q12H: "每 12 小时一次",
    Frequency.PRN: "必要时",
    Frequency.PO: "口服",
    Frequency.IV: "静脉给药",
    Frequency.IM: "肌内注射",
    Frequency.SC: "皮下注射",
    Frequency.QW: "每周一次",
    Frequency.QN: "每晚一次",
    Frequency.HS: "睡前",
}

#: 频次别名 → 枚举。英文码来自院内医嘱常规写法，中文写法来自同一批落地文件。
FREQUENCY_ALIASES: dict[str, tuple[str, ...]] = {
    Frequency.QD: ("qd", "q.d", "每日一次", "一天一次", "一日一次", "日一次", "每天一次"),
    Frequency.BID: ("bid", "b.i.d", "每日两次", "一天两次", "一日两次", "日两次", "每天两次"),
    Frequency.TID: ("tid", "t.i.d", "每日三次", "一天三次", "一日三次", "日三次", "每天三次"),
    Frequency.QID: ("qid", "q.i.d", "每日四次", "一天四次", "一日四次", "每天四次"),
    Frequency.Q6H: ("q6h", "q 6h", "每 6 小时一次", "每6小时一次", "每六小时一次"),
    Frequency.Q8H: ("q8h", "q 8h", "每 8 小时一次", "每8小时一次", "每八小时一次"),
    Frequency.Q12H: ("q12h", "q 12h", "每 12 小时一次", "每12小时一次", "每十二小时一次"),
    Frequency.PRN: ("prn", "sos", "必要时", "需要时"),
    Frequency.PO: ("po", "口服"),
    Frequency.IV: ("iv", "ivgtt", "静滴", "静脉注射", "静脉滴注", "静脉给药"),
    Frequency.IM: ("im", "肌注", "肌内注射"),
    Frequency.SC: ("sc", "皮注", "皮下注射"),
    Frequency.QW: ("qw", "每周一次", "一周一次"),
    Frequency.QN: ("qn", "每晚一次"),
    Frequency.HS: ("hs", "睡前"),
}


def _build_frequency_index() -> dict[str, Frequency]:
    """别名 → 频次枚举（键去空白并大小写折叠）。"""
    index: dict[str, Frequency] = {}
    for member, aliases in FREQUENCY_ALIASES.items():
        for alias in aliases:
            index[_alias_key(alias)] = member
    return index


#: 频次别名 → 枚举。
FREQUENCY_INDEX: dict[str, Frequency] = _build_frequency_index()


# ---------------------------------------------------------------------------
# 枚举别名表
# ---------------------------------------------------------------------------

#: 处方类型别名 → 规范值。
#:
#: 规范值取自《处方审核规则体系》§3 的三档口径（门（急）诊一般患者 /
#: 门（急）诊癌症疼痛和中、重度慢性疼痛患者 / 住院患者），
#: 别名为案例集 ``prescription_review_cases.json`` 里出现的真实写法。
RX_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "门急诊一般": (
        "门急诊一般",
        "门急诊一般患者",
        "门（急）诊一般",
        "门(急)诊一般",
        "门（急）诊一般患者",
        "门诊一般",
        "门诊一般患者",
        "一般门诊",
    ),
    "门急诊癌症疼痛": (
        "门急诊癌症疼痛",
        "门急诊癌症疼痛患者",
        "门（急）诊癌症疼痛",
        "门(急)诊癌症疼痛",
        "门（急）诊癌症疼痛患者",
        "门诊癌症疼痛",
        "门诊癌症疼痛患者",
    ),
    "住院": ("住院", "住院患者", "住院治疗", "在院"),
}

#: 特殊管理类别别名 → 规范值。
#:
#: 规范值取自《处方审核规则体系》§3 的术语（麻醉药品 / 麻醉药品注射剂 /
#: 麻醉药品控缓释制剂 / 第一类精神药品 / 第二类精神药品），
#: 别名含 §3 里的口语简称（精一 / 精二）与院内常见写法。
DRUG_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "麻醉药品": ("麻醉药品", "麻醉药", "麻醉类"),
    "麻醉药品注射剂": ("麻醉药品注射剂", "麻醉注射剂", "麻药注射剂"),
    "麻醉药品控缓释制剂": (
        "麻醉药品控缓释制剂",
        "麻醉控缓释制剂",
        "麻醉药控缓释",
        "麻药控缓释",
    ),
    "第一类精神药品": ("第一类精神药品", "一类精神药品", "第一类精神", "精一"),
    "第一类精神药品注射剂": (
        "第一类精神药品注射剂",
        "一类精神药品注射剂",
        "精一注射剂",
    ),
    "第二类精神药品": ("第二类精神药品", "二类精神药品", "第二类精神", "精二"),
}

#: 皮试结果别名 → 规范值。
#:
#: 规范值取自 spec.md §2.1 声明的皮试结果取值（阳性 / 阴性 / 未做 / 未注明）。
#: **「未做」与「未注明」归一到同一值**：两者都是「已知没写明结果」，
#: 结论同为违规（§1 适宜性审核第 1 项）；与「字段留空」（不可判定）严格区分。
SKIN_TEST_ALIASES: dict[str, tuple[str, ...]] = {
    "阴性": ("阴性", "阴", "negative", "neg", "(-)", "未发现异常"),
    "阳性": ("阳性", "阳", "positive", "pos", "(+)"),
    "未注明": (
        "未注明",
        "未做",
        "未行",
        "未做皮试",
        "未标注",
        "未知",
        "不详",
    ),
}


def _build_enum_index() -> dict[str, dict[str, str]]:
    """字段 → （别名 → 规范值）。"""
    tables = {
        "rx_type": RX_TYPE_ALIASES,
        "drug_category": DRUG_CATEGORY_ALIASES,
        "skin_test_result": SKIN_TEST_ALIASES,
    }
    index: dict[str, dict[str, str]] = {}
    for field, table in tables.items():
        field_index: dict[str, str] = {}
        for canonical, aliases in table.items():
            for alias in aliases:
                field_index[_alias_key(alias)] = canonical
        index[field] = field_index
    return index


#: 字段 → （别名 → 规范值）。
ENUM_INDEX: dict[str, dict[str, str]] = _build_enum_index()

#: 走枚举归一的字段。
ENUM_FIELDS: frozenset[str] = frozenset({"rx_type", "drug_category", "skin_test_result"})

#: 走量值归一的字段（数值 + 单位）。
QUANTITY_FIELDS: frozenset[str] = frozenset({"dosage_value", "quantity", "age_value"})

#: 只放单位、不放数值的字段。
UNIT_FIELDS: frozenset[str] = frozenset({"dosage_unit", "age_unit"})


# ---------------------------------------------------------------------------
# 归一结果
# ---------------------------------------------------------------------------


class ValueKind(str, Enum):
    """归一结果的形态。"""

    EMPTY = "空"
    NUMBER = "数值"
    ENUM = "枚举"
    FREQUENCY = "频次"
    TIMESTAMP = "时间戳"
    UNIT = "单位"
    TEXT = "文本"


@dataclass(frozen=True)
class NormalizedValue:
    """一个字段归一后的结果。

    ``text`` 是给判定层直接比对的规范值；``number`` / ``unit`` /
    ``timestamp`` 是结构化的补充信息，供判定内核做单位口径检查
    （如「``days_qty`` 写成 3mg」判口径不可比）。
    """

    field: str
    raw: str
    text: str
    kind: ValueKind
    state: FieldState
    number: Decimal | None = None
    unit: str = ""
    timestamp: datetime | None = None
    label: str = ""
    comparable: bool = True

    @property
    def is_missing(self) -> bool:
        """字段缺失（空串 / 纯空白 / 缺列）。"""
        return self.state is FieldState.MISSING

    @property
    def is_explicit_no(self) -> bool:
        """字段有值但表示「没做 / 未注明 / 否」。"""
        return self.state is FieldState.EXPLICIT_NO

    @property
    def is_value(self) -> bool:
        """字段有正常取值。"""
        return self.state is FieldState.VALUE


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

#: 显式否定标记（大小写折叠后的查找表）。
_MARKER_INDEX: frozenset[str] = frozenset(
    marker.strip().casefold() for marker in DEFAULT_EXPLICIT_NO_MARKERS
)


def _is_explicit_no(text: str) -> bool:
    """判断一个非空取值是不是显式否定标记（未注明 / 未做 / 否 …）。"""
    return text.strip().casefold() in _MARKER_INDEX


def _leading_decimal(text: str) -> tuple[Decimal | None, str]:
    """取文本开头的数值与其后缀；取不到数值返回 ``(None, 原文)``。

    手写扫描而不引正则：表达式子包保持「只做静态处理」的观感，
    便于静态扫描一刀切确认子包里没有任何动态执行调用。
    """
    body = text.strip()
    index = 0
    sign = ""
    if body[:1] in ("+", "-"):
        sign = body[0]
        index = 1
    start = index
    dot_seen = False
    while index < len(body):
        char = body[index]
        if char.isdigit():
            index += 1
        elif char == "." and index > start and not dot_seen:
            dot_seen = True
            index += 1
        else:
            break
    number_text = body[start:index].rstrip(".")
    suffix = body[index:]
    if not number_text:
        return None, body
    try:
        value = Decimal(number_text)
    except InvalidOperation:
        return None, body
    if sign == "-":
        value = -value
    return value, suffix


def _decimal_text(value: Decimal) -> str:
    """数值的规范文本：整数去掉小数尾巴（``3.00`` → ``3``）。"""
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal(1)))
    return format(normalized, "f")


def _resolve_unit_suffix(suffix: str) -> str:
    """量值后缀 → 规范单位。

    顺序：先查日 / 次专属后缀表，再剥掉「量 / 用量」尾词查通用单位表，
    最后原样保留（未知单位不猜、不丢）。
    """
    text = suffix.strip()
    if not text:
        return ""
    if text in QUANTITY_SUFFIX_UNITS:
        return QUANTITY_SUFFIX_UNITS[text]
    stripped = text
    for tail in ("用量", "量"):
        if stripped.endswith(tail) and len(stripped) > len(tail):
            stripped = stripped[: -len(tail)]
            break
    if stripped:
        canonical = UNIT_INDEX.get(_alias_key(stripped))
        if canonical:
            return canonical
    canonical = UNIT_INDEX.get(_alias_key(text))
    if canonical:
        return canonical
    return text


# ---------------------------------------------------------------------------
# 归一入口（按形态）
# ---------------------------------------------------------------------------


def normalize_unit(text: str) -> str:
    """单位写法归一（``毫克`` → ``mg``；未知单位原样保留）。"""
    body = "" if text is None else str(text).strip()
    if not body:
        return ""
    return UNIT_INDEX.get(_alias_key(body), body)


def normalize_quantity(text: str, *, field: str = "") -> NormalizedValue:
    """量值归一：``3日量`` → 数值 3 + 单位「日」；``5mg`` → 数值 5 + 单位「mg」。

    转不动（``适量`` / ``遵医嘱``）时**保留原文**并把 ``kind`` 标成文本，
    ``number`` 留空——绝不静默当成 0。
    """
    raw = "" if text is None else str(text)
    body = raw.strip()
    if not body:
        return NormalizedValue(
            field=field, raw=raw, text="", kind=ValueKind.EMPTY, state=FieldState.MISSING
        )
    if _is_explicit_no(body):
        return NormalizedValue(
            field=field,
            raw=raw,
            text=body,
            kind=ValueKind.TEXT,
            state=FieldState.EXPLICIT_NO,
        )
    number, suffix = _leading_decimal(body)
    if number is None:
        return NormalizedValue(
            field=field, raw=raw, text=body, kind=ValueKind.TEXT, state=FieldState.VALUE
        )
    return NormalizedValue(
        field=field,
        raw=raw,
        text=_decimal_text(number),
        kind=ValueKind.NUMBER,
        state=FieldState.VALUE,
        number=number,
        unit=_resolve_unit_suffix(suffix),
    )


def normalize_days_quantity(text: str, *, field: str = "days_qty") -> NormalizedValue:
    """日量归一：额外标出「口径可比」。

    ``3日量`` / ``1次常用量`` 的口径可比；``3mg`` 数值是 3 但口径不可比，
    ``comparable`` 置否，由判定内核据此落「不可判定」。
    """
    value = normalize_quantity(text, field=field)
    if value.kind is ValueKind.NUMBER:
        return replace(value, comparable=value.unit in DAY_QUANTITY_UNITS)
    return value


def normalize_frequency(text: str, *, field: str = "freq") -> NormalizedValue:
    """频次归一：``qd`` / ``QD`` / ``每日一次`` → 频次枚举 ``qd``。"""
    raw = "" if text is None else str(text)
    body = raw.strip()
    if not body:
        return NormalizedValue(
            field=field, raw=raw, text="", kind=ValueKind.EMPTY, state=FieldState.MISSING
        )
    if _is_explicit_no(body):
        return NormalizedValue(
            field=field,
            raw=raw,
            text=body,
            kind=ValueKind.TEXT,
            state=FieldState.EXPLICIT_NO,
        )
    member = FREQUENCY_INDEX.get(_alias_key(body))
    if member is None:
        return NormalizedValue(
            field=field, raw=raw, text=body, kind=ValueKind.TEXT, state=FieldState.VALUE
        )
    return NormalizedValue(
        field=field,
        raw=raw,
        text=member.value,
        kind=ValueKind.FREQUENCY,
        state=FieldState.VALUE,
        label=member.label,
    )


def normalize_timestamp(text: str, *, field: str = "event_time") -> NormalizedValue:
    """时间戳归一：``2026-10-28T08:30:00`` 与 ``2026-10-28 08:30`` 归到同一时刻。

    无时区的本地时间按中国标准时间（UTC+8）解释，带偏移的按偏移换算，
    统一收敛到 UTC 时刻；解析不出来时保留原文交判定层按不可比处理。
    """
    raw = "" if text is None else str(text)
    body = raw.strip()
    if not body:
        return NormalizedValue(
            field=field, raw=raw, text="", kind=ValueKind.EMPTY, state=FieldState.MISSING
        )
    if _is_explicit_no(body):
        return NormalizedValue(
            field=field,
            raw=raw,
            text=body,
            kind=ValueKind.TEXT,
            state=FieldState.EXPLICIT_NO,
        )
    moment = _parse_timestamp(body)
    if moment is None:
        return NormalizedValue(
            field=field, raw=raw, text=body, kind=ValueKind.TEXT, state=FieldState.VALUE
        )
    return NormalizedValue(
        field=field,
        raw=raw,
        text=_timestamp_text(moment),
        kind=ValueKind.TIMESTAMP,
        state=FieldState.VALUE,
        timestamp=moment,
    )


def normalize_enum(field: str, text: str) -> NormalizedValue:
    """枚举归一：处方类型 / 药品类别 / 皮试结果的别名 → 规范值。

    空值与显式否严格分开：空串 → 缺失（不可判定），「未注明」→ 显式否（违规）。
    """
    raw = "" if text is None else str(text)
    body = raw.strip()
    if not body:
        return NormalizedValue(
            field=field, raw=raw, text="", kind=ValueKind.EMPTY, state=FieldState.MISSING
        )
    canonical = ENUM_INDEX.get(field, {}).get(_alias_key(body))
    if _is_explicit_no(body):
        return NormalizedValue(
            field=field,
            raw=raw,
            text=canonical if canonical else body,
            kind=ValueKind.ENUM if canonical else ValueKind.TEXT,
            state=FieldState.EXPLICIT_NO,
        )
    if canonical is None:
        return NormalizedValue(
            field=field, raw=raw, text=body, kind=ValueKind.TEXT, state=FieldState.VALUE
        )
    return NormalizedValue(
        field=field,
        raw=raw,
        text=canonical,
        kind=ValueKind.ENUM,
        state=FieldState.VALUE,
        label=canonical,
    )


def normalize_field(field: str, text: str) -> NormalizedValue:
    """按字段语义选择归一器（字段分派入口）。"""
    if field == "days_qty":
        return normalize_days_quantity(text, field=field)
    if field in QUANTITY_FIELDS:
        return normalize_quantity(text, field=field)
    if field in UNIT_FIELDS:
        raw = "" if text is None else str(text)
        body = raw.strip()
        if not body:
            return NormalizedValue(
                field=field,
                raw=raw,
                text="",
                kind=ValueKind.EMPTY,
                state=FieldState.MISSING,
            )
        if _is_explicit_no(body):
            return NormalizedValue(
                field=field,
                raw=raw,
                text=body,
                kind=ValueKind.TEXT,
                state=FieldState.EXPLICIT_NO,
            )
        canonical = normalize_unit(body)
        return NormalizedValue(
            field=field,
            raw=raw,
            text=canonical,
            kind=ValueKind.UNIT,
            state=FieldState.VALUE,
            unit=canonical,
            label=canonical,
        )
    if field == "freq":
        return normalize_frequency(text, field=field)
    if field == "event_time":
        return normalize_timestamp(text, field=field)
    if field in ENUM_FIELDS:
        return normalize_enum(field, text)
    raw = "" if text is None else str(text)
    body = raw.strip()
    if not body:
        return NormalizedValue(
            field=field, raw=raw, text="", kind=ValueKind.EMPTY, state=FieldState.MISSING
        )
    if _is_explicit_no(body):
        return NormalizedValue(
            field=field,
            raw=raw,
            text=body,
            kind=ValueKind.TEXT,
            state=FieldState.EXPLICIT_NO,
        )
    return NormalizedValue(
        field=field, raw=raw, text=body, kind=ValueKind.TEXT, state=FieldState.VALUE
    )


# ---------------------------------------------------------------------------
# 批量入口
# ---------------------------------------------------------------------------


def normalize_row(row: PrescriptionRow) -> dict[str, NormalizedValue]:
    """整行归一：医嘱行 → ``列名 → 归一结果``。"""
    return {name: normalize_field(name, row.value(name)) for name in PRESCRIPTION_FIELDS}


def normalize_context(row: PrescriptionRow) -> dict[str, str]:
    """整行归一后的求值上下文（``列名 → 规范值``），可直接喂给求值器。"""
    return {name: value.text for name, value in normalize_row(row).items()}


def normalize_values(values: Mapping[str, str]) -> dict[str, str]:
    """原始字典 → 归一后的求值上下文（求值器入口用）。"""
    return {
        name: normalize_field(name, values.get(name, "")).text for name in PRESCRIPTION_FIELDS
    }


# ---------------------------------------------------------------------------
# 时间戳解析
# ---------------------------------------------------------------------------


def _parse_timestamp(text: str) -> datetime | None:
    """解析时间戳文本；解析不出来返回 ``None``。

    支持三类写法：``YYYY-MM-DD``、``YYYY-MM-DD HH:MM[:SS]``、
    ``YYYY-MM-DDTHH:MM[:SS]``（可带 ``Z`` 或 ``±HH:MM`` 偏移）。
    """
    body = text.strip()
    if not body:
        return None
    if "T" in body:
        date_text, time_text = body.split("T", 1)
    elif " " in body:
        date_text, time_text = body.split(" ", 1)
    else:
        date_text, time_text = body, ""
    date_text = date_text.strip()
    time_text = time_text.strip()

    offset = None
    if time_text.endswith(("Z", "z")):
        time_text = time_text[:-1].strip()
        offset = timezone.utc
    else:
        for marker in ("+", "-"):
            position = time_text.rfind(marker)
            if position > 0:
                offset_text = time_text[position:]
                time_text = time_text[:position].strip()
                offset = _parse_offset(offset_text)
                break

    date_parts = date_text.replace("/", "-").split("-")
    if len(date_parts) != 3:
        return None
    try:
        year, month, day = (int(part) for part in date_parts)
    except ValueError:
        return None

    hour = minute = second = microsecond = 0
    if time_text:
        time_parts = time_text.split(":")
        if not 2 <= len(time_parts) <= 3:
            return None
        try:
            hour = int(time_parts[0])
            minute = int(time_parts[1])
            if len(time_parts) == 3:
                second_text = time_parts[2]
                if "." in second_text:
                    second_text, fraction = second_text.split(".", 1)
                    microsecond = int((fraction + "000000")[:6])
                else:
                    microsecond = 0
                second = int(second_text)
        except ValueError:
            return None
        if hour == 24 and minute == 0 and second == 0 and microsecond == 0:
            # 24:00 表示次日零点
            hour = 0
            day += 1

    try:
        moment = datetime(
            year, month, day, hour, minute, second, microsecond, tzinfo=offset
        )
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CHINA_STANDARD_TIME)
    return moment.astimezone(timezone.utc)


def _parse_offset(text: str) -> timezone | None:
    """解析 ``+HH:MM`` / ``-HH:MM`` 偏移；解析不出来返回 ``None``。"""
    body = text.strip()
    if len(body) < 6 or body[0] not in "+-":
        return None
    hours_text, minutes_text = body[1:].split(":", 1)
    try:
        hours = int(hours_text)
        minutes = int(minutes_text)
    except ValueError:
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    if body[0] == "-":
        delta = -delta
    return timezone(delta)


def _timestamp_text(moment: datetime) -> str:
    """时间戳的规范文本（UTC，秒级 ISO8601）。"""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "CHINA_STANDARD_TIME",
    "DAY_QUANTITY_UNITS",
    "DRUG_CATEGORY_ALIASES",
    "ENUM_FIELDS",
    "ENUM_INDEX",
    "FREQUENCY_ALIASES",
    "FREQUENCY_INDEX",
    "Frequency",
    "NormalizedValue",
    "QUANTITY_FIELDS",
    "QUANTITY_SUFFIX_UNITS",
    "RX_TYPE_ALIASES",
    "SKIN_TEST_ALIASES",
    "UNIT_ALIASES",
    "UNIT_FIELDS",
    "UNIT_INDEX",
    "ValueKind",
    "normalize_context",
    "normalize_days_quantity",
    "normalize_enum",
    "normalize_field",
    "normalize_frequency",
    "normalize_quantity",
    "normalize_row",
    "normalize_timestamp",
    "normalize_unit",
    "normalize_values",
]
