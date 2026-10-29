#!/usr/bin/env python3
"""采集真实运行截图：把真实终端会话与真实产物落成 PNG。

**这份脚本只做一件事**：把本产品**真实跑起来**的画面原样留下来，放进
``docs/screenshots/``，供 README 的「概念 / 实测对照」小节引用。

**它不是 mock，也不是设计稿**：

* 守护进程在**真实伪终端（PTY）**里启动，拿到的是真 TTY 下的日志渲染
  （批次摘要行加粗、逐行判定次级、心跳行最暗灰）；
* 医嘱落地目录里投的是 ``tests/data/batch/`` 的真实样例批次；
* HIS 审方复核回写与企业微信推送走**本地真实 HTTP 服务**
  （``http.server`` 起在 127.0.0.1，真实 socket + 真实 HTTP 报文），
  它只是**地址不在医院内网**，链路本身是真的 —— 属于「测试数据层」，
  不是最终对接方式（正式对接口径见 ``spec.md`` §2.3 与
  ``docs/data-sources.md``）；
* 截图里的台账片段是从守护进程**真实落盘**的 ``out/`` 产物里截的，
  不是手写的示意数据。

**用法**（项目根目录下执行）::

    python scripts/capture_screenshots.py --out docs/screenshots

依赖：``Pillow``（仅本脚本需要，产品运行不需要）——装在
``requirements-docs.txt`` 里，不进产品 ``requirements.txt``。
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - 仅截图工具需要
    print(
        "缺少 Pillow：请先 `pip install -r requirements-docs.txt`",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

# ---------------------------------------------------------------------------
# 终端画面参数（配色与 logging_setup.py 一致，取自视觉目标卡）
# ---------------------------------------------------------------------------

BG = (13, 17, 23)
FG = (213, 217, 224)
CHROME = (22, 27, 34)
CHROME_TEXT = (139, 148, 158)
FONT_SIZE = 15
LINE_HEIGHT = 22
PAD_X = 18
PAD_TOP = 42
PAD_BOTTOM = 16
WINDOW_MIN_WIDTH = 900
#: 终端折行宽度（真终端到宽就折行，截图按同一口径折，图片宽度才收得住）
WRAP_COLS = 118

MONO_FONT = "/System/Library/Fonts/Menlo.ttc"
CJK_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"
CJK_FONT_FALLBACK = "/System/Library/Fonts/Supplemental/Songti.ttc"

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")

#: 东亚宽字符（终端里占两格）
WIDE_RANGES = (
    (0x1100, 0x115F),
    (0x2E80, 0x303E),
    (0x3041, 0x33FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xA000, 0xA4CF),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F),
    (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6),
    (0x20000, 0x3FFFD),
)


def char_width(ch: str) -> int:
    """字符在终端里占几格（宽字符 2 格，组合符号 0 格）。"""
    code = ord(ch)
    if code == 0:
        return 0
    if 0x0300 <= code <= 0x036F:  # 组合附加符号
        return 0
    for low, high in WIDE_RANGES:
        if low <= code <= high:
            return 2
    return 1


@dataclass
class Style:
    """一段文本的终端样式。"""

    color: tuple[int, int, int] = FG
    bold: bool = False
    dim: bool = False


@dataclass
class Line:
    """一行终端文本（按样式切成若干片段）。"""

    runs: list[tuple[str, Style]] = field(default_factory=list)

    def plain(self) -> str:
        return "".join(text for text, _ in self.runs)

    def cell_width(self) -> int:
        return sum(char_width(ch) for text, _ in self.runs for ch in text)


def parse_ansi(raw: str) -> list[Line]:
    """把带 ANSI 真彩色转义的终端文本解析成「行 + 样式片段」。"""
    lines: list[Line] = []
    current = Line()
    style = Style()
    for chunk in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        pos = 0
        for match in ANSI_RE.finditer(chunk):
            if match.start() > pos:
                current.runs.append((chunk[pos : match.start()], style))
            style = _apply_sgr(style, match.group(1))
            pos = match.end()
        if pos < len(chunk):
            current.runs.append((chunk[pos:], style))
        lines.append(current)
        current = Line()
    return lines


def _apply_sgr(style: Style, params: str) -> Style:
    """应用一段 SGR 参数（只处理本产品日志用到的子集）。"""
    codes = [int(part) for part in params.split(";") if part != ""] or [0]
    index = 0
    while index < len(codes):
        code = codes[index]
        if code == 0:
            style = Style()
        elif code == 1:
            style = Style(style.color, bold=True, dim=style.dim)
        elif code == 2:
            style = Style(style.color, bold=style.bold, dim=True)
        elif code == 22:
            style = Style(style.color, bold=False, dim=False)
        elif code == 38 and index + 4 < len(codes) and codes[index + 1] == 2:
            style = Style(
                (codes[index + 2], codes[index + 3], codes[index + 4]),
                style.bold,
                style.dim,
            )
            index += 4
        index += 1
    return style


class TerminalRenderer:
    """把终端文本渲染成 PNG（等宽 ASCII + 中文宽字符对齐）。"""

    def __init__(self, font_size: int = FONT_SIZE) -> None:
        self.font_size = font_size
        self.mono = ImageFont.truetype(MONO_FONT, font_size)
        self.bold_mono = ImageFont.truetype(MONO_FONT, font_size, index=1)
        try:
            self.cjk = ImageFont.truetype(CJK_FONT, font_size)
            self.cjk_path = CJK_FONT
        except OSError:  # pragma: no cover - 字体缺失时回落
            self.cjk = ImageFont.truetype(CJK_FONT_FALLBACK, font_size)
            self.cjk_path = CJK_FONT_FALLBACK
        self.cell_w = self.mono.getlength("M")
        self.cjk_size = max(1, int(round(self.cell_w * 2)))
        # 宽字符按**步进宽度**缩放到两格宽（不能用墨迹包围盒，否则字形会压格）
        self._cjk_scaled: dict[str, ImageFont.FreeTypeFont] = {}

    def _font_for(self, style: Style, wide: bool) -> ImageFont.FreeTypeFont:
        if wide:
            return self.cjk
        return self.bold_mono if style.bold else self.mono

    def _wide_font(self, ch: str) -> ImageFont.FreeTypeFont:
        """取「正好占两格宽」的中文字形。"""
        cached = self._cjk_scaled.get(ch)
        if cached is not None:
            return cached
        target = self.cell_w * 2
        font = self.cjk
        advance = font.getlength(ch)
        if advance > 0 and abs(advance - target) > 0.5:
            size = max(1, int(round(self.cjk_size * target / advance)))
            try:
                font = ImageFont.truetype(self.cjk_path, size)
                advance = font.getlength(ch)
            except OSError:  # pragma: no cover
                font = self.cjk
        self._cjk_scaled[ch] = font
        return font

    def _wrap_line(self, line: Line, cols: int) -> list[list[tuple[str, Style]]]:
        """按终端宽度折行（保持样式）——真终端就是这么折的。"""
        cells = [(ch, style) for text, style in line.runs for ch in text]
        if not cells:
            return [[]]
        wrapped: list[list[tuple[str, Style]]] = []
        current: list[tuple[str, Style]] = []
        used = 0
        for ch, style in cells:
            width = char_width(ch)
            if used + width > cols and current:
                wrapped.append(current)
                current = []
                used = 0
            current.append((ch, style))
            used += width
        wrapped.append(current)
        return wrapped

    def render(
        self,
        lines: list[Line],
        *,
        title: str,
        out_path: Path,
        max_cols: int | None = None,
    ) -> Path:
        """渲染成 PNG 并落盘，返回落盘路径。"""
        cols = max_cols or min(
            max((line.cell_width() for line in lines), default=40), WRAP_COLS
        )
        cols = max(cols, len(title) + 4)
        width = int(PAD_X * 2 + cols * self.cell_w) + 2
        width = max(width, WINDOW_MIN_WIDTH)
        wrapped = [cell for line in lines for cell in self._wrap_line(line, cols)]
        height = PAD_TOP + len(wrapped) * LINE_HEIGHT + PAD_BOTTOM

        image = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(image)

        # 窗口标题栏（三个圆点 + 路径标题），让截图一眼就是终端而不是网页
        draw.rectangle([0, 0, width, 34], fill=CHROME)
        for offset, color in ((18, (255, 95, 86)), (38, (255, 189, 46)), (58, (39, 201, 63))):
            draw.ellipse(
                [offset - 5, 12, offset + 5, 22],
                fill=color,
            )
        title_font = ImageFont.truetype(self.cjk_path, 13)
        draw.text(
            (width // 2, 17),
            title,
            font=title_font,
            fill=CHROME_TEXT,
            anchor="mm",
        )

        y = PAD_TOP
        for cells in wrapped:
            x = float(PAD_X)
            for ch, style in cells:
                wide = char_width(ch) == 2
                font = self._wide_font(ch) if wide else self._font_for(style, wide)
                color = style.color
                if style.dim:
                    color = tuple(int(c * 0.75) for c in color)
                draw.text((x, y), ch, font=font, fill=color)
                x += self.cell_w * char_width(ch)
            y += LINE_HEIGHT

        out_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(out_path, format="PNG")
        return out_path


def render_plain(
    renderer: TerminalRenderer,
    text: str,
    *,
    title: str,
    out_path: Path,
) -> Path:
    """渲染不带 ANSI 的纯文本（真实产物片段）。"""
    lines = parse_ansi(text)
    return renderer.render(lines, title=title, out_path=out_path)


# ---------------------------------------------------------------------------
# 本地真实 HTTP 服务（替身层：地址在内网外，链路是真的）
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    """HIS 审方复核回写 + 企业微信推送两条链路的本地真实服务。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        """把访问日志压到 DEBUG，避免污染产品画面。"""

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _reply(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        payload = self._read_json()
        if self.path.startswith("/api/v1/prescription/review"):
            # HIS 回写：返回与请求一致的 prescription_no（实体绑定一致）
            self._reply(
                {
                    "code": 0,
                    "data": {"prescription_no": payload.get("prescription_no", "")},
                }
            )
            return
        if self.path.startswith("/wecom"):
            self._reply({"errcode": 0, "errmsg": "ok"})
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._reply({"status": "ok"})


class StubServer:
    """本地真实 HTTP 服务（后台线程）。"""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "StubServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "StubServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# 真实伪终端会话
# ---------------------------------------------------------------------------


def capture_session(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    after_start: callable = None,
    ready_path: Path | None = None,
    stop_after: float = 0.0,
    timeout: float = 90.0,
) -> str:
    """在真实 PTY 里跑一条命令，返回捕获到的终端原文。

    :param after_start: 进程起来后立刻调用的回调（用来往 inbox 投批次）。
    :param ready_path: 出现这个文件即认为批次处理完成。
    :param stop_after: 批次完成后额外等待的秒数（等心跳行出现）。
    """
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(  # noqa: S603 - 本地固定命令
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=str(cwd),
        env=env,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave_fd)

    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    ready_at: float | None = None
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.2)
            if readable:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
            if after_start is not None and time.monotonic() > deadline - timeout + 3.0:
                after_start()
                after_start = None
            if ready_path is not None and ready_at is None and ready_path.exists():
                ready_at = time.monotonic()
            if ready_at is not None and time.monotonic() - ready_at >= stop_after:
                break
            if process.poll() is not None:
                # 进程自己退出了：再把管道里的剩余输出读干净
                while True:
                    try:
                        readable, _, _ = select.select([master_fd], [], [], 0.2)
                        if not readable:
                            break
                        data = os.read(master_fd, 65536)
                        if not data:
                            break
                        chunks.append(data)
                    except OSError:
                        break
                break
    finally:
        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):  # pragma: no cover
                process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:  # pragma: no cover
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait(timeout=10)
        try:
            os.close(master_fd)
        except OSError:  # pragma: no cover
            pass
    return b"".join(chunks).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _pick_sample_batch(project: Path) -> Path:
    """取一份真实的样例批次文件（脱敏数据，tests/data 层）。"""
    batch_dir = project / "tests" / "data" / "batch"
    candidates = sorted(batch_dir.glob("*.csv"))
    if not candidates:
        raise SystemExit(f"找不到样例批次：{batch_dir}")
    return candidates[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="capture_screenshots",
        description="采集真实运行截图（守护进程会话 + 两份真实产物片段）",
    )
    parser.add_argument("--project", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--out", default=None, help="截图输出目录（默认 docs/screenshots）")
    parser.add_argument(
        "--workdir",
        default="/tmp/mrfm-demo",
        help="本次演示运行的工作目录（真实落地目录 / 产物 / 状态库）",
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="不重跑守护进程：用已存档的终端原文与已落盘的产物重新渲染 PNG",
    )
    args = parser.parse_args(argv)

    project = Path(args.project).resolve()
    out_dir = Path(args.out).resolve() if args.out else project / "docs" / "screenshots"
    workdir = Path(args.workdir).resolve()

    if workdir.exists() and not args.render_only:
        shutil.rmtree(workdir)
    inbox = workdir / "inbox"
    out = workdir / "out"
    state = workdir / "state" / "state.sqlite3"
    for path in (inbox, out, state.parent):
        path.mkdir(parents=True, exist_ok=True)

    sample = _pick_sample_batch(project)
    daemon_cmd = [
        sys.executable,
        "-m",
        "med_rule_fieldmap.daemon",
        "--inbox",
        str(inbox),
        "--out",
        str(out),
        "--rules",
        str(project / "rules" / "sample"),
        "--state-db",
        str(state),
        "--heartbeat-sec",
        "3",
    ]

    env = dict(os.environ)
    env["PYTHONPATH"] = str(project / "src")
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("NO_COLOR", None)

    with StubServer() as stub:
        env["HIS_BASE_URL"] = f"http://127.0.0.1:{stub.port}"
        env["HIS_API_TOKEN"] = "local-demo-token"
        env["WECOM_WEBHOOK_URL"] = f"http://127.0.0.1:{stub.port}/wecom/hook"
        env["MRFM_INBOX_DIR"] = str(inbox)
        env["MRFM_OUT_DIR"] = str(out)
        env["MRFM_STATE_DB"] = str(state)
        env["MRFM_RULES_DIR"] = str(project / "rules" / "sample")
        env["MRFM_HEARTBEAT_SEC"] = "3"

        ledger = out / "judgement_ledger.csv"

        def drop_batch() -> None:
            """往落地目录投一份真实样例批次。"""
            shutil.copy2(sample, inbox / "orders_20261029_0900.csv")

        if args.render_only:
            raw = (out_dir / "daemon-session.txt").read_text(encoding="utf-8")
        else:
            raw = capture_session(
                daemon_cmd,
                cwd=project,
                env=env,
                after_start=drop_batch,
                ready_path=ledger,
                stop_after=8.0,
                timeout=120.0,
            )

    # 终端原文落一份文本存档（便于复核截图确实是真实会话）
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "daemon-session.txt").write_text(raw, encoding="utf-8")

    renderer = TerminalRenderer()
    renderer.render(
        parse_ansi(raw),
        title="med_rule_fieldmap · 守护进程真实终端会话",
        out_path=out_dir / "daemon-session.png",
    )

    # 两份真实产物片段
    ledger_path = out / "judgement_ledger.csv"
    priority_path = out / "field_gap_priority.csv"
    if not ledger_path.is_file():
        print(f"判定台账未生成：{ledger_path}", file=sys.stderr)
        return 1
    if not priority_path.is_file():
        print(f"字段补齐顺序表未生成：{priority_path}", file=sys.stderr)
        return 1

    ledger_lines = ledger_path.read_text(encoding="utf-8").splitlines()
    header = ledger_lines[0]
    undeterminable = [row for row in ledger_lines[1:] if ",不可判定," in row][:2]
    violation = [row for row in ledger_lines[1:] if ",违规," in row][:2]
    ledger_head = "\n".join(
        [
            "$ head -1 out/judgement_ledger.csv",
            header,
            "",
            "$ grep ',不可判定,' out/judgement_ledger.csv | head -2",
            *undeterminable,
            "",
            "$ grep ',违规,' out/judgement_ledger.csv | head -2",
            *violation,
        ]
    )
    render_plain(
        renderer,
        ledger_head,
        title="out/judgement_ledger.csv · 判定台账（真实产物片段）",
        out_path=out_dir / "judgement-ledger.png",
    )

    priority_head = "\n".join(
        ["$ head -6 out/field_gap_priority.csv", ""]
        + priority_path.read_text(encoding="utf-8").splitlines()[:7]
    )
    render_plain(
        renderer,
        priority_head,
        title="out/field_gap_priority.csv · 字段补齐顺序表（真实产物片段）",
        out_path=out_dir / "field-gap-priority.png",
    )

    for name in ("daemon-session.png", "judgement-ledger.png", "field-gap-priority.png"):
        path = out_dir / name
        print(f"已生成 {path}（{path.stat().st_size} 字节）")
    print(f"终端原文存档：{out_dir / 'daemon-session.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
