"""归因文本组装：判定结论 → 一句可核对、可复算、可外发的说明。

**对应源产品能力**：源仓库 ``contrib/database/field_rules/registries.py`` 的
``RowRuleValidity`` / ``FieldRuleValidity``——源产品用 ``error_text`` 作行级规则的
**归因位**：判定不通过时给出一条错误说明，行校验结果与说明一一对应。
本轮把这条归因位扩成**三态模板**：

* **合规**：判定条件未命中；
* **违规**：命中了哪条判定、命中了什么；
* **不可判定**：缺哪个字段、卡在哪条规则。

每条归因文本都必须同时给出 **规则号 + 规则版本 + 依据出处 + 缺失字段名 + 判定依据**，
药师逐批复核、医务质控抽查与飞检自证时看一句话就能复算，不必再去翻日志。
三种结论各有固定句式（:data:`CONCLUSION_TEMPLATES`），回归可逐字比对。

**隐私约束**：归因文本只允许出现 ``patient_ref`` 这一脱敏引用号；
患者姓名、身份证号、手机号、住址、联系人与开方医师姓名一律不得进入归因文本
（台账与出站回写同样只带脱敏标识，见 spec.md §2.3）。
本模块在组装末尾强制执行该约束：命中禁止字段的取值一律掩码，
并提供 :func:`detect_pii_leaks` / :func:`assert_privacy_safe` 供审计与测试复核。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from ..models import Conclusion, RuleRow

# ---------------------------------------------------------------------------
# 归因文本模板
# ---------------------------------------------------------------------------

#: 合规句式：条件未命中 + 依据出处（``detail`` 为空时该格不占文字）。
COMPLIANT_TEMPLATE = (
    "规则 {rule_id}（版本 {rule_version}）判定：合规。"
    "{detail}判定条件「{expression}」未命中。依据：{basis}"
)

#: 违规句式：判定依据 + 依据出处。缺必填内容的违规类规则把缺项写进 ``detail``。
VIOLATION_TEMPLATE = (
    "规则 {rule_id}（版本 {rule_version}）判定：违规。{detail}依据：{basis}"
)

#: 不可判定句式：点名缺失字段 + 卡点说明 + 依据出处。
UNDETERMINABLE_TEMPLATE = (
    "规则 {rule_id}（版本 {rule_version}）判定：不可判定。"
    "缺少字段 {missing_fields}，无法完成判定（{detail}）。依据：{basis}"
)

#: 不可判定句式（**字段齐备但仍判不了**）：知识库缺口等情形——
#: 没有字段可点，但系统确实判不了，句式里不能写「缺少字段 相关字段」
#: （那句话会让人以为是少了一列接口字段，去找信息科要列，其实要接知识库）。
UNDETERMINABLE_NO_FIELD_TEMPLATE = (
    "规则 {rule_id}（版本 {rule_version}）判定：不可判定。"
    "字段已齐备，但系统仍无法完成判定（{detail}）。依据：{basis}"
)

#: 三态结论 → 归因文本模板（句式固定，回归可逐字比对）。
CONCLUSION_TEMPLATES: Mapping[Conclusion, str] = {
    Conclusion.COMPLIANT: COMPLIANT_TEMPLATE,
    Conclusion.VIOLATION: VIOLATION_TEMPLATE,
    Conclusion.UNDETERMINABLE: UNDETERMINABLE_TEMPLATE,
}

#: 缺失字段名的分隔符（台账里用 ``|``，此处用顿号，句子读起来更顺）。
MISSING_FIELDS_JOINER = "、"

#: 缺失字段为空时的兜底措辞（正常路径不会走到）。
MISSING_FIELDS_FALLBACK = "相关字段"

# ---------------------------------------------------------------------------
# 隐私约束
# ---------------------------------------------------------------------------

#: 归因文本中**禁止**出现的患者可识别明细字段（英文口径字段名）。
#:
#: 含开方医师姓名：归因文本只描述"哪条规则、缺哪个字段"，
#: 医师姓名属于医嘱业务字段，不进归因文本。
FORBIDDEN_PII_FIELDS: tuple[str, ...] = (
    "patient_name",
    "name",
    "patient_id",
    "id_card",
    "id_card_no",
    "id_no",
    "id_number",
    "identity_no",
    "phone",
    "mobile",
    "tel",
    "telephone",
    "address",
    "home_address",
    "email",
    "guardian_name",
    "contact_name",
    "contact_phone",
    "emergency_contact",
    "prescriber_name",
)

#: 同义的中文表头名（HIS 落地文件常见写法），同样禁止进入归因文本。
FORBIDDEN_PII_FIELD_ALIASES: tuple[str, ...] = (
    "患者姓名",
    "姓名",
    "患者身份证号",
    "身份证号",
    "身份证件号码",
    "证件号码",
    "联系电话",
    "手机号",
    "家庭住址",
    "住址",
    "联系人",
    "紧急联系人",
    "开方医师姓名",
)

#: 归因文本中**允许**出现的患者标识字段：只有这一支脱敏引用号。
ALLOWED_PII_FIELDS: tuple[str, ...] = ("patient_ref",)

#: 掩码替换文本（被判定为禁止外泄的取值一律替换成它）。
MASK_TEXT = "〔已隐去〕"

#: 短于该长度的取值不掩码：避免把"是""无"这类通用字词误伤成乱码。
_MIN_MASK_LENGTH = 2

_FORBIDDEN_KEYS = frozenset(
    {name.lower() for name in FORBIDDEN_PII_FIELDS}
    | {name.lower() for name in FORBIDDEN_PII_FIELD_ALIASES}
)


class EvidenceError(RuntimeError):
    """归因文本组装的显式失败。"""


class EvidencePrivacyError(EvidenceError):
    """归因文本仍带患者可识别明细——属合规事故，必须显式失败而不是静默通过。"""


# ---------------------------------------------------------------------------
# 字段集合读取
# ---------------------------------------------------------------------------


def _iter_fields(fields: object) -> Iterator[tuple[str, str]]:
    """把字段集合（映射或 dataclass 实例）拉平成 ``(字段名, 取值)`` 序列。"""
    if fields is None:
        return
    if isinstance(fields, Mapping):
        items = list(fields.items())
    else:
        items = list(vars(fields).items()) if hasattr(fields, "__dict__") else []
    for name, value in items:
        if value is None:
            yield str(name), ""
        else:
            yield str(name), str(value)


def forbidden_values(fields: object) -> dict[str, str]:
    """挑出禁止外泄的字段取值（字段名 → 取值）。

    只取长度不小于 :data:`_MIN_MASK_LENGTH` 的取值，避免短通用字词被误判。
    """
    values: dict[str, str] = {}
    for name, value in _iter_fields(fields):
        if name.lower() not in _FORBIDDEN_KEYS:
            continue
        text = value.strip()
        if len(text) < _MIN_MASK_LENGTH:
            continue
        values[name] = text
    return values


def detect_pii_leaks(text: str, fields: object) -> list[str]:
    """返回归因文本中仍然出现的禁止字段名（空列表 = 未外泄患者可识别明细）。

    ``patient_ref`` 在 :data:`ALLOWED_PII_FIELDS` 里，属于允许出现的一支，
    不计入外泄。
    """
    leaks: list[str] = []
    for name, value in forbidden_values(fields).items():
        if name.lower() in {item.lower() for item in ALLOWED_PII_FIELDS}:
            continue
        if value and value in text:
            leaks.append(name)
    return leaks


def assert_privacy_safe(text: str, fields: object) -> None:
    """归因文本含患者可识别明细时抛 :class:`EvidencePrivacyError`。"""
    leaks = detect_pii_leaks(text, fields)
    if leaks:
        raise EvidencePrivacyError(
            "归因文本带出患者可识别明细："
            + "、".join(leaks)
            + "（归因文本只允许出现 patient_ref 这一脱敏引用号）"
        )


def mask_forbidden_values(text: str, fields: object) -> str:
    """把归因文本里的禁止外泄取值掩码（替换成 :data:`MASK_TEXT`）。"""
    masked = text
    for value in forbidden_values(fields).values():
        if value and value in masked:
            masked = masked.replace(value, MASK_TEXT)
    return masked


# ---------------------------------------------------------------------------
# 归因要素
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """一条判定结论的归因要素（组装前的结构化形态）。

    字段与 :class:`~med_rule_fieldmap.models.JudgementRow` 一一对应：
    台账、审计与回放都从这组要素取文本，保证同一结论在各处口径一致。
    """

    conclusion: Conclusion
    rule_id: str
    rule_version: str
    basis: str
    expression: str = ""
    detail: str = ""
    missing_fields: tuple[str, ...] = ()

    @property
    def template(self) -> str:
        """该结论对应的固定句式模板。

        「不可判定」分两句式：点名缺失字段的（字段缺口）与**字段齐备但仍判不了**的
        （知识库缺口）——后者没有字段可点，不能套「缺少字段 …」。
        """
        if self.conclusion is Conclusion.UNDETERMINABLE and not self.missing_fields:
            return UNDETERMINABLE_NO_FIELD_TEMPLATE
        return CONCLUSION_TEMPLATES[self.conclusion]

    @property
    def missing_fields_text(self) -> str:
        """缺失字段名的句子口径（顿号分隔）。"""
        names = MISSING_FIELDS_JOINER.join(self.missing_fields)
        return names or MISSING_FIELDS_FALLBACK

    def render(self) -> str:
        """按固定句式渲染归因文本（不做隐私掩码）。"""
        return self.template.format(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            expression=self.expression,
            detail=self.detail,
            basis=self.basis,
            missing_fields=self.missing_fields_text,
        )


def build_evidence(
    conclusion: Conclusion,
    rule: RuleRow,
    *,
    detail: str = "",
    missing_fields: Sequence[str] = (),
    condition: str = "",
) -> Evidence:
    """按规则行与结论组装归因要素（结构化，未渲染）。

    ``condition`` 可覆盖归因文本里「判定条件「…」」那一格的措辞。量限规则用它写
    人话条件（如「开具日量超过 7 日量上限且延长理由未注明」），避免拿阈值表达式
    去描述一条带条件依赖的规则——那样文案与实际判定会对不上。
    """
    return Evidence(
        conclusion=conclusion,
        rule_id=rule.rule_id,
        rule_version=rule.version,
        basis=rule.basis,
        expression=condition.strip() or rule.expression,
        detail=detail,
        missing_fields=tuple(missing_fields),
    )


def render_reason(evidence: Evidence, fields: object = None) -> str:
    """渲染归因文本并执行隐私约束。

    ``fields`` 传入医嘱行（:class:`~med_rule_fieldmap.models.PrescriptionRow`）
    或字段映射时，禁止外泄的患者可识别明细取值会被掩码；掩码后仍检出外泄
    则抛 :class:`EvidencePrivacyError`（宁可显式失败，也不让患者明细流出台账）。
    """
    text = evidence.render()
    if fields is None:
        return text
    text = mask_forbidden_values(text, fields)
    assert_privacy_safe(text, fields)
    return text


def build_reason(
    conclusion: Conclusion,
    rule: RuleRow,
    *,
    detail: str = "",
    missing_fields: Sequence[str] = (),
    fields: object = None,
    condition: str = "",
) -> str:
    """一条命令组装归因文本（组装 + 渲染 + 隐私约束）。

    三种结论各有固定句式，句中同时带出规则号、规则版本、依据出处；
    不可判定句式额外点名缺失字段名；``condition`` 可覆盖「判定条件」那一格的措辞。
    """
    evidence = build_evidence(
        conclusion,
        rule,
        detail=detail,
        missing_fields=missing_fields,
        condition=condition,
    )
    return render_reason(evidence, fields)


__all__ = [
    "ALLOWED_PII_FIELDS",
    "COMPLIANT_TEMPLATE",
    "CONCLUSION_TEMPLATES",
    "Evidence",
    "EvidenceError",
    "EvidencePrivacyError",
    "FORBIDDEN_PII_FIELDS",
    "FORBIDDEN_PII_FIELD_ALIASES",
    "MASK_TEXT",
    "MISSING_FIELDS_FALLBACK",
    "MISSING_FIELDS_JOINER",
    "UNDETERMINABLE_TEMPLATE",
    "UNDETERMINABLE_NO_FIELD_TEMPLATE",
    "VIOLATION_TEMPLATE",
    "assert_privacy_safe",
    "build_evidence",
    "build_reason",
    "detect_pii_leaks",
    "forbidden_values",
    "mask_forbidden_values",
    "render_reason",
]
