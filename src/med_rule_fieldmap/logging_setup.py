"""结构化日志：把常驻进程的终端画面分层渲染出来。

**对应 Phase 1 视觉目标卡**（``docs/concepts/visual-target.md``）：本产品的
交付形态是**事件驱动守护**——没有浏览器面板、没有看板，唯一的画面就是
**服务运行日志**。因此日志的视觉分层不是装饰，是产品本体的一部分：

1. **批次摘要行** —— 最显眼（加粗 + 正文色）；
2. **逐行判定** —— 次级（缩进一格 + 正文色）；
3. **回执、审计与心跳行** —— 最暗灰、最小字号。

**配色**取自视觉目标卡的终端 ANSI 色值（正文 ``#D5D9E0``、判定完成
``#3FB950``、不可判定 ``#D29922``、回写成功 ``#39C5CF``、错误告警
``#F85149``、审计与心跳 ``#8B949E``），用真彩色 ANSI 转义渲染；
``NO_COLOR`` 环境变量或非 TTY 时自动退化为无色（管道 / CI 里日志不脏）。

**日志级别**（自定义级别，数值越大越显眼）：

===============  =====  ==================================================
级别              数值   用途
===============  =====  ==================================================
``LEVEL_BATCH``   26    批次摘要行（最显眼）
``LEVEL_WRITEBACK`` 22  回写 / 撤回回执行
``LEVEL_ROW``     21    逐行判定（次级）
``LEVEL_INFO``    20    通用信息（台账落盘路径等回执）
``LEVEL_AUDIT``    9    审计留痕（最暗灰）
``LEVEL_HEARTBEAT`` 8   心跳（最暗灰）
===============  =====  ==================================================

默认门槛 ``LEVEL_INFO``：批次摘要、逐行判定、回执可见，审计与心跳两档
（最暗灰的两档）默认不刷屏——它们是留痕，不是现场要看的东西。
守护进程要把两档都显示出来时用 ``setup_logging(level=LEVEL_HEARTBEAT)``
（门槛取最低的那一档，审计与心跳就都出来了）。

**心跳间隔**读环境变量 ``MRFM_HEARTBEAT_SEC``（缺省 30 秒，取值非法时
回落默认值并在配置告警里留痕），口径与 :mod:`med_rule_fieldmap.config` 一致。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO, Any

from .config import DEFAULT_HEARTBEAT_SEC, ENV_HEARTBEAT_SEC

# ---------------------------------------------------------------------------
# 日志级别
# ---------------------------------------------------------------------------

#: 批次摘要行（最显眼）
LEVEL_BATCH = 26
#: 回写 / 撤回回执行
LEVEL_WRITEBACK = 22
#: 逐行判定（次级）
LEVEL_ROW = 21
#: 审计留痕（最暗灰）
LEVEL_AUDIT = 9
#: 心跳（最暗灰）
LEVEL_HEARTBEAT = 8

logging.addLevelName(LEVEL_BATCH, "BATCH")
logging.addLevelName(LEVEL_WRITEBACK, "WRITEBACK")
logging.addLevelName(LEVEL_ROW, "ROW")
logging.addLevelName(LEVEL_AUDIT, "AUDIT")
logging.addLevelName(LEVEL_HEARTBEAT, "HEARTBEAT")

#: 默认日志门槛（审计与心跳两档最暗灰，默认不刷屏）
DEFAULT_LOG_LEVEL = logging.INFO

# ---------------------------------------------------------------------------
# 配色（视觉目标卡的终端 ANSI 色值）
# ---------------------------------------------------------------------------

#: 正文色 #D5D9E0
COLOR_BODY = (213, 217, 224)
#: 最暗灰 #8B949E（审计与心跳）
COLOR_DIM = (139, 148, 158)
#: 判定完成（绿）#3FB950
COLOR_OK = (63, 185, 80)
#: 不可判定（琥珀）#D29922
COLOR_WARN = (210, 153, 34)
#: 回写成功（青）#39C5CF
COLOR_CYAN = (57, 197, 207)
#: 错误 / 告警（红）#F85149
COLOR_RED = (248, 81, 73)

_RESET = "\x1b[0m"


@dataclass(frozen=True)
class Style:
    """一段文本的终端样式（颜色 + 粗细）。"""

    color: tuple[int, int, int] | None = None
    bold: bool = False
    dim: bool = False

    def wrap(self, text: str, *, enabled: bool = True) -> str:
        """套上样式；``enabled=False`` 时原样返回（无色输出）。"""
        if not enabled or not text:
            return text
        prefix = ""
        if self.bold:
            prefix += "\x1b[1m"
        if self.dim:
            prefix += "\x1b[2m"
        if self.color is not None:
            prefix += f"\x1b[38;2;{self.color[0]};{self.color[1]};{self.color[2]}m"
        if not prefix:
            return text
        return f"{prefix}{text}{_RESET}"


#: 批次摘要行：加粗 + 正文色
STYLE_BATCH = Style(color=COLOR_BODY, bold=True)
#: 逐行判定：正文色
STYLE_ROW = Style(color=COLOR_BODY)
#: 回写回执：青色
STYLE_WRITEBACK = Style(color=COLOR_CYAN)
#: 审计与心跳：最暗灰
STYLE_DIM = Style(color=COLOR_DIM)
#: 告警：红色
STYLE_ALERT = Style(color=COLOR_RED)
#: 警告：琥珀
STYLE_WARN = Style(color=COLOR_WARN)
#: 成功：绿色
STYLE_OK = Style(color=COLOR_OK)


def supports_color(stream: IO[str] | None = None) -> bool:
    """当前输出是否支持彩色。

    ``NO_COLOR`` 存在 → 不上色（惯例）；``FORCE_COLOR`` 存在 → 强制上色
    （管道里也要看得到层级）；否则看是不是 TTY。
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    target = stream if stream is not None else sys.stdout
    try:
        return bool(target.isatty())
    except (AttributeError, ValueError):
        return False


