"""Critic: deterministic findings, proposals with diffs, the eval gate
(refusal on regression), apply/reject via git commits, shadow promotion,
auto-apply tier, dirty-tree refusal."""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from freebie_agent import gitops, ledger
from freebie_agent.config import Config
from freebie_agent.critic import apply as apply_mod
from freebie_agent.critic.analyze import analyze
from freebie_agent.critic.evalgate import run_gate
from freebie_agent.critic.propose import propose_from_findings, score_prompt_append_diff
from freebie_agent.critic.run import (
    DirtyTreeError,
    maybe_propose_auto_tier,
    run_critic_once,
)
from freebie_agent.difftools import DiffError, apply_unified_diff
from freebie_agent.models import (
    ClaimState,
    ProposalKind,
    ProposalState,
    RawItem,
    SourceStatus,
)

from .mocks import FakeLLM

ACTIVE_SRC = "desidime-freebies-rss"  # exists in sources.yaml with status: active


def _seed_items(
    conn: sqlite3.Connection, source_id: str, n: int, prefix: str = "item"
) -> list[int]:
    ids = []
    for i in range(n):
        item_id, _ = ledger.upsert_raw_item(
            conn, RawItem(f"https://{source_id}.test/{prefix}-{i}", f"{prefix} {i}", "b", source_id)
        )
        ids.append(item_id)
    return ids


# ---------------------------------------------------------------- findings


def test_noisy_zero_claim_source_yields_kill_proposal(
    conn: sqlite3.Connection, cfg: Config, repo_copy: Path
) -> None:
    """Acceptance: seeded ledger fixture yields a source-prune proposal."""
    ledger.sync_source(conn, ACTIVE_SRC, "rss", "u", "2h", SourceStatus.ACTIVE)
    _seed_items(conn, ACTIVE_SRC, 35)

    findings = analyze(conn, cfg)
    kinds = [f.kind for f in findings]
    assert "source_kill" in kinds

    created = propose_from_findings(conn, cfg, findings, repo_copy / "config.toml")
    assert len(created) == 1
    proposal = ledger.get_proposal(conn, created[0])
    assert proposal is not None
    assert proposal["kind"] == ProposalKind.SOURCE_CHANGE.value
    assert proposal["state"] == ProposalState.PENDING.value
    assert "-    status: active" in proposal["diff"]
    assert "+    status: killed" in proposal["diff"]
    assert "0 claims" in proposal["rationale"] or "35 items" in proposal["rationale"]
    # a proposal card was queued for the bot
    assert [o["kind"] for o in ledger.unsent_outbox(conn)] == ["proposal"]


def test_shadow_source_promotion_after_window(
    conn: sqlite3.Connection, cfg: Config, repo_copy: Path
) -> None:
    # a shadow source present in sources.yaml
    sources_yaml = cfg.paths.sources
    sources_yaml.write_text(
        sources_yaml.read_text(encoding="utf-8")
        + "\n  - id: shadow-newsletter\n    type: rss\n    url: https://s.test/feed\n"
        "    schedule: 6h\n    status: shadow\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo_copy, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "add shadow"],
        cwd=repo_copy,
        check=True,
    )
    ledger.sync_source(conn, "shadow-newsletter", "rss", "u", "6h", SourceStatus.SHADOW)
    # shadow for longer than the window
    conn.execute(
        "UPDATE sources SET shadow_since = ? WHERE id = 'shadow-newsletter'",
        (ledger.iso(ledger.utcnow() - timedelta(days=20)),),
    )
    conn.commit()
    item_ids = _seed_items(conn, "shadow-newsletter", 5, prefix="shadow")
    for item_id in item_ids[:3]:
        ledger.add_event(
            conn,
            "route_demoted",
            {"reason": "shadow_source", "score": 90},
            item_id=item_id,
        )

    findings = analyze(conn, cfg)
    assert [f.kind for f in findings if f.kind == "source_promote"]

    created = propose_from_findings(conn, cfg, findings, repo_copy / "config.toml")
    proposal = ledger.get_proposal(conn, created[0])
    assert proposal is not None
    assert "+    status: active" in proposal["diff"]

    # apply: yaml updated, git commit recorded, DB resynced
    outcome = apply_mod.apply_proposal(conn, cfg, created[0])
    assert outcome.startswith("✅")
    assert (
        "status: active"
        in sources_yaml.read_text(encoding="utf-8").split("shadow-newsletter", 1)[1]
    )
    proposal = ledger.get_proposal(conn, created[0])
    assert proposal is not None
    assert proposal["state"] == ProposalState.APPLIED.value
    assert proposal["git_commit"]
    assert not gitops.is_dirty(repo_copy)
    src = ledger.get_source(conn, "shadow-newsletter")
    assert src is not None and src["status"] == SourceStatus.ACTIVE.value


