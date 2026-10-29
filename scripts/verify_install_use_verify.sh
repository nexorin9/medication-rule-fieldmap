#!/usr/bin/env bash
#
# 合理用药审方字段对照系统 · 「安装 → 使用 → 验证」一条龙脚本
#
# 给科员一条可重复的命令，2 分钟跑出可核对结果：
#
#   1. 装依赖        —— 独立虚拟环境，不污染系统 Python
#   2. 起本地 HTTP 替身 —— 充当 HIS 审方复核接口 + 企业微信 webhook
#   3. 起守护进程     —— 往落地目录投一份样例批次，等它判定完
#   4. 断言三份台账   —— judgement / undeterminable / field_gap_priority 行数 > 0
#   5. 药师确认 + 回写 —— 断言出站请求的方法 / 路径 / Authorization / JSON 字段
#
# **替身边界**：替身只顶替**对端服务**，链路是真的——真实 socket、真实 HTTP
# 报文、真实 Authorization 头、真实 JSON 请求体。真实 HIS 的凭据从环境变量
# 读（``HIS_BASE_URL`` / ``HIS_API_TOKEN``），本脚本把它们指到本地替身，
# 换成真实内网地址即可直接验真实接口。
#
# 用法：
#   bash scripts/verify_install_use_verify.sh
#
# 环境变量：
#   MRFM_VERIFY_PYTHON   指定解释器（默认自动挑 python3.13/3.12/3.11/python3）
#   MRFM_VERIFY_WORKDIR  指定工作目录（默认 mktemp -d；结束后删除）
#   MRFM_VERIFY_KEEP=1   保留工作目录（排障用）
#   MRFM_VERIFY_TIMEOUT  单步等待秒数上限（默认 120）

set -euo pipefail

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RULES_DIR="${PROJECT_DIR}/rules/sample"
SAMPLE_BATCH="${PROJECT_DIR}/tests/data/batch/demo_batch_001.csv"

TIMEOUT_SEC="${MRFM_VERIFY_TIMEOUT:-120}"
STEP_NO=0

# ---------------------------------------------------------------------------
# 输出小工具
# ---------------------------------------------------------------------------

step() {
    STEP_NO=$((STEP_NO + 1))
    printf '\n=== [%d/%d] %s ===\n' "${STEP_NO}" 8 "$1"
}

ok() {
    printf '  [通过] %s\n' "$1"
}

fail() {
    printf '  [失败] %s\n' "$1" >&2
    exit 1
}

need() {
    [[ -e "$1" ]] || fail "$2"
}

# ---------------------------------------------------------------------------
# 清理钩子
# ---------------------------------------------------------------------------

WORKDIR=""
STUB_PID=""
DAEMON_PID=""
CLEANED=0

