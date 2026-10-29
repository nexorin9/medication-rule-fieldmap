"""配置面：凭据与路径一律读环境变量。

规则：

* 凭据位（``HIS_BASE_URL`` / ``HIS_API_TOKEN`` / ``WECOM_WEBHOOK_URL``）只从环境变量读，
  不硬编码、不从规则文件读；缺失时由 :meth:`Config.require_his_base_url` 等方法显式抛
  :class:`ConfigError`，由调用方决定退出还是告警——不允许静默降级。
* 路径类变量（inbox / out / state / rules）有内置默认值，集中在模块顶部常量；
  相对路径按 ``base_dir`` 解析为绝对路径，避免守护进程工作目录变化导致读错目录。
* 心跳间隔等数值型变量取值非法时回落到默认值并在 :meth:`Config.warnings` 里留痕。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 模块级默认值（唯一集中处；改这里就能改全套默认路径）
# ---------------------------------------------------------------------------

DEFAULT_INBOX_DIR = "inbox"
DEFAULT_OUT_DIR = "out"
DEFAULT_STATE_DB = "state/mrfm.sqlite3"
DEFAULT_RULES_DIR = "rules"
DEFAULT_HEARTBEAT_SEC = 30

#: HIS 审方复核回写路径（spec.md §2.3）
HIS_REVIEW_PATH = "/api/v1/prescription/review"

#: HIS 审方复核**撤回**路径（spec.md §2.3 补充）——药师撤回确认后，
#: 已经发出去的那条复核记录必须让 HIS 端也撤掉，否则药师端与 HIS 端
#: 对「这条复核还算不算数」的说法不一致。
HIS_REVIEW_WITHDRAW_PATH = "/api/v1/prescription/review/withdraw"

# 环境变量名
ENV_BASE_DIR = "MRFM_BASE_DIR"
ENV_HIS_BASE_URL = "HIS_BASE_URL"
ENV_HIS_API_TOKEN = "HIS_API_TOKEN"
ENV_WECOM_WEBHOOK_URL = "WECOM_WEBHOOK_URL"
ENV_INBOX_DIR = "MRFM_INBOX_DIR"
ENV_OUT_DIR = "MRFM_OUT_DIR"
ENV_STATE_DB = "MRFM_STATE_DB"
ENV_RULES_DIR = "MRFM_RULES_DIR"
ENV_HEARTBEAT_SEC = "MRFM_HEARTBEAT_SEC"


class ConfigError(RuntimeError):
    """配置错误：凭据位缺失或取值非法。

    调用方必须显式处理（守护进程退出 / CLI 返回非 0 退出码），不得吞掉。
    """


def _clean(value: str | None) -> str | None:
    """去掉首尾空白；空串视为未配置。"""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _resolve_path(raw: str, base_dir: Path) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base_dir / path)


@dataclass(frozen=True)
class Config:
    """一次运行所需的全部配置快照。

    字段全部为「已解析」形态：路径是绝对路径，凭据是去空白后的字符串或 ``None``。
    """

    base_dir: Path
    his_base_url: str | None = None
    his_api_token: str | None = None
    wecom_webhook_url: str | None = None
    inbox_dir: Path = field(default_factory=lambda: Path(DEFAULT_INBOX_DIR))
    out_dir: Path = field(default_factory=lambda: Path(DEFAULT_OUT_DIR))
    state_db: Path = field(default_factory=lambda: Path(DEFAULT_STATE_DB))
    rules_dir: Path = field(default_factory=lambda: Path(DEFAULT_RULES_DIR))
    heartbeat_sec: int = DEFAULT_HEARTBEAT_SEC
    warnings: tuple[str, ...] = ()

    # -- 构造 ---------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        base_dir: str | Path | None = None,
    ) -> "Config":
        """从环境变量构造配置快照。

        ``env`` 默认取 ``os.environ``；``base_dir`` 缺省取 ``MRFM_BASE_DIR`` 环境变量，
        再缺省取当前工作目录。相对路径一律相对 ``base_dir`` 解析。
        """
        source: Mapping[str, str] = os.environ if env is None else env
        warnings: list[str] = []

        raw_base = base_dir if base_dir is not None else source.get(ENV_BASE_DIR)
        base_text = str(raw_base).strip() if raw_base is not None else ""
        resolved_base = Path(base_text or os.getcwd()).expanduser().resolve()

        heartbeat_raw = _clean(source.get(ENV_HEARTBEAT_SEC))
        if heartbeat_raw is None:
            heartbeat = DEFAULT_HEARTBEAT_SEC
        else:
            try:
                heartbeat = int(heartbeat_raw)
            except ValueError:
                heartbeat = DEFAULT_HEARTBEAT_SEC
                warnings.append(
                    f"{ENV_HEARTBEAT_SEC} 取值 {heartbeat_raw!r} 不是整数，回落到默认值 "
                    f"{DEFAULT_HEARTBEAT_SEC}"
                )
            else:
                if heartbeat <= 0:
                    heartbeat = DEFAULT_HEARTBEAT_SEC
                    warnings.append(
                        f"{ENV_HEARTBEAT_SEC} 取值必须为正整数，回落到默认值 "
                        f"{DEFAULT_HEARTBEAT_SEC}"
                    )

        def _path(env_name: str, default: str) -> Path:
            raw = _clean(source.get(env_name)) or default
            return _resolve_path(raw, resolved_base)

        return cls(
            base_dir=resolved_base,
            his_base_url=_clean(source.get(ENV_HIS_BASE_URL)),
            his_api_token=_clean(source.get(ENV_HIS_API_TOKEN)),
            wecom_webhook_url=_clean(source.get(ENV_WECOM_WEBHOOK_URL)),
            inbox_dir=_path(ENV_INBOX_DIR, DEFAULT_INBOX_DIR),
            out_dir=_path(ENV_OUT_DIR, DEFAULT_OUT_DIR),
            state_db=_path(ENV_STATE_DB, DEFAULT_STATE_DB),
            rules_dir=_path(ENV_RULES_DIR, DEFAULT_RULES_DIR),
            heartbeat_sec=heartbeat,
            warnings=tuple(warnings),
        )

    # -- 凭据位（显式失败，不静默跳过）-------------------------------------

    def require_his_base_url(self) -> str:
        """HIS 审方复核基址。未配置时显式失败。"""
        if not self.his_base_url:
            raise ConfigError(
                f"{ENV_HIS_BASE_URL} 未配置：HIS 审方复核回写是本产品唯一改变业务状态的路径，"
                "缺失时必须显式失败并退出，不得静默跳过。"
            )
        return self.his_base_url

    def require_his_api_token(self) -> str:
        """回写用 Bearer 令牌。未配置时显式失败。"""
        if not self.his_api_token:
            raise ConfigError(
                f"{ENV_HIS_API_TOKEN} 未配置：拒绝发出 HIS 回写请求（缺少凭据位的请求会被 HIS 拒绝）。"
            )
        return self.his_api_token

    @property
    def his_review_url(self) -> str:
        """HIS 审方复核回写完整地址（spec.md §2.3）。"""
        return self.require_his_base_url().rstrip("/") + HIS_REVIEW_PATH

    @property
    def has_his_credentials(self) -> bool:
        return bool(self.his_base_url and self.his_api_token)

    # -- 展示（绝不外泄令牌明文）-------------------------------------------

    def describe(self) -> dict[str, object]:
        """给 banner / status 用的可读快照；令牌只暴露「是否已配置」。"""
        return {
            "base_dir": str(self.base_dir),
            "inbox_dir": str(self.inbox_dir),
            "out_dir": str(self.out_dir),
            "state_db": str(self.state_db),
            "rules_dir": str(self.rules_dir),
            "heartbeat_sec": self.heartbeat_sec,
            "his_base_url": self.his_base_url,
            "his_api_token_set": bool(self.his_api_token),
            "wecom_webhook_set": bool(self.wecom_webhook_url),
            "warnings": list(self.warnings),
        }


def load_env_file(path: str | Path, *, override: bool = False) -> list[str]:
    """读取 ``.env`` 文件并写入 ``os.environ``。

    只支持 ``KEY=VALUE`` 与 ``#`` 注释两种写法；环境里已存在的键默认不覆盖
    （真实环境变量优先于文件），返回本次实际写入的键名列表。文件不存在时返回空列表。
    """
    env_path = Path(path)
    if not env_path.is_file():
        return []
    loaded: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded
