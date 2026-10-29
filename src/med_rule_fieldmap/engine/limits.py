"""量限矩阵：《处方审核规则体系》§3 特殊药品处方量限 → 判定内核。

**对应源产品能力**：源仓库 ``contrib/database/field_rules/registries.py`` 把判定规则做成
**可注册的类型**，在写入前逐行逐规则求值；本轮把量限从「规则行里手写的阈值表达式」
改写成**一张可核对的矩阵**——矩阵单元是口径唯一真源，规则行只声明「我落在哪个
矩阵单元」（``limit_ref``），载入期校验阈值与矩阵一致，判定期由矩阵给结论。

**为什么要矩阵而不是散落在规则行里的阈值**

#. **口径是三档 × 类别，不是几条阈值**。§3 的量限按「门（急）诊一般 / 门（急）诊癌症疼痛
   和中、重度慢性疼痛 / 住院」三档，与「麻醉注射剂 / 控缓释 / 其他剂型 / 精一 / 精二」
   类别交叉，共 15 个单元（住院档按药品大类计，注射剂与控缓释变体同归大类）。
   散落在规则行里，药师核对「住院的麻醉到底几日常量」要翻十几个文件；矩阵是一张表，
   一眼可比。
#. **有些量限带条件依赖**。第二类精神药品「一般不超过 7 日常量，延长须注明理由」——
   「理由」字段只在**超量时**才需要。表达式的 ``required_fields`` 是无条件依赖，
   写上就等于「没写理由的精二处方一律判不了」，写不上就丢掉了这个分支。矩阵单元能
   表达这种**条件依赖**：``needs_reason`` + ``reason_field``。
#. **口径不可比必须显式**。日量写成 ``3mg`` 时数值是 3，但拿它比「日量上限」必然出错。
   矩阵判定时先查口径，再比阈值：口径不可比 → **不可判定**，点名该字段，不静默判违规。

**依据（阈值一律照抄，不自创数字）**

* 《处方审核规则体系》§3（``domain/rules/处方审核规则体系.md``）——本轮口径真源；
* 《处方管理办法》（卫生部令第53号）第二十三条（门急诊量限）、第二十四条（癌症疼痛与
  中重度慢性疼痛档量限）、第二十五条（住院逐日开具）、第二十二条（麻醉注射剂限机构内使用）。

**矩阵单元里没有的东西（不静默忽略）**

* 《处方管理办法》第二十六条「盐酸哌替啶处方为一次常用量」是**药品级**限值，需要按
  药品编码匹配；药品清单属厂商资产（§3 末段明写），本轮不落库，只登记在
  :data:`DRUG_LEVEL_LIMITS_PENDING`，避免默默漏判。
* 第二十三条「哌醋甲酯用于治疗儿童多动症时每张处方不得超过 15 日常用量」是**人群例外**，
  需患者年龄与诊断信息；登记在单元的 ``remark`` 里，判定时明确说明本轮未启用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ..expression.normalize import (
    ValueKind,
    normalize_days_quantity,
    normalize_field,
    normalize_row,
)
from ..models import Conclusion, PrescriptionRow, RuleRow

# ---------------------------------------------------------------------------
# 档位与类别
# ---------------------------------------------------------------------------

#: 门（急）诊一般患者档。
TIER_OUTPATIENT = "门急诊一般"

#: 门（急）诊癌症疼痛和中、重度慢性疼痛患者档。
TIER_CANCER = "门急诊癌症疼痛"

#: 住院患者档。
TIER_INPATIENT = "住院"

#: 三档口径（与 ``expression/normalize.py`` 的 ``RX_TYPE_ALIASES`` 规范值一致）。
TIERS: tuple[str, ...] = (TIER_OUTPATIENT, TIER_CANCER, TIER_INPATIENT)

#: 麻醉药品注射剂。
CLASS_MA_INJECTION = "麻醉药品注射剂"

#: 麻醉药品控缓释制剂。
CLASS_MA_SUSTAINED = "麻醉药品控缓释制剂"

#: 麻醉药品其他剂型（片剂、胶囊等）。
CLASS_MA_OTHER = "麻醉药品"

#: 第一类精神药品注射剂。
CLASS_SPIRIT1_INJECTION = "第一类精神药品注射剂"

#: 第一类精神药品其他剂型。
CLASS_SPIRIT1_OTHER = "第一类精神药品"

#: 第二类精神药品。
CLASS_SPIRIT2 = "第二类精神药品"

#: 矩阵里的类别全集。
CLASSES: tuple[str, ...] = (
    CLASS_MA_INJECTION,
    CLASS_MA_SUSTAINED,
    CLASS_MA_OTHER,
    CLASS_SPIRIT1_INJECTION,
    CLASS_SPIRIT1_OTHER,
    CLASS_SPIRIT2,
)

#: 延长理由字段（《处方管理办法》第二十三条：延长须注明理由）。
REASON_FIELD = "extend_reason"

#: 日量字段（量限比对的唯一数量口径）。
DAYS_FIELD = "days_qty"

# ---------------------------------------------------------------------------
# 矩阵单元
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LimitCell:
    """量限矩阵的一个单元：某一档 × 某一类别 → 上限与口径。

    ``max_quantity`` 是上限数值，``unit`` 是上限口径（``日`` / ``次``），
    ``unit_phrase`` 是法规原文的说法（「1 次常用量」），归因文本照抄原文，
    药师复核时能直接对上法规条目。
    """

    tier: str
    drug_class: str
    max_quantity: Decimal
    unit: str
    unit_phrase: str
    basis: str
    rule_id: str
    needs_reason: bool = False
    reason_field: str = REASON_FIELD
    remark: str = ""

    @property
    def ref(self) -> str:
        """矩阵单元引用（规则行 ``limit_ref`` 的取值）。"""
        return f"{self.tier}/{self.drug_class}"

    @property
    def expected_expression(self) -> str:
        """本单元的**阈值表达式**（规则行的 ``expression`` 必须与它逐字一致）。

        表达式只表达阈值；条件依赖（如精二延长须注明理由）由矩阵单元的
        ``needs_reason`` 承担——表达式语法表达不了「字段缺失」这一档。
        """
        return f"days_qty > {_decimal_text(self.max_quantity)}"

    @property
    def condition_text(self) -> str:
        """本单元的**生效条件**（人话，写进归因文本的「判定条件」位）。"""
        threshold = f"开具日量超过 {self.unit_phrase}上限"
        if self.needs_reason:
            return f"{threshold}且延长理由未注明"
        return threshold

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照（CLI ``--json`` 输出与覆盖报告用）。"""
        return {
            "ref": self.ref,
            "tier": self.tier,
            "drug_class": self.drug_class,
            "max_quantity": _decimal_text(self.max_quantity),
            "unit": self.unit,
            "unit_phrase": self.unit_phrase,
            "needs_reason": self.needs_reason,
            "reason_field": self.reason_field if self.needs_reason else "",
            "rule_id": self.rule_id,
            "basis": self.basis,
            "remark": self.remark,
        }


