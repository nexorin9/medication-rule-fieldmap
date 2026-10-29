#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MIMIC-IV demo 公开对照子集下载器（可重跑 + 完整性校验 + 断网降级）。

数据来源与许可（随产物一并保留，不要删改本段）
------------------------------------------------
* 数据集：MIMIC-IV Demo（Medical Information Mart for Intensive Care IV Demo），
  版本 2.2。
* 官方页面：https://physionet.org/content/mimic-iv-demo/2.2/
* 官方文件入口：https://physionet.org/files/mimic-iv-demo/2.2/prescriptions.csv.gz
* 维护方：MIT Laboratory for Computational Physiology（MIT-LCP）。
  引用：Johnson AEW, Pollard TJ, Mark RG, et al. MIMIC-IV Demo.
  DOI: https://doi.org/10.7910/DVN/H1WKJP
* 访问级别：Credentialed Access —— 需要 PhysioNet 账号、接受 PhysioNet 数据使用
  协议（DUA），并完成 CITI「Data or Specimens Only」培训。匿名直连会被拒
  （HTTP 403 Forbidden Access）。凭据从环境变量读取，本仓库不写死任何账号口令。
* 许可口径：PhysioNet Credentialed Health Data License Agreement
  （https://physionet.org/content/physionet-licenses-and-citations/）。
  本仓库**不转发、不再分发**该数据集本身，只保留下载脚本与结构快照。

用途声明（重要，勿删）
----------------------
本脚本取得的数据**只作对照与测试数据层**（字段形态对照、回归基线），
**不是本产品的对接边界**。本产品的正式对接口径是：

* 入站：``spec.md`` §2.1「医嘱落地文件字段规范」（26 列 CSV / HL7 v2 ORM^O01）
* 出站：``spec.md`` §2.3「HIS 审方复核字段规范」
  （POST ``{HIS_BASE_URL}/api/v1/prescription/review``）

MIMIC-IV Demo 恰好缺麻精类别、皮试结果、处方类型与医师处方权，因此它覆盖的是
本轮「不可判定」路径的触发条件，而不是合规判定结论本身。

用法
----
    python3 scripts/fetch_mimic_demo.py                    # 下载到 data/mimic_demo/
    python3 scripts/fetch_mimic_demo.py --force            # 强制重下（覆盖已有文件）
    python3 scripts/fetch_mimic_demo.py --json             # 机器可读结果
    python3 scripts/fetch_mimic_demo.py --source-url URL   # 走镜像（内网 / 自建）
    python3 scripts/fetch_mimic_demo.py --dest <目录>      # 换落盘目录

环境变量
--------
    MIMIC_PHYSIONET_USERNAME  PhysioNet 账号（下载需凭据时必填）
    MIMIC_PHYSIONET_PASSWORD  PhysioNet 口令（下载需凭据时必填）

退出码
------
    0  下载成功并通过完整性校验（或目标目录已有可复用的合法文件）
    2  降级：网络不可用 / 缺凭据 / HTTP 错误，改用仓库内结构快照并打印告警
    3  下载到了文件但完整性校验不通过（硬失败，**不降级**，避免用快照掩盖损坏）
    4  参数或环境错误
"""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXIT_OK = 0
EXIT_DEGRADED = 2
EXIT_VALIDATION_FAILED = 3
EXIT_USAGE = 4

DATASET_NAME = "MIMIC-IV Demo v2.2"
DATASET_HOME = "https://physionet.org/content/mimic-iv-demo/2.2/"
LICENSE_URL = "https://physionet.org/content/physionet-licenses-and-citations/"
FILENAME = "prescriptions.csv.gz"
SNAPSHOT_RELATIVE = Path("tests/data/mimic_demo_snapshot/prescriptions.snapshot.csv")
MANIFEST_NAME = "manifest.json"
DEGRADED_NOTE_NAME = "DEGRADED.txt"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

#: 官方文件入口（需 PhysioNet 凭据）。数组顺序即尝试顺序。
DEFAULT_SOURCES: tuple[str, ...] = (
    "https://physionet.org/files/mimic-iv-demo/2.2/prescriptions.csv.gz",
)

#: 校验用的必需列：这几个列一旦缺了，说明拿到的不是 prescriptions 表。
REQUIRED_COLUMNS: tuple[str, ...] = (
    "hadm_id",
    "drug",
    "dose_val_rx",
    "route",
)

#: 完整性校验下限（可由命令行覆盖）。MIMIC-IV Demo 的 prescriptions 约 5 万行，
#: 压缩后数 MB；下限取得宽松，避免不同版本体量变化造成误判。
DEFAULT_MIN_BYTES = 100_000
DEFAULT_MIN_RECORDS = 1_000


class FetchError(Exception):
    """下载阶段失败（网络不可用 / HTTP 错误 / 凭据不足）。"""


class ValidationError(Exception):
    """完整性校验不通过（体积 / 记录数 / 必需列）。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}：{detail}")
        self.reason = reason
        self.detail = detail


