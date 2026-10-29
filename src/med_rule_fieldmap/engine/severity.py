"""严重度解析 + 点评分级打标（《处方审核规则体系》§4 / §5）。

本模块回答两个问题，都不改动判定结论本身：

#. **这条问题有多严重**（严重度）——取值**直接来自规则行的 ``severity`` 字段**：
   严重度是药事审核岗的口径，不是程序该替人决定的事
   （``rules/schema.py`` 的 ``require_severity=True`` 即这条纪律的落点：规则行没写
   严重度即拒载入）。只有当规则行确实没写时，才按**审核层次**回落，并在
   **归因文本里注明回落来源**——回落值必须看得出来，不能让药师以为这是审核岗
   给的严重度。

#. **这条问题属于哪一类点评**（点评分级）——§5 引擎化要点：「点评分级
   （不适宜处方 / 不规范处方 / 超常处方三类判定）对应不同处置流」。三类互斥且
   穷尽，**只打标不改结论**：打标是给处置流分流用的标签，不是第四种结论。

**回落口径（写在这里，不藏）**

* **合法性** → **严重不合理用药**：§1 合法性审核的对象是《药品管理法》
  《处方管理办法》与麻精药品管理规定这类**强制性规定**；§4 对这类问题给的是
  「拒绝调配，告知医师并记录上报」，与「严重不合理用药 / 用药错误」同一处置档。
* **规范性** → **不规范处方**：§1 规范性审核的问题（前记 / 后记缺项、用法用量
  含糊字句、儿童实际年龄口径）在点评口径里就是「不规范处方」。
* **适宜性** → **不合理用药**：§1 适宜性审核七项（诊断相符性、剂量用法正确性、
  重复给药等）属用药本身不合理。

层次未声明 / 不认识时落 :data:`SEVERITY_UNKNOWN_LAYER`，同样注明回落来源——
**宁可给一个看得出来的回落值，也不静默留空**：严重度留空会让台账与点评报表
出现「看不出轻重」的空洞格子。

**点评分级归类口径（写在这里，不藏）**

* **超常处方** —— 超常处方规则（``engine/supernormal.py`` 认的那一条：跨行计数
  + 提示限制处方权）。判据是规则行**显式声明的依赖**（``prescriber_id`` 与
  ``extend_reason``），不是按规则号前缀猜。
* **不规范处方** —— 规范性审核层的规则（书写与格式类）。
* **不适宜处方** —— 其余（合法性层与适宜性层的其余规则）：用药本身不合理，
  或违反《处方管理办法》等强制性规定。

**只有三类**：§5 明确给了三类，本轮不新增第四类；合法性违规虽属强制性问题，
在点评口径里仍归入「不适宜处方」。加一类就偏离了法规口径，而点评报表是按这三类
分流处置的。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from ..models import RuleLayer, RuleRow
from .supernormal import is_supernormal_rule

# ---------------------------------------------------------------------------
# 严重度口径
# ---------------------------------------------------------------------------

#: 严重不合理用药（§4：严重不合理用药或用药错误 → 拒绝调配、告知医师并记录上报）。
SEVERITY_SEVERE_UNREASONABLE = "严重不合理用药"

#: 用药错误（§4 同一处置档）。
SEVERITY_MEDICATION_ERROR = "用药错误"

#: 不合理用药（适宜性审核类问题）。
SEVERITY_UNREASONABLE = "不合理用药"

#: 不规范处方（规范性审核类问题）。
SEVERITY_NON_STANDARD = "不规范处方"

#: 超常处方（§3 尾段：超常处方 3 次以上且无正当理由 → 警告并限制处方权）。
SEVERITY_SUPERNORMAL = "超常处方"

#: 审核层次 → 严重度回落值（规则行未声明 ``severity`` 时用）。
#:
#: 回落依据见模块文档；**只在该院规则行没写严重度时发生**——写了就以规则行为准。
LAYER_SEVERITY_FALLBACK: Mapping[str, str] = {
    RuleLayer.LEGALITY.value: SEVERITY_SEVERE_UNREASONABLE,
    RuleLayer.NORMATIVITY.value: SEVERITY_NON_STANDARD,
    RuleLayer.APPROPRIATENESS.value: SEVERITY_UNREASONABLE,
}

#: 审核层次未声明 / 不认识时的兜底严重度（不抛错，回落来源写进归因文本）。
SEVERITY_UNKNOWN_LAYER = SEVERITY_UNREASONABLE

#: 层次未声明时的文案（写进归因文本的回落来源）。
UNKNOWN_LAYER_LABEL = "未声明"


class SeveritySource(str, Enum):
    """严重度的来源：规则行显式给出 / 按审核层次回落。"""

    RULE = "规则行"
    LAYER = "层次回落"


@dataclass(frozen=True)
class SeverityResolution:
    """一条规则的严重度解析结果。"""

    value: str
    source: SeveritySource
    layer: str = ""
    rule_id: str = ""

    @property
    def is_fallback(self) -> bool:
        """是否走了层次回落（回落值必须看得见，故归因文本要注明来源）。"""
        return self.source is SeveritySource.LAYER

    @property
    def layer_label(self) -> str:
        """层次的展示文案（未声明时给 :data:`UNKNOWN_LAYER_LABEL`）。"""
        return self.layer.strip() or UNKNOWN_LAYER_LABEL

    @property
    def note(self) -> str:
        """回落来源的一句说明（写进归因文本）；规则行给出严重度时为空串。"""
        if not self.is_fallback:
            return ""
        return (
            f"严重度按审核层次「{self.layer_label}」回落为「{self.value}」"
            "（规则行未声明 severity）"
        )

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照（CLI ``--json`` 输出用）。"""
        return {
            "rule_id": self.rule_id,
            "severity": self.value,
            "source": self.source.value,
            "layer": self.layer,
            "is_fallback": self.is_fallback,
            "note": self.note,
        }


