"""规则行 schema 与载入期校验。

**对应源产品能力**：源仓库 ``core/workflow_actions/registries.py`` 的
``WorkflowActionType.prepare_values()``（落库前把配置值统一转换并做校验）与
``raise_if_misconfigured()``（拒绝跑不起来的已保存配置）。本轮把「工作流动作」
改写成「审方规则行」：载入期把 YAML 值转成 :class:`~med_rule_fieldmap.models.RuleRow`，
非法的规则**拒绝生效且不影响其余规则**。

校验分三类：

* **硬要求** —— ``rule_id`` / ``layer`` / ``basis`` / ``version`` / ``expression``
  缺一即拒；``basis`` 为空的规则不得进入生效集（《处方审核规则体系》§4
  「有明确的临床用药依据来源」）。
* **口径校验** —— 生效区间可解析且 ``effective_to >= effective_from``；
  ``applies_when`` / ``required_fields`` 引用的字段名必须是医嘱列全集内的真字段；
  ``expression`` 括号引号配平、不含多语句分隔符。
* **可扩展字段** —— ``severity``（默认必填，缺失时引擎侧按层次回落）、
  ``missing_conclusion``（缺字段时的结论，默认「不可判定」）、
  ``forbidden_terms``（含糊字句词表）、``note``。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime

from ..expression.cache import ParseTreeCache
from ..expression.validator import (
    KIND_CAPABILITY,
    KIND_LEX,
    KIND_SYNTAX,
    KIND_UNDECLARED_FIELD,
    ExpressionIssue,
    validate_expression,
)
from ..engine.limits import (
    CLASSES,
    LIMIT_MATRIX,
    classify_category,
    resolve_limit_ref,
)
from ..engine.duplicate import DUPLICATE_KEY_FIELDS
from ..engine.indication import INDICATION_FIELDS
from ..engine.normativity import (
    BODY_MATTER_FIELDS,
    VAGUE_USAGE_BASELINE_TERMS,
)
from ..engine.supernormal import (
    SUPERNNORMAL_PRESCRIBER_FIELD,
    SUPERNNORMAL_REASON_FIELD,
)
from ..expression.normalize import normalize_field
from ..models import PRESCRIPTION_FIELDS, Conclusion, RuleLayer, RuleRow

#: 规则行允许出现的顶层键（写错键名会被拒绝，避免依赖声明被静默忽略）。
ALLOWED_RULE_KEYS: frozenset[str] = frozenset(
    {
        "rule_id",
        "layer",
        "basis",
        "version",
        "effective_from",
        "effective_to",
        "applies_when",
        "required_fields",
        "expression",
        "severity",
        "missing_conclusion",
        "forbidden_terms",
        "note",
        "limit_ref",
    }
)

#: 必填键（缺一即拒）。
REQUIRED_RULE_KEYS: tuple[str, ...] = ("rule_id", "layer", "basis", "version", "expression")

_RULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")

_MISSING_CONCLUSION_ALIASES = {
    "不可判定": Conclusion.UNDETERMINABLE,
    "undeterminable": Conclusion.UNDETERMINABLE,
    "违规": Conclusion.VIOLATION,
    "violation": Conclusion.VIOLATION,
}


@dataclass(frozen=True)
class SchemaIssue:
    """一条校验问题；带字段名，必要时带行列定位。"""

    field: str | None
    message: str
    line: int | None = None
    column: int | None = None

    def __str__(self) -> str:
        location = ""
        if self.line is not None:
            location = f" (第 {self.line} 行"
            if self.column is not None:
                location += f" 第 {self.column} 列"
            location += ")"
        target = f"{self.field}: " if self.field else ""
        return f"{target}{self.message}{location}"


class RuleValidationError(ValueError):
    """规则行校验失败；``issues`` 逐条给出字段名与定位信息。"""

    def __init__(self, rule_id: str | None, issues: list[SchemaIssue]) -> None:
        self.rule_id = rule_id
        self.issues = list(issues)
        detail = "；".join(str(issue) for issue in self.issues)
        super().__init__(f"规则行 {rule_id or '<未知规则号>'} 校验失败：{detail}")


# ---------------------------------------------------------------------------
# 表达式预检入口
# ---------------------------------------------------------------------------


def default_precheck(expression: str) -> list[SchemaIssue]:
    """表达式载入期预检（轻量、纯文本层）。

    只做**不依赖完整解析器**的检查：括号配平、引号配平、无多语句分隔符。
    完整的语法解析、能力禁用清单与字段依赖声明核对由受限表达式求值器接管
    （``expression/parser.py``、``expression/validator.py``），本函数是它们的挂载点。
    """
    issues: list[SchemaIssue] = []
    stack: list[tuple[str, int]] = []
    quote: str | None = None
    pairs = {")": "(", "]": "[", "}": "{"}
    depth = 0
    for index, char in enumerate(expression):
        column = index + 1
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char in "([{":
            stack.append((char, column))
            depth += 1
        elif char in ")]}":
            depth += 1
            if not stack:
                issues.append(
                    SchemaIssue("expression", f"多余的分隔符 {char!r}", line=1, column=column)
                )
                continue
            opener, opener_column = stack.pop()
            if opener != pairs[char]:
                issues.append(
                    SchemaIssue(
                        "expression",
                        f"分隔符 {char!r} 与 {opener!r} 不配对（后者出现在第 {opener_column} 列）",
                        line=1,
                        column=column,
                    )
                )
    if quote is not None:
        issues.append(SchemaIssue("expression", "引号未闭合", line=1, column=len(expression)))
    if stack:
        opener, opener_column = stack[-1]
        issues.append(
            SchemaIssue(
                "expression",
                f"分隔符 {opener!r} 未闭合（出现在第 {opener_column} 列）",
                line=1,
                column=len(expression),
            )
        )
    if ";" in expression:
        issues.append(SchemaIssue("expression", "表达式不允许多语句分隔符 ';'"))
    if "\n" in expression or "\r" in expression:
        issues.append(SchemaIssue("expression", "表达式不允许换行"))
    return issues


# ---------------------------------------------------------------------------
# 表达式预检结果翻译
# ---------------------------------------------------------------------------

#: 语法类问题类型（能力越界 / 词法 / 语法）——不包含字段声明类问题。
_SYNTAX_ISSUE_KINDS: frozenset[str] = frozenset({KIND_CAPABILITY, KIND_LEX, KIND_SYNTAX})


def _expression_schema_issues(issues: list[ExpressionIssue]) -> list[SchemaIssue]:
    """把表达式预检问题翻译成规则行校验问题（保留字段名与行列定位）。

    ``field`` 沿用「哪个配置键写错了」的口径：字段声明类问题指向
    ``required_fields``，其余指向 ``expression``；字段名本身写在 message 里。
    """
    translated: list[SchemaIssue] = []
    for issue in issues:
        if issue.kind == KIND_UNDECLARED_FIELD:
            field_name = "required_fields"
        else:
            field_name = "expression"
        message = issue.message
        if issue.rule_id:
            message = f"规则 {issue.rule_id}：{message}"
        translated.append(
            SchemaIssue(field_name, message, line=issue.line, column=issue.column)
        )
    return translated


# ---------------------------------------------------------------------------
# 取值解析
# ---------------------------------------------------------------------------


def _parse_date(value: object, field_name: str) -> date | None:
    """把 ``effective_from`` / ``effective_to`` 解析成 ``date``；空值返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} 不是合法日期（期望 YYYY-MM-DD）：{text!r}") from exc


