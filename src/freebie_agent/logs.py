"""Structured JSON logging to stdout and data/logs/, with secret redaction.

The redaction filter runs on every record and scrubs anything shaped like a
Telegram bot token, plus any explicitly registered secret values. It is also
reused by the bot before writing operator free-text into the events table.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

# Telegram bot token shape: digits, colon, 30+ token chars.
_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")

# Things the bot must never persist: card numbers (with separators), OTP-ish
# phrasing, government-ID-ish phrasing. Deliberately over-broad — this guards
# an events log, not a payments system.
SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    _BOT_TOKEN_RE,
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # card / long PAN-like runs
    re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),  # Indian PAN
    re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),  # Aadhaar-like
    re.compile(r"(?i)\b(otp|one[- ]time password)\b[^\n]{0,40}"),
    re.compile(r"(?i)\b(password|passwd)\s*[:=]\s*\S+"),
)

_registered_secrets: list[str] = []


def register_secret(value: str) -> None:
    """Register an exact secret string (e.g. the live bot token) for redaction."""
    if value and value not in _registered_secrets:
        _registered_secrets.append(value)


def redact(text: str) -> str:
    """Scrub registered secrets and sensitive patterns from text."""
    for secret in _registered_secrets:
        text = text.replace(secret, "[REDACTED]")
    for pattern in SENSITIVE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def contains_sensitive(text: str) -> bool:
    """True if text matches any sensitive pattern (used by the bot to refuse storage)."""
    return any(p.search(text) for p in SENSITIVE_PATTERNS) or any(
        s in text for s in _registered_secrets
    )


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(
                redact(a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            entry.update(extra)
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(logs_dir: Path | None, process: str) -> logging.Logger:
    """Configure root logging: JSON to stdout and to data/logs/<process>.log."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    formatter = JsonFormatter()
    redactor = RedactionFilter()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(redactor)
    root.addHandler(stream)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(logs_dir / f"{process}.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)

    return logging.getLogger(process)
