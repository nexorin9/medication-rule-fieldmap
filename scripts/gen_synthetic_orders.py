#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性种子合成医嘱规模集（默认 2000 行 · 同种子逐字节可复现）。

数据来源阶梯（第 2 级：标准合成生成器）
----------------------------------------
真实院内 HIS 医嘱流本轮拿不到；公开数据集（MIMIC-IV Demo，脚本见
``scripts/fetch_mimic_demo.py``）缺麻精类别、皮试结果、处方类型与医师处方权，
恰好覆盖不到本轮的「不可判定」路径。本脚本按**固定随机种子**合成一批医嘱，
规模、三类结论配比与脏数据形态全部写死在代码里：同种子两次生成的产物
逐字节相同（sha256 一致），规模化回归（缓存命中率 / 吞吐 / 三态分布）
才有可复现性。

三类结论配比（:data:`TARGET_MIX`，脚本内固定）
----------------------------------------------
=========  ======  ==========================================================
结论       配比    含义
=========  ======  ==========================================================
合规       60%     字段齐备、在量限之内、用法用量写得清楚
违规       25%     至少命中一条硬规则（超量 / 皮试 / 含糊字句 / 缺项 /
                   重复给药 / 超常处方）
不可判定   15%     规则行显式声明过的依赖字段没落地 —— 系统判不了，
                   交给信息科补字段
=========  ======  ==========================================================

「结论」是**这张处方被设计成什么样**（``expected_class``），不是引擎跑出来的
结果：本脚本不调用判定内核（那是规模化回归那条任务的活）。但每张处方的设计
都对着 ``rules/sample/`` 里的规则行写：

* 合规行——字段齐备、量限之内、用法用量不含含糊字句、跨行键不撞车；
* 违规行——至少命中一条规则（超量落在矩阵阈值之上、药名含青霉素而皮试写
  「未注明」/「阳性」、用法用量含「遵医嘱」、前记缺临床诊断、后记缺签章、
  同就诊同药同日重复、超常处方第 4 张起）；
* 不可判定行——缺的是**规则行显式声明过的依赖字段**（``rx_type`` /
  ``drug_category`` / ``days_qty`` / ``skin_test_result`` / ``age_unit``）。
  缺在规则没声明的地方落不出第三态（那条规则压根不适用），所以缺项是挑过的。

产物
----
=============================  ============================================
``orders_<seed>.csv``          主规模集（UTF-8 · 2000 行 · 29 列口径）
``orders_<seed>.gbk.csv``      同一批数据的 GB18030 编码版（入站编码兜底）
``orders_<seed>.mixed.csv``    同一批数据的**混编**版（部分行是 GBK 字节）
``labels_<seed>.csv``          逐行标签（处方号 / 设计结论 / 画像 / 形态）
``manifest.json``              种子、行数、配比、形态覆盖、各产物 sha256
=============================  ============================================

用法
----
    python3 scripts/gen_synthetic_orders.py --out data/synthetic_orders
    python3 scripts/gen_synthetic_orders.py --seed 20261028 --rows 2000 --json

退出码
------
    0  生成成功
    2  参数错误（行数非正、种子非法等）
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = PROJECT_ROOT / "src"
if str(_SRC_DIR) not in sys.path:  # 允许未安装时直接跑脚本
    sys.path.insert(0, str(_SRC_DIR))

from med_rule_fieldmap.models import (  # noqa: E402
    PRESCRIPTION_FIELDS,
    Conclusion,
)

EXIT_OK = 0
EXIT_USAGE = 2

#: 默认随机种子（固定不变：换种子就是换一批数据，同种子必须逐字节一致）。
DEFAULT_SEED = 20261028
#: 默认行数。
DEFAULT_ROWS = 2000
#: 批次号（写进每一行的 ``batch_no``）。
BATCH_NO = "SYN-BATCH-001"
#: 三类结论的配比（脚本内固定，测试按这组数字断言）。
TARGET_MIX: dict[str, float] = {
    Conclusion.COMPLIANT.value: 0.60,
    Conclusion.VIOLATION.value: 0.25,
    Conclusion.UNDETERMINABLE.value: 0.15,
}

#: 违规行里「重复给药」占的比例（其余是单条违规画像）。
DUP_PAIR_SHARE = 0.10
#: 违规行里「超常处方」占的比例（按 4 张一组铺，同一开方医师）。
SUPERNORMAL_SHARE = 0.04
#: 超常处方一组几张（同组同医师，第 4 张起触发提示线）。
SUPERNORMAL_GROUP_SIZE = 4