def test_threshold_tuning_finding(conn: sqlite3.Connection, cfg: Config, repo_copy: Path) -> None:
    ledger.sync_source(conn, "s", "rss", "u", "1h", SourceStatus.ACTIVE)
    item_ids = _seed_items(conn, "s", 12)
    for i, item_id in enumerate(item_ids):
        claim_id = ledger.create_claim(conn, item_id)
        if i < 8:  # 8/12 skipped > 50%
            ledger.set_claim_state(conn, claim_id, ClaimState.SKIPPED)
    findings = [f for f in analyze(conn, cfg) if f.kind == "threshold_tune"]
    assert len(findings) == 1
    assert findings[0].data["proposed_push"] == 85

    created = propose_from_findings(conn, cfg, findings, repo_copy / "config.toml")
    proposal = ledger.get_proposal(conn, created[0])
    assert proposal is not None
    assert proposal["kind"] == ProposalKind.THRESHOLD_CHANGE.value
    assert "+push = 85" in proposal["diff"]

    outcome = apply_mod.apply_proposal(conn, cfg, created[0])
    assert outcome.startswith("✅")
    assert "push = 85" in (repo_copy / "config.toml").read_text(encoding="utf-8")


# ---------------------------------------------------------------- eval gate


def _id_extract(payload: dict[str, Any]) -> dict[str, Any]:
    from .mocks import default_extract

    extract = default_extract(payload)
    extract["what_you_get"] = str(payload["url"]).rsplit("/", 1)[-1]
    return extract


def _perfect_score(payload: dict[str, Any]) -> dict[str, Any]:
    what = str(payload["extract"]["what_you_get"])
    if what.startswith("gem-"):
        return {"score": 90, "reason": "gem"}
    if what.startswith("mid-"):
        return {"score": 65, "reason": "mid"}
    return {"score": 5, "reason": "junk"}


def _push_everything(payload: dict[str, Any]) -> dict[str, Any]:
    return {"score": 99, "reason": "everything is amazing"}


def _factory(cfg: Config, candidate_is_worse: bool) -> Any:
    """LLMFactory: the real prompts dir gets the perfect scorer; a candidate
    (staging) dir gets a deliberately worse one when requested."""

    def factory(prompts_dir: Path) -> FakeLLM:
        is_current = prompts_dir.resolve() == cfg.paths.prompts.resolve()
        if is_current or not candidate_is_worse:
            return FakeLLM(extract_fn=_id_extract, score_fn=_perfect_score)
        return FakeLLM(extract_fn=_id_extract, score_fn=_push_everything)

    return factory


def test_eval_gate_blocks_worse_candidate(cfg: Config, conn: sqlite3.Connection) -> None:
    """Acceptance: a deliberately-worse prompt candidate is blocked."""
    diff = score_prompt_append_diff(cfg, ["- (candidate change under test)"])
    gate = run_gate(cfg, diff, _factory(cfg, candidate_is_worse=True))
    assert gate.before == 1.0
    assert gate.after == pytest.approx(10 / 30)
    assert not gate.passed


