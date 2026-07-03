"""Config loading with fail-fast validation.

One `config.toml` plus four required environment variables. Anything missing
or malformed exits non-zero with a message naming the offending key. No
silent defaults for secrets or identity.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REQUIRED_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "OLLAMA_HOST", "OLLAMA_MODEL")

_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(Exception):
    """Raised when config.toml or the environment is missing/malformed."""


@dataclass(frozen=True)
class Paths:
    db: Path
    logs: Path
    prompts: Path
    profile: Path
    sources: Path
    eval_items: Path


@dataclass(frozen=True)
class Thresholds:
    push: int
    digest: int


@dataclass(frozen=True)
class Config:
    paths: Paths
    thresholds: Thresholds
    pushes_per_day: int
    digest_send_at: str
    digest_top_n: int
    nag_hours: tuple[int, ...]
    question_ttl_hours: int
    ollama_timeout_seconds: float
    ollama_retries: int
    ollama_temperature: float
    metrics_enabled: bool
    metrics_port: int
    critic_day_of_week: str
    critic_hour: int
    critic_lookback_weeks: int
    critic_shadow_days: int
    critic_llm_backend: str
    default_fetch_minutes: int
    outbox_poll_seconds: int
    # From environment (secrets/identity — never logged, never persisted).
    telegram_bot_token: str = field(repr=False, default="")
    telegram_owner_id: int = 0
    ollama_host: str = ""
    ollama_model: str = ""


class _Section:
    """Typed accessors over one TOML table; every miss is a ConfigError."""

    def __init__(self, data: dict[str, object], name: str) -> None:
        section = data.get(name)
        if not isinstance(section, dict):
            raise ConfigError(f"config.toml: missing required section [{name}]")
        self._table: dict[str, object] = section
        self._name = name

    def _get(self, key: str) -> object:
        if key not in self._table:
            raise ConfigError(f"config.toml: missing required key [{self._name}].{key}")
        return self._table[key]

    def _fail_type(self, key: str, expected: str, value: object) -> ConfigError:
        return ConfigError(
            f"config.toml: [{self._name}].{key} must be {expected}, got {type(value).__name__}"
        )

    def str_(self, key: str) -> str:
        value = self._get(key)
        if not isinstance(value, str):
            raise self._fail_type(key, "a string", value)
        return value

    def int_(self, key: str) -> int:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._fail_type(key, "an integer", value)
        return value

    def float_(self, key: str) -> float:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise self._fail_type(key, "a number", value)
        return float(value)

    def bool_(self, key: str) -> bool:
        value = self._get(key)
        if not isinstance(value, bool):
            raise self._fail_type(key, "a boolean", value)
        return value

    def int_list(self, key: str) -> list[int]:
        value = self._get(key)
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
        ):
            raise self._fail_type(key, "a non-empty list of integers", value)
        return list(value)


def load_env(dotenv_path: Path | None = None) -> dict[str, str]:
    """Load .env (if present) and validate required environment variables.

    The default .env location is explicitly ./​.env (the runbook runs all
    processes from the repo root) — not python-dotenv's stack-walking
    discovery, which would find the repo's .env even when cwd is elsewhere.
    """
    load_dotenv(dotenv_path if dotenv_path is not None else Path(".env"))
    env: dict[str, str] = {}
    for key in REQUIRED_ENV:
        value = os.environ.get(key, "").strip()
        if not value:
            raise ConfigError(f"environment: {key} is required and not set (see .env.example)")
        env[key] = value
    if not _BOT_TOKEN_RE.match(env["TELEGRAM_BOT_TOKEN"]):
        raise ConfigError(
            "environment: TELEGRAM_BOT_TOKEN is malformed (expected '<digits>:<token>')"
        )
    if not env["TELEGRAM_OWNER_ID"].isdigit():
        raise ConfigError("environment: TELEGRAM_OWNER_ID must be a numeric Telegram user id")
    if not env["OLLAMA_HOST"].startswith(("http://", "https://")):
        raise ConfigError("environment: OLLAMA_HOST must be an http(s) URL")
    return env


def load_config(
    config_path: Path | str = Path("config.toml"),
    dotenv_path: Path | None = None,
) -> Config:
    """Load and validate config.toml + environment. Raises ConfigError."""
    config_path = Path(config_path)
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path} (copy config.example.toml)")
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml: invalid TOML: {exc}") from exc

    env = load_env(dotenv_path)

    paths_t = _Section(data, "paths")
    base = config_path.resolve().parent
    paths = Paths(
        db=base / paths_t.str_("db"),
        logs=base / paths_t.str_("logs"),
        prompts=base / paths_t.str_("prompts"),
        profile=base / paths_t.str_("profile"),
        sources=base / paths_t.str_("sources"),
        eval_items=base / paths_t.str_("eval_items"),
    )

    th = _Section(data, "thresholds")
    push = th.int_("push")
    digest = th.int_("digest")
    if not (0 <= digest < push <= 100):
        raise ConfigError(
            f"config.toml: thresholds must satisfy 0 <= digest < push <= 100 "
            f"(got digest={digest}, push={push})"
        )

    rl = _Section(data, "rate_limit")
    dg = _Section(data, "digest")
    send_at = dg.str_("send_at")
    if not _TIME_RE.match(send_at):
        raise ConfigError(f"config.toml: [digest].send_at must be HH:MM 24h, got {send_at!r}")
    dl = _Section(data, "deadlines")
    nag_hours = dl.int_list("nag_hours")
    if any(h <= 0 for h in nag_hours):
        raise ConfigError("config.toml: [deadlines].nag_hours entries must be > 0")
    qs = _Section(data, "questions")
    ol = _Section(data, "ollama")
    me = _Section(data, "metrics")
    cr = _Section(data, "critic")
    wk = _Section(data, "worker")

    return Config(
        paths=paths,
        thresholds=Thresholds(push=push, digest=digest),
        pushes_per_day=rl.int_("pushes_per_day"),
        digest_send_at=send_at,
        digest_top_n=dg.int_("top_n"),
        nag_hours=tuple(sorted(nag_hours, reverse=True)),
        question_ttl_hours=qs.int_("ttl_hours"),
        ollama_timeout_seconds=ol.float_("timeout_seconds"),
        ollama_retries=ol.int_("retries"),
        ollama_temperature=ol.float_("temperature"),
        metrics_enabled=me.bool_("enabled"),
        metrics_port=me.int_("port"),
        critic_day_of_week=cr.str_("day_of_week"),
        critic_hour=cr.int_("hour"),
        critic_lookback_weeks=cr.int_("lookback_weeks"),
        critic_shadow_days=cr.int_("shadow_days"),
        critic_llm_backend=cr.str_("llm_backend"),
        default_fetch_minutes=wk.int_("default_fetch_minutes"),
        outbox_poll_seconds=wk.int_("outbox_poll_seconds"),
        telegram_bot_token=env["TELEGRAM_BOT_TOKEN"],
        telegram_owner_id=int(env["TELEGRAM_OWNER_ID"]),
        ollama_host=env["OLLAMA_HOST"].rstrip("/"),
        ollama_model=env["OLLAMA_MODEL"],
    )


def load_or_exit(config_path: Path | str = Path("config.toml")) -> Config:
    """Entrypoint helper: load config or exit(2) with a clear message."""
    try:
        return load_config(config_path)
    except ConfigError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