@dataclass(frozen=True)
class LevelStyle:
    """一个日志级别的渲染方式：前缀 + 样式 + 缩进。"""

    prefix: str
    style: Style
    indent: str = ""


#: 级别 → 渲染方式（对应视觉目标卡的三层信息层级）
LEVEL_STYLES: dict[int, LevelStyle] = {
    LEVEL_BATCH: LevelStyle(prefix="", style=STYLE_BATCH),
    LEVEL_WRITEBACK: LevelStyle(prefix="", style=STYLE_WRITEBACK),
    LEVEL_ROW: LevelStyle(prefix="", style=STYLE_ROW, indent="  "),
    logging.INFO: LevelStyle(prefix="", style=STYLE_ROW),
    logging.WARNING: LevelStyle(prefix="[warn] ", style=STYLE_WARN),
    logging.ERROR: LevelStyle(prefix="[alert] ", style=STYLE_ALERT),
    logging.CRITICAL: LevelStyle(prefix="[alert] ", style=STYLE_ALERT),
    LEVEL_AUDIT: LevelStyle(prefix="[audit] ", style=STYLE_DIM),
    LEVEL_HEARTBEAT: LevelStyle(prefix="[alive] ", style=STYLE_DIM),
}

#: 结论三态 → 颜色（合规 / 违规 绿、不可判定 琥珀）
CONCLUSION_STYLES: dict[str, Style] = {
    "合规": STYLE_OK,
    "违规": STYLE_OK,
    "不可判定": STYLE_WARN,
}


class HierarchicalFormatter(logging.Formatter):
    """按信息层级渲染日志行。

    渲染结果形如::

        批次 DEMO-BATCH-001 · 收到 26 行 · 合规 85 · 违规 11 · 不可判定 9
          判定 RX-0002 · RX-LEGAL-MA-OUT-DAYS · 违规 · 日量 12 超上限 7
        [audit] 批次 DEMO-BATCH-001 · 判定 192 条 · 审计 192 条
        [alive] 规则 18 条 · 已处理 1 批 · 待判定 0 条
    """

    def __init__(self, *, color: bool = False) -> None:
        super().__init__("%(message)s")
        self._color = bool(color)

    def format(self, record: logging.LogRecord) -> str:
        spec = LEVEL_STYLES.get(record.levelno)
        message = record.getMessage()
        if spec is None:
            return message
        text = f"{spec.prefix}{spec.indent}{message}"
        return spec.style.wrap(text, enabled=self._color)


def setup_logging(
    *,
    level: int = DEFAULT_LOG_LEVEL,
    stream: IO[str] | None = None,
    color: bool | None = None,
    force: bool = False,
) -> logging.Logger:
    """配置根日志器，返回它。

    :param level: 日志门槛（默认 :data:`DEFAULT_LOG_LEVEL`）。
    :param stream: 输出流，缺省 ``sys.stdout``。
    :param color: 是否上色；缺省按 :func:`supports_color` 自动判断。
    :param force: 已有 handler 时是否强制重配（默认不重复挂 handler）。
    """
    root = logging.getLogger()
    if not force and root.handlers:
        for handler in root.handlers:
            if getattr(handler, "_mrfm_hierarchical", False):
                handler.setLevel(level)
                return root
    use_color = supports_color(stream) if color is None else bool(color)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(HierarchicalFormatter(color=use_color))
    handler._mrfm_hierarchical = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    return root