def test_eval_gate_passes_equal_candidate(cfg: Config) -> None:
    diff = score_prompt_append_diff(cfg, ["- (harmless caution line)"])
    gate = run_gate(cfg, diff, _factory(cfg, candidate_is_worse=False))
    assert gate.passed and gate.before == gate.after == 1.0


def test_worse_prompt_proposal_never_reaches_pending(
    conn: sqlite3.Connection, cfg: Config, repo_copy: Path
) -> None:
    """The gate keeps regressing prompt diffs away from the operator."""
    ledger.sync_source(conn, "s", "rss", "u", "1h", SourceStatus.ACTIVE)
    item_ids = _seed_items(conn, "s", 3)
    for item_id in item_ids:
        ledger.set_extract(conn, item_id, {"category": "merch"})
        ledger.add_score(conn, item_id, 90, "r", "v", "m")
        claim_id = ledger.create_claim(conn, item_id)
        ledger.set_claim_state(conn, claim_id, ClaimState.SKIPPED)

    findings = [f for f in analyze(conn, cfg) if f.kind == "high_score_skipped"]
    assert findings
    created = propose_from_findings(
        conn,
        cfg,
        findings,
        repo_copy / "config.toml",
        llm_factory=_factory(cfg, candidate_is_worse=True),
    )
    assert created == []
    events = [r["kind"] for r in conn.execute("SELECT kind FROM events")]
    assert "proposal_gate_blocked" in events


def test_prompt_proposal_created_with_eval_numbers_and_applied(
    conn: sqlite3.Connection, cfg: Config, repo_copy: Path
) -> None:
    ledger.sync_source(conn, "s", "rss", "u", "1h", SourceStatus.ACTIVE)
    item_ids = _seed_items(conn, "s", 3)
    for item_id in item_ids:
        ledger.set_extract(conn, item_id, {"category": "merch"})
        ledger.add_score(conn, item_id, 90, "r", "v", "m")
        claim_id = ledger.create_claim(conn, item_id)
        ledger.set_claim_state(conn, claim_id, ClaimState.SKIPPED)

    findings = [f for f in analyze(conn, cfg) if f.kind == "high_score_skipped"]
    factory = _factory(cfg, candidate_is_worse=False)
    created = propose_from_findings(
        conn, cfg, findings, repo_copy / "config.toml", llm_factory=factory
    )
    assert len(created) == 1
    proposal = ledger.get_proposal(conn, created[0])
    assert proposal is not None
    assert proposal["eval_before"] == 1.0 and proposal["eval_after"] == 1.0

    outcome = apply_mod.apply_proposal(conn, cfg, created[0], llm_factory=factory)
    assert outcome.startswith("✅")
    score_md = (cfg.paths.prompts / "score.md").read_text(encoding="utf-8")
    assert "Operator recently skipped" in score_md
    assert not gitops.is_dirty(repo_copy)


def test_apply_reverts_on_post_write_regression(
    conn: sqlite3.Connection, cfg: Config, repo_copy: Path
) -> None:
    diff = score_prompt_append_diff(cfg, ["- (regressive line)"])
    proposal_id = ledger.add_proposal(
        conn, ProposalKind.PROMPT_DIFF, diff, "test", eval_before=1.0, eval_after=1.0
    )
    original = (cfg.paths.prompts / "score.md").read_text(encoding="utf-8")

    def always_bad(prompts_dir: Path) -> FakeLLM:
        return FakeLLM(extract_fn=_id_extract, score_fn=_push_everything)

    outcome = apply_mod.apply_proposal(conn, cfg, proposal_id, llm_factory=always_bad)
    assert "reverted" in outcome
    assert (cfg.paths.prompts / "score.md").read_text(encoding="utf-8") == original
    proposal = ledger.get_proposal(conn, proposal_id)
    assert proposal is not None and proposal["state"] == ProposalState.REJECTED.value