cleanup() {
    local code=$?
    [[ ${CLEANED} -eq 1 ]] && return
    CLEANED=1
    if [[ -n "${DAEMON_PID}" ]] && kill -0 "${DAEMON_PID}" 2>/dev/null; then
        kill -TERM "${DAEMON_PID}" 2>/dev/null || true
        wait "${DAEMON_PID}" 2>/dev/null || true
    fi
    if [[ -n "${STUB_PID}" ]] && kill -0 "${STUB_PID}" 2>/dev/null; then
        kill -TERM "${STUB_PID}" 2>/dev/null || true
        wait "${STUB_PID}" 2>/dev/null || true
    fi
    if [[ -n "${WORKDIR}" && -d "${WORKDIR}" && "${MRFM_VERIFY_KEEP:-0}" != "1" ]]; then
        rm -rf "${WORKDIR}"
    elif [[ -n "${WORKDIR}" ]]; then
        printf '\n（已保留工作目录：%s）\n' "${WORKDIR}"
    fi
    exit "${code}"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# 0. 挑解释器
# ---------------------------------------------------------------------------

pick_python() {
    if [[ -n "${MRFM_VERIFY_PYTHON:-}" ]]; then
        command -v "${MRFM_VERIFY_PYTHON}" >/dev/null 2>&1 || fail "MRFM_VERIFY_PYTHON=${MRFM_VERIFY_PYTHON} 不可用"
        printf '%s' "${MRFM_VERIFY_PYTHON}"
        return
    fi
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "${candidate}" >/dev/null 2>&1; then
            printf '%s' "${candidate}"
            return
        fi
    done
    fail "找不到可用的 python3 解释器"
}

step "环境自检"
PY_BIN="$(pick_python)"
ok "解释器：${PY_BIN}（$(${PY_BIN} -V 2>&1)）"
need "${SCRIPT_DIR}" "找不到脚本目录"
need "${PROJECT_DIR}/requirements.txt" "缺少 requirements.txt"
need "${RULES_DIR}" "缺少样例规则目录 rules/sample"
need "${SAMPLE_BATCH}" "缺少样例批次 tests/data/batch/demo_batch_001.csv"
ok "项目目录：${PROJECT_DIR}"

if [[ -n "${MRFM_VERIFY_WORKDIR:-}" ]]; then
    WORKDIR="${MRFM_VERIFY_WORKDIR}"
    rm -rf "${WORKDIR}"
else
    WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/mrfm-verify-XXXXXX")"
fi
INBOX="${WORKDIR}/inbox"
OUT_DIR="${WORKDIR}/out"
STATE_DIR="${WORKDIR}/state"
STATE_DB="${STATE_DIR}/state.sqlite3"
VENV="${WORKDIR}/venv"
mkdir -p "${INBOX}" "${OUT_DIR}" "${STATE_DIR}"
ok "工作目录：${WORKDIR}"

# ---------------------------------------------------------------------------
# 1. 装依赖
# ---------------------------------------------------------------------------

step "创建虚拟环境并安装依赖"
"${PY_BIN}" -m venv "${VENV}"
VENV_PY="${VENV}/bin/python"
[[ -x "${VENV_PY}" ]] || VENV_PY="${VENV}/Scripts/python.exe"
[[ -x "${VENV_PY}" ]] || fail "虚拟环境里找不到 python（${VENV}）"
"${VENV_PY}" -m pip install --quiet --upgrade pip
"${VENV_PY}" -m pip install --quiet -r "${PROJECT_DIR}/requirements.txt"
"${VENV_PY}" - <<'PY'
import importlib

for name in ("httpx", "yaml", "watchdog"):
    importlib.import_module(name)
print("  依赖就位：httpx / PyYAML / watchdog")
PY
ok "依赖安装完成（$(${VENV_PY} -V 2>&1)）"

export PYTHONPATH="${PROJECT_DIR}/src"
export PYTHONUNBUFFERED=1
# 验证脚本不该给仓库留下运行时垃圾：跑完不落 .pyc
export PYTHONDONTWRITEBYTECODE=1
unset NO_COLOR || true

# ---------------------------------------------------------------------------
# 2. 起本地 HTTP 替身
# ---------------------------------------------------------------------------

step "启动本地 HTTP 替身（HIS 审方复核接口 + 企业微信 webhook）"

cat > "${WORKDIR}/his_stub.py" <<'PY'
"""本地 HTTP 替身：充当 HIS 审方复核接口 + 企业微信 webhook。

**只顶替对端服务**：真实 socket、真实 HTTP 报文、真实 Authorization 头、
真实 JSON 请求体。每收到一次请求就往记录文件追加一行 JSONL，
供外层脚本断言「方法 / 路径 / 头 / 体」。
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RECORD_LOCK = threading.Lock()


class StubHandler(BaseHTTPRequestHandler):
    """审方复核 + 消息推送两条链路的本地服务。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - BaseHTTPRequestHandler 约定
        """访问日志压掉，避免污染脚本输出。"""

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _reply(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body: dict) -> None:
        record = {
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
        }
        with RECORD_LOCK:
            with open(self.server.record_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        body = self._read_json()
        self._record(body)
        if self.path.startswith("/api/v1/prescription/review"):
            # 审方复核：回一个与请求一致的 prescription_no（实体绑定一致）
            self._reply(
                {
                    "code": 0,
                    "data": {"prescription_no": body.get("prescription_no", "")},
                }
            )
            return
        if self.path.startswith("/wecom"):
            self._reply({"errcode": 0, "errmsg": "ok"})
            return
        self._reply({"code": 404, "message": "not found"}, status=404)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._reply({"status": "ok"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", required=True, help="请求记录文件（JSONL）")
    parser.add_argument("--port-file", required=True, help="端口号文件")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    server.record_path = args.record
    Path(args.record).write_text("", encoding="utf-8")
    Path(args.port_file).write_text(str(server.server_address[1]), encoding="utf-8")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY

RECORD_FILE="${WORKDIR}/his_requests.jsonl"
PORT_FILE="${WORKDIR}/stub.port"
"${VENV_PY}" "${WORKDIR}/his_stub.py" --record "${RECORD_FILE}" --port-file "${PORT_FILE}" &
STUB_PID=$!

for _ in $(seq 1 50); do
    [[ -s "${PORT_FILE}" ]] && break
    sleep 0.2
done
need "${PORT_FILE}" "本地 HTTP 替身没起来（端口文件未生成）"
STUB_PORT="$(cat "${PORT_FILE}")"
ok "本地 HTTP 替身已启动：127.0.0.1:${STUB_PORT}（pid=${STUB_PID}）"

# ---------------------------------------------------------------------------
# 3. 起守护进程 + 投样例批次
# ---------------------------------------------------------------------------

step "启动守护进程并投一份样例批次"

export HIS_BASE_URL="http://127.0.0.1:${STUB_PORT}"
export HIS_API_TOKEN="local-verify-token"
export WECOM_WEBHOOK_URL="http://127.0.0.1:${STUB_PORT}/wecom/hook"
export MRFM_INBOX_DIR="${INBOX}"
export MRFM_OUT_DIR="${OUT_DIR}"
export MRFM_STATE_DB="${STATE_DB}"
export MRFM_RULES_DIR="${RULES_DIR}"
export MRFM_HEARTBEAT_SEC=3

DAEMON_LOG="${WORKDIR}/daemon.log"
"${VENV_PY}" -m med_rule_fieldmap.daemon \
    --inbox "${INBOX}" \
    --out "${OUT_DIR}" \
    --rules "${RULES_DIR}" \
    --state-db "${STATE_DB}" \
    --heartbeat-sec 3 > "${DAEMON_LOG}" 2>&1 &
DAEMON_PID=$!

for _ in $(seq 1 100); do
    grep -q "合理用药审方字段对照系统" "${DAEMON_LOG}" 2>/dev/null && break
    kill -0 "${DAEMON_PID}" 2>/dev/null || fail "守护进程启动失败，日志：${DAEMON_LOG}"
    sleep 0.2
done
grep -q "合理用药审方字段对照系统" "${DAEMON_LOG}" || fail "守护进程没在 ${TIMEOUT_SEC} 秒内打出启动横幅"
ok "守护进程已启动（pid=${DAEMON_PID}）"

cp "${SAMPLE_BATCH}" "${INBOX}/orders_verify_001.csv"
ok "样例批次已落地：${INBOX}/orders_verify_001.csv"

LEDGER="${OUT_DIR}/judgement_ledger.csv"
for _ in $(seq 1 $((TIMEOUT_SEC * 5))); do
    [[ -f "${LEDGER}" ]] && [[ "$(wc -l < "${LEDGER}" | tr -d ' ')" -gt 1 ]] && break
    kill -0 "${DAEMON_PID}" 2>/dev/null || fail "守护进程中途退出，日志：${DAEMON_LOG}"
    sleep 0.2
done
need "${LEDGER}" "判定台账没生成：${LEDGER}"
ok "判定台账已落盘：${LEDGER}"

kill -TERM "${DAEMON_PID}" 2>/dev/null || true
wait "${DAEMON_PID}" 2>/dev/null || true
DAEMON_PID=""
ok "守护进程已优雅停止"

# ---------------------------------------------------------------------------
# 4. 断言三份台账产物
# ---------------------------------------------------------------------------

step "断言三份台账产物存在且行数大于 0"

"${VENV_PY}" - "${OUT_DIR}" <<'PY'
import csv
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])
targets = [
    ("judgement_ledger.csv", "判定台账"),
    ("undeterminable_ledger.csv", "不可判定台账"),
    ("field_gap_priority.csv", "字段补齐顺序表"),
]
failed = []
for name, label in targets:
    path = out_dir / name
    if not path.is_file():
        failed.append(f"{label} {name} 不存在")
        continue
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    data_rows = max(len(rows) - 1, 0)
    if data_rows <= 0:
        failed.append(f"{label} {name} 没有数据行")
        continue
    print(f"  [通过] {label} {name}：{data_rows} 行数据（表头 1 行）")
if failed:
    for item in failed:
        print(f"  [失败] {item}", file=sys.stderr)
    raise SystemExit(1)
PY
ok "三份台账产物齐备且有数据"

# ---------------------------------------------------------------------------
# 5. 药师确认 → 触发回写
# ---------------------------------------------------------------------------

step "药师确认一条结论并触发回写"

# 从判定台账里挑一条「违规」结论（药师复核后要写回 HIS 的那种）
BUSINESS_KEY="$("${VENV_PY}" - "${LEDGER}" <<'PY'
import csv
import sys
from pathlib import Path

ledger = Path(sys.argv[1])
with ledger.open(encoding="utf-8-sig", newline="") as handle:
    for row in csv.DictReader(handle):
        if row.get("conclusion") == "违规":
            key = "｜".join(
                (
                    row["batch_no"],
                    row["prescription_no"],
                    row["rule_id"],
                    row["rule_version"],
                )
            )
            print(key)
            break
PY
)"
[[ -n "${BUSINESS_KEY}" ]] || fail "判定台账里没有「违规」结论，无法演示回写"
ok "待确认结论：${BUSINESS_KEY}"

"${VENV_PY}" -m med_rule_fieldmap.cli confirm \
    --db "${STATE_DB}" \
    --key "${BUSINESS_KEY}" \
    --by "verify-pharmacist" \
    --note "验证脚本模拟药师复核确认" \
    || fail "药师确认失败"

"${VENV_PY}" -m med_rule_fieldmap.cli confirm \
    --db "${STATE_DB}" \
    --trigger \
    || fail "回写触发失败"

ok "已触发回写"

# ---------------------------------------------------------------------------
# 6. 断言出站请求
# ---------------------------------------------------------------------------

step "断言回写请求的方法 / 路径 / Authorization / JSON 字段"

"${VENV_PY}" - "${RECORD_FILE}" <<'PY'
import json
import sys
from pathlib import Path

record_path = Path(sys.argv[1])
expected_body_fields = [
    "batch_no",
    "prescription_no",
    "rule_id",
    "rule_version",
    "conclusion",
    "severity",
    "reason",
    "basis",
    "missing_fields",
    "reviewed_by",
    "reviewed_at",
]

records = []
if record_path.is_file():
    for line in record_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))