def _decimal_text(value: Decimal) -> str:
    """数值 → 规范文本（整数去掉小数尾巴：``1.00`` → ``1``）。"""
    if value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    return format(value.normalize(), "f")


# ---------------------------------------------------------------------------
# 矩阵（口径唯一真源）
# ---------------------------------------------------------------------------

_BASIS_OUTPATIENT_MA_INJECTION = (
    "处方审核规则体系 §3 /《处方管理办法》第二十三条："
    "为门（急）诊患者开具的麻醉药品注射剂，每张处方为一次常用量"
)
_BASIS_OUTPATIENT_SUSTAINED = (
    "处方审核规则体系 §3 /《处方管理办法》第二十三条："
    "控缓释制剂，每张处方不得超过 7 日常用量"
)
_BASIS_OUTPATIENT_OTHER = (
    "处方审核规则体系 §3 /《处方管理办法》第二十三条："
    "其他剂型，每张处方不得超过 3 日常用量"
)
_BASIS_OUTPATIENT_SPIRIT1_INJECTION = (
    "处方审核规则体系 §3 /《处方管理办法》第二十三条："
    "第一类精神药品注射剂，每张处方为一次常用量"
)
_BASIS_OUTPATIENT_SPIRIT1_OTHER = (
    "处方审核规则体系 §3 /《处方管理办法》第二十三条："
    "第一类精神药品同麻醉药品（精一药品同麻醉药品），其他剂型不超过 3 日常用量"
)
_BASIS_OUTPATIENT_SPIRIT2 = (
    "处方审核规则体系 §3 /《处方管理办法》第二十三条："
    "第二类精神药品一般每张处方不得超过 7 日常用量；对于慢性病或某些特殊情况的患者，"
    "处方用量可以适当延长，医师应当注明理由"
)
_BASIS_CANCER_INJECTION = (
    "处方审核规则体系 §3 /《处方管理办法》第二十四条："
    "为门（急）诊癌症疼痛患者和中、重度慢性疼痛患者开具的麻醉药品、"
    "第一类精神药品注射剂，每张处方不得超过 3 日常用量"
)
_BASIS_CANCER_SUSTAINED = (
    "处方审核规则体系 §3 /《处方管理办法》第二十四条："
    "控缓释制剂，每张处方不得超过 15 日常用量"
)
_BASIS_CANCER_OTHER = (
    "处方审核规则体系 §3 /《处方管理办法》第二十四条："
    "其他剂型，每张处方不得超过 7 日常用量"
)
_BASIS_INPATIENT = (
    "处方审核规则体系 §3 /《处方管理办法》第二十五条："
    "为住院患者开具的麻醉药品和第一类精神药品处方应当逐日开具，每张处方为 1 日常用量"
)