# ---------------------------------------------------------------------------
# 词表（贴近国内医院落地文件的常见写法；口径值与 rules/sample/ 的规则行对齐）
# ---------------------------------------------------------------------------

#: 科室（编码, 名称）。含「儿科」——儿童实际年龄规则按科室圈定适用面。
DEPARTMENTS: tuple[tuple[str, str], ...] = (
    ("D101", "心血管内科门诊"),
    ("D102", "呼吸内科门诊"),
    ("D103", "消化内科门诊"),
    ("D104", "肿瘤内科门诊"),
    ("D105", "内分泌科门诊"),
    ("D106", "神经内科门诊"),
    ("D201", "普通外科门诊"),
    ("D202", "骨科门诊"),
    ("D301", "儿科门诊"),
    ("D401", "急诊内科诊室"),
    ("D501", "疼痛科门诊"),
    ("D502", "麻醉科门诊"),
)

#: 药品（编码, 名称, 规格, 剂型）。
DRUGS: tuple[tuple[str, str, str, str], ...] = (
    ("DC-001", "阿莫西林胶囊", "0.25g:24粒", "胶囊"),
    ("DC-002", "注射用青霉素钠", "1g", "注射剂"),
    ("DC-003", "盐酸氨溴索口服液", "100ml", "口服液"),
    ("DC-004", "硝苯地平控释片", "30mg:7片", "控释片"),
    ("DC-005", "盐酸吗啡注射液", "10mg:1ml", "注射剂"),
    ("DC-006", "地西泮片", "5mg:14片", "片剂"),
    ("DC-007", "布洛芬混悬液", "100ml", "混悬液"),
    ("DC-008", "二甲双胍缓释片", "0.5g:30片", "缓释片"),
    ("DC-009", "阿托伐他汀钙片", "20mg:7片", "片剂"),
    ("DC-010", "泮托拉唑钠肠溶片", "40mg:14片", "肠溶片"),
)

#: 不含青霉素的药品池（常规画像用）。
#:
#: 皮试规则按药名里的「青霉素」圈定适用面：这类药若没填皮试结果，落到的是
#: 「不可判定」而不是「合规」（空值 ≠ 显式否）。因此只有**皮试相关画像**
#: 才用青霉素，其余画像一律从本池里取药，免得合规画像被皮试规则拽进第三态。
DRUGS_GENERAL: tuple[tuple[str, str, str, str], ...] = tuple(
    drug for drug in DRUGS if "青霉素" not in drug[1]
)

#: 常用诊断（前记栏位；空值是「缺项」画像的一部分）。
DIAGNOSES: tuple[str, ...] = (
    "高血压",
    "2型糖尿病",
    "慢性阻塞性肺疾病",
    "社区获得性肺炎",
    "慢性心力衰竭",
    "术后镇痛",
    "急性阑尾炎",
    "泌尿系感染",
)

#: 常用处方类型（归一层会归一到「门急诊一般 / 门急诊癌症疼痛 / 住院」三档）。
RX_TYPES: tuple[str, ...] = ("门诊一般患者", "住院患者", "门诊癌症疼痛患者")

#: 生僻字（真实 HIS 导出里的医师姓名常带生僻字；GB18030 之外的字也只有
#: GB18030 编得动，故入站侧的编码兜底取 gb18030 而不是 gbk）。
RARE_CHARS: tuple[str, ...] = ("䶮", "龘", "㙓", "昝", "頔", "㵘", "𠮷")

#: 含糊字句（§1 规范性审核明列的两个字句——命中即违规）。
VAGUE_TERMS: tuple[str, ...] = ("遵医嘱", "自用")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RowPlan:
    """一张处方的设计稿。

    ``values`` 是按口径列名填好的落地字段（值就是 HIS 导出文件里的原文）；
    ``expected_class`` 是这张处方**被设计成**的结论；
    ``features`` 是这张处方带上的脏数据形态。
    """

    index: int
    prescription_no: str
    expected_class: str
    profile: str
    features: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)
    extra_cells: tuple[str, ...] = ()

    def row_values(self) -> list[str]:
        """按口径列序取一行（缺列补空串）。"""
        return [self.values.get(name, "") for name in PRESCRIPTION_FIELDS]


@dataclass(frozen=True)
class Unit:
    """生成单元：一组必须挨着落地的行。

    重复给药的两张（首张合规、次张违规）与超常处方的一组四张（同开方医师、
    同批累计）都靠「挨着」与「共享键」表达，拆成独立单元就没法表达跨行判定。
    """

    profile: str
    expected_class: str
    rows: int = 1
    repeat: int = 1


