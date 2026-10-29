"""公开对照数据集下载脚本（MIMIC-IV Demo 子集）。

对应任务书 T36：脚本要**联网下载**公开 demo 子集、做完整性校验（体积 + 记录数
下限）、可重复执行，并且断网时降级到仓库内本地结构快照且打印明确告警；
下载成功与降级用不同退出码区分，校验不通过必须非 0 退出。

覆盖七组硬断言：

1. **断网降级**：monkeypatch 网络层抛断网异常 → 退出码为降级码（≠ 0），输出含
   明确告警，落盘目录出现结构快照与降级说明文件，清单标记 ``degraded=true``；
2. **校验不通过退出码非 0**：下到体积过小的内容 → 退出码非 0，且**不落降级快照**
   （不用快照掩盖损坏）；
3. **真实 HTTP 通道自检**：本地真实 http.server + 真实 urllib 传输层（非 mock）
   → 下载成功、退出码 0、清单记录 sha256 与记录数；
4. **可重复执行**：目标目录已有合法文件时再跑一次不再打网络（请求数恒为 0），
   退出码仍为 0；
5. **凭据位**：给了账号口令就带 HTTP Basic 认证头，没给就不带；
6. **退出码语义**：成功 / 降级 / 校验失败三码互不相同，降级码与校验码都非 0；
7. **用途边界**：清单里写明该数据只作对照与测试数据层，不是产品对接边界。
"""

from __future__ import annotations

import base64
import csv
import gzip
import importlib.util
import io
import json
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "fetch_mimic_demo.py"

_spec = importlib.util.spec_from_file_location("fetch_mimic_demo", SCRIPT_PATH)
assert _spec and _spec.loader
fetch_mimic_demo = importlib.util.module_from_spec(_spec)
# dataclass 装饰器需要模块已登记进 sys.modules
sys.modules["fetch_mimic_demo"] = fetch_mimic_demo
_spec.loader.exec_module(fetch_mimic_demo)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------
def _make_payload(rows: int = 8, with_required_columns: bool = True) -> bytes:
    """造一份 gzip 过的 prescriptions CSV（结构与 MIMIC-IV Demo 一致）。"""
    header = [
        "row_id",
        "subject_id",
        "hadm_id",
        "icustay_id",
        "startdate",
        "enddate",
        "drug_type",
        "formulary_drug_cod",
        "gsn_ndc",
        "prod_strength",
        "dose_val_rx",
        "dose_unit_rx",
        "form_val_disp",
        "form_unit_disp",
        "route",
        "starttime",
        "endtime",
        "stoptime",
        "orderid",
        "order_provider_id",
        "order_category",
        "order_subcategory",
        "order_reason",
        "order_source",
        "volume",
        "dose_unit_syntax",
        "drug",
    ]
    if not with_required_columns:
        header = [column for column in header if column != "route"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for index in range(rows):
        writer.writerow(
            [
                900001 + index,
                900001,
                800001,
                700001,
                "2201-01-03",
                "2201-01-06",
                "MAIN",
                "DEMO-0001",
                "0000000001",
                "500 MG",
                1,
                "MG",
                1,
                "MG",
                "PO",
                "08:00",
                "08:00",
                "08:00",
                910001 + index,
                920001,
                "MAIN",
                "DEMO",
                "SYNTHETIC",
                "PROVIDER",
                0.5,
                "SYNTHETIC",
                "Acetaminophen",
            ]
        )
    return gzip.compress(buffer.getvalue().encode("utf-8"))


class _Handler(BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定名
        if self.path.endswith("prescriptions.csv.gz"):
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)
        else:
            self.send_error(404)

    def log_message(self, *args):  # noqa: D102 - 静默测试服务器日志
        return


@pytest.fixture()
def local_http_server():
    """起一个本地真实 HTTP 服务器，充当数据集下载源。"""
    _Handler.payload = _make_payload(rows=8)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host}:{port}/prescriptions.csv.gz"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 断言组