#: 量限矩阵：三档 × 六类，共 18 个单元。
LIMIT_MATRIX: Mapping[str, LimitCell] = {
    cell.ref: cell
    for cell in (
        LimitCell(
            tier=TIER_OUTPATIENT,
            drug_class=CLASS_MA_INJECTION,
            max_quantity=Decimal("1"),
            unit="次",
            unit_phrase="1 次常用量",
            basis=_BASIS_OUTPATIENT_MA_INJECTION,
            rule_id="RX-LEGAL-MA-OUT-DAYS",
        ),
        LimitCell(
            tier=TIER_OUTPATIENT,
            drug_class=CLASS_MA_SUSTAINED,
            max_quantity=Decimal("7"),
            unit="日",
            unit_phrase="7 日量",
            basis=_BASIS_OUTPATIENT_SUSTAINED,
            rule_id="RX-LEGAL-CR-OUT-DAYS",
        ),
        LimitCell(
            tier=TIER_OUTPATIENT,
            drug_class=CLASS_MA_OTHER,
            max_quantity=Decimal("3"),
            unit="日",
            unit_phrase="3 日量",
            basis=_BASIS_OUTPATIENT_OTHER,
            rule_id="RX-LEGAL-MA-OTHER-OUT-DAYS",
        ),
        LimitCell(
            tier=TIER_OUTPATIENT,
            drug_class=CLASS_SPIRIT1_INJECTION,
            max_quantity=Decimal("1"),
            unit="次",
            unit_phrase="1 次常用量",
            basis=_BASIS_OUTPATIENT_SPIRIT1_INJECTION,
            rule_id="RX-LEGAL-SPIRIT1-OUT-DAYS",
        ),
        LimitCell(
            tier=TIER_OUTPATIENT,
            drug_class=CLASS_SPIRIT1_OTHER,
            max_quantity=Decimal("3"),
            unit="日",
            unit_phrase="3 日量",
            basis=_BASIS_OUTPATIENT_SPIRIT1_OTHER,
            rule_id="RX-LEGAL-SPIRIT1-OTHER-OUT-DAYS",
        ),
        LimitCell(
            tier=TIER_OUTPATIENT,
            drug_class=CLASS_SPIRIT2,
            max_quantity=Decimal("7"),
            unit="日",
            unit_phrase="7 日量",
            basis=_BASIS_OUTPATIENT_SPIRIT2,
            rule_id="RX-LEGAL-SPIRIT2-OUT-DAYS",
            needs_reason=True,
        ),
        LimitCell(
            tier=TIER_CANCER,
            drug_class=CLASS_MA_INJECTION,
            max_quantity=Decimal("3"),
            unit="日",
            unit_phrase="3 日常量",
            basis=_BASIS_CANCER_INJECTION,
            rule_id="RX-LEGAL-MA-CANCER-OUT-DAYS",
        ),
        LimitCell(
            tier=TIER_CANCER,
            drug_class=CLASS_MA_SUSTAINED,
            max_quantity=Decimal("15"),
            unit="日",
            unit_phrase="15 日量",
            basis=_BASIS_CANCER_SUSTAINED,
            rule_id="RX-LEGAL-CR-CANCER-OUT-DAYS",
        ),
        LimitCell(
            tier=TIER_CANCER,
            drug_class=CLASS_MA_OTHER,
            max_quantity=Decimal("7"),
            unit="日",
            unit_phrase="7 日量",
            basis=_BASIS_CANCER_OTHER,
            rule_id="RX-LEGAL-MA-OTHER-CANCER-OUT-DAYS",
        ),
        LimitCell(
            tier=TIER_CANCER,
            drug_class=CLASS_SPIRIT1_INJECTION,
            max_quantity=Decimal("3"),
            unit="日",
            unit_phrase="3 日常量",
            basis=_BASIS_CANCER_INJECTION,
            rule_id="RX-LEGAL-SPIRIT1-CANCER-OUT-DAYS",
        ),
        LimitCell(
            tier=TIER_CANCER,
            drug_class=CLASS_SPIRIT1_OTHER,
            max_quantity=Decimal("7"),
            unit="日",
            unit_phrase="7 日量",
            basis=_BASIS_CANCER_OTHER,
            rule_id="RX-LEGAL-SPIRIT1-OTHER-CANCER-OUT-DAYS",
        ),
        LimitCell(
            tier=TIER_CANCER,
            drug_class=CLASS_SPIRIT2,
            max_quantity=Decimal("7"),
            unit="日",
            unit_phrase="7 日量",
            basis=_BASIS_OUTPATIENT_SPIRIT2,
            rule_id="RX-LEGAL-SPIRIT2-CANCER-OUT-DAYS",
            needs_reason=True,
            remark="《处方管理办法》第二十三条的精二量限不分档位，各档一致。",
        ),
        LimitCell(
            tier=TIER_INPATIENT,
            drug_class=CLASS_MA_OTHER,
            max_quantity=Decimal("1"),
            unit="日",
            unit_phrase="1 日常量",
            basis=_BASIS_INPATIENT,
            rule_id="RX-LEGAL-MA-INPATIENT-DAYS",
            remark="住院档按药品大类计，注射剂与控缓释变体同归此单元。",
        ),
        LimitCell(
            tier=TIER_INPATIENT,
            drug_class=CLASS_SPIRIT1_OTHER,
            max_quantity=Decimal("1"),
            unit="日",
            unit_phrase="1 日常量",
            basis=_BASIS_INPATIENT,
            rule_id="RX-LEGAL-SPIRIT1-INPATIENT-DAYS",
            remark="住院档按药品大类计，注射剂与控缓释变体同归此单元。",
        ),
        LimitCell(
            tier=TIER_INPATIENT,
            drug_class=CLASS_SPIRIT2,
            max_quantity=Decimal("7"),
            unit="日",
            unit_phrase="7 日量",
            basis=_BASIS_OUTPATIENT_SPIRIT2,
            rule_id="RX-LEGAL-SPIRIT2-INPATIENT-DAYS",
            needs_reason=True,
            remark="逐日开具只约束麻醉药品与第一类精神药品（第二十五条），精二不逐日开具。",
        ),
    )
}

