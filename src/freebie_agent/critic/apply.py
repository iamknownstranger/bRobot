"""Apply/reject operator decisions on proposals.

Apply = write the diff, re-run the eval gate post-write for prompt changes,
git commit with the rationale as message, mark applied with the sha.
Reject = record it. Every self-modification is a git commit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from freebie_agent import gitops, ledger, sourcecfg
from freebie_agent.config import Config
from freebie_agent.critic.evalgate import LLMFactory, _run_eval_with, default_llm_factory
from freebie_agent.difftools import DiffError, apply_diff_to_file
from freebie_agent.models import ProposalKind, ProposalState, SourceStatus

# Pseudo-diff marker for the behavioral "enable auto-apply" proposal, which
# flips a kv flag instead of patching a file (see DECISIONS.md).
AUTO_APPLY_MARKER = "AUTO-APPLY-SOURCE-CHANGES"
AUTO_APPLY_KV_KEY = "auto_apply_source_change"


def reject_proposal(conn: sqlite3.Connection, proposal_id: int) -> str:
    proposal = ledger.get_proposal(conn, proposal_id)
    if proposal is None:
        return f"Proposal #{proposal_id} not found."
    if proposal["state"] != ProposalState.PENDING.value:
        return f"Proposal #{proposal_id} is already {proposal['state']}."
    ledger.set_proposal_state(conn, proposal_id, ProposalState.REJECTED)
    ledger.add_event(conn, "proposal_rejected", {"proposal_id": proposal_id})
    return f"❌ Proposal #{proposal_id} rejected and recorded."


def _resync_sources(conn: sqlite3.Connection, cfg: Config) -> None:
    for entry in sourcecfg.load_sources(cfg.paths.sources):
        ledger.sync_source(
            conn,
            source_id=str(entry["id"]),
            type_=str(entry["type"]),
            url=str(entry.get("url", "")),
            schedule=str(entry.get("schedule", "")),
            status=SourceStatus(str(entry.get("status", "active"))),
        )


def apply_proposal(
    conn: sqlite3.Connection,
    cfg: Config,
    proposal_id: int,
    llm_factory: LLMFactory | None = None,
) -> str:
    proposal = ledger.get_proposal(conn, proposal_id)
    if proposal is None:
        return f"Proposal #{proposal_id} not found."
    if proposal["state"] != ProposalState.PENDING.value:
        return f"Proposal #{proposal_id} is already {proposal['state']}."

    repo_root = cfg.paths.profile.resolve().parent
    kind = ProposalKind(proposal["kind"])
    diff = str(proposal["diff"])
    rationale = str(proposal["rationale"])

    # Behavioral proposal: enable the source-change auto-apply tier.
    if AUTO_APPLY_MARKER in diff:
        ledger.kv_set(conn, AUTO_APPLY_KV_KEY, "true")
        ledger.set_proposal_state(conn, proposal_id, ProposalState.APPLIED)
        ledger.add_event(conn, "proposal_applied", {"proposal_id": proposal_id})
        return (
            f"✅ Proposal #{proposal_id} applied: source pruning/promotion "
            "proposals now auto-apply (still notified)."
        )

    try:
        target = apply_diff_to_file(diff, repo_root)
    except (DiffError, OSError) as exc:
        return f"⚠️ Proposal #{proposal_id} no longer applies cleanly: {exc}"

    # Prompt changes re-run the eval gate post-write; a regression reverts.
    if kind is ProposalKind.PROMPT_DIFF:
        factory = llm_factory or default_llm_factory(cfg)
        baseline = proposal["eval_before"]
        post = _run_eval_with(cfg, cfg.paths.prompts, factory)
        if baseline is not None and post < float(baseline):
            _git_restore(repo_root, target)
            ledger.set_proposal_state(
                conn,
                proposal_id,
                ProposalState.REJECTED,
                rationale_suffix=f" [post-write eval {post:.1%} < baseline "
                f"{float(baseline):.1%}; reverted]",
            )
            ledger.add_event(
                conn,
                "proposal_gate_blocked",
                {"proposal_id": proposal_id, "phase": "post_write", "eval_after": post},
            )
            return (
                f"⚠️ Proposal #{proposal_id} reverted: post-write eval "
                f"{post:.1%} regressed below {float(baseline):.1%}."
            )

    sha = gitops.commit_paths(repo_root, [target], f"critic proposal #{proposal_id}: {rationale}")
    ledger.set_proposal_state(conn, proposal_id, ProposalState.APPLIED, git_commit=sha)
    ledger.add_event(conn, "proposal_applied", {"proposal_id": proposal_id, "commit": sha})
    if kind is ProposalKind.SOURCE_CHANGE:
        _resync_sources(conn, cfg)
    note = ""
    if kind is ProposalKind.THRESHOLD_CHANGE:
        note = " Restart freebie-worker to pick up the new thresholds."
    return f"✅ Proposal #{proposal_id} applied (commit {sha}).{note}"


def _git_restore(repo_root: Path, target: Path) -> None:
    import subprocess

    subprocess.run(
        ["git", "checkout", "--", str(target)],
        cwd=repo_root,
        capture_output=True,
        timeout=30,
        check=False,
    )