def _canonical_layer(layer: str) -> str:
    """把层次名归成口径值（``合法性审核`` → ``合法性``）；不认识就原样返回。"""
    text = (layer or "").strip()
    if not text:
        return ""
    try:
        return RuleLayer.from_value(text).value
    except ValueError:
        return text


def resolve_severity(rule: RuleRow) -> SeverityResolution:
    """解析一条规则的严重度。

    规则行写了 ``severity`` → 直接用（来源：规则行）；
    没写 → 按 ``layer`` 回落（来源：层次回落），回落来源写进
    :attr:`SeverityResolution.note`。
    """
    text = (rule.severity or "").strip()
    layer = _canonical_layer(rule.layer)
    if text:
        return SeverityResolution(
            value=text,
            source=SeveritySource.RULE,
            layer=layer,
            rule_id=rule.rule_id,
        )
    fallback = LAYER_SEVERITY_FALLBACK.get(layer, SEVERITY_UNKNOWN_LAYER)
    return SeverityResolution(
        value=fallback,
        source=SeveritySource.LAYER,
        layer=layer,
        rule_id=rule.rule_id,
    )


def render_severity_note(resolution: SeverityResolution) -> str:
    """渲染回落来源说明；规则行给出严重度时返回空串（不制造噪音）。"""
    return resolution.note


# ---------------------------------------------------------------------------
# 点评分级
# ---------------------------------------------------------------------------

#: 点评分级的依据出处（《处方审核规则体系》§5 引擎化要点）。
GRADE_BASIS = (
    "处方审核规则体系 §5 引擎化要点"
    "（点评分级：不适宜处方 / 不规范处方 / 超常处方 三类判定，对应不同处置流）"
)


class Grade(str, Enum):
    """点评分级三类（§5）。

    **只打标不改结论**——三态判定结论仍是合规 / 违规 / 不可判定，
    打标是给处置流分流用的标签。
    """

    INAPPROPRIATE = "不适宜处方"
    NON_STANDARD = "不规范处方"
    SUPERNORMAL = "超常处方"


#: 审核层次 → 点评分级（超常处方规则另有专用判据，见 :func:`classify_grade`）。
GRADE_BY_LAYER: Mapping[str, Grade] = {
    RuleLayer.NORMATIVITY.value: Grade.NON_STANDARD,
    RuleLayer.LEGALITY.value: Grade.INAPPROPRIATE,
    RuleLayer.APPROPRIATENESS.value: Grade.INAPPROPRIATE,
}

