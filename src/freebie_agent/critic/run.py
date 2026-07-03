"""freebie-critic entrypoint: weekly self-improvement analysis.

Long-running by default (APScheduler cron from [critic] config); `--once`
runs a single pass for on-demand use. Refuses to run with a dirty git tree —
every self-modification must be attributable to a commit.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from freebie_agent import db as dbmod
from freebie_agent import gitops, ledger
from freebie_agent.config import Config, load_or_exit
from freebie_agent.critic import apply as apply_mod
from freebie_agent.critic.analyze import analyze
from freebie_agent.critic.evalgate import LLMFactory, default_llm_factory
from freebie_agent.critic.propose import propose_from_findings
from freebie_agent.logs import register_secret, setup_logging
from freebie_agent.models import ProposalKind, ProposalState

log = logging.getLogger("critic")

PROPOSAL_TTL_DAYS = 14
AUTO_TIER_MIN_APPROVALS = 5


class DirtyTreeError(Exception):
    pass


def expire_stale_proposals(conn: sqlite3.Connection) -> int:
    cutoff = ledger.iso(ledger.utcnow() - timedelta(days=PROPOSAL_TTL_DAYS))
    rows = list(
        conn.execute(
            "SELECT id FROM proposals WHERE state = 'pending' AND created_at < ?", (cutoff,)
        )
    )
    for row in rows:
        ledger.set_proposal_state(conn, int(row["id"]), ProposalState.EXPIRED)
    return len(rows)


def maybe_propose_auto_tier(conn: sqlite3.Connection) -> int | None:
    """Once the operator has approved >= 5 source changes, propose making
    that kind auto-apply. The tier promotion is itself a proposal."""
    if ledger.kv_get(conn, apply_mod.AUTO_APPLY_KV_KEY) == "true":
        return None
    approvals = ledger.approved_proposal_count(conn, ProposalKind.SOURCE_CHANGE)
    if approvals < AUTO_TIER_MIN_APPROVALS:
        return None
    pending = ledger.proposals_in_state(conn, ProposalState.PENDING)
    if any(apply_mod.AUTO_APPLY_MARKER in str(p["diff"]) for p in pending):
        return None
    proposal_id = ledger.add_proposal(
        conn,
        ProposalKind.SOURCE_CHANGE,
        f"{apply_mod.AUTO_APPLY_MARKER}: behavioral change, no file diff.\n"
        "Enable auto-apply for source pruning/promotion proposals "
        "(they will still be created, committed and notified).",
        f"You have approved {approvals} source-change proposals so far; "
        "auto-applying this kind removes a manual step with a track record "
        "behind it.",
    )
    ledger.enqueue_outbox(conn, "proposal", {"proposal_id": proposal_id})
    return proposal_id


def run_critic_once(
    cfg: Config,
    config_path: Path,
    llm_factory: LLMFactory | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[int]:
    """One full critic pass. Returns created proposal ids."""
    repo_root = cfg.paths.profile.resolve().parent
    if not gitops.is_repo(repo_root):
        raise DirtyTreeError(f"{repo_root} is not a git repository")
    if gitops.is_dirty(repo_root):
        raise DirtyTreeError(
            "working tree is dirty — commit or stash before running the critic "
            "(every self-modification must map to a clean commit)"
        )

    own_conn = conn is None
    conn = conn or dbmod.connect(cfg.paths.db)
    try:
        dbmod.assert_migrated(conn, dbmod.default_migrations_dir())
        expired = expire_stale_proposals(conn)
        if expired:
            log.info("expired stale proposals", extra={"ctx": {"n": expired}})

        findings = analyze(conn, cfg)
        gate_factory = llm_factory
        if gate_factory is None and any(
            f.kind in ("high_score_skipped", "worth_it_negative") for f in findings
        ):
            gate_factory = default_llm_factory(cfg)
        created = propose_from_findings(conn, cfg, findings, config_path, gate_factory)

        # Auto-apply tier for source changes, if the operator enabled it.
        if ledger.kv_get(conn, apply_mod.AUTO_APPLY_KV_KEY) == "true":
            for proposal_id in created:
                row = ledger.get_proposal(conn, proposal_id)
                if row is not None and row["kind"] == ProposalKind.SOURCE_CHANGE.value:
                    outcome = apply_mod.apply_proposal(conn, cfg, proposal_id, llm_factory)
                    ledger.enqueue_outbox(conn, "text", {"text": f"(auto-applied) {outcome}"})

        auto_tier = maybe_propose_auto_tier(conn)
        if auto_tier is not None:
            created.append(auto_tier)

        ledger.add_event(conn, "critic_run", {"proposals": created})
        log.info("critic pass complete", extra={"ctx": {"proposals": created}})
        return created
    finally:
        if own_conn:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="freebie-critic")
    parser.add_argument("--config", default="config.toml", type=Path)
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    args = parser.parse_args(argv)

    cfg = load_or_exit(args.config)
    register_secret(cfg.telegram_bot_token)
    setup_logging(cfg.paths.logs, "critic")

    def run_pass() -> None:
        try:
            run_critic_once(cfg, args.config)
        except DirtyTreeError as exc:
            log.error("critic refused to run", extra={"ctx": {"reason": str(exc)}})
            raise

    if args.once:
        try:
            run_pass()
        except (DirtyTreeError, dbmod.PendingMigrationsError) as exc:
            print(f"fatal: {exc}")
            return 3
        return 0

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_pass,
        "cron",
        day_of_week=cfg.critic_day_of_week,
        hour=cfg.critic_hour,
        id="critic-weekly",
    )
    log.info(
        "critic scheduler starting",
        extra={"ctx": {"cron": f"{cfg.critic_day_of_week}@{cfg.critic_hour:02d}:00 UTC"}},
    )
    scheduler.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