def _normalize_applies_when(value: object) -> dict[str, object]:
    """归一适用条件：``{字段: 精确值}`` 或 ``{字段: {"contains": 子串}}``。"""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("applies_when 必须是键值映射")
    normalized: dict[str, object] = {}
    for key, condition in value.items():
        field_name = str(key).strip()
        if field_name not in PRESCRIPTION_FIELDS:
            raise ValueError(
                f"applies_when 引用了不存在的医嘱字段：{field_name!r}"
                f"（合法字段共 {len(PRESCRIPTION_FIELDS)} 个）"
            )
        if isinstance(condition, Mapping):
            keys = set(condition.keys())
            if keys != {"contains"}:
                raise ValueError(
                    f"applies_when.{field_name} 的条件对象只支持 contains 一种写法，"
                    f"当前为：{sorted(keys)}"
                )
            needle = str(condition["contains"]).strip()
            if not needle:
                raise ValueError(f"applies_when.{field_name}.contains 不能为空")
            normalized[field_name] = {"contains": needle}
        else:
            normalized[field_name] = str(condition).strip()
    return normalized


def _normalize_required_fields(value: object) -> tuple[str, ...]:
    """归一依赖字段声明：去重并保持声明顺序。"""
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError("required_fields 必须是字段名列表")
    seen: list[str] = []
    for item in value:
        name = str(item).strip()
        if name not in PRESCRIPTION_FIELDS:
            raise ValueError(f"required_fields 引用了不存在的医嘱字段：{name!r}")
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def _normalize_terms(value: object) -> tuple[str, ...]:
    """归一含糊字句词表（§1 规范性审核明列的字句）。"""
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError("forbidden_terms 必须是字符串列表")
    terms: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            raise ValueError("forbidden_terms 不能含空串")
        if text not in terms:
            terms.append(text)
    return tuple(terms)


