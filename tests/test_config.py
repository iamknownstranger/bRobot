"""Config fail-fast behavior: missing/malformed keys and env vars exit loudly."""

from __future__ import annotations

from pathlib import Path

import pytest

from freebie_agent.config import ConfigError, load_config, load_or_exit


def test_loads_valid_config(cfg: object) -> None:
    assert cfg is not None


def test_missing_config_file_fails(env: None, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    "var", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "OLLAMA_HOST", "OLLAMA_MODEL"]
)
def test_missing_env_var_fails(
    repo_copy: Path, env: None, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    monkeypatch.delenv(var)
    with pytest.raises(ConfigError, match=var):
        load_config(repo_copy / "config.toml")


def test_malformed_bot_token_fails(
    repo_copy: Path, env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-token")
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(repo_copy / "config.toml")


def test_non_numeric_owner_id_fails(
    repo_copy: Path, env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "alice")
    with pytest.raises(ConfigError, match="TELEGRAM_OWNER_ID"):
        load_config(repo_copy / "config.toml")


def test_missing_section_fails(repo_copy: Path, env: None) -> None:
    config = repo_copy / "config.toml"
    text = config.read_text().replace("[thresholds]", "[thresholds_gone]")
    config.write_text(text)
    with pytest.raises(ConfigError, match=r"\[thresholds\]"):
        load_config(config)


def test_inverted_thresholds_fail(repo_copy: Path, env: None) -> None:
    config = repo_copy / "config.toml"
    text = config.read_text().replace("push = 80", "push = 40")
    config.write_text(text)
    with pytest.raises(ConfigError, match="digest < push"):
        load_config(config)


def test_load_or_exit_exits_nonzero(env: None, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        load_or_exit(tmp_path / "nope.toml")
    assert excinfo.value.code == 2