#: 严重度 → 点评分级的**提示对应**（只用于口径自检，不参与归类）。
#:
#: 严重度里带「超常处方」「不规范处方」字样的，其点评分级应当一致；
#: 对不上说明规则行写串了，由 :func:`grade_conflict` 点出来。
SEVERITY_GRADE_HINT: Mapping[str, Grade] = {
    SEVERITY_SUPERNORMAL: Grade.SUPERNORMAL,
    SEVERITY_NON_STANDARD: Grade.NON_STANDARD,
}


@dataclass(frozen=True)
class GradeMarking:
    """一条规则的点评分级打标（**只打标，不改判定结论**）。"""

    grade: Grade
    basis: str = GRADE_BASIS
    rule_id: str = ""
    layer: str = ""

    @property
    def value(self) -> str:
        """打标取值（中文分级名）。"""
        return self.grade.value

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照。"""
        return {
            "rule_id": self.rule_id,
            "grade": self.grade.value,
            "layer": self.layer,
            "basis": self.basis,
        }


def classify_grade(rule: RuleRow) -> Grade:
    """归类一条规则的点评分级（三类互斥且穷尽）。

    判据顺序：

    #. **超常处方规则** → 超常处方。判据是规则行**显式声明的依赖**
       （``prescriber_id`` 与 ``extend_reason``，见 ``engine/supernormal.py``），
       不是按规则号前缀猜；
    #. **规范性审核层** → 不规范处方；
    #. 其余（合法性层与适宜性层的其余规则）→ 不适宜处方。
    """
    if is_supernormal_rule(rule):
        return Grade.SUPERNORMAL
    return GRADE_BY_LAYER.get(_canonical_layer(rule.layer), Grade.INAPPROPRIATE)


def mark_grade(rule: RuleRow) -> GradeMarking:
    """给一条规则打上点评分级标记（附依据出处）。"""
    return GradeMarking(
        grade=classify_grade(rule),
        basis=GRADE_BASIS,
        rule_id=rule.rule_id,
        layer=_canonical_layer(rule.layer),
    )


def mark_grades(rules: Iterable[RuleRow]) -> Mapping[str, GradeMarking]:
    """给一批规则打标，返回 ``rule_id → 打标`` 映射。"""
    return {rule.rule_id: mark_grade(rule) for rule in rules}


def grade_distribution(rules: Iterable[RuleRow]) -> Mapping[str, int]:
    """一批规则的点评分级分布（三类各一条，缺者计 0）。"""
    counts = {member.value: 0 for member in Grade}
    for rule in rules:
        counts[classify_grade(rule).value] += 1
    return counts


def grade_conflict(rule: RuleRow) -> str | None:
    """严重度与点评分级对不上时给出说明（对得上返回 ``None``）。

    例：一条规范性规则把 ``severity`` 写成了「超常处方」——严重度与分级各说各话，
    点评报表会分流错。这类口径漂移必须显式说出来，不能带着两套口径往下跑。
    """
    severity = (rule.severity or "").strip()
    hint = SEVERITY_GRADE_HINT.get(severity)
    if hint is None:
        return None
    grade = classify_grade(rule)
    if grade is hint:
        return None
    return (
        f"规则 {rule.rule_id} 的 severity 写作「{severity}」，"
        f"按规则形态应归「{grade.value}」——严重度与点评分级对不上"
    )


__all__ = [
    "GRADE_BASIS",
    "GRADE_BY_LAYER",
    "Grade",
    "GradeMarking",
    "LAYER_SEVERITY_FALLBACK",
    "SEVERITY_GRADE_HINT",
    "SEVERITY_MEDICATION_ERROR",
    "SEVERITY_NON_STANDARD",
    "SEVERITY_SEVERE_UNREASONABLE",
    "SEVERITY_SUPERNORMAL",
    "SEVERITY_UNKNOWN_LAYER",
    "SEVERITY_UNREASONABLE",
    "SeverityResolution",
    "SeveritySource",
    "UNKNOWN_LAYER_LABEL",
    "classify_grade",
    "grade_conflict",
    "grade_distribution",
    "mark_grade",
    "mark_grades",
    "render_severity_note",
    "resolve_severity",
]