review = [r for r in records if r["path"].startswith("/api/v1/prescription/review")]
if not review:
    print("  [失败] 没有收到任何审方复核回写请求", file=sys.stderr)
    raise SystemExit(1)

problems = []
first = review[0]
if first["method"] != "POST":
    problems.append(f"请求方法应为 POST，实际 {first['method']}")
if first["path"] != "/api/v1/prescription/review":
    problems.append(f"请求路径应为 /api/v1/prescription/review，实际 {first['path']}")
if first["authorization"] != "Bearer local-verify-token":
    problems.append(
        f"Authorization 头应为 'Bearer local-verify-token'，实际 {first['authorization']!r}"
    )
if "application/json" not in first["content_type"]:
    problems.append(f"Content-Type 应含 application/json，实际 {first['content_type']!r}")

body = first["body"]
if not isinstance(body, dict):
    problems.append("请求体不是 JSON 对象")
else:
    missing = [f for f in expected_body_fields if f not in body]
    if missing:
        problems.append(f"请求体缺字段：{', '.join(missing)}")
    extra = [f for f in body if f not in expected_body_fields]
    if extra:
        problems.append(f"请求体多了字段：{', '.join(extra)}")
    if body.get("reviewed_by") != "verify-pharmacist":
        problems.append(f"reviewed_by 应为 verify-pharmacist，实际 {body.get('reviewed_by')!r}")
    if not body.get("prescription_no"):
        problems.append("prescription_no 为空")

