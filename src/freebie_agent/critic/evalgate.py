"""Eval gate: a proposal touching prompts/ may only be offered (or kept) if
the candidate prompts score >= the current prompts on the fixed eval set."""

from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from freebie_agent.config import Config
from freebie_agent.difftools import apply_diff_to_file

# eval/run_eval.py lives outside the package; make it importable.
_REPO_EVAL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "eval"


class ChatJson(Protocol):
    def chat_json(
        self,
        prompt_name: str,
        payload: dict[str, object],
        schema: dict[str, object] | None = None,
    ) -> dict[str, object]: ...


class LLMFactory(Protocol):
    """Builds a chat client bound to a specific prompts directory, so the
    gate can evaluate candidate prompt trees."""

    def __call__(self, prompts_dir: Path) -> ChatJson: ...


@dataclass(frozen=True)
class GateResult:
    before: float
    after: float

    @property
    def passed(self) -> bool:
        return self.after >= self.before


def default_llm_factory(cfg: Config) -> LLMFactory:
    from freebie_agent.llm import OllamaClient

    def factory(prompts_dir: Path) -> ChatJson:
        return OllamaClient(
            host=cfg.ollama_host,
            model=cfg.ollama_model,
            prompts_dir=prompts_dir,
            temperature=cfg.ollama_temperature,
            timeout_seconds=cfg.ollama_timeout_seconds,
            retries=cfg.ollama_retries,
        )

    return factory


def _run_eval_with(cfg: Config, prompts_dir: Path, llm_factory: LLMFactory) -> float:
    if str(_REPO_EVAL_DIR) not in sys.path:
        sys.path.insert(0, str(_REPO_EVAL_DIR))
    from run_eval import run_eval  # noqa: PLC0415

    llm = llm_factory(prompts_dir)
    try:
        result = run_eval(
            llm,
            cfg.paths.eval_items,
            cfg.paths.profile.read_text(encoding="utf-8"),
            cfg.thresholds,
        )
    finally:
        close = getattr(llm, "close", None)
        if callable(close):
            close()
    return float(result.accuracy)


def apply_diff_to_copy(cfg: Config, diff: str, workdir: Path) -> Path:
    """Materialize a candidate prompts dir: copy prompts/, apply the diff.

    Diff paths look like a/prompts/score.md; the staging dir mirrors the
    repo layout so the same diff applies here and (on approval) in the repo.
    """
    staging = workdir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    candidate_prompts = staging / cfg.paths.prompts.name
    shutil.copytree(cfg.paths.prompts, candidate_prompts)
    apply_diff_to_file(diff, staging)
    return candidate_prompts


def run_gate(cfg: Config, prompt_diff: str, llm_factory: LLMFactory) -> GateResult:
    """Evaluate current prompts vs. the diff-applied candidate."""
    before = _run_eval_with(cfg, cfg.paths.prompts, llm_factory)
    with tempfile.TemporaryDirectory(prefix="freebie-evalgate-") as tmp:
        candidate = apply_diff_to_copy(cfg, prompt_diff, Path(tmp))
        after = _run_eval_with(cfg, candidate, llm_factory)
    return GateResult(before=before, after=after)