# ---------------------------------------------------------------------------
def test_offline_degrades_to_local_snapshot(tmp_path, monkeypatch, capsys):
    """断网 → 降级到仓库内结构快照 + 明确告警 + 降级退出码（非 0）。"""

    def _offline(url, timeout=60.0, username=None, password=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(fetch_mimic_demo, "http_get", _offline)
    dest = tmp_path / "out"
    code = fetch_mimic_demo.main(["--dest", str(dest)])

    assert code == fetch_mimic_demo.EXIT_DEGRADED
    assert code != 0

    out = capsys.readouterr().out
    assert "降级" in out
    assert "结构快照" in out

    assert (dest / "prescriptions.csv").exists()
    assert (dest / fetch_mimic_demo.DEGRADED_NOTE_NAME).exists()
    manifest = json.loads((dest / fetch_mimic_demo.MANIFEST_NAME).read_text("utf-8"))
    assert manifest["degraded"] is True
    assert manifest["degrade_reason"]
    assert manifest["snapshot_origin"].endswith("prescriptions.snapshot.csv")


def test_validation_failure_exits_non_zero_without_degrading(tmp_path, monkeypatch, capsys):
    """校验不通过 → 退出码非 0，且不落降级快照（不用快照掩盖损坏）。"""

    def _garbage(url, timeout=60.0, username=None, password=None):
        return b"not-a-gzip-dataset"

    monkeypatch.setattr(fetch_mimic_demo, "http_get", _garbage)
    dest = tmp_path / "out"
    code = fetch_mimic_demo.main(["--dest", str(dest), "--min-bytes", "10", "--min-records", "1"])

    assert code != 0
    assert code == fetch_mimic_demo.EXIT_VALIDATION_FAILED

    out = capsys.readouterr().out
    assert "校验" in out
    assert not (dest / "prescriptions.csv").exists()
    assert not (dest / fetch_mimic_demo.DEGRADED_NOTE_NAME).exists()
    manifest = json.loads((dest / fetch_mimic_demo.MANIFEST_NAME).read_text("utf-8"))
    assert manifest["degraded"] is False


def test_missing_required_column_is_validation_failure(tmp_path, monkeypatch):
    """缺必需列（route）同样判校验失败，退出码非 0。"""

    def _no_route(url, timeout=60.0, username=None, password=None):
        return _make_payload(rows=8, with_required_columns=False)

    monkeypatch.setattr(fetch_mimic_demo, "http_get", _no_route)
    code = fetch_mimic_demo.main(
        ["--dest", str(tmp_path / "out"), "--min-bytes", "10", "--min-records", "1"]
    )
    assert code == fetch_mimic_demo.EXIT_VALIDATION_FAILED


def test_real_http_download_succeeds(tmp_path, local_http_server):
    """真实 HTTP 通道自检：本地 http.server + 真实 urllib 传输层（非 mock）。"""
    dest = tmp_path / "out"
    code = fetch_mimic_demo.main(
        [
            "--dest",
            str(dest),
            "--source-url",
            local_http_server,
            "--source-name",
            "local-http",
            "--min-bytes",
            "64",
            "--min-records",
            "3",
            "--json",
        ]
    )
    assert code == fetch_mimic_demo.EXIT_OK

    target = dest / fetch_mimic_demo.FILENAME
    assert target.exists()
    manifest = json.loads((dest / fetch_mimic_demo.MANIFEST_NAME).read_text("utf-8"))
    assert manifest["degraded"] is False
    assert manifest["source_name"] == "local-http"
    assert manifest["validation"]["record_count"] == 8
    assert len(manifest["validation"]["sha256"]) == 64
    assert "hadm_id" in manifest["validation"]["columns"]


def test_rerun_reuses_valid_file_without_network(tmp_path, local_http_server, monkeypatch):
    """可重复执行：已有合法文件时再跑一次不打网络，退出码仍为 0。"""
    dest = tmp_path / "out"
    args = [
        "--dest",
        str(dest),
        "--source-url",
        local_http_server,
        "--min-bytes",
        "64",
        "--min-records",
        "3",
    ]
    assert fetch_mimic_demo.main(args) == fetch_mimic_demo.EXIT_OK

    calls: list[str] = []

    def _counting(url, timeout=60.0, username=None, password=None):
        calls.append(url)
        raise AssertionError("第二次运行不应再打网络")

    monkeypatch.setattr(fetch_mimic_demo, "http_get", _counting)
    assert fetch_mimic_demo.main(args) == fetch_mimic_demo.EXIT_OK
    assert calls == []


def test_force_re_downloads(tmp_path, local_http_server, monkeypatch):
    """--force 强制重下（重新发起网络请求）。"""
    dest = tmp_path / "out"
    args = [
        "--dest",
        str(dest),
        "--source-url",
        local_http_server,
        "--min-bytes",
        "64",
        "--min-records",
        "3",
    ]
    assert fetch_mimic_demo.main(args) == fetch_mimic_demo.EXIT_OK

    calls: list[str] = []

    def _counting(url, timeout=60.0, username=None, password=None):
        calls.append(url)
        return _make_payload(rows=8)

    monkeypatch.setattr(fetch_mimic_demo, "http_get", _counting)
    assert fetch_mimic_demo.main(args + ["--force"]) == fetch_mimic_demo.EXIT_OK
    assert len(calls) == 1


def test_credentials_produce_basic_auth_header(monkeypatch):
    """凭据位：给了账号口令带 HTTP Basic 认证头，没给就不带。"""
    request = fetch_mimic_demo.build_request("https://example.invalid/x", "u", "p")
    assert request.get_header("Authorization") == "Basic " + base64.b64encode(
        b"u:p"
    ).decode("ascii")

    anonymous = fetch_mimic_demo.build_request("https://example.invalid/x")
    assert anonymous.get_header("Authorization") is None

    monkeypatch.delenv("MIMIC_PHYSIONET_USERNAME", raising=False)
    monkeypatch.delenv("MIMIC_PHYSIONET_PASSWORD", raising=False)
    assert fetch_mimic_demo.read_credentials() == (None, None)
    monkeypatch.setenv("MIMIC_PHYSIONET_USERNAME", "u")
    monkeypatch.setenv("MIMIC_PHYSIONET_PASSWORD", "p")
    assert fetch_mimic_demo.read_credentials() == ("u", "p")


def test_exit_codes_are_distinct_and_degraded_is_non_zero():
    """退出码语义：成功 / 降级 / 校验失败三码互不相同，后两者都非 0。"""
    assert fetch_mimic_demo.EXIT_OK == 0
    assert fetch_mimic_demo.EXIT_DEGRADED != fetch_mimic_demo.EXIT_OK
    assert fetch_mimic_demo.EXIT_VALIDATION_FAILED != fetch_mimic_demo.EXIT_OK
    assert fetch_mimic_demo.EXIT_DEGRADED != fetch_mimic_demo.EXIT_VALIDATION_FAILED
    assert fetch_mimic_demo.EXIT_DEGRADED != 0
    assert fetch_mimic_demo.EXIT_VALIDATION_FAILED != 0


def test_manifest_declares_usage_boundary(tmp_path, monkeypatch):
    """用途边界：清单写明只作对照与测试数据层，不是产品对接边界。"""
    monkeypatch.setattr(
        fetch_mimic_demo,
        "http_get",
        lambda url, timeout=60.0, username=None, password=None: _make_payload(rows=8),
    )
    dest = tmp_path / "out"
    code = fetch_mimic_demo.main(
        ["--dest", str(dest), "--min-bytes", "64", "--min-records", "3"]
    )
    assert code == fetch_mimic_demo.EXIT_OK
    manifest = json.loads((dest / fetch_mimic_demo.MANIFEST_NAME).read_text("utf-8"))
    assert "测试数据层" in manifest["usage_scope"]
    assert manifest["license"].startswith("https://physionet.org/")
    assert manifest["dataset_home"].startswith("https://physionet.org/")


def test_snapshot_is_documented_as_not_real_data():
    """结构快照不得被误当成 MIMIC 真实数据：目录内附说明且标了 SYNTHETIC。"""
    snapshot_dir = PROJECT_ROOT / "tests" / "data" / "mimic_demo_snapshot"
    readme = (snapshot_dir / "README.md").read_text("utf-8")
    assert "不是 MIMIC-IV Demo 真实数据" in readme or "不是 MIMIC" in readme

    rows = list(csv.DictReader((snapshot_dir / "prescriptions.snapshot.csv").open(encoding="utf-8")))
    assert rows
    assert all(row["order_source"] == "SYNTHETIC" for row in rows)
    assert all(row["subject_id"].startswith("9") for row in rows)