# ---------------------------------------------------------------------------
# 计数与分配
# ---------------------------------------------------------------------------
def class_counts(rows: int) -> dict[str, int]:
    """三类结论的行数（和恒等于 ``rows``）。"""
    compliant = round(rows * TARGET_MIX[Conclusion.COMPLIANT.value])
    violation = round(rows * TARGET_MIX[Conclusion.VIOLATION.value])
    undeterminable = rows - compliant - violation
    return {
        Conclusion.COMPLIANT.value: compliant,
        Conclusion.VIOLATION.value: violation,
        Conclusion.UNDETERMINABLE.value: undeterminable,
    }


def allocate(total: int, weights: dict[str, float]) -> dict[str, int]:
    """按权重把 ``total`` 拆成整数份（最大余数法，和恒等于 ``total``）。"""
    if total < 0:
        raise ValueError("total 不能为负")
    if not weights:
        return {}
    scale = sum(weights.values())
    if scale <= 0:
        raise ValueError("权重之和必须为正")
    raw = {name: total * weight / scale for name, weight in weights.items()}
    base = {name: int(value) for name, value in raw.items()}
    remainder = total - sum(base.values())
    # 余数给「小数部分最大」的画像；同余按名字排序，保证同权重同结果。
    order = sorted(raw, key=lambda name: (-(raw[name] - base[name]), name))
    for name in order[:remainder]:
        base[name] += 1
    return base


# ---------------------------------------------------------------------------
# 行构造
# ---------------------------------------------------------------------------
def _fmt_time(index: int, *, iso: bool = False) -> str:
    """事件时间：同一批次落在同一天 08:00–17:59 内（时间随行号推进）。"""
    moment = datetime(2026, 10, 28, 8, 0, 0) + timedelta(minutes=index % 600)
    return moment.strftime("%Y-%m-%dT%H:%M:%S" if iso else "%Y-%m-%d %H:%M:%S")


def _rare_name(index: int) -> str:
    """带生僻字的医师名。"""
    char = RARE_CHARS[index % len(RARE_CHARS)]
    return f"医师{char}"


def _base_values(
    index: int,
    *,
    dept: tuple[str, str],
    drug: tuple[str, str, str, str],
    prescriber: tuple[str, str],
    **overrides: str,
) -> dict[str, str]:
    """一张处方的公共字段（口径 29 列的默认值）。"""
    values = {
        "batch_no": BATCH_NO,
        "order_no": f"SYN-ORD-{index:05d}",
        "prescription_no": f"SYN-RX-{index:05d}",
        "visit_no": f"SYN-VIS-{index:05d}",
        "patient_ref": f"SYN-PT-{index % 400 + 1:04d}",
        "dept_code": dept[0],
        "dept_name": dept[1],
        "prescriber_id": prescriber[0],
        "prescriber_name": prescriber[1],
        "drug_code": drug[0],
        "drug_name": drug[1],
        "spec": drug[2],
        "dosage_form": drug[3],
        "dosage_text": "5mg 口服 每日1次",
        "dosage_value": "5",
        "dosage_unit": "mg",
        "route": "口服",
        "freq": "每日1次",
        "quantity": "1",
        "rx_type": "门诊一般患者",
        "drug_category": "普通药品",
        "days_qty": "1",
        "extend_reason": "",
        "skin_test_result": "",
        "clinical_diag": "高血压",
        "age_value": "45",
        "age_unit": "岁",
        "event_time": _fmt_time(index),
    }
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# 脏数据形态
# ---------------------------------------------------------------------------
#: 形态 → 目标字段。空值字段不套形态（免得把「缺项」变成「有值」）。
STYLE_TARGETS: dict[str, str] = {
    "空白填充": "dosage_text",
    "值内含逗号与引号": "clinical_diag",
    "值内含换行": "clinical_diag",
    "超长诊断": "clinical_diag",
    "全角括号": "prescriber_name",
    "超长医师名": "prescriber_name",
    "生僻字医师名": "prescriber_name",
    "单位后缀日量": "days_qty",
    "频次别名": "freq",
    "ISO 时间": "event_time",
    "多余列": "",
}

#: 全部形态（顺序固定，按行号轮转）。
ALL_STYLES: tuple[str, ...] = tuple(STYLE_TARGETS)