# ---------------------------------------------------------------------------
# 量限矩阵一致性校验
# ---------------------------------------------------------------------------


def _matched_categories(condition: object) -> tuple[str, ...]:
    """适用条件（药品类别）命中的落地药品类别。

    精确值走枚举归一（``精一`` → ``第一类精神药品``）；``contains`` 子串写法同时
    用原写法与归一写法去匹配，两侧口径一致。
    """
    if isinstance(condition, Mapping):
        needle = str(condition.get("contains", "")).strip()
        if not needle:
            return ()
        normalized = normalize_field("drug_category", needle).text
        return tuple(
            category
            for category in CLASSES
            if needle in category or normalized in category
        )
    text = normalize_field("drug_category", str(condition)).text
    return (text,) if text in CLASSES else ()


def _limit_ref_issues(
    limit_ref: str,
    *,
    expression: str,
    required_fields: tuple[str, ...],
    applies_when: Mapping[str, object],
) -> list[SchemaIssue]:
    """量限规则行与 §3 量限矩阵的一致性校验。

    四条：单元必须存在；阈值表达式必须与矩阵逐字一致；依赖字段必须含
    ``rx_type`` / ``drug_category`` / ``days_qty``；适用条件不得与矩阵单元矛盾
    （例如规则声明住院档、适用条件却写门急诊）。
    """
    issues: list[SchemaIssue] = []
    cell = resolve_limit_ref(limit_ref)
    if cell is None:
        issues.append(
            SchemaIssue(
                "limit_ref",
                f"量限矩阵里没有单元「{limit_ref}」（合法单元：{len(LIMIT_MATRIX)} 个，"
                "形如「门急诊一般/麻醉药品注射剂」）",
            )
        )
        return issues

    if expression and expression != cell.expected_expression:
        issues.append(
            SchemaIssue(
                "expression",
                f"阈值表达式与量限矩阵不一致：矩阵口径是「{cell.expected_expression}」，"
                f"规则行写的是「{expression}」",
            )
        )

    for name in ("rx_type", "drug_category", "days_qty"):
        if name not in required_fields:
            issues.append(
                SchemaIssue(
                    "required_fields",
                    f"量限规则必须声明依赖字段 {name}（矩阵单元 {cell.ref} 的判定口径需要它）",
                )
            )

    tier_condition = applies_when.get("rx_type")
    if isinstance(tier_condition, str) and tier_condition.strip():
        tier_value = normalize_field("rx_type", tier_condition).text
        if tier_value and tier_value != cell.tier:
            issues.append(
                SchemaIssue(
                    "applies_when",
                    f"适用条件的处方类型「{tier_condition}」与量限矩阵档位「{cell.tier}」不一致",
                )
            )

    category_condition = applies_when.get("drug_category")
    if category_condition is not None:
        for category in _matched_categories(category_condition):
            mapped = classify_category(cell.tier, category)
            if mapped != cell.drug_class:
                issues.append(
                    SchemaIssue(
                        "applies_when",
                        f"适用条件命中的药品类别「{category}」在该档归到矩阵单元"
                        f"「{cell.tier}/{mapped}」，与规则声明的「{cell.ref}」不一致",
                    )
                )
    return issues


