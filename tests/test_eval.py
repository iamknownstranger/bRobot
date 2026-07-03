"""Eval runner: accuracy + confusion against the fixed eval set, mock LLM."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from freebie_agent.config import Config

from .mocks import FakeLLM, default_extract

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "eval"))

from run_eval import format_report, load_items, run_eval  # noqa: E402


def perfect_score_fn(payload: dict[str, Any]) -> dict[str, Any]:
    """Scores derived from the eval item id embedded in what_you_get by the
    extract mock below — simulates a perfect scorer."""
    what = str(payload["extract"]["what_you_get"])
    if what.startswith("gem-"):
        return {"score": 90, "reason": "gem"}
    if what.startswith("mid-"):
        return {"score": 65, "reason": "middle"}
    return {"score": 5, "reason": "junk"}


def id_carrying_extract(payload: dict[str, Any]) -> dict[str, Any]:
    extract = default_extract(payload)
    # eval urls look like https://eval.local/<id>
    extract["what_you_get"] = str(payload["url"]).rsplit("/", 1)[-1]
    return extract


def test_eval_set_shape() -> None:
    items = load_items(REPO_ROOT / "eval/items.jsonl")
    assert len(items) == 30
    buckets = [i["expected_bucket"] for i in items]
    assert buckets.count("push") == 10
    assert buckets.count("digest") == 10
    assert buckets.count("drop") == 10


def test_run_eval_perfect_mock_scores_100(cfg: Config) -> None:
    llm = FakeLLM(extract_fn=id_carrying_extract, score_fn=perfect_score_fn)
    result = run_eval(llm, REPO_ROOT / "eval/items.jsonl", "profile", cfg.thresholds)
    assert result.total == 30
    assert result.accuracy == 1.0
    assert result.errors == 0
    assert result.confusion["push"]["push"] == 10
    assert result.confusion["drop"]["drop"] == 10


def test_run_eval_reports_confusion_for_bad_scorer(cfg: Config) -> None:
    """A scorer that pushes everything gets exactly the push third right."""
    llm = FakeLLM(
        extract_fn=id_carrying_extract,
        score_fn=lambda p: {"score": 99, "reason": "everything is amazing"},
    )
    result = run_eval(llm, REPO_ROOT / "eval/items.jsonl", "profile", cfg.thresholds)
    assert result.accuracy == 10 / 30
    assert result.confusion["drop"]["push"] == 10
    report = format_report(result)
    assert "bucket accuracy: 33.3%" in report
    assert "confusion" in report