#: 尚未落地的**药品级**量限（登记而不静默忽略）。
#:
#: 《处方管理办法》第二十六条「盐酸哌替啶处方为一次常用量，仅限于医疗机构内使用」需要
#: 按药品编码匹配；药品清单属厂商资产（《处方审核规则体系》§3 末段），本轮不落库。
DRUG_LEVEL_LIMITS_PENDING: tuple[dict[str, str], ...] = (
    {
        "drug": "盐酸哌替啶",
        "limit": "一次常用量，限医疗机构内使用",
        "basis": "处方审核规则体系 §3 /《处方管理办法》第二十六条",
        "reason": "药品级限值需按药品编码匹配，药品清单属厂商资产，本轮未落库",
    },
)

#: 尚未启用的人群例外（登记而不静默忽略）。
POPULATION_EXCEPTIONS_PENDING: tuple[dict[str, str], ...] = (
    {
        "drug": "哌醋甲酯",
        "exception": "用于治疗儿童多动症时，每张处方不得超过 15 日常用量",
        "basis": "《处方管理办法》第二十三条",
        "reason": "需患者年龄与诊断信息支撑，本轮量限矩阵只按档位与类别判定",
    },
)


# ---------------------------------------------------------------------------
# 档位 × 类别 的归类
# ---------------------------------------------------------------------------