def get_logger(name: str = "med_rule_fieldmap") -> logging.Logger:
    """取一个本项目的日志器。"""
    return logging.getLogger(name)


def log_batch_summary(logger: logging.Logger, message: str) -> None:
    """批次摘要行（最显眼的一档）。"""
    logger.log(LEVEL_BATCH, message)


def log_row_detail(logger: logging.Logger, message: str) -> None:
    """逐行判定（次级一档，缩进一格）。"""
    logger.log(LEVEL_ROW, message)


def log_writeback(logger: logging.Logger, message: str) -> None:
    """回写 / 撤回回执行（青色）。"""
    logger.log(LEVEL_WRITEBACK, message)


def log_audit(logger: logging.Logger, message: str) -> None:
    """审计留痕行（最暗灰）。"""
    logger.log(LEVEL_AUDIT, message)


def log_heartbeat(logger: logging.Logger, message: str) -> None:
    """心跳行（最暗灰）。"""
    logger.log(LEVEL_HEARTBEAT, message)


def log_alert(logger: logging.Logger, message: str) -> None:
    """告警行（红色）。"""
    logger.error(message)


def heartbeat_interval(env: Mapping[str, str] | None = None) -> int:
    """心跳间隔（秒），读 ``MRFM_HEARTBEAT_SEC``。

    取值非法（非整数 / 非正数）时回落到默认值 :data:`DEFAULT_HEARTBEAT_SEC`
    ——心跳只是画面节奏，取不到就按默认节奏走，不让守护进程起不来。
    """
    source = os.environ if env is None else env
    raw = str(source.get(ENV_HEARTBEAT_SEC, "") or "").strip()
    if not raw:
        return DEFAULT_HEARTBEAT_SEC
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_HEARTBEAT_SEC
    if value <= 0:
        return DEFAULT_HEARTBEAT_SEC
    return value


class Heartbeat:
    """等间隔心跳：到点才出一行，不刷屏。

    :param interval_sec: 心跳间隔（秒）。
    :param logger_: 日志器（缺省用本模块的）。
    :param clock: 时钟（缺省 :func:`time.monotonic`；测试可注入假时钟）。
    """

    def __init__(
        self,
        interval_sec: int | None = None,
        *,
        logger_: logging.Logger | None = None,
        clock: Any = None,
    ) -> None:
        self.interval_sec = (
            int(interval_sec) if interval_sec is not None else heartbeat_interval()
        )
        self._logger = logger_ or get_logger("med_rule_fieldmap.heartbeat")
        self._clock = clock if clock is not None else time.monotonic
        self._last: float | None = None

    def due(self, now: float | None = None) -> bool:
        """到点了吗（第一次调用恒为真）。"""
        moment = self._clock() if now is None else float(now)
        if self._last is None:
            return True
        return (moment - self._last) >= self.interval_sec

    def beat(self, message: str, now: float | None = None) -> bool:
        """到点就打一行心跳，返回是否真的打了。"""
        moment = self._clock() if now is None else float(now)
        if not self.due(moment):
            return False
        self._last = moment
        log_heartbeat(self._logger, message)
        return True


__all__ = [
    "COLOR_BODY",
    "COLOR_CYAN",
    "COLOR_DIM",
    "COLOR_OK",
    "COLOR_RED",
    "COLOR_WARN",
    "DEFAULT_LOG_LEVEL",
    "LEVEL_AUDIT",
    "LEVEL_BATCH",
    "LEVEL_HEARTBEAT",
    "LEVEL_ROW",
    "LEVEL_STYLES",
    "LEVEL_WRITEBACK",
    "STYLE_ALERT",
    "STYLE_BATCH",
    "STYLE_DIM",
    "STYLE_OK",
    "STYLE_ROW",
    "STYLE_WARN",
    "STYLE_WRITEBACK",
    "Heartbeat",
    "HierarchicalFormatter",
    "LevelStyle",
    "Style",
    "get_logger",
    "heartbeat_interval",
    "log_alert",
    "log_audit",
    "log_batch_summary",
    "log_heartbeat",
    "log_row_detail",
    "log_writeback",
    "setup_logging",
    "supports_color",
]