# ------------------------------------------------------------ reject & tree


def test_reject_proposal_records(conn: sqlite3.Connection, cfg: Config) -> None:
    proposal_id = ledger.add_proposal(conn, ProposalKind.SOURCE_CHANGE, "diff", "why")
    outcome = apply_mod.reject_proposal(conn, proposal_id)
    assert outcome.startswith("❌")
    proposal = ledger.get_proposal(conn, proposal_id)
    assert proposal is not None and proposal["state"] == ProposalState.REJECTED.value
    # double-tap is a no-op
    assert "already rejected" in apply_mod.reject_proposal(conn, proposal_id)


def test_critic_refuses_dirty_tree(conn: sqlite3.Connection, cfg: Config, repo_copy: Path) -> None:
    (repo_copy / "profile.md").write_text("dirty", encoding="utf-8")
    with pytest.raises(DirtyTreeError, match="dirty"):
        run_critic_once(cfg, repo_copy / "config.toml", conn=conn)


# ------------------------------------------------------------ auto-apply tier


def test_auto_tier_proposed_after_five_approvals_and_enables(
    conn: sqlite3.Connection, cfg: Config, repo_copy: Path
) -> None:
    for _ in range(5):
        pid = ledger.add_proposal(conn, ProposalKind.SOURCE_CHANGE, "d", "r")
        ledger.set_proposal_state(conn, pid, ProposalState.APPLIED)

    auto_id = maybe_propose_auto_tier(conn)
    assert auto_id is not None
    proposal = ledger.get_proposal(conn, auto_id)
    assert proposal is not None and apply_mod.AUTO_APPLY_MARKER in proposal["diff"]
    # not proposed twice while pending
    assert maybe_propose_auto_tier(conn) is None

    outcome = apply_mod.apply_proposal(conn, cfg, auto_id)
    assert "auto-apply" in outcome
    assert ledger.kv_get(conn, apply_mod.AUTO_APPLY_KV_KEY) == "true"
    # once enabled, no further tier proposals
    assert maybe_propose_auto_tier(conn) is None


def test_critic_run_auto_applies_source_changes_when_enabled(
    conn: sqlite3.Connection, cfg: Config, repo_copy: Path
) -> None:
    ledger.kv_set(conn, apply_mod.AUTO_APPLY_KV_KEY, "true")
    ledger.sync_source(conn, ACTIVE_SRC, "rss", "u", "2h", SourceStatus.ACTIVE)
    _seed_items(conn, ACTIVE_SRC, 35)

    created = run_critic_once(conn=conn, cfg=cfg, config_path=repo_copy / "config.toml")
    kill = [
        p
        for p in (ledger.get_proposal(conn, i) for i in created)
        if p is not None and p["kind"] == ProposalKind.SOURCE_CHANGE.value
    ]
    assert kill and kill[0]["state"] == ProposalState.APPLIED.value
    assert kill[0]["git_commit"]
    text = cfg.paths.sources.read_text(encoding="utf-8")
    assert "status: killed" in text.split(ACTIVE_SRC, 1)[1]


# ----------------------------------------------------------------- difftools


def test_apply_unified_diff_roundtrip() -> None:
    import difflib

    old = "line1\nline2\nline3\n"
    new = "line1\nline2 changed\nline3\nline4\n"
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="a/f.txt",
            tofile="b/f.txt",
        )
    )
    assert apply_unified_diff(diff, old) == new


def test_apply_unified_diff_rejects_stale_target() -> None:
    import difflib

    old = "alpha\nbeta\n"
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            "alpha\ngamma\n".splitlines(keepends=True),
            fromfile="a/f.txt",
            tofile="b/f.txt",
        )
    )
    with pytest.raises(DiffError):
        apply_unified_diff(diff, "totally different content\n")