@dataclass
class Source:
    """一个下载源。"""

    name: str
    url: str
    requires_physionet_auth: bool = False


@dataclass
class ValidationReport:
    """一次完整性校验的结果。"""

    byte_size: int
    record_count: int
    columns: list[str] = field(default_factory=list)
    sha256: str = ""

    def as_dict(self) -> dict:
        return {
            "byte_size": self.byte_size,
            "record_count": self.record_count,
            "columns": list(self.columns),
            "sha256": self.sha256,
        }


# ---------------------------------------------------------------------------
# 网络层（测试可 monkeypatch http_get 模拟断网 / 非法响应）
# ---------------------------------------------------------------------------
def read_credentials() -> tuple[str | None, str | None]:
    """从环境变量读取 PhysioNet 凭据；缺任一项即视为无凭据。"""
    username = os.environ.get("MIMIC_PHYSIONET_USERNAME") or None
    password = os.environ.get("MIMIC_PHYSIONET_PASSWORD") or None
    if not username or not password:
        return (None, None)
    return (username, password)


def build_request(
    url: str,
    username: str | None = None,
    password: str | None = None,
) -> urllib.request.Request:
    """构造请求；给了凭据就带 HTTP Basic 认证头（PhysioNet 官方下载方式）。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if username and password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
    return request


def http_get(
    url: str,
    timeout: float = 60.0,
    username: str | None = None,
    password: str | None = None,
) -> bytes:
    """真实发起一次 GET 并读回全部字节；失败一律抛 :class:`FetchError`。"""
    request = build_request(url, username, password)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise FetchError(
                f"HTTP {exc.code}：目标服务器拒绝匿名访问。"
                "PhysioNet 的 MIMIC-IV Demo 下载属 Credentialed Access，"
                "需在环境变量 MIMIC_PHYSIONET_USERNAME / MIMIC_PHYSIONET_PASSWORD "
                "提供账号（需已接受数据使用协议并完成 CITI 培训）"
            ) from exc
        raise FetchError(f"HTTP {exc.code}：{exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"网络不可用：{exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise FetchError(f"下载超时（{timeout}s）：{exc}") from exc
    except OSError as exc:  # pragma: no cover - 极端网络故障
        raise FetchError(f"网络错误：{exc}") from exc


# ---------------------------------------------------------------------------
# 校验层
# ---------------------------------------------------------------------------
def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def decode_rows(payload: bytes) -> list[list[str]]:
    """解开 gzip 并解析成 CSV 行；不是 gzip 或解不开即判校验失败。"""
    try:
        text = gzip.decompress(payload).decode("utf-8-sig")
    except (OSError, EOFError) as exc:
        raise ValidationError("文件不是合法的 gzip 数据", str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise ValidationError("CSV 不是 UTF-8 编码", str(exc)) from exc
    reader = csv.reader(io.StringIO(text))
    return [row for row in reader]


def validate_payload(
    payload: bytes,
    min_bytes: int = DEFAULT_MIN_BYTES,
    min_records: int = DEFAULT_MIN_RECORDS,
    required_columns: tuple[str, ...] = REQUIRED_COLUMNS,
) -> ValidationReport:
    """完整性校验：体积下限 + 记录数下限 + 必需列齐全。"""
    if len(payload) < min_bytes:
        raise ValidationError(
            "文件体积低于下限",
            f"{len(payload)} 字节 < {min_bytes} 字节（多半是错误页而非数据集）",
        )
    rows = decode_rows(payload)
    if not rows:
        raise ValidationError("文件解析后为空", "没有表头")
    header = [cell.strip() for cell in rows[0]]
    missing = [column for column in required_columns if column not in header]
    if missing:
        raise ValidationError("缺少必需列", "、".join(missing))
    record_count = max(len(rows) - 1, 0)
    if record_count < min_records:
        raise ValidationError(
            "记录数低于下限",
            f"{record_count} 行 < {min_records} 行",
        )
    return ValidationReport(
        byte_size=len(payload),
        record_count=record_count,
        columns=header,
        sha256=sha256_hex(payload),
    )


# ---------------------------------------------------------------------------
# 落盘层
# ---------------------------------------------------------------------------
def build_manifest(
    source_name: str,
    source_url: str,
    report: ValidationReport | None,
    degraded: bool,
    degrade_reason: str = "",
    snapshot_origin: str = "",
    min_bytes: int = DEFAULT_MIN_BYTES,
    min_records: int = DEFAULT_MIN_RECORDS,
) -> dict:
    """清单：来源、时间、校验结果、降级标记、许可口径。"""
    return {
        "dataset": DATASET_NAME,
        "dataset_home": DATASET_HOME,
        "license": LICENSE_URL,
        "usage_scope": "仅作对照与测试数据层；产品对接边界见 spec.md §2.1 / §2.3",
        "source_name": source_name,
        "source_url": source_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "snapshot_origin": snapshot_origin,
        "min_bytes": min_bytes,
        "min_records": min_records,
        "validation": report.as_dict() if report else {},
    }


def write_manifest(dest_dir: Path, manifest: dict) -> Path:
    path = dest_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def install_snapshot(dest_dir: Path, snapshot_path: Path) -> Path:
    """降级：把仓库内结构快照复制到落盘目录，并留下醒目的说明文件。"""
    target = dest_dir / "prescriptions.csv"
    target.write_bytes(snapshot_path.read_bytes())
    note = dest_dir / DEGRADED_NOTE_NAME
    note.write_text(
        "本目录下的 prescriptions.csv 是**结构快照**（手写占位样例），"
        "不是 MIMIC-IV Demo 真实数据。\n"
        f"结构快照来源：{snapshot_path}\n"
        "原因：公开数据集下载未成功（见 manifest.json 的 degrade_reason）。\n"
        "恢复办法：配置 MIMIC_PHYSIONET_USERNAME / MIMIC_PHYSIONET_PASSWORD "
        "后重跑 scripts/fetch_mimic_demo.py --force。\n"
        "该数据只作对照与测试数据层，不是本产品的对接边界。\n",
        encoding="utf-8",
    )
    return target


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fetch_mimic_demo.py",
        description="下载 MIMIC-IV Demo 的 prescriptions 子集（可重跑 + 校验 + 断网降级）",
    )
    parser.add_argument(
        "--dest",
        default="data/mimic_demo",
        help="落盘目录（相对项目根；默认 data/mimic_demo）",
    )
    parser.add_argument(
        "--source-url",
        action="append",
        default=None,
        help="指定下载源，可重复；不给则用官方 PhysioNet 文件入口",
    )
    parser.add_argument("--source-name", default="", help="下载源名称（写进清单）")
    parser.add_argument("--timeout", type=float, default=60.0, help="单次下载超时秒数")
    parser.add_argument("--min-bytes", type=int, default=DEFAULT_MIN_BYTES, help="体积下限")
    parser.add_argument(
        "--min-records", type=int, default=DEFAULT_MIN_RECORDS, help="记录数下限"
    )
    parser.add_argument(
        "--snapshot",
        default=str(PROJECT_ROOT / SNAPSHOT_RELATIVE),
        help="降级用的本地结构快照路径",
    )
    parser.add_argument("--force", action="store_true", help="强制重下（覆盖已有文件）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser.parse_args(argv)


def resolve_sources(args: argparse.Namespace) -> list[Source]:
    urls = args.source_url or list(DEFAULT_SOURCES)
    name = args.source_name or ("custom-mirror" if args.source_url else "physionet-files")
    return [Source(name=name, url=url) for url in urls]


def emit(args: argparse.Namespace, manifest: dict, message: str) -> None:
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        print(message)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dest_dir = Path(args.dest).expanduser()
    if not dest_dir.is_absolute():
        dest_dir = (PROJECT_ROOT / dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    target = dest_dir / FILENAME
    sources = resolve_sources(args)

    # 1) 可重复执行：已有文件且校验通过就直接复用，不再打网络。
    if target.exists() and not args.force:
        cached = target.read_bytes()
        try:
            report = validate_payload(cached, args.min_bytes, args.min_records)
        except ValidationError:
            report = None
        if report is not None:
            manifest = build_manifest(
                source_name="cached",
                source_url=str(target),
                report=report,
                degraded=False,
                min_bytes=args.min_bytes,
                min_records=args.min_records,
            )
            write_manifest(dest_dir, manifest)
            emit(
                args,
                manifest,
                f"已有文件且校验通过，跳过下载：{target}\n"
                f"  记录数 {report.record_count} 行 · 体积 {report.byte_size} 字节 · "
                f"sha256 {report.sha256[:16]}…",
            )
            return EXIT_OK

    username, password = read_credentials()
    fetch_errors: list[str] = []
    validation_errors: list[str] = []

    # 2) 逐个源尝试下载；任何一个源校验通过即落盘返回成功。
    for source in sources:
        try:
            payload = http_get(source.url, args.timeout, username, password)
        except FetchError as exc:
            fetch_errors.append(f"{source.url}：{exc}")
            continue
        except OSError as exc:
            # 网络层的其它异常（URLError / socket.timeout 等）同样按断网处理
            fetch_errors.append(f"{source.url}：网络异常 {exc}")
            continue
        try:
            report = validate_payload(payload, args.min_bytes, args.min_records)
        except ValidationError as exc:
            validation_errors.append(f"{source.url}：{exc}")
            continue
        target.write_bytes(payload)
        manifest = build_manifest(
            source_name=source.name,
            source_url=source.url,
            report=report,
            degraded=False,
            min_bytes=args.min_bytes,
            min_records=args.min_records,
        )
        write_manifest(dest_dir, manifest)
        emit(
            args,
            manifest,
            f"下载成功：{target}\n"
            f"  来源 {source.url}\n"
            f"  记录数 {report.record_count} 行 · 体积 {report.byte_size} 字节 · "
            f"sha256 {report.sha256[:16]}…\n"
            "  用途：仅作对照与测试数据层，不是本产品对接边界",
        )
        return EXIT_OK

    # 3) 下载到了但校验不通过 → 硬失败，不降级（避免用快照掩盖数据损坏）。
    if validation_errors:
        manifest = build_manifest(
            source_name="validation-failed",
            source_url=sources[0].url if sources else "",
            report=None,
            degraded=False,
            degrade_reason="；".join(validation_errors),
            min_bytes=args.min_bytes,
            min_records=args.min_records,
        )
        write_manifest(dest_dir, manifest)
        emit(
            args,
            manifest,
            "完整性校验不通过（硬失败，不降级）：\n  " + "\n  ".join(validation_errors),
        )
        return EXIT_VALIDATION_FAILED

    # 4) 一个源都没下下来 → 降级到本地结构快照。
    snapshot_path = Path(args.snapshot).expanduser()
    if not snapshot_path.is_absolute():
        snapshot_path = (PROJECT_ROOT / snapshot_path).resolve()
    reason = "；".join(fetch_errors) or "没有可用下载源"
    if not snapshot_path.exists():
        emit(
            args,
            {"degraded": True, "degrade_reason": reason},
            f"下载失败且本地结构快照缺失，无法降级：{reason}",
        )
        return EXIT_VALIDATION_FAILED

    installed = install_snapshot(dest_dir, snapshot_path)
    manifest = build_manifest(
        source_name="local-snapshot",
        source_url="",
        report=None,
        degraded=True,
        degrade_reason=reason,
        snapshot_origin=str(snapshot_path),
        min_bytes=args.min_bytes,
        min_records=args.min_records,
    )
    write_manifest(dest_dir, manifest)
    emit(
        args,
        manifest,
        "警告：公开数据集下载未成功，已降级到仓库内结构快照。\n"
        f"  原因：{reason}\n"
        f"  落点：{installed}（**结构快照，非 MIMIC-IV Demo 真实数据**）\n"
        "  恢复：配置 MIMIC_PHYSIONET_USERNAME / MIMIC_PHYSIONET_PASSWORD 后重跑 "
        "--force\n"
        "  用途：该数据只作对照与测试数据层，不是本产品对接边界",
    )
    return EXIT_DEGRADED


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    sys.exit(main())
