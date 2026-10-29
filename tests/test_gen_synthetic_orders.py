"""确定性种子合成 2000 行规模集（任务书 T37 · 数据层 2/4）。

对应任务书要求：固定随机种子生成 2000 行医嘱，规模与分布写进验收，覆盖
合规 / 违规 / 不可判定三类，含生僻字、GBK 混编行、空值、脏行与超长字段；
脚本内固定三类结论的配比并打印分布摘要；同种子两次生成的文件 sha256 一致。

本文件把「生成器写了 2000 行」与「这 2000 行是可用数据」分开验：

1. **规模与口径**：行数 = 2000，表头 = 29 列口径，每行列数不少于口径列数；
2. **可复现**：同种子两次生成的四份产物 + 清单逐字节一致；换种子产物不同；
3. **三类配比**：逐行标签的结论分布严格等于脚本内固定的配比（1200 / 500 / 300）；
4. **脏数据形态**：生僻字、空值、脏行、超长字段、GBK 混编逐项有样本；
5. **入站可读**：主规模集与 GB18030 变体逐字段等价，完整性校验通过；
6. **混编不被部分判定**：混编文件要么整文件读进来、要么整文件挂起，
   **不允许读出少于全量的行**（半份数据比判不了更危险）；
7. **标签不是编的**：把 2000 行喂给真实判定内核，逐条核对每行的设计结论与
   引擎实跑结论一致——合规行只出合规、不可判定行出第三态且不出违规、
   违规行至少命中一条规则；
8. **退出码**：行数非法时退出码非 0。
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "gen_synthetic_orders.py"
RULES_DIR = PROJECT_ROOT / "rules" / "sample"

_spec = importlib.util.spec_from_file_location("gen_synthetic_orders", SCRIPT_PATH)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
# dataclass 装饰器需要模块已登记进 sys.modules
sys.modules["gen_synthetic_orders"] = gen
_spec.loader.exec_module(gen)

from med_rule_fieldmap.engine.judge import judge_batch  # noqa: E402
from med_rule_fieldmap.ingest.csv_reader import (  # noqa: E402
    CsvReadError,
    read_prescription_rows,
)
from med_rule_fieldmap.ingest.integrity import (  # noqa: E402
    KIND_UNDECODABLE,
    check_csv_integrity,
)
from med_rule_fieldmap.models import PRESCRIPTION_FIELDS, Conclusion  # noqa: E402
from med_rule_fieldmap.rules.repository import RuleRepository  # noqa: E402

SEED = gen.DEFAULT_SEED
ROWS = gen.DEFAULT_ROWS


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    """生成一批规模集（模块级夹具：一次生成，多条断言复用）。"""
    dest = tmp_path_factory.mktemp("synthetic")
    manifest = gen.generate(dest, seed=SEED, rows=ROWS)
    return dest, manifest


@pytest.fixture(scope="module")
def labels(generated) -> dict[str, tuple[str, str]]:
    """处方号 → (设计结论, 画像)。"""
    dest, _ = generated
    path = dest / f"labels_{SEED}.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            row["prescription_no"]: (row["expected_class"], row["profile"])
            for row in csv.DictReader(handle)
        }


def _read_raw_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """按 csv 口径读原始行（含表头）。

    不用 ``wc -l``：部分样本的单元格里带换行（真实导出里也有），
    物理行数与记录数不是一回事，记录数才是「多少张处方」。
    """
    text = path.read_text(encoding="utf-8")
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = list(reader)
    return rows[0], rows[1:]


# ---------------------------------------------------------------------------
# 1. 规模与口径
# ---------------------------------------------------------------------------
def test_规模集行数为2000且列口径齐全(generated):
    dest, manifest = generated
    header, records = _read_raw_rows(dest / f"orders_{SEED}.csv")

    assert header == list(PRESCRIPTION_FIELDS), "表头必须是 29 列口径"
    assert len(records) == ROWS, f"应有 {ROWS} 行处方，实际 {len(records)} 行"
    assert manifest["rows"] == ROWS
    # 每行字段数不少于口径列数（脏数据形态里有多余列，故只做下限断言）。
    assert all(len(record) >= len(PRESCRIPTION_FIELDS) for record in records)
    # 批次号写在每一行上。
    assert {record[0] for record in records} == {gen.BATCH_NO}


# ---------------------------------------------------------------------------
# 2. 可复现
# ---------------------------------------------------------------------------
def test_同种子两次生成逐字节一致(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    gen.generate(first, seed=SEED, rows=ROWS)
    gen.generate(second, seed=SEED, rows=ROWS)

    names = sorted(item.name for item in first.iterdir())
    assert names == sorted(item.name for item in second.iterdir())
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), (
            f"{name} 同种子两次生成不一致（sha256 应相同）"
        )

    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["artifacts"]:
        import hashlib

        digest = hashlib.sha256((first / item["name"]).read_bytes()).hexdigest()
        assert digest == item["sha256"], f"{item['name']} 的 sha256 与清单对不上"


def test_换种子产物不同(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    gen.generate(first, seed=SEED, rows=ROWS)
    gen.generate(second, seed=SEED + 1, rows=ROWS)

    name = f"orders_{SEED}.csv"
    assert (first / name).read_bytes() != (second / f"orders_{SEED + 1}.csv").read_bytes()


# ---------------------------------------------------------------------------
# 3. 三类结论配比
# ---------------------------------------------------------------------------
def test_三类结论配比严格等于设定(generated, labels):
    _, manifest = generated
    counts = Counter(expected for expected, _ in labels.values())

    assert counts[Conclusion.COMPLIANT.value] == 1200
    assert counts[Conclusion.VIOLATION.value] == 500
    assert counts[Conclusion.UNDETERMINABLE.value] == 300
    assert sum(counts.values()) == ROWS
    assert manifest["class_counts"] == {
        Conclusion.COMPLIANT.value: 1200,
        Conclusion.VIOLATION.value: 500,
        Conclusion.UNDETERMINABLE.value: 300,
    }
    # 配比与脚本内固定的设定一致（行数取整后误差不超过一行）。
    for name, ratio in gen.TARGET_MIX.items():
        assert abs(counts[name] / ROWS - ratio) <= 1 / ROWS


def test_分布摘要打印三类计数(tmp_path, capsys):
    exit_code = gen.main(
        ["--out", str(tmp_path), "--seed", str(SEED), "--rows", str(ROWS)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "2000 行" in out
    for name in (Conclusion.COMPLIANT.value, Conclusion.VIOLATION.value,
                 Conclusion.UNDETERMINABLE.value):
        assert name in out
    assert "1200" in out and "500" in out and "300" in out


def test_机器可读清单含种子与各产物指纹(tmp_path, capsys):
    exit_code = gen.main(
        ["--out", str(tmp_path), "--seed", str(SEED), "--rows", "200", "--json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["seed"] == SEED
    assert payload["rows"] == 200
    names = {item["name"] for item in payload["artifacts"]}
    assert f"orders_{SEED}.csv" in names
    assert f"orders_{SEED}.gbk.csv" in names
    assert f"orders_{SEED}.mixed.csv" in names
    assert f"labels_{SEED}.csv" in names
    for item in payload["artifacts"]:
        assert len(item["sha256"]) == 64


# ---------------------------------------------------------------------------
# 4. 脏数据形态覆盖
# ---------------------------------------------------------------------------
def test_脏数据形态逐项有样本(generated, labels):
    dest, manifest = generated
    features = manifest["feature_counts"]

    # 生僻字、超长字段、脏行（空白 / 多余列 / 逗号引号 / 换行）都在形态表里。
    for name in (
        "生僻字医师名",
        "超长诊断",
        "超长医师名",
        "空白填充",
        "多余列",
        "值内含逗号与引号",
        "值内含换行",
        "单位后缀日量",
        "频次别名",
        "ISO 时间",
    ):
        assert features.get(name, 0) > 0, f"脏数据形态「{name}」一条样本都没有"

    # 空值：缺的是规则行声明过的依赖字段（rx_type / drug_category / days_qty /
    # skin_test_result / age_unit）。
    _, records = _read_raw_rows(dest / f"orders_{SEED}.csv")
    index = {name: position for position, name in enumerate(PRESCRIPTION_FIELDS)}
    for field in ("rx_type", "drug_category", "days_qty", "skin_test_result", "age_unit"):
        empty = sum(
            1 for record in records if not record[index[field]].strip()
        )
        assert empty > 0, f"字段 {field} 没有空值样本"

    # 超长字段：真的有超长单元（不是「比平均长一点」）。
    longest = max(len(cell) for record in records for cell in record)
    assert longest >= 500, f"最长单元只有 {longest} 字，不构成超长字段样本"

    # 生僻字：真的落在单元格里。
    assert any(
        any(char in cell for cell in record for char in gen.RARE_CHARS)
        for record in records
    )


def test_GBK混编行单独成产物(generated):
    dest, _ = generated
    mixed = dest / f"orders_{SEED}.mixed.csv"
    raw = mixed.read_bytes()

    # 混编：整份文件解不开 UTF-8，但其中确有 GB18030 字节。
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    assert len(raw) > 0
    # 纯 GBK 变体同样解不开 UTF-8（否则入站的编码兜底分支根本走不到）。
    with pytest.raises(UnicodeDecodeError):
        (dest / f"orders_{SEED}.gbk.csv").read_bytes().decode("utf-8")


# ---------------------------------------------------------------------------
# 5. 入站可读
# ---------------------------------------------------------------------------
def test_入站读主规模集与GBK变体字段完全等价(generated):
    dest, _ = generated
    main_rows = read_prescription_rows(dest / f"orders_{SEED}.csv")
    gbk_rows = read_prescription_rows(dest / f"orders_{SEED}.gbk.csv")

    assert len(main_rows) == ROWS
    assert len(gbk_rows) == ROWS
    for left, right in zip(main_rows, gbk_rows):
        assert asdict(left) == asdict(right), "编码兜底读出来的字段与主文件不一致"

    # 完整性校验通过（不挂起）。
    assert check_csv_integrity(dest / f"orders_{SEED}.csv").ok
    assert check_csv_integrity(dest / f"orders_{SEED}.gbk.csv").ok


def test_混编文件不被部分判定(generated):
    """混编文件要么整份读进来、要么整份挂起——不允许读出少于全量的行。"""
    dest, _ = generated
    mixed = dest / f"orders_{SEED}.mixed.csv"

    try:
        rows = read_prescription_rows(mixed)
    except CsvReadError as exc:
        # 整文件挂起：解不开就明确报错，不做部分读取。
        report = check_csv_integrity(mixed)
        assert not report.ok
        assert report.kind == KIND_UNDECODABLE
        assert "挂起" in str(exc) or "不做部分" in str(exc)
    else:
        # 兜底解开了：也必须是全量，不许少行。
        assert len(rows) == ROWS


# ---------------------------------------------------------------------------
# 6. 标签不是编的：与真实判定内核逐条核对
# ---------------------------------------------------------------------------
def test_设计结论与引擎实跑结论逐条一致(generated, labels):
    dest, _ = generated
    rows = read_prescription_rows(dest / f"orders_{SEED}.csv")

    repo = RuleRepository(RULES_DIR)
    repo.load()
    assert not repo.errors, f"样例规则集载入有错：{[str(e) for e in repo.errors]}"
    assert repo.effective_rules, "生效规则集为空"

    result = judge_batch(rows, repo.effective_rules, batch_no=gen.BATCH_NO)
    by_prescription: dict[str, list] = defaultdict(list)
    for judgement in result.rows:
        by_prescription[judgement.prescription_no].append(judgement)

    mismatches: list[str] = []
    for prescription_no, (expected, profile) in labels.items():
        conclusions = [
            judgement.conclusion.value
            for judgement in by_prescription.get(prescription_no, [])
        ]
        if expected == Conclusion.COMPLIANT.value:
            # 合规行：只允许出合规（有不可判定就说明画像设计漏了依赖字段）。
            if any(item != Conclusion.COMPLIANT.value for item in conclusions):
                mismatches.append(f"{prescription_no}[{profile}] 设计合规却出了 {conclusions}")
        elif expected == Conclusion.UNDETERMINABLE.value:
            # 不可判定行：必须出第三态，且不得同时出违规。
            if Conclusion.VIOLATION.value in conclusions:
                mismatches.append(f"{prescription_no}[{profile}] 设计不可判定却出了违规")
            if Conclusion.UNDETERMINABLE.value not in conclusions:
                mismatches.append(f"{prescription_no}[{profile}] 设计不可判定却没出第三态")
        else:
            # 违规行：至少命中一条规则。
            if Conclusion.VIOLATION.value not in conclusions:
                mismatches.append(f"{prescription_no}[{profile}] 设计违规却没命中规则")

    assert not mismatches, "设计结论与引擎实跑不一致：\n" + "\n".join(mismatches[:10])


def test_三类结论在判定台账里都出现(generated):
    """跑出来的判定行三态齐全（不是所有行都落同一态）。"""
    dest, _ = generated
    rows = read_prescription_rows(dest / f"orders_{SEED}.csv")
    repo = RuleRepository(RULES_DIR)
    repo.load()
    result = judge_batch(rows, repo.effective_rules, batch_no=gen.BATCH_NO)

    counts = Counter(judgement.conclusion.value for judgement in result.rows)
    assert counts[Conclusion.COMPLIANT.value] > 0
    assert counts[Conclusion.VIOLATION.value] > 0
    assert counts[Conclusion.UNDETERMINABLE.value] > 0
    # 逐处方 × 逐规则恰好一行，无重复无遗漏。
    assert len(result.rows) == sum(result.candidate_rule_counts.values())


# ---------------------------------------------------------------------------
# 7. 退出码
# ---------------------------------------------------------------------------
def test_行数非法时退出码非0(tmp_path):
    assert gen.main(["--out", str(tmp_path), "--rows", "0"]) == 2
    assert gen.main(["--out", str(tmp_path), "--rows", "-5"]) == 2


def test_清单写明数据来源边界(generated):
    """清单里必须写清「合成数据，只作测试数据层」。"""
    _, manifest = generated
    notes = " ".join(manifest["notes"])
    assert "确定性合成" in notes
    assert "不是真实院内医嘱" in notes
    assert "测试数据层" in manifest["usage_scope"]