def apply_style(
    values: dict[str, str], style: str, index: int
) -> tuple[dict[str, str], tuple[str, ...]]:
    """给一行套一种脏数据形态，返回 (新值, 形态名)。

    只动**不参与判定**或**判定结论不变**的字段：自由文本、频次、时间写法。
    目标字段当前为空时跳过该形态——空值本身是「缺项」画像的一部分，
    不能被形态改写成有值。
    """
    target = STYLE_TARGETS.get(style)
    if target is None:
        raise ValueError(f"未知脏数据形态：{style}")
    new_values = dict(values)
    if style == "多余列":
        return new_values, (style,)
    if not new_values.get(target, "").strip():
        return new_values, ()

    if style == "空白填充":
        new_values[target] = f"  {new_values[target]}  "
    elif style == "值内含逗号与引号":
        new_values[target] = f'{new_values[target]},"随诊"'
    elif style == "值内含换行":
        new_values[target] = f"{new_values[target]}\n（门诊随诊）"
    elif style == "超长诊断":
        base = new_values[target]
        new_values[target] = base + "；" + ("补充说明，" * 120)
    elif style == "全角括号":
        new_values[target] = f"{new_values[target]}（门诊）"
    elif style == "超长医师名":
        new_values[target] = new_values[target] + "（" + ("兼" * 400) + "）"
    elif style == "生僻字医师名":
        new_values[target] = _rare_name(index)
    elif style == "单位后缀日量":
        new_values[target] = f"{new_values[target]}日量"
    elif style == "频次别名":
        new_values[target] = "qd"
    elif style == "ISO 时间":
        new_values[target] = _fmt_time(index, iso=True)
    else:  # pragma: no cover - STYLE_TARGETS 已穷举
        raise ValueError(f"未实现的脏数据形态：{style}")
    return new_values, (style,)


# ---------------------------------------------------------------------------
# 画像
# ---------------------------------------------------------------------------
#: 合规画像 → 权重。
COMPLIANT_PROFILES: dict[str, float] = {
    "普通门急诊": 0.45,
    "普通住院": 0.15,
    "癌症疼痛档": 0.10,
    "儿科处方": 0.15,
    "青霉素皮试阴性": 0.15,
}

#: 违规画像 → 权重（单条违规画像；重复给药与超常处方另按单元铺）。
VIOLATION_PROFILES: dict[str, float] = {
    "超量-麻醉注射剂": 0.24,
    "超量-精二延长未注明理由": 0.14,
    "皮试结果未注明": 0.15,
    "皮试结果阳性仍开具": 0.07,
    "用法用量含糊字句": 0.11,
    "前记缺临床诊断": 0.14,
    "后记缺签章": 0.15,
}

#: 不可判定画像 → 权重（缺的都是规则行显式声明过的依赖字段）。
UNDETERMINABLE_PROFILES: dict[str, float] = {
    "缺处方类型": 0.30,
    "缺药品类别": 0.30,
    "缺日量": 0.20,
    "缺皮试结果": 0.13,
    "缺年龄单位": 0.07,
}


def _compliant_row(
    index: int, profile: str, rng: random.Random
) -> tuple[dict[str, str], tuple[str, ...]]:
    """合规画像 → 一行。字段齐备、量限之内、用法用量写得清楚。"""
    dept = rng.choice(DEPARTMENTS)
    drug = rng.choice(DRUGS_GENERAL)
    prescriber = (f"PR-{index % 97 + 1:03d}", f"医师{chr(0x7532 + index % 20)}")
    overrides: dict[str, str] = {}

    if profile == "普通住院":
        overrides.update(
            rx_type="住院患者",
            clinical_diag=rng.choice(DIAGNOSES),
            days_qty="1",
        )
        dept = ("D401", "急诊内科诊室")
    elif profile == "癌症疼痛档":
        overrides.update(
            rx_type="门诊癌症疼痛患者",
            clinical_diag="肿瘤化疗后疼痛",
            drug_category="普通药品",
            days_qty=str(rng.randint(1, 7)),
        )
        dept = ("D501", "疼痛科门诊")
    elif profile == "儿科处方":
        dept = ("D301", "儿科门诊")
        unit, value = rng.choice(
            (("岁", "3"), ("月", "5"), ("日", "10"), ("周", "2"), ("岁", "8"))
        )
        overrides.update(
            age_value=value,
            age_unit=unit,
            clinical_diag="急性上呼吸道感染",
        )
        drug = DRUGS[6]
    elif profile == "青霉素皮试阴性":
        drug = DRUGS[1]
        overrides.update(
            skin_test_result="阴性",
            clinical_diag="社区获得性肺炎",
            drug_category="普通药品",
        )

    values = _base_values(index, dept=dept, drug=drug, prescriber=prescriber, **overrides)
    return values, ()


