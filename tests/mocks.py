"""Shared test doubles. No network anywhere: this is the whole point."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeLLM:
    """Stands in for OllamaClient in pipeline/eval tests."""

    def __init__(
        self,
        extract_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        score_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.model = "mock-model"
        self.last_latency_ms = 1.0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._extract_fn = extract_fn or default_extract
        self._score_fn = score_fn or default_score

    def chat_json(
        self,
        prompt_name: str,
        payload: dict[str, Any],
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((prompt_name, payload))
        if prompt_name == "extract":
            return self._extract_fn(payload)
        if prompt_name == "score":
            return self._score_fn(payload)
        raise AssertionError(f"unexpected prompt {prompt_name!r}")

    def close(self) -> None:
        pass


def default_extract(payload: dict[str, Any]) -> dict[str, Any]:
    """A minimal but schema-complete extract derived from the raw title."""
    return {
        "what_you_get": str(payload.get("title", ""))[:80],
        "estimated_value_inr": 1000,
        "effort_minutes": 5,
        "deadline_iso": None,
        "requirements": [],
        "region": "IN",
        "category": "other",
        "red_flags": [],
        "is_lead_gen_trap": False,
    }


def default_score(payload: dict[str, Any]) -> dict[str, Any]:
    return {"score": 60, "reason": "mock score"}


def scores_by_title(mapping: dict[str, int]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """score_fn that looks up the score by substring of what_you_get."""

    def fn(payload: dict[str, Any]) -> dict[str, Any]:
        what = str(payload["extract"]["what_you_get"])
        for needle, score in mapping.items():
            if needle in what:
                return {"score": score, "reason": f"mock: matched {needle!r}"}
        raise AssertionError(f"no score mapping for {what!r}")

    return fn
