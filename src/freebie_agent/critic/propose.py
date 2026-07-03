"""Findings -> proposal rows (unified diffs + rationales).

Diffs are generated deterministically; when [critic].llm_backend is not
"none", the LLM may reword the rationale (never the diff, never the decision).
Prompt-touching proposals only become `pending` if they pass the eval gate.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from pathlib import Path

from freebie_agent import ledger
from freebie_agent.config import Config
from freebie_agent.critic.analyze import Finding
from freebie_agent.critic.evalgate import GateResult, LLMFactory, run_gate
from freebie_agent.models import ProposalKind


def unified_diff(old: str, new: str, rel_path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )


def _set_source_status_text(yaml_text: str, source_id: str, new_status: str) -> str:
    """Flip the status: line inside one source's block, preserving comments."""
    pattern = re.compile(
        rf"(- id: {re.escape(source_id)}\n(?:(?!\s*- id:).*\n)*?\s*status:\s*)(\w+)",
    )
    new_text, n = pattern.subn(rf"\g<1>{new_status}", yaml_text, count=1)
    if n == 0:
        raise ValueError(f"source {source_id!r} has no status line in sources.yaml")
    return new_text


def source_change_diff(cfg: Config, source_id: str, new_status: str) -> str:
    text = cfg.paths.sources.read_text(encoding="utf-8")
    new_text = _set_source_status_text(text, source_id, new_status)
    return unified_diff(text, new_text, cfg.paths.sources.name)


def score_prompt_append_diff(cfg: Config, lines: list[str]) -> str:
    """A prompt_diff that appends caution lines to prompts/score.md."""
    path = cfg.paths.prompts / "score.md"
    old = path.read_text(encoding="utf-8")
    new = old.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    return unified_diff(old, new, f"{cfg.paths.prompts.name}/score.md")


def threshold_diff(cfg: Config, config_path: Path, new_push: int) -> str:
    old = config_path.read_text(encoding="utf-8")
    new, n = re.subn(r"(\[thresholds\][^\[]*?push\s*=\s*)\d+", rf"\g<1>{new_push}", old, count=1)
    if n == 0:
        raise ValueError("config.toml has no [thresholds].push key")
    return unified_diff(old, new, config_path.name)


def _reword_rationale(cfg: Config, finding: Finding, deterministic: str) -> str:
    """Optionally let a (possibly bigger) LLM word the rationale. The critic
    stays functional with llm_backend = 'none'."""
    if cfg.critic_llm_backend == "none":
        return deterministic
    try:
        from freebie_agent.llm import OllamaClient

        client = OllamaClient(
            host=cfg.ollama_host,
            model=cfg.critic_llm_backend,
            prompts_dir=cfg.paths.prompts,
            temperature=cfg.ollama_temperature,
            timeout_seconds=cfg.ollama_timeout_seconds,
            retries=cfg.ollama_retries,
        )
        try:
            result = client.chat_json("critic", {"finding": {"kind": finding.kind, **finding.data}})
        finally:
            client.close()
        rationale = str(result.get("rationale", "")).strip()
        return rationale or deterministic
    except Exception:
        return deterministic


def propose_from_findings(
    conn: sqlite3.Connection,
    cfg: Config,
    findings: list[Finding],
    config_path: Path,
    llm_factory: LLMFactory | None = None,
) -> list[int]:
    """Create proposal rows (+ outbox cards). Returns created proposal ids.

    Prompt-diff proposals run the eval gate first: candidate accuracy must be
    >= current accuracy or the proposal is discarded (recorded as an event,
    never shown to the operator).
    """
    created: list[int] = []
    for finding in findings:
        proposal_id: int | None = None
        if finding.kind in ("source_kill", "source_promote"):
            source_id = str(finding.data["source_id"])
            new_status = "killed" if finding.kind == "source_kill" else "active"
            diff = source_change_diff(cfg, source_id, new_status)
            if finding.kind == "source_kill":
                deterministic = (
                    f"kill source {source_id}: {finding.data['items_claimed']} claims / "
                    f"{finding.data['items_seen']} items over {finding.data['weeks']} weeks."
                )
            else:
                deterministic = (
                    f"promote shadow source {source_id}: {finding.data['would_push']} "
                    f"would-have-pushed items in {finding.data['days_shadow']} days of shadow."
                )
            proposal_id = ledger.add_proposal(
                conn,
                ProposalKind.SOURCE_CHANGE,
                diff,
                _reword_rationale(cfg, finding, deterministic),
            )
        elif finding.kind in ("high_score_skipped", "worth_it_negative"):
            cats_raw = finding.data.get("categories", [])
            examples_raw = finding.data.get("examples", [])
            categories = (
                ", ".join(str(c) for c in cats_raw) if isinstance(cats_raw, list) else "various"
            ) or "various"
            examples = (
                "; ".join(str(e) for e in examples_raw) if isinstance(examples_raw, list) else ""
            )
            if finding.kind == "high_score_skipped":
                caution = (
                    f"- Operator recently skipped {finding.data['count']} high-scored items "
                    f"(categories: {categories}; e.g. {examples}). Score similar items at "
                    f"most 60 unless the profile explicitly wants them."
                )
                deterministic = (
                    f"{finding.data['count']} items scored >= push threshold were skipped; "
                    f"tighten score.md for categories: {categories}."
                )
            else:
                caution = (
                    f"- Operator marked {finding.data['count']} claimed items as not worth it "
                    f"(categories: {categories}; e.g. {examples}). Score similar items at "
                    f"most 40."
                )
                deterministic = (
                    f"{finding.data['count']} claims got 👎 worth-it feedback; penalize "
                    f"categories: {categories} in score.md."
                )
            diff = score_prompt_append_diff(cfg, [caution])
            gate: GateResult | None = None
            if llm_factory is not None:
                gate = run_gate(cfg, diff, llm_factory)
                if not gate.passed:
                    ledger.add_event(
                        conn,
                        "proposal_gate_blocked",
                        {
                            "finding": finding.kind,
                            "eval_before": gate.before,
                            "eval_after": gate.after,
                        },
                    )
                    continue
            proposal_id = ledger.add_proposal(
                conn,
                ProposalKind.PROMPT_DIFF,
                diff,
                _reword_rationale(cfg, finding, deterministic),
                eval_before=gate.before if gate else None,
                eval_after=gate.after if gate else None,
            )
        elif finding.kind == "threshold_tune":
            diff = threshold_diff(cfg, config_path, int(str(finding.data["proposed_push"])))
            deterministic = (
                f"{finding.data['skipped']}/{finding.data['pushes']} pushes were skipped "
                f"(rate {finding.data['skip_rate']}); raise push threshold "
                f"{finding.data['current_push']} -> {finding.data['proposed_push']}."
            )
            proposal_id = ledger.add_proposal(
                conn,
                ProposalKind.THRESHOLD_CHANGE,
                diff,
                _reword_rationale(cfg, finding, deterministic),
            )
        if proposal_id is not None:
            ledger.enqueue_outbox(conn, "proposal", {"proposal_id": proposal_id})
            ledger.add_event(
                conn, "proposal_created", {"proposal_id": proposal_id, "finding": finding.kind}
            )
            created.append(proposal_id)
    return created
