"""Free-text -> intent via the LLM (fixed enum), plus the profile append
helper used by the update_profile intent.

Low-confidence classifications never guess: the caller asks a clarifying
question instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from freebie_agent import gitops
from freebie_agent.llm import LLMError, OllamaClient
from freebie_agent.models import Intent

CONFIDENCE_FLOOR = 0.6

INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": [i.value for i in Intent]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "args": {"type": "object"},
    },
    "required": ["intent", "confidence", "args"],
}


@dataclass(frozen=True)
class Classified:
    intent: Intent | None  # None => unknown/low confidence, ask back
    confidence: float
    args: dict[str, Any]


def classify(llm: OllamaClient, text: str) -> Classified:
    """Classify one owner message. Anything unparseable or below the
    confidence floor comes back as intent=None (ask, don't guess)."""
    try:
        raw = llm.chat_json("intent", {"message": text}, schema=INTENT_SCHEMA)
    except LLMError:
        return Classified(None, 0.0, {})
    try:
        intent = Intent(str(raw.get("intent")))
    except ValueError:
        return Classified(None, 0.0, {})
    confidence = float(raw.get("confidence", 0.0))
    args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
    if confidence < CONFIDENCE_FLOOR:
        return Classified(None, confidence, args or {})
    return Classified(intent, confidence, args or {})


OPERATOR_NOTES_HEADER = "## Operator notes"


def append_operator_note(profile_path: Path, note: str, repo_root: Path) -> str:
    """Append a timestamped line to the '## Operator notes' section of
    profile.md and commit it. Returns the commit sha."""
    text = profile_path.read_text(encoding="utf-8")
    if OPERATOR_NOTES_HEADER not in text:
        text = text.rstrip("\n") + f"\n\n{OPERATOR_NOTES_HEADER}\n"
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    line = f"- [{stamp}] {note.strip()}"
    text = text.rstrip("\n") + f"\n{line}\n"
    profile_path.write_text(text, encoding="utf-8")
    return gitops.commit_paths(
        repo_root, [profile_path], f"profile: operator note — {note.strip()[:60]}"
    )


def set_value_floor(profile_path: Path, floor_inr: int, repo_root: Path) -> str:
    """Rewrite the ₹ amount in the '## Value floor' section and commit."""
    import re

    text = profile_path.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r"(## Value floor\n+[^\n]*?)₹\d+",
        rf"\g<1>₹{floor_inr}",
        text,
        count=1,
    )
    if n == 0:
        raise ValueError("profile.md has no '## Value floor' section with a ₹ amount")
    profile_path.write_text(new_text, encoding="utf-8")
    return gitops.commit_paths(repo_root, [profile_path], f"profile: value floor -> ₹{floor_inr}")