def _violation_row(
    index: int, profile: str, rng: random.Random
) -> tuple[dict[str, str], tuple[str, ...]]:
    """违规画像 → 一行。至少命中一条规则。"""
    dept = rng.choice(DEPARTMENTS)
    drug = rng.choice(DRUGS_GENERAL)
    prescriber = (f"PR-{index % 97 + 1:03d}", f"医师{chr(0x7532 + index % 20)}")
    overrides: dict[str, str] = {}

    if profile == "超量-麻醉注射剂":
        # §3：门（急）诊一般患者，麻醉药品注射剂每张处方 1 次常用量 → 3 日量超量。
        drug = DRUGS[4]
        overrides.update(
            rx_type="门诊一般患者",
            drug_category="麻醉药品注射剂",
            days_qty="3",
            clinical_diag="术后镇痛",
            route="静脉滴注",
            dosage_form="注射剂",
        )
    elif profile == "超量-精二延长未注明理由":
        # §3：精二一般不超过 7 日常量，延长须注明理由 → 14 日量且理由「未注明」。
        drug = DRUGS[5]
        overrides.update(
            rx_type="门诊一般患者",
            drug_category="第二类精神药品",
            days_qty="14",
            extend_reason="未注明",
            clinical_diag="慢性失眠",
        )
    elif profile == "皮试结果未注明":
        drug = DRUGS[1]
        overrides.update(
            skin_test_result="未注明",
            drug_category="普通药品",
            clinical_diag="社区获得性肺炎",
        )
    elif profile == "皮试结果阳性仍开具":
        drug = DRUGS[1]
        overrides.update(
            skin_test_result="阳性",
            drug_category="普通药品",
            clinical_diag="社区获得性肺炎",
        )
    elif profile == "用法用量含糊字句":
        term = VAGUE_TERMS[index % len(VAGUE_TERMS)]
        overrides.update(
            dosage_text=f"5mg 口服 {term}",
            clinical_diag=rng.choice(DIAGNOSES),
        )
    elif profile == "前记缺临床诊断":
        overrides.update(
            clinical_diag="",
            drug_category="普通药品",
        )
    elif profile == "后记缺签章":
        overrides.update(
            prescriber_id="",
            clinical_diag=rng.choice(DIAGNOSES),
        )

    values = _base_values(index, dept=dept, drug=drug, prescriber=prescriber, **overrides)
    return values, ()


