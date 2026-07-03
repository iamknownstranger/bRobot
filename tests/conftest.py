"""Shared fixtures. No network anywhere in the test suite: Ollama and
Telegram are always mocked."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from freebie_agent import db as dbmod
from freebie_agent.config import Config, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent

FAKE_TOKEN = "123456789:TESTFAKEtokenTESTFAKEtokenTESTFAKE0"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Run in a .env-free cwd: a deployed clone has a real .env next to
    # pytest's default cwd, and load_dotenv would resurrect deleted vars in
    # the fail-fast tests (regression test guard, found on fresh-clone check).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "424242")
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "gemma3:test")


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A working directory that looks like a deployed repo: config, prompts,
    profile, sources, migrations, eval set."""
    for name in ("prompts", "migrations", "eval"):
        src = REPO_ROOT / name
        if src.exists():
            shutil.copytree(src, tmp_path / name)
    for name in ("profile.md", "sources.yaml", ".gitignore"):
        src = REPO_ROOT / name
        if src.exists():
            shutil.copy(src, tmp_path / name)
    shutil.copy(REPO_ROOT / "config.example.toml", tmp_path / "config.toml")
    (tmp_path / "data").mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "init"], cwd=tmp_path, check=True
    )
    return tmp_path


@pytest.fixture
def cfg(repo_copy: Path, env: None) -> Config:
    return load_config(repo_copy / "config.toml")


@pytest.fixture
def conn(cfg: Config) -> Iterator[sqlite3.Connection]:
    connection = dbmod.connect(cfg.paths.db)
    dbmod.migrate(connection, REPO_ROOT / "migrations")
    yield connection
    connection.close()