if problems:
    for item in problems:
        print(f"  [失败] {item}", file=sys.stderr)
    raise SystemExit(1)

print(f"  [通过] 回写请求：{first['method']} {first['path']}")
print(f"  [通过] Authorization：{first['authorization']}")
print(f"  [通过] 请求体字段齐备（{len(expected_body_fields)} 项）：{', '.join(expected_body_fields)}")
print(f"  [通过] 处方 {body.get('prescription_no')} · 规则 {body.get('rule_id')} · "
      f"结论 {body.get('conclusion')} · 确认人 {body.get('reviewed_by')}")
print(f"  [通过] 共收到 {len(review)} 条审方复核回写请求")
PY
ok "出站请求断言全部通过"

# ---------------------------------------------------------------------------
# 7. 收尾
# ---------------------------------------------------------------------------

step "验证收尾"
"${VENV_PY}" -m med_rule_fieldmap.cli state --db "${STATE_DB}" || fail "状态库读取失败"
"${VENV_PY}" -m med_rule_fieldmap.cli rules validate --rules "${RULES_DIR}" || fail "规则行校验失败"
ok "状态库与规则行均可读取"

printf '\n==================================================\n'
printf ' 全部通过：安装 → 使用 → 验证 一条龙跑通\n'
printf ' 判定台账 / 不可判定台账 / 字段补齐顺序表 三份产物均已生成\n'
printf ' 回写请求的方法 / 路径 / Authorization / JSON 字段均已断言\n'
printf '==================================================\n'
exit 0
