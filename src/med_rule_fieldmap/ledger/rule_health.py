"""规则集体检：僵尸规则 / 恒不可判定 / 覆盖率。

**对应源产品能力**：源仓库 ``contrib/database/webhooks/models.py`` 的
``TableWebhookCall``（请求 / 响应副本留痕，可导出复核）与
``contrib/database/field_rules/registries.py`` 的 ``before_row_created`` /
``before_row_updated``（逐行逐规则求值并挂 ``error_text`` 归因）。

**改写**：源产品只有「这一行过没过」的判定，与「这次调用发了什么」的留痕，
**没有「这条规则自己健不健壮」这一层体检**。药事审核岗每月改版规则后要回答
三个问题：

1. 哪条规则从来没命中过（**僵尸规则**）——写错了都不会有人发现；
2. 哪条规则永远判不可判定（**恒不可判定**）——规则依赖的字段根本拿不到；
3. 这套规则整体覆盖了多少适用处方（**覆盖率**）。

本模块把这三问做成一张 ``out/rule_health.csv``，与 ``ledger/gap.py``、
``ledger/priority.py`` 一起构成「字段对照」三件套：**缺哪个字段**（T23）→
**先补哪个字段**（T24）→ **补完之后规则本身还健不健壮**（T25）。

**覆盖率的分母口径（本模块最容易做错的一处）**

分母是**适用处方数**（这条规则在本批数据上真正适用的处方张数，由调度层
``engine/sched.py`` 的 ``match_rule()`` 判定），**不是**批次里的全部处方数。

用全部处方数当分母，量限规则只对麻醉药品的处方适用，却要拿「全院所有
处方」做分母——覆盖率永远是个位数，看着像规则没用，其实是口径错了。
换成适用处方数之后，每条规则的分母只包含它该管的那些处方，覆盖率才可比。

**分子是「判出结论」的处方数**（合规 / 违规），不含不可判定：不可判定不是
「这条规则审过了」，而是「系统判不了、要人工审」。分子排除不可判定之后，
覆盖率才等于「这条规则真正审下来的比例」，剩下的差额恰好就是被字段缺口
挡住的那部分——和 ``ledger/gap.py`` 的字段缺口台账对得上。

**分母为 0 时覆盖率记 0**（不抛除零错）：一条规则在本批数据里没有适用处方
是常态（新规则上线、场景本批没出现），这是**事实**而不是**错误**。

**五种体检结论**

==================  ==================================================
体检结论            判据
==================  ==================================================
僵尸规则            一条判定行都没产出（适用 0 张 = 从未适用；适用 > 0
                    张却无结论 = 规则写错或判定链漏判）
恒不可判定          有判定行，但**全部**落不可判定——依赖字段拿不到
判定与规则不一致    判定台账里的结论数多于当前规则集的适用处方数
                    （规则改版后未重判 / 历史结论未清账）
规则不在生效集      台账里有结论，但规则已不在生效集里
部分不可判定        有结论也有不可判定，覆盖率未满 100%
正常                适用处方全部判出结论
==================  ==================================================

**判定与规则不一致 / 规则不在生效集**这两档源产品没有对应概念：源产品的
规则与数据在同一次请求里绑定，不存在「规则改了但结论还是旧的」这一层。
本轮把它做成体检结论，是为了让药事审核岗改完规则后知道**哪些批次要重判**。

**排序稳定**：体检结论权重升序（僵尸 → 恒不可判定 → 不一致 → 不在生效集 →
部分不可判定 → 正常），同级按规则号升序——同一输入必得同一输出，逐月对账
时两张表对得上。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..engine.sched import match_rule
from ..models import Conclusion, JudgementRow, PrescriptionRow, RuleRow
from .gap import row_identity

#: 规则体检表列（顺序固定）
RULE_HEALTH_COLUMNS: tuple[str, ...] = (
    "rule_id",
    "rule_version",
    "layer",
    "applicable_prescriptions",
    "hit_prescriptions",
    "undeterminable_prescriptions",
    "coverage_pct",
    "health",
    "is_zombie",
    "always_undeterminable",
    "blocking_fields",
    "reason",
)

#: 规则体检表文件名
RULE_HEALTH_FILENAME = "rule_health.csv"

#: 体检结论 —— 正常
HEALTH_NORMAL = "正常"
#: 体检结论 —— 部分不可判定（覆盖率未满，有结论也有判不了的）
HEALTH_PARTIAL = "部分不可判定"
#: 体检结论 —— 恒不可判定（全部落不可判定）
HEALTH_ALWAYS_UNDETERMINABLE = "恒不可判定"
#: 体检结论 —— 僵尸规则（从未命中）
HEALTH_ZOMBIE = "僵尸规则"
#: 体检结论 —— 判定与规则不一致（规则改版后未重判）
HEALTH_INCONSISTENT = "判定与规则不一致"
#: 体检结论 —— 规则不在生效集（台账里留着已下线规则的结论）
HEALTH_RETIRED = "规则不在生效集"

#: 体检结论的排序权重（越小越靠前：药事审核岗先看最该动手改的）
_HEALTH_ORDER: dict[str, int] = {
    HEALTH_ZOMBIE: 0,
    HEALTH_ALWAYS_UNDETERMINABLE: 1,
    HEALTH_INCONSISTENT: 2,
    HEALTH_RETIRED: 3,
    HEALTH_PARTIAL: 4,
    HEALTH_NORMAL: 5,
}

#: 多字段分隔符（与判定台账的 missing_fields 分隔符一致）
FIELD_SEPARATOR = "|"


@dataclass(frozen=True)
class RuleHealth:
    """一条规则的体检结论（体检表的一行）。

    ``applicable_prescriptions`` 是**分母**（这条规则在本批数据上适用的处方
    张数），``hit_prescriptions`` 是**分子**（判出合规 / 违规结论的处方
    张数），``undeterminable_prescriptions`` 是判不了的那部分——差额恰好是
    被字段缺口挡住的部分。
    """

    rule_id: str
    rule_version: str
    layer: str
    applicable_prescriptions: int
    hit_prescriptions: int
    undeterminable_prescriptions: int
    health: str
    reason: str
    judged_prescriptions: int = 0
    blocking_fields: tuple[str, ...] = ()
    is_zombie: bool = False
    always_undeterminable: bool = False
    in_effective_rules: bool = True

    @property
    def coverage(self) -> float:
        """覆盖率（0–1 的比值）；分母为 0 时记 0，不抛除零错。"""
        if self.applicable_prescriptions <= 0:
            return 0.0
        return self.hit_prescriptions / self.applicable_prescriptions

    @property
    def coverage_pct(self) -> float:
        """覆盖率（百分比，保留一位小数）。"""
        return round(self.coverage * 100, 1)

    def to_record(self) -> dict[str, str]:
        """体检表行（字符串口径）。"""
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "layer": self.layer,
            "applicable_prescriptions": str(self.applicable_prescriptions),
            "hit_prescriptions": str(self.hit_prescriptions),
            "undeterminable_prescriptions": str(self.undeterminable_prescriptions),
            "coverage_pct": f"{self.coverage_pct:.1f}",
            "health": self.health,
            "is_zombie": "是" if self.is_zombie else "否",
            "always_undeterminable": "是" if self.always_undeterminable else "否",
            "blocking_fields": FIELD_SEPARATOR.join(self.blocking_fields),
            "reason": self.reason,
        }


@dataclass
class _RuleStats:
    """一条规则在判定台账里的累计情况（内部统计用）。"""

    versions: set[str] = field(default_factory=set)
    judged: set[str] = field(default_factory=set)
    hit: set[str] = field(default_factory=set)
    undeterminable: set[str] = field(default_factory=set)
    blocking_fields: set[str] = field(default_factory=set)


def _dedupe_prescriptions(
    prescriptions: Iterable[PrescriptionRow],
) -> list[PrescriptionRow]:
    """按身份键（处方号优先、医嘱号兜底）去重处方行。

    与 ``ledger/gap.py`` 的 ``row_identity()`` 同一口径——同一批里同一张
    处方出现两次属落地异常，体检按「最后读到的那条」算。
    """
    index: dict[str, PrescriptionRow] = {}
    for row in prescriptions:
        key = row_identity(row)
        if key:
            index[key] = row
    return list(index.values())


def _applicable_count(rule: RuleRow, prescriptions: Sequence[PrescriptionRow]) -> int:
    """这条规则在本批数据上适用的处方张数（覆盖率的分母）。

    口径与调度层一致：``match_rule()`` 判为**候选**的才算适用——适用条件
    命中，或适用条件字段缺失但它是本规则声明的依赖（此时判定层落不可判定，
    该处方仍属这条规则的管辖范围）。不适用的处方**不产生判定行**，不进分母。
    """
    return sum(1 for row in prescriptions if match_rule(rule, row).is_candidate)


def _join_fields(names: Iterable[str]) -> str:
    """字段名列表 → 一行文本（空列表返回占位说明）。"""
    items = sorted(name for name in names if name)
    if not items:
        return "（归因未点名缺失字段）"
    return "、".join(items)


def _health_of(
    *,
    applicable: int,
    judged: int,
    hit: int,
    undeterminable: int,
    known_applicability: bool,
    blocking_fields: Sequence[str],
) -> tuple[str, bool, bool, str]:
    """判定体检结论、两个标记位与归因文本。

    返回 ``(health, is_zombie, always_undeterminable, reason)``。
    """
    fields_text = _join_fields(blocking_fields)

    if judged == 0:
        # 从未被命中：一条结论都没产出。分两种成因，处置方式不同。
        if applicable > 0:
            reason = (
                f"适用 {applicable} 张处方却一条判定行都没产出："
                "疑似规则写错（表达式引用了不存在的字段）或判定链漏判，"
                "请核对该规则的表达式与依赖字段。"
            )
        else:
            reason = (
                "本批无适用处方，规则从未被触发：适用条件（applies_when）"
                "可能写得过窄或字段名写错，请核对规则行与落地文件表头。"
            )
        return HEALTH_ZOMBIE, True, False, reason

    if known_applicability and judged > applicable:
        # 台账里的结论比当前规则集的适用面还多：规则改版后未重判
        reason = (
            f"判定台账里有 {judged} 张结论，但按当前规则集只有 {applicable} 张适用："
            "规则改版后未重判，或历史结论未清账。"
        )
        return HEALTH_INCONSISTENT, False, False, reason

    if hit == 0:
        # 有结论但全是不可判定：规则依赖的字段根本拿不到。
        reason = (
            f"判了 {judged} 张处方、全部落不可判定："
            f"规则依赖的字段（{fields_text}）在本批始终拿不到——"
            "要么改规则依赖，要么让信息科补接口字段。"
        )
        return HEALTH_ALWAYS_UNDETERMINABLE, False, True, reason

    if not known_applicability:
        reason = (
            f"判出结论 {hit} 张、未见不可判定；本批未提供处方行，"
            "适用面与覆盖率无从计算。"
        )
        return HEALTH_NORMAL, False, False, reason

    if hit < applicable:
        reason = (
            f"适用 {applicable} 张、判出结论 {hit} 张、不可判定 {undeterminable} 张"
            f"（缺 {fields_text}）：补齐这些字段可把覆盖率"
            f"从 {round(hit / applicable * 100, 1):.1f}% 提到 100%。"
        )
        return HEALTH_PARTIAL, False, False, reason

    reason = f"适用 {applicable} 张、判出结论 {hit} 张，覆盖率 100%，无待办。"
    return HEALTH_NORMAL, False, False, reason


def build_rule_health(
    rules: Iterable[RuleRow],
    judgements: Iterable[JudgementRow],
    prescriptions: Iterable[PrescriptionRow] | None = None,
) -> list[RuleHealth]:
    """做一遍规则集体检，返回每条规则的体检结论。

    ``rules`` 是**生效规则集**（``RuleRepository.effective_rules``）——
    体检覆盖生效集里的每一条规则，包括一条结论都没产出的新规则；
    ``judgements`` 是判定台账里的判定行；
    ``prescriptions`` 是本批的处方行（**覆盖率分母的来源**）。

    ``prescriptions`` 缺省时**不知道适用面**：适用处方数与覆盖率都记 0，
    僵尸规则与恒不可判定仍可识别，但「判定与规则不一致」这一档不判
    （没有适用面就无从比对，不猜）。

    判定台账里有、但不在生效集里的规则也会单列一档（``规则不在生效集``）——
    规则下线后历史结论还留在台账里，药事审核岗要知道自己该清哪一批账。
    """
    rule_list = list(rules)
    judgement_rows = list(judgements)

    stats: dict[str, _RuleStats] = {}
    for row in judgement_rows:
        bucket = stats.setdefault(row.rule_id, _RuleStats())
        bucket.versions.add(row.rule_version)
        bucket.judged.add(row.prescription_no)
        if row.conclusion is Conclusion.UNDETERMINABLE:
            bucket.undeterminable.add(row.prescription_no)
            bucket.blocking_fields.update(row.missing_fields)
        else:
            bucket.hit.add(row.prescription_no)

    known_applicability = prescriptions is not None
    unique_rows = _dedupe_prescriptions(prescriptions or ())

    items: list[RuleHealth] = []
    seen_rule_ids: set[str] = set()

    for rule in rule_list:
        seen_rule_ids.add(rule.rule_id)
        bucket = stats.get(rule.rule_id)
        applicable = _applicable_count(rule, unique_rows)
        judged = len(bucket.judged) if bucket else 0
        hit = len(bucket.hit) if bucket else 0
        undeterminable = len(bucket.undeterminable) if bucket else 0
        blocking = tuple(sorted(bucket.blocking_fields)) if bucket else ()
        health, is_zombie, always_undeterminable, reason = _health_of(
            applicable=applicable,
            judged=judged,
            hit=hit,
            undeterminable=undeterminable,
            known_applicability=known_applicability,
            blocking_fields=blocking,
        )
        version = rule.version or (
            "、".join(sorted(bucket.versions)) if bucket else ""
        )
        items.append(
            RuleHealth(
                rule_id=rule.rule_id,
                rule_version=version,
                layer=rule.layer,
                applicable_prescriptions=applicable,
                hit_prescriptions=hit,
                undeterminable_prescriptions=undeterminable,
                health=health,
                reason=reason,
                judged_prescriptions=judged,
                blocking_fields=blocking,
                is_zombie=is_zombie,
                always_undeterminable=always_undeterminable,
                in_effective_rules=True,
            )
        )

    # 台账里有、但不在生效集里的规则（规则下线 / 改版后未重判）
    for rule_id, bucket in stats.items():
        if rule_id in seen_rule_ids:
            continue
        judged = len(bucket.judged)
        items.append(
            RuleHealth(
                rule_id=rule_id,
                rule_version="、".join(sorted(bucket.versions)),
                layer="",
                applicable_prescriptions=0,
                hit_prescriptions=len(bucket.hit),
                undeterminable_prescriptions=len(bucket.undeterminable),
                health=HEALTH_RETIRED,
                reason=(
                    f"该规则不在当前生效规则集里，但判定台账仍有 {judged} 张结论："
                    "规则改版后未重判，或历史结论未清账。"
                ),
                judged_prescriptions=judged,
                blocking_fields=tuple(sorted(bucket.blocking_fields)),
                in_effective_rules=False,
            )
        )

    items.sort(key=lambda item: (_HEALTH_ORDER.get(item.health, 99), item.rule_id))
    return items


def summarize_rule_health(items: Sequence[RuleHealth]) -> dict[str, object]:
    """规则集体检小结（终端摘要与 JSON 载荷用）。

    整体覆盖率 = 命中处方数合计 / 适用处方数合计（分母为 0 时记 0）。
    """
    applicable = sum(item.applicable_prescriptions for item in items)
    hit = sum(item.hit_prescriptions for item in items)
    undeterminable = sum(item.undeterminable_prescriptions for item in items)
    overall = 0.0 if applicable <= 0 else hit / applicable
    return {
        "规则总数": len(items),
        "僵尸规则数": sum(1 for item in items if item.is_zombie),
        "恒不可判定规则数": sum(
            1 for item in items if item.always_undeterminable
        ),
        "部分不可判定规则数": sum(
            1 for item in items if item.health == HEALTH_PARTIAL
        ),
        "不在生效集规则数": sum(
            1 for item in items if item.health == HEALTH_RETIRED
        ),
        "适用处方数合计": applicable,
        "命中处方数合计": hit,
        "不可判定处方数合计": undeterminable,
        "整体覆盖率": round(overall * 100, 1),
    }


def write_rule_health(
    items: Iterable[RuleHealth],
    out_dir: str | Path,
    *,
    filename: str = RULE_HEALTH_FILENAME,
) -> Path:
    """把规则体检结论写成 CSV，返回落盘路径。

    ``out_dir`` 不存在时自动创建；编码用 ``utf-8-sig``（Excel 直开不乱码）。
    没有规则时也落一个只有表头的文件（下游脚本不必判存在性）。
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RULE_HEALTH_COLUMNS))
        writer.writeheader()
        for item in items:
            writer.writerow(item.to_record())
    return target


def load_rule_health(path: str | Path) -> list[dict[str, str]]:
    """读回规则体检表（供回归与运维 CLI 消费）。"""
    target = Path(path)
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(record) for record in csv.DictReader(handle)]


__all__ = [
    "FIELD_SEPARATOR",
    "HEALTH_ALWAYS_UNDETERMINABLE",
    "HEALTH_INCONSISTENT",
    "HEALTH_NORMAL",
    "HEALTH_PARTIAL",
    "HEALTH_RETIRED",
    "HEALTH_ZOMBIE",
    "RULE_HEALTH_COLUMNS",
    "RULE_HEALTH_FILENAME",
    "RuleHealth",
    "build_rule_health",
    "load_rule_health",
    "summarize_rule_health",
    "write_rule_health",
]