def _usage_vagueness_issues(
    *,
    rule_id: str | None,
    layer: RuleLayer | None,
    required_fields: tuple[str, ...],
    forbidden_terms: tuple[str, ...],
) -> list[SchemaIssue]:
    """含糊字句词表的载入期校验：§1 明列的字句必须被覆盖。

    只对**用法用量含糊字句类规则**生效（层次为「规范性」且依赖字段落在正文段
    栏位内）。漏掉基线字句意味着一类被法规明令禁止的写法会反过来被判成合规——
    删词表属规则改版，须走版本号与依据流程，不该由配置缺口静默生效。
    """
    if layer is None or layer is not RuleLayer.NORMATIVITY:
        return []
    if not forbidden_terms:
        return []
    if not all(name in BODY_MATTER_FIELDS for name in required_fields):
        return []
    effective = set(forbidden_terms)
    missing = [term for term in VAGUE_USAGE_BASELINE_TERMS if term not in effective]
    if not missing:
        return []
    return [
        SchemaIssue(
            "forbidden_terms",
            f"规则 {rule_id} 的含糊字句词表未覆盖 §1 明列字句：{'、'.join(missing)}；"
            "删词表属规则改版，须同时改版本号与 basis",
        )
    ]


def _duplicate_rule_issues(
    *,
    rule_id: str | None,
    layer: RuleLayer | None,
    required_fields: tuple[str, ...],
    expression: str,
    missing_conclusion: Conclusion,
) -> list[SchemaIssue]:
    """重复给药规则行的载入期校验（口径真源在 ``engine/duplicate.py``）。

    只对**疑似该族**的规则生效：层次为「适宜性」且 ``required_fields`` 里
    出现了键字段中的任意一个。声明不全的规则在这里被拒——若放过去，它会
    悄悄退化成一条普通规则，「键字段没声明全」这件事就永远没人发现。

    三条：

    #. 必须声明全部三个键字段——少一个都判不了；
    #. ``expression`` 必须显式引用三个键字段——判定前提写在规则行里；
    #. ``missing_conclusion`` 必须是「不可判定」——键字段没落地时系统判不了，
       落「违规」会把接口缺口伪装成处方不合格。
    """
    if layer is None or layer is not RuleLayer.APPROPRIATENESS:
        return []
    if not (set(required_fields) & set(DUPLICATE_KEY_FIELDS)):
        return []
    issues: list[SchemaIssue] = []
    missing = [name for name in DUPLICATE_KEY_FIELDS if name not in required_fields]
    if missing:
        issues.append(
            SchemaIssue(
                "required_fields",
                f"规则 {rule_id} 未声明重复给药判定键字段 {missing}；"
                "重复给药判定键须同时声明 visit_no / drug_code / event_time",
            )
        )
    unreferenced = [name for name in DUPLICATE_KEY_FIELDS if name not in expression]
    if unreferenced:
        issues.append(
            SchemaIssue(
                "expression",
                f"规则 {rule_id} 的 expression 未引用键字段 {unreferenced}；"
                "跨行聚合口径不由表达式给，但依赖字段必须由规则行显式声明",
            )
        )
    if missing_conclusion is not Conclusion.UNDETERMINABLE:
        issues.append(
            SchemaIssue(
                "missing_conclusion",
                f"规则 {rule_id} 的 missing_conclusion 声明为「{missing_conclusion.value}」；"
                "键字段未落地只能落「不可判定」（系统判不了，不等于处方不合格）",
            )
        )
    return issues


