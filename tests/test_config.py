"""配置面回归测试：默认值、环境变量覆盖、凭据位显式失败、.env 载入。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from med_rule_fieldmap.config import (
    DEFAULT_HEARTBEAT_SEC,
    Config,
    ConfigError,
    load_env_file,
)


def test_defaults_when_environment_is_empty(tmp_path: Path) -> None:
    config = Config.from_env({}, base_dir=tmp_path)

    assert config.inbox_dir == tmp_path / "inbox"
    assert config.out_dir == tmp_path / "out"
    assert config.state_db == tmp_path / "state" / "mrfm.sqlite3"
    assert config.rules_dir == tmp_path / "rules"
    assert config.heartbeat_sec == DEFAULT_HEARTBEAT_SEC
    assert config.his_base_url is None
    assert config.his_api_token is None
    assert config.wecom_webhook_url is None


def test_environment_overrides_and_relative_paths_resolve_against_base(
    tmp_path: Path,
) -> None:
    config = Config.from_env(
        {
            "HIS_BASE_URL": "https://his.example.cn",
            "HIS_API_TOKEN": "token-placeholder",
            "WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
            "MRFM_INBOX_DIR": "his/inbox",
            "MRFM_OUT_DIR": "/var/lib/mrfm/out",
            "MRFM_STATE_DB": "state/main.sqlite3",
            "MRFM_RULES_DIR": "rules",
            "MRFM_HEARTBEAT_SEC": "15",
        },
        base_dir=tmp_path,
    )

    assert config.his_base_url == "https://his.example.cn"
    assert config.his_api_token == "token-placeholder"
    assert config.inbox_dir == tmp_path / "his" / "inbox"
    assert config.out_dir == Path("/var/lib/mrfm/out"), "绝对路径不应被 base_dir 改写"
    assert config.state_db == tmp_path / "state" / "main.sqlite3"
    assert config.heartbeat_sec == 15
    assert config.warnings == ()


def test_missing_his_base_url_fails_explicitly(tmp_path: Path) -> None:
    config = Config.from_env({}, base_dir=tmp_path)

    with pytest.raises(ConfigError) as excinfo:
        config.require_his_base_url()
    assert "HIS_BASE_URL" in str(excinfo.value)

    with pytest.raises(ConfigError):
        config.require_his_api_token()


def test_his_review_url_is_built_from_base_url(tmp_path: Path) -> None:
    config = Config.from_env(
        {"HIS_BASE_URL": "https://his.example.cn/"}, base_dir=tmp_path
    )
    assert config.his_review_url == "https://his.example.cn/api/v1/prescription/review"


def test_invalid_heartbeat_falls_back_to_default_with_warning(tmp_path: Path) -> None:
    config = Config.from_env({"MRFM_HEARTBEAT_SEC": "abc"}, base_dir=tmp_path)

    assert config.heartbeat_sec == DEFAULT_HEARTBEAT_SEC
    assert config.warnings and "MRFM_HEARTBEAT_SEC" in config.warnings[0]


def test_describe_never_exposes_token_value(tmp_path: Path) -> None:
    config = Config.from_env(
        {"HIS_API_TOKEN": "super-secret-token"}, base_dir=tmp_path
    )
    described = config.describe()

    assert described["his_api_token_set"] is True
    assert "super-secret-token" not in str(described)


def test_load_env_file_does_not_override_existing_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# 注释行",
                "HIS_BASE_URL=https://from-file.example.cn",
                'HIS_API_TOKEN="quoted-token"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HIS_BASE_URL", "https://from-env.example.cn")
    monkeypatch.delenv("HIS_API_TOKEN", raising=False)

    loaded = load_env_file(env_file)

    assert os.environ["HIS_BASE_URL"] == "https://from-env.example.cn"
    assert os.environ["HIS_API_TOKEN"] == "quoted-token"
    assert loaded == ["HIS_API_TOKEN"]


def test_load_env_file_on_missing_path_returns_empty_list(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "missing.env") == []