def _undeterminable_row(
    index: int, profile: str, rng: random.Random
) -> tuple[dict[str, str], tuple[str, ...]]:
    """不可判定画像 → 一行。缺的是规则行声明过的依赖字段。"""
    dept = rng.choice(DEPARTMENTS)
    drug = rng.choice(DRUGS_GENERAL)
    prescriber = (f"PR-{index % 97 + 1:03d}", f"医师{chr(0x7532 + index % 20)}")
    overrides: dict[str, str] = {"clinical_diag": rng.choice(DIAGNOSES)}

    if profile == "缺处方类型":
        # 量限规则声明依赖 rx_type → 缺它就落第三态。
        drug = DRUGS[4]
        overrides.update(rx_type="", drug_category="麻醉药品注射剂", days_qty="1")
    elif profile == "缺药品类别":
        drug = DRUGS[4]
        overrides.update(rx_type="门诊一般患者", drug_category="", days_qty="1")
    elif profile == "缺日量":
        drug = DRUGS[4]
        overrides.update(rx_type="门诊一般患者", drug_category="麻醉药品注射剂", days_qty="")
    elif profile == "缺皮试结果":
        drug = DRUGS[1]
        overrides.update(skin_test_result="", drug_category="普通药品")
    elif profile == "缺年龄单位":
        dept = ("D301", "儿科门诊")
        drug = DRUGS[6]
        overrides.update(age_value="3", age_unit="", clinical_diag="急性上呼吸道感染")

    values = _base_values(index, dept=dept, drug=drug, prescriber=prescriber, **overrides)
    return values, ()


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def build_plans(seed: int = DEFAULT_SEED, rows: int = DEFAULT_ROWS) -> list[RowPlan]:
    """按种子与规模生成全部处方的设计稿。"""
    if rows <= 0:
        raise ValueError("行数必须为正")
    rng = random.Random(seed)
    counts = class_counts(rows)

    n_violation = counts[Conclusion.VIOLATION.value]
    n_compliant = counts[Conclusion.COMPLIANT.value]
    n_undeterminable = counts[Conclusion.UNDETERMINABLE.value]

    n_dup_pairs = round(n_violation * DUP_PAIR_SHARE)
    n_supernormal_rows = round(n_violation * SUPERNORMAL_SHARE)
    n_supernormal_groups = n_supernormal_rows // SUPERNORMAL_GROUP_SIZE
    n_supernormal_rows = n_supernormal_groups * SUPERNORMAL_GROUP_SIZE

    violation_singles = n_violation - n_dup_pairs - n_supernormal_rows
    compliant_singles = n_compliant - n_dup_pairs

    units: list[Unit] = []
    for name, count in allocate(violation_singles, VIOLATION_PROFILES).items():
        units.extend(Unit(name, Conclusion.VIOLATION.value) for _ in range(count))
    for name, count in allocate(compliant_singles, COMPLIANT_PROFILES).items():
        units.extend(Unit(name, Conclusion.COMPLIANT.value) for _ in range(count))
    for name, count in allocate(n_undeterminable, UNDETERMINABLE_PROFILES).items():
        units.extend(
            Unit(name, Conclusion.UNDETERMINABLE.value) for _ in range(count)
        )
    # 重复给药：同就诊 · 同药 · 同日两张 —— 首张合规、次张违规。
    units.extend(Unit("重复给药", Conclusion.VIOLATION.value, rows=2) for _ in range(n_dup_pairs))
    # 超常处方：同一开方医师一组几张，第 4 张起触发提示线。
    units.extend(
        Unit(
            "超常处方",
            Conclusion.VIOLATION.value,
            rows=SUPERNORMAL_GROUP_SIZE,
            repeat=SUPERNORMAL_GROUP_SIZE,
        )
        for _ in range(n_supernormal_groups)
    )

    rng.shuffle(units)

    plans: list[RowPlan] = []
    index = 0
    dup_counter = 0
    supernormal_counter = 0
    for unit in units:
        if unit.profile == "重复给药":
            index = _append_duplicate_pair(plans, index, dup_counter, rng)
            dup_counter += 1
            continue
        if unit.profile == "超常处方":
            index = _append_supernormal_group(
                plans, index, supernormal_counter, unit.rows, rng
            )
            supernormal_counter += 1
            continue
        values, features = _build_single(unit.profile, index, rng)
        values, style_features = _maybe_style(unit.profile, values, index, rng)
        plans.append(
            RowPlan(
                index=index,
                prescription_no=values["prescription_no"],
                expected_class=unit.expected_class,
                profile=unit.profile,
                features=features + style_features,
                values=values,
            )
        )
        index += 1
    return plans


def _build_single(
    profile: str, index: int, rng: random.Random
) -> tuple[dict[str, str], tuple[str, ...]]:
    """按画像造一行（不含脏数据形态）。"""
    if profile in COMPLIANT_PROFILES:
        return _compliant_row(index, profile, rng)
    if profile in VIOLATION_PROFILES:
        return _violation_row(index, profile, rng)
    if profile in UNDETERMINABLE_PROFILES:
        return _undeterminable_row(index, profile, rng)
    raise ValueError(f"未知画像：{profile}")


def _maybe_style(
    profile: str, values: dict[str, str], index: int, rng: random.Random
) -> tuple[dict[str, str], tuple[str, ...]]:
    """按行号轮转，给一部分行套上脏数据形态。

    约 1/4 的行带形态：形态是「真实导出里常见的杂质」，不是判定口径的变化。
    形态只碰自由文本与写法字段，**不改变这张处方的设计结论**。
    """
    if index % 4 != 1:
        return values, ()
    style = ALL_STYLES[index % len(ALL_STYLES)]
    # 「单位后缀日量」在量限行上会改变可比性口径，只给普通药品行套。
    if style == "单位后缀日量" and values.get("drug_category") != "普通药品":
        style = "ISO 时间"
    return apply_style(values, style, index)


def _append_duplicate_pair(
    plans: list[RowPlan], index: int, counter: int, rng: random.Random
) -> int:
    """追加一组「重复给药」：同就诊 · 同药 · 同日两张，首张合规、次张违规。"""
    dept = rng.choice(DEPARTMENTS)
    drug = rng.choice(DRUGS_GENERAL)
    prescriber = (f"PR-{index % 97 + 1:03d}", f"医师{chr(0x7532 + index % 20)}")
    visit_no = f"SYN-VIS-DUP-{counter:04d}"
    day = f"2026-10-28 0{8 + counter % 2}:{(counter * 7) % 60:02d}:00"
    common = {
        "visit_no": visit_no,
        "drug_code": drug[0],
        "drug_name": drug[1],
        "spec": drug[2],
        "dosage_form": drug[3],
        "rx_type": "门诊一般患者",
        "drug_category": "普通药品",
        "days_qty": "1",
        "clinical_diag": rng.choice(DIAGNOSES),
    }
    for offset, expected in ((0, Conclusion.COMPLIANT.value), (1, Conclusion.VIOLATION.value)):
        row_index = index + offset
        values = _base_values(
            row_index,
            dept=dept,
            drug=drug,
            prescriber=prescriber,
            event_time=day.replace(" 0", f" {offset}"),
            **common,
        )
        plans.append(
            RowPlan(
                index=row_index,
                prescription_no=values["prescription_no"],
                expected_class=expected,
                profile="重复给药",
                features=("同就诊同药同日",),
                values=values,
            )
        )
    return index + 2


def _append_supernormal_group(
    plans: list[RowPlan], index: int, counter: int, size: int, rng: random.Random
) -> int:
    """追加一组「超常处方」：同一开方医师连开超量处方，第 4 张起触发提示线。"""
    dept = ("D502", "麻醉科门诊")
    drug = DRUGS[4]
    prescriber = (f"PR-SUP-{counter:03d}", f"医师{chr(0x7532 + counter % 20)}")
    for offset in range(size):
        row_index = index + offset
        values = _base_values(
            row_index,
            dept=dept,
            drug=drug,
            prescriber=prescriber,
            rx_type="门诊一般患者",
            drug_category="麻醉药品注射剂",
            days_qty="3",
            route="静脉滴注",
            clinical_diag="术后镇痛",
        )
        plans.append(
            RowPlan(
                index=row_index,
                prescription_no=values["prescription_no"],
                expected_class=Conclusion.VIOLATION.value,
                profile="超常处方",
                features=("同一开方医师连续超量",),
                values=values,
            )
        )
    return index + size


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render_csv(plans: list[RowPlan]) -> str:
    """渲染成 CSV 文本（口径列序 + 表头）。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(PRESCRIPTION_FIELDS)
    for plan in plans:
        writer.writerow(plan.row_values() + list(plan.extra_cells))
    return buffer.getvalue()


def render_gbk(plans: list[RowPlan]) -> bytes:
    """渲染成 GB18030 字节（验证入站侧的编码兜底）。

    GB18030 是 GBK 的超集：既编得动 GBK 里的汉字，也编得动生僻字。
    """
    return render_csv(plans).encode("gb18030")


def render_mixed(plans: list[RowPlan]) -> bytes:
    """渲染成**混编**字节：一段行是 GB18030，其余是 UTF-8。

    现实里同一批导出常被两套系统各写一段（老系统写 GBK、新系统写 UTF-8），
    拼在一起就是这个形态。混编文件在入站侧要么整文件解不开 → 整文件挂起，
    要么兜底解开后内容已乱——**两种结局都不允许部分判定**，所以本脚本把它
    单列成一份产物，让入站层的「整文件挂起」口径有真实样本可验。
    """
    total = len(plans)
    start = total // 3
    end = start + max(1, total // 10)

    chunks: list[bytes] = []
    header = io.StringIO(newline="")
    csv.writer(header, lineterminator="\n").writerow(PRESCRIPTION_FIELDS)
    chunks.append(header.getvalue().encode("utf-8"))
    for position, plan in enumerate(plans):
        line = io.StringIO(newline="")
        csv.writer(line, lineterminator="\n").writerow(
            plan.row_values() + list(plan.extra_cells)
        )
        text = line.getvalue()
        if start <= position < end:
            chunks.append(text.encode("gb18030"))
        else:
            chunks.append(text.encode("utf-8"))
    return b"".join(chunks)


def render_labels(plans: list[RowPlan]) -> str:
    """渲染逐行标签（处方号 / 设计结论 / 画像 / 脏数据形态）。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["prescription_no", "expected_class", "profile", "features"])
    for plan in plans:
        writer.writerow(
            [
                plan.prescription_no,
                plan.expected_class,
                plan.profile,
                "|".join(plan.features),
            ]
        )
    return buffer.getvalue()