def _supernormal_rule_issues(
    *,
    rule_id: str | None,
    layer: RuleLayer | None,
    required_fields: tuple[str, ...],
    expression: str,
    missing_conclusion: Conclusion,
) -> list[SchemaIssue]:
    """超常处方规则行的载入期校验（口径真源在 ``engine/supernormal.py``）。

    同样只对**疑似该族**的规则生效：层次为「适宜性」且 ``required_fields`` 里
    出现了 ``prescriber_id`` 或 ``extend_reason``。

    三条：

    #. 必须声明 ``prescriber_id``（跨行分组键）与 ``extend_reason``（正当理由）；
    #. ``expression`` 必须引用 ``prescriber_id``；
    #. ``missing_conclusion`` 必须是「不可判定」——理由字段没落地时系统判不了，
       落「违规」会把接口缺口伪装成医师超常开方。
    """
    if layer is None or layer is not RuleLayer.APPROPRIATENESS:
        return []
    if not (set(required_fields) & {SUPERNNORMAL_PRESCRIBER_FIELD, SUPERNNORMAL_REASON_FIELD}):
        return []
    issues: list[SchemaIssue] = []
    missing = [
        name
        for name in (SUPERNNORMAL_PRESCRIBER_FIELD, SUPERNNORMAL_REASON_FIELD)
        if name not in required_fields
    ]
    if missing:
        issues.append(
            SchemaIssue(
                "required_fields",
                f"规则 {rule_id} 未声明依赖字段 {missing}；"
                "超常处方判定须同时声明 prescriber_id（分组键）与 extend_reason（正当理由）",
            )
        )
    if SUPERNNORMAL_PRESCRIBER_FIELD not in expression:
        issues.append(
            SchemaIssue(
                "expression",
                f"规则 {rule_id} 的 expression 未引用 {SUPERNNORMAL_PRESCRIBER_FIELD}；"
                "跨行计数的分组键须在规则行里写明",
            )
        )
    if missing_conclusion is not Conclusion.UNDETERMINABLE:
        issues.append(
            SchemaIssue(
                "missing_conclusion",
                f"规则 {rule_id} 的 missing_conclusion 声明为「{missing_conclusion.value}」；"
                "理由字段未落地只能落「不可判定」（系统判不了，不等于医师超常开方）",
            )
        )
    return issues