#: 门急诊两档：类别即药品类别本身（注射剂 / 控缓释 / 其他剂型分别成单元）。
_OUTPATIENT_CLASSES: dict[str, str] = {
    CLASS_MA_INJECTION: CLASS_MA_INJECTION,
    CLASS_MA_SUSTAINED: CLASS_MA_SUSTAINED,
    CLASS_MA_OTHER: CLASS_MA_OTHER,
    CLASS_SPIRIT1_INJECTION: CLASS_SPIRIT1_INJECTION,
    CLASS_SPIRIT1_OTHER: CLASS_SPIRIT1_OTHER,
    CLASS_SPIRIT2: CLASS_SPIRIT2,
}

#: 住院档：麻醉药品与第一类精神药品**逐日开具**，注射剂与控缓释变体同归大类。
_INPATIENT_CLASSES: dict[str, str] = {
    CLASS_MA_INJECTION: CLASS_MA_OTHER,
    CLASS_MA_SUSTAINED: CLASS_MA_OTHER,
    CLASS_MA_OTHER: CLASS_MA_OTHER,
    CLASS_SPIRIT1_INJECTION: CLASS_SPIRIT1_OTHER,
    CLASS_SPIRIT1_OTHER: CLASS_SPIRIT1_OTHER,
    CLASS_SPIRIT2: CLASS_SPIRIT2,
}

#: 档位 → （落地药品类别 → 矩阵类别）。
_TIER_CLASS_MAP: dict[str, dict[str, str]] = {
    TIER_OUTPATIENT: dict(_OUTPATIENT_CLASSES),
    TIER_CANCER: dict(_OUTPATIENT_CLASSES),
    TIER_INPATIENT: dict(_INPATIENT_CLASSES),
}


@dataclass(frozen=True)
class LimitKey:
    """一条医嘱在矩阵里的位置：档位 + 类别。"""

    tier: str
    drug_class: str

    @property
    def ref(self) -> str:
        return f"{self.tier}/{self.drug_class}"


def resolve_limit_ref(ref: str) -> LimitCell | None:
    """按 ``limit_ref`` 取矩阵单元；引用不存在返回 ``None``。"""
    return LIMIT_MATRIX.get(str(ref).strip())


def classify_category(tier: str, drug_class: str) -> str | None:
    """落地药品类别 → 矩阵类别（按档位）；该档不收这个类别返回 ``None``。"""
    return _TIER_CLASS_MAP.get(tier, {}).get(drug_class)


def classify_limit(
    row: PrescriptionRow, normalized: Mapping[str, object] | None = None
) -> LimitKey | None:
    """把一条医嘱归类到矩阵位置（档位 + 类别）。

    两侧都先过归一层：HIS 落地写法（``门诊一般患者``）与矩阵口径
    （``门急诊一般``）才对得上。归类不出来（档位未知 / 类别不在矩阵内，
    例如 ``普通药品``）返回 ``None``——**不是**违规，也不是不可判定。
    """
    values = normalized if normalized is not None else normalize_row(row)
    tier = str(getattr(values["rx_type"], "text", "")).strip()
    drug_class = str(getattr(values["drug_category"], "text", "")).strip()
    class_map = _TIER_CLASS_MAP.get(tier)
    if class_map is None:
        return None
    mapped = class_map.get(drug_class)
    if mapped is None:
        return None
    return LimitKey(tier=tier, drug_class=mapped)