def sha256_hex(payload: bytes) -> str:
    """字节流的 sha256。"""
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------
def build_manifest(
    plans: list[RowPlan],
    artifacts: list[dict[str, object]],
    *,
    seed: int,
    rows: int,
) -> dict[str, object]:
    """生成清单：种子、行数、配比、画像分布、形态覆盖、各产物 sha256。"""
    class_counter = Counter(plan.expected_class for plan in plans)
    profile_counter = Counter(plan.profile for plan in plans)
    feature_counter = Counter(
        feature for plan in plans for feature in plan.features
    )
    return {
        "generator": "gen_synthetic_orders",
        "seed": seed,
        "rows": rows,
        "batch_no": BATCH_NO,
        "columns": list(PRESCRIPTION_FIELDS),
        "usage_scope": (
            "仅作测试数据层与规模化回归用；产品对接边界见 spec.md §2.1 入站与 "
            "§2.3 出站字段规范"
        ),
        "target_mix": dict(TARGET_MIX),
        "class_counts": {
            name: class_counter.get(name, 0)
            for name in (
                Conclusion.COMPLIANT.value,
                Conclusion.VIOLATION.value,
                Conclusion.UNDETERMINABLE.value,
            )
        },
        "profile_counts": dict(sorted(profile_counter.items())),
        "feature_counts": dict(sorted(feature_counter.items())),
        "artifacts": artifacts,
        "notes": [
            "本批数据是**确定性合成**的，不是真实院内医嘱，也不冒充真实医嘱。",
            "字段口径以 spec.md §2.1 为准（29 列）；值是 HIS 导出文件里的原文。",
            "expected_class 是这张处方的设计结论；引擎实跑结论以判定台账为准。",
            "只作测试数据层与规模化回归用，不是产品对接边界。",
        ],
    }


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gen_synthetic_orders.py",
        description="按固定种子合成医嘱规模集（默认 2000 行，同种子逐字节可复现）",
    )
    parser.add_argument(
        "--out",
        default="data/synthetic_orders",
        help="落盘目录（相对项目根；默认 data/synthetic_orders）",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS, help="行数")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser.parse_args(argv)


def generate(
    dest_dir: Path, *, seed: int = DEFAULT_SEED, rows: int = DEFAULT_ROWS
) -> dict[str, object]:
    """生成全套产物并落盘，返回清单。"""
    if rows <= 0:
        raise ValueError("行数必须为正")
    plans = build_plans(seed=seed, rows=rows)
    dest_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[dict[str, object]] = []

    def _write(name: str, payload: bytes) -> None:
        path = dest_dir / name
        path.write_bytes(payload)
        artifacts.append(
            {
                "name": name,
                "bytes": len(payload),
                "sha256": sha256_hex(payload),
            }
        )

    csv_text = render_csv(plans)
    _write(f"orders_{seed}.csv", csv_text.encode("utf-8"))
    _write(f"orders_{seed}.gbk.csv", render_gbk(plans))
    _write(f"orders_{seed}.mixed.csv", render_mixed(plans))
    _write(f"labels_{seed}.csv", render_labels(plans).encode("utf-8"))

    manifest = build_manifest(plans, artifacts, seed=seed, rows=rows)
    manifest_path = dest_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def summarize(manifest: dict[str, object]) -> str:
    """分布摘要（人读）。"""
    counts = manifest["class_counts"]
    total = int(manifest["rows"])
    lines = [
        f"规模集已生成：{total} 行 · 种子 {manifest['seed']} · 批次 {manifest['batch_no']}",
        "三类结论配比：",
    ]
    for name, count in counts.items():  # type: ignore[union-attr]
        ratio = float(count) / total if total else 0.0
        lines.append(f"  {name:<6} {count:>6} 行 · {ratio:6.1%}（设定 {TARGET_MIX[name]:.0%}）")
    lines.append("画像分布：")
    for name, count in manifest["profile_counts"].items():  # type: ignore[union-attr]
        lines.append(f"  {name:<24} {count:>6} 行")
    lines.append("脏数据形态覆盖：")
    for name, count in manifest["feature_counts"].items():  # type: ignore[union-attr]
        lines.append(f"  {name:<24} {count:>6} 行")
    lines.append("产物：")
    for item in manifest["artifacts"]:  # type: ignore[union-attr]
        lines.append(
            f"  {item['name']:<28} {item['bytes']:>9} 字节 · sha256 {item['sha256'][:16]}…"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.rows <= 0:
        print("行数必须为正", file=sys.stderr)
        return EXIT_USAGE
    dest_dir = Path(args.out).expanduser()
    if not dest_dir.is_absolute():
        dest_dir = (PROJECT_ROOT / dest_dir).resolve()

    try:
        manifest = generate(dest_dir, seed=args.seed, rows=args.rows)
    except ValueError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        print(summarize(manifest))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    sys.exit(main())
