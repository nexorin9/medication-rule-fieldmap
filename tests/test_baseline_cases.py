"""数据层 3/4：官方案例基线回归（10 条官方案例 + 新增第三态案例）。

对应任务书 T38。回归基准 = ``domain/cases/prescription_review_cases.json`` 的
**10 条官方案例**（《处方审核规则体系》配套案例集，含 9 条 ``confidence: official``
与 1 条 ``regional`` 临床判断场景）。

**这条回归守住三件事**

#. **结论对不对**：10 条案例逐条判定，结论与案例集的 ``expect.拦截`` 一致
   （拦截 → 违规；不拦截 → 合规）。官方案例 rx-008 属临床判断场景，
   期望落「**不可判定**」——系统不能审核的部分必须人工审核，
   绝不能自动拦截成违规，也绝不能放行成合规。
#. **规则号对不对**：每条案例都要落在**指定的那条规则**上，不是"某条规则
   碰巧给了个一样的结论"。规则号错了，药师按规则号回查依据就会查错地方。
#. **依据出处对不对**：结论带的 ``basis`` 必须与案例文件授权的条款定位一致
   （条款编号不得超出案例授权范围），且审核层次（合法性 / 规范性 / 适宜性）
   与案例所属层次一致——**不虚构法规条款编号**。

**第三态案例组**（``tests/data/baseline_cases/third_state_cases.json``）

官方案例集里只有「该拦 / 不该拦」两档，测不出三态判定的价值。本轮另建一组
**第三态案例**：同一规则下「字段为空」与「字段被显式填为否」两种语义必须产出
**不同结论**——混成「非空即有值」，最常见的现场错误就是把「没填皮试结果」
当成「皮试阴性」放过去。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from med_rule_fieldmap.engine.judge import judge_batch, judge_prescription
from med_rule_fieldmap.models import Conclusion, PrescriptionRow, RuleRow
from med_rule_fieldmap.rules.repository import RuleRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RULES = PROJECT_ROOT / "rules" / "sample"

#: 官方案例集（``domain/cases/prescription_review_cases.json``）。
CASES_FILE = PROJECT_ROOT.parents[2] / "domain" / "cases" / "prescription_review_cases.json"

#: 本轮新增的第三态案例组（测试数据层）。
THIRD_STATE_FILE = (
    PROJECT_ROOT / "tests" / "data" / "baseline_cases" / "third_state_cases.json"
)

AS_OF = date(2026, 10, 28)

BATCH_NO = "CASE-BATCH"

#: 医嘱行的公共底座（结构性字段）。
#:
#: 案例文件只描述该案例的关键输入，其余字段按「落地文件里本来就有」补齐——
#: 补的是**身份与栏位**，不补案例没给的**临床字段**（处方类型 / 药品类别 /
#: 日量 / 皮试结果 / 用法用量 / 剂量 / 延长理由），那些字段留空才能测出
#: 「字段没落地 → 不可判定」这条路径。
BASE_FIELDS: dict[str, str] = {
    "batch_no": BATCH_NO,
    "patient_ref": "PAT-CASE",
    "dept_code": "D-CASE",
    "dept_name": "心血管内科门诊",
    "prescriber_id": "PR-CASE",
    "prescriber_name": "医师甲",
    "clinical_diag": "高血压",
    "age_value": "58",
    "age_unit": "岁",
    "event_time": "2026-10-28 09:00:00",
}


@dataclass(frozen=True)
class OfficialCase:
    """一条官方案例的回归期望。

    :param rule_id: 结论应当落在的那条规则（不是"某条规则碰巧结论一样"）。
    :param layer: 期望的审核层次（合法性 / 规范性 / 适宜性）。
    :param rows: 逐条医嘱行（跨行案例有多行：重复给药 2 张、超常处方 4 张）。
    :param expected_by_row: 该规则在**每一行**上的期望结论。
    """

    case_id: str
    rule_id: str
    layer: str
    rows: tuple[dict[str, str], ...]
    expected_by_row: tuple[Conclusion, ...]


#: 10 条官方案例 → 回归期望。
#:
#: ``rows`` 里的键是医嘱落地列（spec.md §2.1 的 26 列口径）；案例输入里的中文键
#: （``处方类型`` / ``药品类别`` / ``开具量`` / ``理由`` / ``药品`` / ``皮试结果`` /
#: ``临床诊断`` / ``处方用法用量``）按同一口径映射到对应列。
OFFICIAL_CASES: dict[str, OfficialCase] = {
    # §3 量限：门（急）诊一般患者麻醉药品注射剂 1 次常用量，3 日量超限 → 拦截
    "rx-001": OfficialCase(
        case_id="rx-001",
        rule_id="RX-LEGAL-MA-OUT-DAYS",
        layer="合法性",
        rows=(
            {
                "prescription_no": "RX-001",
                "visit_no": "VISIT-001",
                "drug_code": "DC-001",
                "drug_name": "盐酸吗啡注射液",
                "rx_type": "门诊一般患者",
                "drug_category": "麻醉药品注射剂",
                "days_qty": "3日量",
            },
        ),
        expected_by_row=(Conclusion.VIOLATION,),
    ),
    # §3 量限：癌症疼痛患者控缓释制剂不超过 15 日常量，15 日量合规 → 不拦截
    "rx-002": OfficialCase(
        case_id="rx-002",
        rule_id="RX-LEGAL-CR-CANCER-OUT-DAYS",
        layer="合法性",
        rows=(
            {
                "prescription_no": "RX-002",
                "visit_no": "VISIT-002",
                "drug_code": "DC-002",
                "drug_name": "盐酸吗啡缓释片",
                "rx_type": "门诊癌症疼痛患者",
                "drug_category": "麻醉药品控缓释制剂",
                "days_qty": "15日量",
            },
        ),
        expected_by_row=(Conclusion.COMPLIANT,),
    ),
    # §1 规范性：用法用量写「遵医嘱」属含糊字句 → 拦截
    "rx-003": OfficialCase(
        case_id="rx-003",
        rule_id="RX-NORM-USAGE-VAGUE",
        layer="规范性",
        rows=(
            {
                "prescription_no": "RX-003",
                "visit_no": "VISIT-003",
                "drug_code": "DC-003",
                "drug_name": "阿莫西林胶囊",
                "dosage_text": "遵医嘱",
            },
        ),
        expected_by_row=(Conclusion.VIOLATION,),
    ),
    # §1 适宜性第 1 项：皮试结果「未注明」→ 拦截
    "rx-004": OfficialCase(
        case_id="rx-004",
        rule_id="RX-APPR-SKIN-TEST-UNSPECIFIED",
        layer="适宜性",
        rows=(
            {
                "prescription_no": "RX-004",
                "visit_no": "VISIT-004",
                "drug_code": "DC-004",
                "drug_name": "青霉素类注射剂",
                "skin_test_result": "未注明",
            },
        ),
        expected_by_row=(Conclusion.VIOLATION,),
    ),
    # §1 适宜性第 5 项：同一患者同日两张氨氯地平 → 第 2 张拦截
    "rx-005": OfficialCase(
        case_id="rx-005",
        rule_id="RX-APPR-DUPLICATE-ADMINISTRATION",
        layer="适宜性",
        rows=(
            {
                "prescription_no": "RX-005-A",
                "visit_no": "VISIT-005",
                "drug_code": "DC-005",
                "drug_name": "氨氯地平片",
                "dosage_text": "5mg 口服 每日一次",
                "dosage_value": "5",
                "dosage_unit": "mg",
                "rx_type": "门急诊一般",
                "drug_category": "普通药品",
                "days_qty": "1",
                "event_time": "2026-10-28 09:00:00",
            },
            {
                "prescription_no": "RX-005-B",
                "visit_no": "VISIT-005",
                "drug_code": "DC-005",
                "drug_name": "氨氯地平片",
                "dosage_text": "5mg 口服 每日一次",
                "dosage_value": "5",
                "dosage_unit": "mg",
                "rx_type": "门急诊一般",
                "drug_category": "普通药品",
                "days_qty": "1",
                "event_time": "2026-10-28 09:00:00",
            },
        ),
        expected_by_row=(Conclusion.COMPLIANT, Conclusion.VIOLATION),
    ),
    # §3 量限：精二 7 日常量未超量 → 不拦截
    "rx-006": OfficialCase(
        case_id="rx-006",
        rule_id="RX-LEGAL-SPIRIT2-OUT-DAYS",
        layer="合法性",
        rows=(
            {
                "prescription_no": "RX-006",
                "visit_no": "VISIT-006",
                "drug_code": "DC-006",
                "drug_name": "地西泮片",
                "rx_type": "门诊一般患者",
                "drug_category": "第二类精神药品",
                "days_qty": "7日常用量",
                "extend_reason": "无延长说明",
            },
        ),
        expected_by_row=(Conclusion.COMPLIANT,),
    ),
    # §3 量限：精二超 7 日常量且延长未注明理由 → 拦截
    "rx-007": OfficialCase(
        case_id="rx-007",
        rule_id="RX-LEGAL-SPIRIT2-OUT-DAYS",
        layer="合法性",
        rows=(
            {
                "prescription_no": "RX-007",
                "visit_no": "VISIT-007",
                "drug_code": "DC-007",
                "drug_name": "地西泮片",
                "rx_type": "门诊一般患者",
                "drug_category": "第二类精神药品",
                "days_qty": "14日常用量",
                "extend_reason": "未注明",
            },
        ),
        expected_by_row=(Conclusion.VIOLATION,),
    ),
    # §1 适宜性第 2 项：诊断相符性属临床判断场景 → 转人工（不可判定）
    "rx-008": OfficialCase(
        case_id="rx-008",
        rule_id="RX-APPR-INDICATION-MATCH",
        layer="适宜性",
        rows=(
            {
                "prescription_no": "RX-008",
                "visit_no": "VISIT-008",
                "drug_code": "DC-008",
                "drug_name": "硝酸甘油片含服（用于降压）",
                "clinical_diag": "高血压",
                "age_value": "65",
                "age_unit": "岁",
            },
        ),
        expected_by_row=(Conclusion.UNDETERMINABLE,),
    ),
    # §3 尾段：同一医师超常 4 次且无正当理由 → 第 4 次拦截（提示限制处方权）
    "rx-009": OfficialCase(
        case_id="rx-009",
        rule_id="RX-APPR-SUPERNORMAL-PRESCRIBING-RIGHT",
        layer="适宜性",
        rows=tuple(
            {
                "prescription_no": f"RX-009-{index}",
                "visit_no": f"VISIT-009-{index}",
                "drug_code": "DC-009",
                "drug_name": "盐酸吗啡注射液",
                "prescriber_id": "PR-009",
                "rx_type": "门诊一般患者",
                "drug_category": "麻醉药品注射剂",
                "days_qty": "3日量",
                "extend_reason": "未注明",
                "event_time": f"2026-10-28 09:{index:02d}:00",
            }
            for index in range(1, 5)
        ),
        expected_by_row=(
            Conclusion.COMPLIANT,
            Conclusion.COMPLIANT,
            Conclusion.COMPLIANT,
            Conclusion.VIOLATION,
        ),
    ),
    # §3 量限：住院患者麻醉药品逐日开具 1 日常用量 → 不拦截
    "rx-010": OfficialCase(
        case_id="rx-010",
        rule_id="RX-LEGAL-MA-INPATIENT-DAYS",
        layer="合法性",
        rows=(
            {
                "prescription_no": "RX-010",
                "visit_no": "VISIT-010",
                "drug_code": "DC-010",
                "drug_name": "盐酸吗啡注射液",
                "rx_type": "住院患者",
                "drug_category": "麻醉药品",
                "days_qty": "1日常用量",
            },
        ),
        expected_by_row=(Conclusion.COMPLIANT,),
    ),
}

#: 案例文件的 ``basis`` 原文 → 审核层次（《处方审核规则体系》§1 审核三层次）。
#:
#: 键是**官方案例文件里的原文**——案例集改了 basis，这条映射就失配，
#: 回归随即报错（而不是悄悄按旧口径继续判）。
#:
#: 映射依据：
#:
#: * 「特殊药品处方量限 §3」→ 合法性（§3 是麻精药品硬规则，归 §1 第 1 项合法性审核）；
#: * 「审核三层次 §1-2」→ 规范性（案例的 ``expect.规则`` 明写「规范性审核：…」）；
#: * 「适宜性审核七项 §1-3」→ 适宜性（案例的 ``expect.规则`` 明写「适宜性审核第 N 项」）；
#: * 「处方管理办法 §45（处方审核规范 §3 尾段）」→ 适宜性（超常处方，点评分级单列一类）。
CASE_BASIS_LAYER: dict[str, str] = {
    "特殊药品处方量限 §3": "合法性",
    "审核三层次 §1-2": "规范性",
    "适宜性审核七项 §1-3": "适宜性",
    "适宜性审核七项 §1-3；此案例演示「系统筛出转人工」流程": "适宜性",
    "处方管理办法 §45（处方审核规范 §3 尾段）": "适宜性",
}

#: 审核家族关键词（案例的 ``expect.规则`` 里明写时，用来交叉核对层次）。
FAMILY_KEYWORDS: dict[str, str] = {
    "合法性审核": "合法性",
    "规范性审核": "规范性",
    "适宜性审核": "适宜性",
}

#: 条款定位符（``§3`` / ``§1-2`` / ``§45``）。
LOCATOR_RE = re.compile(r"§(\d+)(?:-(\d+))?")


def locators(text: str) -> set[int]:
    """抽出一段依据文本里的条款定位编号（``§1-2`` 展开成 ``{1, 2}``）。"""
    found: set[int] = set()
    for match in LOCATOR_RE.finditer(text):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        found.update(range(start, end + 1))
    return found


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rules() -> tuple[RuleRow, ...]:
    repository = RuleRepository(SAMPLE_RULES, as_of=AS_OF)
    repository.load()
    assert repository.errors == (), "样例规则集不应有载入错误"
    return repository.effective_rules


@pytest.fixture(scope="module")
def official_cases() -> dict[str, dict]:
    """官方案例集（缺文件即报错——回归基准必须是真文件，不能是硬编码副本）。"""
    assert CASES_FILE.exists(), f"官方案例集缺失：{CASES_FILE}"
    payload = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    return {case["case_id"]: case for case in payload["cases"]}


def _case_row(case_id: str, values: dict[str, str], index: int = 0) -> PrescriptionRow:
    """按案例输入造一条医嘱行（公共底座 + 案例字段）。"""
    payload = dict(BASE_FIELDS)
    payload.update(values)
    payload.setdefault("prescription_no", f"{case_id.upper()}-{index}")
    payload.setdefault("visit_no", f"VISIT-{case_id.upper()}-{index}")
    payload.setdefault("drug_code", f"DC-{case_id.upper()}")
    return PrescriptionRow(**payload)


def _rows_for(spec: OfficialCase) -> list[PrescriptionRow]:
    return [_case_row(spec.case_id, values, index) for index, values in enumerate(spec.rows)]


def _judgements(spec: OfficialCase, rules: tuple[RuleRow, ...]):
    """跑一遍案例，返回该案例全部判定行（跨行判定需要整批一起跑）。"""
    rows = _rows_for(spec)
    result = judge_batch(rows, rules, batch_no=BATCH_NO)
    return rows, result.rows


def _designated(spec: OfficialCase, rules: tuple[RuleRow, ...]):
    """取该案例**指定规则**在每一行上的判定行。"""
    rows, judgements = _judgements(spec, rules)
    picked: list = []
    for row in rows:
        key = row.prescription_no or row.order_no
        matched = [
            item
            for item in judgements
            if item.rule_id == spec.rule_id and item.prescription_no == key
        ]
        assert matched, (
            f"{spec.case_id}：规则 {spec.rule_id} 未命中处方 {key}（案例落错规则）"
        )
        picked.append(matched[0])
    return picked


# ---------------------------------------------------------------------------
# 1. 官方案例集自身：基准必须是真文件、口径完整
# ---------------------------------------------------------------------------


def test_official_case_set_is_intact(official_cases: dict[str, dict]) -> None:
    """案例集有 10 条、编号齐全、每条都带期望与依据出处。"""
    assert len(official_cases) == 10, "官方案例集应当是 10 条"
    assert set(official_cases) == set(OFFICIAL_CASES), "本轮回归覆盖的案例编号与案例集不一致"
    for case_id, case in official_cases.items():
        assert case["basis"], f"{case_id} 缺依据出处"
        assert "拦截" in case["expect"], f"{case_id} 缺 expect.拦截"
        assert case["expect"].get("规则"), f"{case_id} 缺 expect.规则"
        assert case.get("confidence"), f"{case_id} 缺 confidence"


def test_case_basis_layer_mapping_covers_every_case(official_cases: dict[str, dict]) -> None:
    """案例 basis → 审核层次的映射覆盖案例集的全部 basis 原文。

    键写错或案例集改了 basis，这条回归直接报错——不靠旧口径悄悄继续判。
    """
    basis_values = {case["basis"] for case in official_cases.values()}
    assert basis_values == set(CASE_BASIS_LAYER), (
        "案例集的 basis 原文与层次映射对不上："
        f"{basis_values ^ set(CASE_BASIS_LAYER)}"
    )


def test_case_rule_text_family_agrees_with_the_basis_mapping(
    official_cases: dict[str, dict]
) -> None:
    """案例 ``expect.规则`` 里明写的审核家族与 basis 映射出的层次一致。

    案例文件自己的两个字段互相印证——不靠本轮的表自说自话。
    """
    checked = 0
    for case_id, case in official_cases.items():
        text = str(case["expect"].get("规则", ""))
        family = next(
            (value for key, value in FAMILY_KEYWORDS.items() if key in text),
            "",
        )
        if not family:
            continue
        checked += 1
        assert CASE_BASIS_LAYER[case["basis"]] == family, (
            f"{case_id}：expect.规则 明写「{family}」审核，"
            f"但 basis 映射出的层次是「{CASE_BASIS_LAYER[case['basis']]}」"
        )
    assert checked >= 4, "至少四条案例应当在 expect.规则 里明写审核家族"


# ---------------------------------------------------------------------------
# 2. 官方案例逐条：结论、规则号、依据出处
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", sorted(OFFICIAL_CASES))
def test_official_case_conclusion_matches_the_case_set(
    case_id: str, rules: tuple[RuleRow, ...], official_cases: dict[str, dict]
) -> None:
    """10 条官方案例逐条结论与案例集一致。"""
    spec = OFFICIAL_CASES[case_id]
    picked = _designated(spec, rules)
    assert len(picked) == len(spec.expected_by_row)
    for index, (judgement, expected) in enumerate(zip(picked, spec.expected_by_row)):
        assert judgement.conclusion is expected, (
            f"{case_id} 第 {index + 1} 行期望 {expected.value}，"
            f"实际 {judgement.conclusion.value}"
        )


@pytest.mark.parametrize("case_id", sorted(OFFICIAL_CASES))
def test_official_case_block_expectation_matches_the_conclusion(
    case_id: str, rules: tuple[RuleRow, ...], official_cases: dict[str, dict]
) -> None:
    """案例集的 ``expect.拦截`` 与判定结论对得上。

    拦截 = 违规；不拦截 = 合规。**rx-008 是唯一的例外**：案例集写「拦截」，
    但备注明写它是临床判断场景、要「须人工复核」——系统**不得**自动拦截，
    结论必须是「不可判定」（专项用例另有一组断言把它钉死）。
    """
    spec = OFFICIAL_CASES[case_id]
    should_block = bool(official_cases[case_id]["expect"]["拦截"])
    picked = _designated(spec, rules)
    final = picked[-1].conclusion

    if case_id == "rx-008":
        assert final is Conclusion.UNDETERMINABLE, (
            "rx-008 是临床判断场景，结论必须是「不可判定」（转人工），"
            f"实际 {final.value}"
        )
        return

    assert (final is Conclusion.VIOLATION) is should_block, (
        f"{case_id}：案例集 expect.拦截={should_block}，实际结论 {final.value}"
    )


@pytest.mark.parametrize("case_id", sorted(OFFICIAL_CASES))
def test_official_case_judgement_carries_rule_id_and_basis(
    case_id: str, rules: tuple[RuleRow, ...], official_cases: dict[str, dict]
) -> None:
    """每条结论都带出规则号与依据出处（药师按规则号回查依据要查得到）。"""
    spec = OFFICIAL_CASES[case_id]
    rule = next(item for item in rules if item.rule_id == spec.rule_id)
    for judgement in _designated(spec, rules):
        assert judgement.rule_id == spec.rule_id
        assert judgement.rule_version == rule.version
        assert judgement.basis, f"{case_id} 的判定行缺依据出处"
        assert judgement.basis == rule.basis, (
            f"{case_id}：判定行的依据出处与规则行不一致"
        )
        assert spec.rule_id in judgement.reason, (
            f"{case_id}：归因文本没带出规则号"
        )


@pytest.mark.parametrize("case_id", sorted(OFFICIAL_CASES))
def test_official_case_basis_stays_within_the_authorised_clauses(
    case_id: str, rules: tuple[RuleRow, ...], official_cases: dict[str, dict]
) -> None:
    """结论引用的条款定位不得超出案例集授权的范围（**不虚构法规条款编号**）。

    三条一起守：

    #. **子集**：结论依据里出现的 ``§N`` 必须都在案例集 ``basis`` 授权的编号内；
    #. **共享**：两组编号必须有交集——既没乱写，也确实指的是同一条款；
    #. **层次**：命中的规则行层次与案例所属审核层次一致。
    """
    spec = OFFICIAL_CASES[case_id]
    case = official_cases[case_id]
    authorised = locators(str(case["basis"]))
    assert authorised, f"{case_id} 的 basis 里没有条款定位符"

    rule = next(item for item in rules if item.rule_id == spec.rule_id)
    cited = locators(rule.basis)
    assert cited, f"{spec.rule_id} 的依据出处里没有条款定位符"
    assert cited <= authorised, (
        f"{case_id}：规则依据引用了案例未授权的条款编号 "
        f"{sorted(cited - authorised)}（案例授权 {sorted(authorised)}）"
    )
    assert cited & authorised, (
        f"{case_id}：规则依据与案例依据没有共同的条款定位符"
    )
    assert rule.layer == CASE_BASIS_LAYER[str(case["basis"])], (
        f"{case_id}：规则层次「{rule.layer}」与案例层次 "
        f"「{CASE_BASIS_LAYER[str(case['basis'])]}」不一致"
    )
    assert spec.layer == rule.layer


# ---------------------------------------------------------------------------
# 3. rx-008 专项：临床判断场景 → 转人工，不自动拦截
# ---------------------------------------------------------------------------


def test_rx008_lands_on_undeterminable_instead_of_blocking(
    rules: tuple[RuleRow, ...], official_cases: dict[str, dict]
) -> None:
    """rx-008 落「不可判定」：转人工复核，不自动拦截、也不放行成合规。

    官方案例原文：「适宜性审核第2项：处方用药与临床诊断的相符性**存疑须人工复核**」，
    ``confidence: regional``，备注明写「属临床判断场景，演示适宜性审核须人工复核
    的设计」——本系统的正确行为就是把这条筛出来交人工，而不是替药师下结论。
    """
    spec = OFFICIAL_CASES["rx-008"]
    rows, judgements = _judgements(spec, rules)

    violations = [item for item in judgements if item.conclusion is Conclusion.VIOLATION]
    assert violations == [], (
        "rx-008 是临床判断场景，系统不得自动拦截："
        f"{[(item.rule_id, item.reason) for item in violations]}"
    )

    picked = _designated(spec, rules)
    assert picked[0].conclusion is Conclusion.UNDETERMINABLE
    assert picked[0].missing_fields == (), "字段齐备，缺的不是字段而是知识库"
    assert "人工复核" in picked[0].reason
    assert "药品说明书" in picked[0].reason
    assert picked[0].rule_id == "RX-APPR-INDICATION-MATCH"

    # 知识库缺口不许写成字段缺口——写成「缺少字段 …」会让人去找信息科要一列接口字段
    assert "缺少字段" not in picked[0].reason
    assert "字段已齐备" in picked[0].reason

    # 转人工的结论必须真的落在台账上（不是「什么都不产出」）
    assert any(item.rule_id == "RX-APPR-INDICATION-MATCH" for item in judgements)
    assert rows[0].drug_name == "硝酸甘油片含服（用于降压）"


def test_rx008_never_falls_back_to_compliant(rules: tuple[RuleRow, ...]) -> None:
    """诊断相符性规则**永不**落「合规」——系统不能审核的部分不许替药师放行。

    字段齐备也只落「不可判定」：字段齐不等于这一条审过了。
    """
    spec = OFFICIAL_CASES["rx-008"]
    rule = next(item for item in rules if item.rule_id == spec.rule_id)
    rows = _rows_for(spec)
    judgements = judge_prescription(rows[0], [rule], batch_no=BATCH_NO)
    assert len(judgements) == 1
    assert judgements[0].conclusion is not Conclusion.COMPLIANT
    assert judgements[0].conclusion is not Conclusion.VIOLATION


# ---------------------------------------------------------------------------
# 4. 第三态案例组：字段为空 ≠ 字段为显式否
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def third_state_groups() -> list[dict]:
    assert THIRD_STATE_FILE.exists(), f"第三态案例文件缺失：{THIRD_STATE_FILE}"
    payload = json.loads(THIRD_STATE_FILE.read_text(encoding="utf-8"))
    return payload["groups"]


def _third_state_row(group: dict, variant: dict) -> PrescriptionRow:
    """按第三态案例造一条医嘱行（``absent_columns`` = 整列未落地）。

    整列未落地的列**取值一并清空**——接口没回传这一列时，落地记录里本就没有
    它的值；不清空就成了「列在但有值」，两种「没有值」就分不开了。
    """
    payload: dict[str, object] = {}
    payload.update(group.get("base_fields", {}))
    payload.update(variant.get("fields", {}))
    payload["batch_no"] = payload.get("batch_no", "THIRD-STATE-BATCH")
    payload["prescription_no"] = variant["variant_id"]

    absent = set(variant.get("absent_columns", ()))
    if absent:
        from med_rule_fieldmap.models import PRESCRIPTION_FIELDS

        for name in absent:
            payload[name] = ""
        payload["present_columns"] = frozenset(
            name for name in PRESCRIPTION_FIELDS if name not in absent
        )
    return PrescriptionRow(**payload)  # type: ignore[arg-type]


def test_third_state_case_file_is_well_formed(third_state_groups: list[dict]) -> None:
    """第三态案例文件本身自洽：每组都有规则号、依据出处与至少两个变体。"""
    assert third_state_groups, "第三态案例文件为空"
    for group in third_state_groups:
        assert group["group_id"]
        assert group["rule_id"]
        assert group["basis"]
        assert group.get("why")
        assert len(group["variants"]) >= 2, f"{group['group_id']} 至少要两个变体"
        for variant in group["variants"]:
            assert variant["variant_id"]
            assert variant["expect_conclusion"] in {"合规", "违规", "不可判定"}


@pytest.mark.parametrize(
    "group_id,variant_id",
    [
        (group["group_id"], variant["variant_id"])
        for group in json.loads(
            (PROJECT_ROOT / "tests" / "data" / "baseline_cases" / "third_state_cases.json").read_text(
                encoding="utf-8"
            )
        )["groups"]
        for variant in group["variants"]
    ],
)
def test_third_state_variant_conclusion(
    group_id: str,
    variant_id: str,
    third_state_groups: list[dict],
    rules: tuple[RuleRow, ...],
) -> None:
    """每个第三态变体的结论、缺失字段与归因都要对得上。"""
    group = next(item for item in third_state_groups if item["group_id"] == group_id)
    variant = next(item for item in group["variants"] if item["variant_id"] == variant_id)

    row = _third_state_row(group, variant)
    matched = [
        item
        for item in judge_prescription(row, rules, batch_no="THIRD-STATE-BATCH")
        if item.rule_id == group["rule_id"]
    ]
    assert matched, f"{variant_id}：规则 {group['rule_id']} 未命中该变体"
    judgement = matched[0]

    assert judgement.conclusion.value == variant["expect_conclusion"], (
        f"{variant_id}：期望 {variant['expect_conclusion']}，"
        f"实际 {judgement.conclusion.value}"
    )
    if "expect_missing_fields" in variant:
        assert list(judgement.missing_fields) == list(variant["expect_missing_fields"]), (
            f"{variant_id}：缺失字段与期望不一致"
        )
    for fragment in variant.get("expect_reason_contains", ()):
        assert fragment in judgement.reason, (
            f"{variant_id}：归因文本里找不到「{fragment}」"
        )


def test_third_state_groups_keep_empty_and_explicit_no_apart(
    third_state_groups: list[dict], rules: tuple[RuleRow, ...]
) -> None:
    """同一规则下「字段为空」与「字段为显式否」必须产出**不同结论**。

    这是三态判定存在的全部理由：混成「非空即有值」，「没填皮试结果」就会被
    当成「皮试阴性」放过去。
    """
    checked = 0
    for group in third_state_groups:
        pairs = group.get("must_differ", ())
        for first_id, second_id in pairs:
            by_id = {item["variant_id"]: item for item in group["variants"]}
            first = by_id[first_id]
            second = by_id[second_id]

            def _conclusion(variant: dict):
                row = _third_state_row(group, variant)
                matched = [
                    item
                    for item in judge_prescription(
                        row, rules, batch_no="THIRD-STATE-BATCH"
                    )
                    if item.rule_id == group["rule_id"]
                ]
                assert matched, f"{variant['variant_id']}：规则未命中"
                return matched[0].conclusion

            first_conclusion = _conclusion(first)
            second_conclusion = _conclusion(second)
            assert first_conclusion is not second_conclusion, (
                f"{group['group_id']}：{first_id} 与 {second_id} 结论相同"
                f"（{first_conclusion.value}）——字段为空与字段为显式否必须分开"
            )
            checked += 1
    assert checked >= 2, "至少两组第三态案例要锁住「空值 ≠ 显式否」这条边界"