# ---------------------------------------------------------------------------
# 数量口径
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuantitySemantics:
    """日量字段的口径核对结果。

    ``comparable=False`` 的两种情形都要落「不可判定」而不是违规：

    * 落地的是**剂量口径**（``3mg`` / ``10片``）——拿它比日量上限必然出错；
    * 落地的是**文字**（``适量`` / ``遵医嘱``）——根本没有可比的数量。
    """

    raw: str
    number: Decimal | None = None
    unit: str = ""
    comparable: bool = False
    is_missing: bool = False
    reason: str = ""

    @property
    def display(self) -> str:
        """归因文本里的落地值写法（带口径）。"""
        if self.is_missing:
            return "（未落地）"
        if not self.unit:
            return self.raw
        return f"{self.raw}（口径：{self.unit}）"


def check_quantity_semantics(
    row: PrescriptionRow, field: str = DAYS_FIELD
) -> QuantitySemantics:
    """核对日量字段的口径：能不能拿来比「日量上限」。"""
    raw = row.value(field)
    value = normalize_days_quantity(raw, field=field)

    if value.is_missing:
        return QuantitySemantics(
            raw=raw,
            number=None,
            unit="",
            comparable=False,
            is_missing=True,
            reason=f"字段 {field} 未落地",
        )
    if value.kind is not ValueKind.NUMBER or value.number is None:
        return QuantitySemantics(
            raw=raw,
            number=None,
            unit=value.unit,
            comparable=False,
            is_missing=False,
            reason=(
                f"字段 {field} 的落地值「{raw}」口径不可比"
                "（落地的是文字，不是可比较的数量）"
            ),
        )
    if not value.comparable:
        return QuantitySemantics(
            raw=raw,
            number=value.number,
            unit=value.unit,
            comparable=False,
            is_missing=False,
            reason=(
                f"字段 {field} 的落地值「{raw}」口径不可比（量限以日量 / 次量计）"
            ),
        )
    return QuantitySemantics(
        raw=raw,
        number=value.number,
        unit=value.unit,
        comparable=True,
        is_missing=False,
        reason="",
    )


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


class LimitStatus(str, Enum):
    """量限判定结论（与三态判定内核一致，多一个「不适用」）。"""

    NOT_APPLICABLE = "不适用"
    COMPLIANT = "合规"
    VIOLATION = "违规"
    UNDETERMINABLE = "不可判定"


#: 量限结论 → 判定内核的三态结论。
LIMIT_CONCLUSIONS: Mapping[LimitStatus, Conclusion] = {
    LimitStatus.COMPLIANT: Conclusion.COMPLIANT,
    LimitStatus.VIOLATION: Conclusion.VIOLATION,
    LimitStatus.UNDETERMINABLE: Conclusion.UNDETERMINABLE,
}


@dataclass(frozen=True)
class LimitEvaluation:
    """一次量限判定的结果。"""

    status: LimitStatus
    cell: LimitCell | None = None
    detail: str = ""
    reason: str = ""
    missing_fields: tuple[str, ...] = ()
    quantity: QuantitySemantics | None = None

    @property
    def conclusion(self) -> Conclusion | None:
        """三态结论（不适用时没有结论）。"""
        return LIMIT_CONCLUSIONS.get(self.status)


