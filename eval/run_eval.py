"""Run the fixed eval set through extract+score with a given prompts dir.

Usage (real model):
    uv run python eval/run_eval.py [--prompts prompts] [--items eval/items.jsonl]

The core entrypoint `run_eval` accepts any object with the OllamaClient
chat_json interface, so the eval gate can run candidate prompt dirs and tests
can run a mock with no network.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from freebie_agent.config import Thresholds  # noqa: E402
from freebie_agent.llm import LLMError  # noqa: E402
from freebie_agent.pipeline import (  # noqa: E402
    EXTRACT_SCHEMA,
    SCORE_SCHEMA,
    clamp_score,
    validate_extract,
)
from freebie_agent.router import bucket_for_score  # noqa: E402

BUCKETS = ("push", "digest", "drop")


class ChatJson(Protocol):
    def chat_json(
        self,
        prompt_name: str,
        payload: dict[str, Any],
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@dataclass
class EvalResult:
    total: int = 0
    correct: int = 0
    errors: int = 0
    # confusion[expected][actual] = count
    confusion: dict[str, dict[str, int]] = field(
        default_factory=lambda: {e: {a: 0 for a in BUCKETS} for e in BUCKETS}
    )
    mistakes: list[dict[str, str]] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def load_items(items_path: Path) -> list[dict[str, Any]]:
    items = []
    with items_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def run_eval(
    llm: ChatJson,
    items_path: Path,
    profile_text: str,
    thresholds: Thresholds,
) -> EvalResult:
    result = EvalResult()
    for item in load_items(items_path):
        expected = str(item["expected_bucket"])
        result.total += 1
        try:
            extract = validate_extract(
                llm.chat_json(
                    "extract",
                    {
                        "title": item["title"],
                        "body": item["body"],
                        "url": item["url"],
                        "source": "eval",
                    },
                    schema=EXTRACT_SCHEMA,
                )
            )
            scored = llm.chat_json(
                "score", {"extract": extract, "profile": profile_text}, schema=SCORE_SCHEMA
            )
            score = clamp_score(scored.get("score"))
        except LLMError as exc:
            result.errors += 1
            result.mistakes.append(
                {"id": str(item["id"]), "expected": expected, "actual": f"error: {exc}"}
            )
            continue
        actual = bucket_for_score(score, thresholds).value
        result.confusion[expected][actual] += 1
        if actual == expected:
            result.correct += 1
        else:
            result.mistakes.append({"id": str(item["id"]), "expected": expected, "actual": actual})
    return result


def format_report(result: EvalResult) -> str:
    lines = [
        f"items: {result.total}  correct: {result.correct}  errors: {result.errors}",
        f"bucket accuracy: {result.accuracy:.1%}",
        "",
        "confusion (rows = expected, cols = actual):",
        f"{'':>8} {'push':>6} {'digest':>7} {'drop':>6}",
    ]
    for expected in BUCKETS:
        row = result.confusion[expected]
        lines.append(f"{expected:>8} {row['push']:>6} {row['digest']:>7} {row['drop']:>6}")
    if result.mistakes:
        lines.append("")
        lines.append("mistakes:")
        for m in result.mistakes:
            lines.append(f"  {m['id']}: expected {m['expected']}, got {m['actual']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompts", default=str(REPO_ROOT / "prompts"), type=Path)
    parser.add_argument("--items", default=str(REPO_ROOT / "eval/items.jsonl"), type=Path)
    parser.add_argument("--profile", default=str(REPO_ROOT / "profile.md"), type=Path)
    parser.add_argument("--config", default=str(REPO_ROOT / "config.toml"), type=Path)
    args = parser.parse_args()

    from freebie_agent.config import load_or_exit
    from freebie_agent.llm import OllamaClient

    cfg = load_or_exit(args.config)
    llm = OllamaClient(
        host=cfg.ollama_host,
        model=cfg.ollama_model,
        prompts_dir=args.prompts,
        temperature=cfg.ollama_temperature,
        timeout_seconds=cfg.ollama_timeout_seconds,
        retries=cfg.ollama_retries,
    )
    try:
        result = run_eval(llm, args.items, args.profile.read_text(encoding="utf-8"), cfg.thresholds)
    finally:
        llm.close()
    print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