def _indication_rule_issues(
    *,
    rule_id: str | None,
    layer: RuleLayer | None,
    required_fields: tuple[str, ...],
    expression: str,
    missing_conclusion: Conclusion,
) -> list[SchemaIssue]:
    """诊断相符性规则行的载入期校验（口径真源在 ``engine/indication.py``）。

    只对**疑似该族**的规则生效：层次为「适宜性」且 ``required_fields`` 里
    出现了药品名或临床诊断。声明不全的规则在这里被拒——若放过去，它会
    悄悄退化成一条普通规则，「相符性要哪几个字段」这件事就永远没人发现。

    三条：

    #. 必须同时声明 ``drug_name`` 与 ``clinical_diag``——缺一个都判不了；
    #. ``expression`` 必须显式引用这两个字段——判定前提写在规则行里；
    #. ``missing_conclusion`` 必须是「不可判定」——字段没落地时系统判不了，
       落「违规」会把接口缺口伪装成处方不合格。
    """
    if layer is None or layer is not RuleLayer.APPROPRIATENESS:
        return []
    if not (set(required_fields) & set(INDICATION_FIELDS)):
        return []
    issues: list[SchemaIssue] = []
    missing = [name for name in INDICATION_FIELDS if name not in required_fields]
    if missing:
        issues.append(
            SchemaIssue(
                "required_fields",
                f"规则 {rule_id} 未声明诊断相符性依赖字段 {missing}；"
                "相符性判定须同时声明 drug_name（药品标识）与 clinical_diag（临床诊断）",
            )
        )
    unreferenced = [name for name in INDICATION_FIELDS if name not in expression]
    if unreferenced:
        issues.append(
            SchemaIssue(
                "expression",
                f"规则 {rule_id} 的 expression 未引用依赖字段 {unreferenced}；"
                "字段完备性口径由规则行显式声明，不靠规则号猜",
            )
        )
    if missing_conclusion is not Conclusion.UNDETERMINABLE:
        issues.append(
            SchemaIssue(
                "missing_conclusion",
                f"规则 {rule_id} 的 missing_conclusion 声明为「{missing_conclusion.value}」；"
                "依赖字段未落地只能落「不可判定」（系统判不了，不等于处方不合格）",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 主校验入口
# ---------------------------------------------------------------------------


def validate_rule(
    raw: object,
    *,
    require_severity: bool = True,
    precheck: Callable[[str], list[SchemaIssue]] = default_precheck,
    source_file: str = "",
    cache: ParseTreeCache | None = None,
) -> RuleRow:
    """校验并转换一条规则行；不合法时抛 :class:`RuleValidationError`。

    ``require_severity=True``（默认，规则仓库与 CLI ``rules validate`` 使用）：
    ``severity`` 缺失即拒——严重度必须由药事审核岗显式给出，不得由程序默认。
    ``require_severity=False``：允许 ``severity`` 为空，引擎侧按层次回落
    （``engine/severity.py``），并在归因文本里注明回落来源。

    ``cache`` 是表达式解析树缓存：规则文件反复热重载时，文本没变的表达式
    不必重新解析。缺省用进程级默认缓存。
    """
    issues: list[SchemaIssue] = []

    if not isinstance(raw, Mapping):
        raise RuleValidationError(
            None, [SchemaIssue(None, "规则行必须是键值映射（YAML 文档）")]
        )

    rule_id_raw = raw.get("rule_id")
    rule_id = str(rule_id_raw).strip() if rule_id_raw is not None else None

    unknown = sorted(set(str(k) for k in raw.keys()) - ALLOWED_RULE_KEYS)
    if unknown:
        issues.append(
            SchemaIssue(
                unknown[0],
                f"出现未知键 {unknown}（合法键：{sorted(ALLOWED_RULE_KEYS)}）",
            )
        )

    for key in REQUIRED_RULE_KEYS:
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            issues.append(SchemaIssue(key, f"必填项缺失：{key}"))

    if rule_id and not _RULE_ID_PATTERN.match(rule_id):
        issues.append(
            SchemaIssue(
                "rule_id",
                f"规则号不合法：{rule_id!r}（只允许字母、数字、点、下划线与连字符，长度 3–64）",
            )
        )

    layer: RuleLayer | None = None
    try:
        layer = RuleLayer.from_value(raw.get("layer"))
    except ValueError as exc:
        issues.append(SchemaIssue("layer", str(exc)))

    basis = raw.get("basis")
    basis_text = str(basis).strip() if basis is not None else ""
    if not basis_text:
        issues.append(
            SchemaIssue("basis", "规则行必须带依据出处（basis），无依据的规则不得生效")
        )

    version = raw.get("version")
    version_text = str(version).strip() if version is not None else ""
    if not version_text:
        issues.append(SchemaIssue("version", "规则行必须带版本号（version）"))

    severity_raw = raw.get("severity")
    severity_text = str(severity_raw).strip() if severity_raw is not None else ""
    if not severity_text and require_severity:
        issues.append(
            SchemaIssue("severity", "规则行必须带严重度（severity）；允许回落时按层次推导")
        )

    effective_from: date | None = None
    effective_to: date | None = None
    for key in ("effective_from", "effective_to"):
        try:
            parsed = _parse_date(raw.get(key), key)
        except ValueError as exc:
            issues.append(SchemaIssue(key, str(exc)))
            continue
        if key == "effective_from":
            effective_from = parsed
        else:
            effective_to = parsed
    if effective_from and effective_to and effective_to < effective_from:
        issues.append(
            SchemaIssue(
                "effective_to",
                f"生效区间非法：effective_to {effective_to} 早于 effective_from {effective_from}",
            )
        )

    applies_when: dict[str, object] = {}
    try:
        applies_when = _normalize_applies_when(raw.get("applies_when"))
    except ValueError as exc:
        issues.append(SchemaIssue("applies_when", str(exc)))

    required_fields: tuple[str, ...] = ()
    required_fields_ok = True
    try:
        required_fields = _normalize_required_fields(raw.get("required_fields"))
    except ValueError as exc:
        required_fields_ok = False
        issues.append(SchemaIssue("required_fields", str(exc)))

    expression_raw = raw.get("expression")
    expression = str(expression_raw).strip() if expression_raw is not None else ""
    if expression:
        # 载入期预检（能力禁用清单 + 字段依赖声明核对）；语法层的问题
        # 已经定位到行列时，不再叠加轻量文本预检，避免同一个问题报两遍。
        expression_issues = validate_expression(
            expression,
            required_fields=required_fields,
            rule_id=rule_id,
            check_declared_fields=required_fields_ok,
            cache=cache,
        )
        if not any(issue.kind in _SYNTAX_ISSUE_KINDS for issue in expression_issues):
            issues.extend(precheck(expression))
        issues.extend(_expression_schema_issues(expression_issues))

    missing_conclusion = Conclusion.UNDETERMINABLE
    missing_raw = raw.get("missing_conclusion")
    if missing_raw is not None:
        key = str(missing_raw).strip()
        if key not in _MISSING_CONCLUSION_ALIASES:
            issues.append(
                SchemaIssue(
                    "missing_conclusion",
                    f"缺字段时的结论取值非法：{missing_raw!r}（合法取值：不可判定 / 违规）",
                )
            )
        else:
            missing_conclusion = _MISSING_CONCLUSION_ALIASES[key]

    forbidden_terms: tuple[str, ...] = ()
    try:
        forbidden_terms = _normalize_terms(raw.get("forbidden_terms"))
    except ValueError as exc:
        issues.append(SchemaIssue("forbidden_terms", str(exc)))
    issues.extend(
        _usage_vagueness_issues(
            rule_id=rule_id,
            layer=layer,
            required_fields=required_fields,
            forbidden_terms=forbidden_terms,
        )
    )
    issues.extend(
        _duplicate_rule_issues(
            rule_id=rule_id,
            layer=layer,
            required_fields=required_fields,
            expression=expression,
            missing_conclusion=missing_conclusion,
        )
    )
    issues.extend(
        _supernormal_rule_issues(
            rule_id=rule_id,
            layer=layer,
            required_fields=required_fields,
            expression=expression,
            missing_conclusion=missing_conclusion,
        )
    )
    issues.extend(
        _indication_rule_issues(
            rule_id=rule_id,
            layer=layer,
            required_fields=required_fields,
            expression=expression,
            missing_conclusion=missing_conclusion,
        )
    )

    note_raw = raw.get("note")
    note = str(note_raw).strip() if note_raw is not None else ""

    limit_ref_raw = raw.get("limit_ref")
    limit_ref = str(limit_ref_raw).strip() if limit_ref_raw is not None else ""
    if limit_ref:
        issues.extend(
            _limit_ref_issues(
                limit_ref,
                expression=expression,
                required_fields=required_fields,
                applies_when=applies_when,
            )
        )

    if issues:
        raise RuleValidationError(rule_id, issues)

    return RuleRow(
        rule_id=rule_id or "",
        layer=layer.value if layer else "",
        basis=basis_text,
        version=version_text,
        effective_from=effective_from,
        effective_to=effective_to,
        applies_when=applies_when,
        required_fields=required_fields,
        expression=expression,
        severity=severity_text or None,
        missing_conclusion=missing_conclusion,
        forbidden_terms=forbidden_terms,
        note=note,
        limit_ref=limit_ref,
        source_file=source_file,
    )