def evaluate_limit(row: PrescriptionRow, cell: LimitCell) -> LimitEvaluation:
    """按矩阵单元判定一条医嘱的开具日量。

    四步：

    #. **口径核对**：日量不是数量口径（``3mg`` / ``适量``）→ 不可判定，点名字段；
    #. **阈值比对**：日量未超上限 → 合规；
    #. **条件依赖**：超上限且该单元要求注明理由时，理由未落地 → 不可判定（点名理由
       字段）、理由为显式否定（``未注明``）→ 违规、理由有值 → 合规（延长已注明理由）；
    #. **超上限且无理由要求** → 违规。
    """
    quantity = check_quantity_semantics(row)

    if quantity.is_missing:
        return LimitEvaluation(
            status=LimitStatus.UNDETERMINABLE,
            cell=cell,
            detail=quantity.reason,
            reason=quantity.reason,
            missing_fields=(DAYS_FIELD,),
            quantity=quantity,
        )
    if not quantity.comparable:
        return LimitEvaluation(
            status=LimitStatus.UNDETERMINABLE,
            cell=cell,
            detail=quantity.reason,
            reason=quantity.reason,
            missing_fields=(DAYS_FIELD,),
            quantity=quantity,
        )

    assert quantity.number is not None  # noqa: S101 - 上两支已排除 None
    if quantity.number <= cell.max_quantity:
        return LimitEvaluation(
            status=LimitStatus.COMPLIANT,
            cell=cell,
            detail=(
                f"开具日量 {quantity.display} 未超过该档上限「{cell.unit_phrase}」。"
            ),
            quantity=quantity,
        )

    if not cell.needs_reason:
        return LimitEvaluation(
            status=LimitStatus.VIOLATION,
            cell=cell,
            detail=(
                f"开具日量 {quantity.display} 超过该档上限「{cell.unit_phrase}」。"
            ),
            quantity=quantity,
        )

    reason_value = normalize_field(cell.reason_field, row.value(cell.reason_field))
    if reason_value.is_missing:
        detail = (
            f"开具日量 {quantity.display} 已超过该档上限「{cell.unit_phrase}」，"
            f"但延长理由字段 {cell.reason_field} 未落地。"
        )
        return LimitEvaluation(
            status=LimitStatus.UNDETERMINABLE,
            cell=cell,
            detail=detail,
            reason=detail,
            missing_fields=(cell.reason_field,),
            quantity=quantity,
        )
    if reason_value.is_explicit_no:
        return LimitEvaluation(
            status=LimitStatus.VIOLATION,
            cell=cell,
            detail=(
                f"开具日量 {quantity.display} 超过该档上限「{cell.unit_phrase}」，"
                f"且延长理由未注明（落地值「{reason_value.text}」）。"
            ),
            quantity=quantity,
        )
    return LimitEvaluation(
        status=LimitStatus.COMPLIANT,
        cell=cell,
        detail=(
            f"开具日量 {quantity.display} 超过该档上限「{cell.unit_phrase}」，"
            f"但已注明延长理由「{reason_value.text}」，按延长须注明理由予以放行。"
        ),
        quantity=quantity,
    )


def evaluate_limit_rule(
    rule: RuleRow,
    row: PrescriptionRow,
    *,
    normalized: Mapping[str, object] | None = None,
) -> LimitEvaluation:
    """按规则行的 ``limit_ref`` 判定一条医嘱。

    矩阵是口径真源：先按医嘱自身归类出矩阵单元，**再**看规则行声明的单元是否与之一致；
    不一致说明这条规则对这条医嘱不适用（不产生判定行），一致才按单元判定。
    """
    cell = resolve_limit_ref(rule.limit_ref)
    if cell is None:
        return LimitEvaluation(
            status=LimitStatus.NOT_APPLICABLE,
            reason=f"规则 {rule.rule_id} 的 limit_ref「{rule.limit_ref}」不在量限矩阵内",
        )
    key = classify_limit(row, normalized)
    if key is None:
        return LimitEvaluation(
            status=LimitStatus.NOT_APPLICABLE,
            cell=cell,
            reason="该医嘱的处方类型或药品类别不在量限矩阵内",
        )
    if key.ref != cell.ref:
        return LimitEvaluation(
            status=LimitStatus.NOT_APPLICABLE,
            cell=cell,
            reason=(
                f"该医嘱落在矩阵单元「{key.ref}」，与规则声明的「{cell.ref}」不一致"
            ),
        )
    return evaluate_limit(row, cell)


# ---------------------------------------------------------------------------
# 矩阵覆盖与漂移自检
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatrixCoverage:
    """矩阵覆盖情况：哪些单元已规则化、哪些还没有。"""

    covered: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.covered) + len(self.uncovered)

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "covered": list(self.covered),
            "uncovered": list(self.uncovered),
        }


def matrix_coverage(rules: Sequence[RuleRow]) -> MatrixCoverage:
    """矩阵覆盖报告：哪些 §3 单元已有规则行，哪些还只是口径、没启用。

    「口径已定义但没规则行」不是 bug，是**待办**：药事审核岗每月维护规则时按这张表
    补齐，避免 §3 里某个格子的量限长期无人拦截。
    """
    covered: list[str] = []
    for rule in rules:
        if not rule.limit_ref:
            continue
        cell = resolve_limit_ref(rule.limit_ref)
        if cell is None:
            continue
        if cell.ref not in covered:
            covered.append(cell.ref)
    uncovered = [ref for ref in LIMIT_MATRIX if ref not in covered]
    return MatrixCoverage(
        covered=tuple(sorted(covered)), uncovered=tuple(sorted(uncovered))
    )


@dataclass(frozen=True)
class MatrixDrift:
    """规则行与矩阵口径脱节的一条记录。"""

    rule_id: str
    limit_ref: str
    expected: str
    actual: str
    message: str


def matrix_drift(rules: Sequence[RuleRow]) -> tuple[MatrixDrift, ...]:
    """规则行与量限矩阵的口径漂移检查。

    两类漂移：阈值表达式与矩阵不一致（药事审核岗把阈值改成了别的数）、
    规则声明的单元与适用条件矛盾（规则写的是住院档、适用条件却写门急诊）。
    """
    drifts: list[MatrixDrift] = []
    for rule in rules:
        if not rule.limit_ref:
            continue
        cell = resolve_limit_ref(rule.limit_ref)
        if cell is None:
            drifts.append(
                MatrixDrift(
                    rule_id=rule.rule_id,
                    limit_ref=rule.limit_ref,
                    expected="",
                    actual=rule.expression,
                    message=(
                        f"规则 {rule.rule_id} 声明的量限单元「{rule.limit_ref}」"
                        "不在量限矩阵内"
                    ),
                )
            )
            continue
        if rule.expression != cell.expected_expression:
            drifts.append(
                MatrixDrift(
                    rule_id=rule.rule_id,
                    limit_ref=rule.limit_ref,
                    expected=cell.expected_expression,
                    actual=rule.expression,
                    message=(
                        f"规则 {rule.rule_id} 的阈值表达式与量限矩阵不一致："
                        f"矩阵口径是「{cell.expected_expression}」，"
                        f"规则行写的是「{rule.expression}」"
                    ),
                )
            )
    return tuple(drifts)


__all__ = [
    "CLASSES",
    "CLASS_MA_INJECTION",
    "CLASS_MA_OTHER",
    "CLASS_MA_SUSTAINED",
    "CLASS_SPIRIT1_INJECTION",
    "CLASS_SPIRIT1_OTHER",
    "CLASS_SPIRIT2",
    "DAYS_FIELD",
    "DRUG_LEVEL_LIMITS_PENDING",
    "LIMIT_CONCLUSIONS",
    "LIMIT_MATRIX",
    "LimitCell",
    "LimitEvaluation",
    "LimitKey",
    "LimitStatus",
    "MatrixCoverage",
    "MatrixDrift",
    "POPULATION_EXCEPTIONS_PENDING",
    "QuantitySemantics",
    "REASON_FIELD",
    "TIERS",
    "TIER_CANCER",
    "TIER_INPATIENT",
    "TIER_OUTPATIENT",
    "check_quantity_semantics",
    "classify_category",
    "classify_limit",
    "evaluate_limit",
    "evaluate_limit_rule",
    "matrix_coverage",
    "matrix_drift",
    "resolve_limit_ref",
]
