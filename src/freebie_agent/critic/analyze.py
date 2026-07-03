"""Ledger -> findings. Deterministic SQL + heuristics only (the LLM is used
at most to word rationales, never to decide — see critic/propose.py)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from freebie_agent import ledger
from freebie_agent.config import Config

# Heuristic knobs. Deliberately conservative; the operator approves anyway.
KILL_MIN_ITEMS = 30  # a source must have produced this much noise...
KILL_MAX_CLAIMS = 0  # ...with zero claims to be proposed for killing
PROMOTE_MIN_WOULD_PUSH = 3  # shadow items that would have pushed
SKIP_PATTERN_MIN = 3  # high-score-but-skipped items before we react
WORTH_IT_NEG_MIN = 2  # 👎 feedbacks before we react
PUSH_SKIP_RATE_TRIGGER = 0.5  # >50% of pushes skipped => threshold proposal
THRESHOLD_STEP = 5


@dataclass(frozen=True)
class Finding:
    kind: str  # source_kill | source_promote | high_score_skipped |
    #            worth_it_negative | threshold_tune
    data: dict[str, object] = field(default_factory=dict)


def _window_cutoff(cfg: Config) -> str:
    return ledger.iso(ledger.utcnow() - timedelta(weeks=cfg.critic_lookback_weeks))


def source_findings(conn: sqlite3.Connection, cfg: Config) -> list[Finding]:
    """Kill noisy zero-yield sources; promote shadow sources that earned it."""
    cutoff = _window_cutoff(cfg)
    findings: list[Finding] = []
    for src in ledger.all_sources(conn):
        sid = str(src["id"])
        window = conn.execute(
            """
            SELECT
              COUNT(*) AS seen,
              SUM(CASE WHEN i.status IN ('queued','claimed') THEN 1 ELSE 0 END) AS queued,
              (SELECT COUNT(*) FROM claims c JOIN items i2 ON i2.id = c.item_id
               WHERE i2.source_id = ? AND c.state IN ('claimed','arrived')
                 AND c.created_at >= ?) AS claimed
            FROM items i WHERE i.source_id = ? AND i.created_at >= ?
            """,
            (sid, cutoff, sid, cutoff),
        ).fetchone()
        seen = int(window["seen"] or 0)
        queued = int(window["queued"] or 0)
        claimed = int(window["claimed"] or 0)

        if src["status"] == "active" and seen >= KILL_MIN_ITEMS and claimed <= KILL_MAX_CLAIMS:
            findings.append(
                Finding(
                    "source_kill",
                    {
                        "source_id": sid,
                        "items_seen": seen,
                        "items_queued": queued,
                        "items_claimed": claimed,
                        "weeks": cfg.critic_lookback_weeks,
                    },
                )
            )
        elif src["status"] == "shadow" and src["shadow_since"] is not None:
            days_shadow = (ledger.utcnow() - ledger.parse_iso(src["shadow_since"])).days
            if days_shadow < cfg.critic_shadow_days:
                continue
            would_push = conn.execute(
                """
                SELECT COUNT(*) AS n FROM events e JOIN items i ON i.id = e.item_id
                WHERE e.kind = 'route_demoted' AND i.source_id = ?
                  AND e.created_at >= ?
                  AND json_extract(e.payload, '$.reason') = 'shadow_source'
                  AND json_extract(e.payload, '$.score') >= ?
                """,
                (sid, cutoff, cfg.thresholds.push),
            ).fetchone()
            if int(would_push["n"]) >= PROMOTE_MIN_WOULD_PUSH:
                findings.append(
                    Finding(
                        "source_promote",
                        {
                            "source_id": sid,
                            "would_push": int(would_push["n"]),
                            "days_shadow": days_shadow,
                            "items_seen": seen,
                        },
                    )
                )
    return findings


def scoring_findings(conn: sqlite3.Connection, cfg: Config) -> list[Finding]:
    """Disagreements between the scorer and the operator."""
    cutoff = _window_cutoff(cfg)
    findings: list[Finding] = []

    skipped = list(
        conn.execute(
            """
            SELECT i.raw_title, s.score, json_extract(i.extract_json, '$.category') AS cat
            FROM claims c
            JOIN items i ON i.id = c.item_id
            JOIN scores s ON s.item_id = i.id
            WHERE c.state = 'skipped' AND c.created_at >= ? AND s.score >= ?
            ORDER BY s.score DESC
            """,
            (cutoff, cfg.thresholds.push),
        )
    )
    if len(skipped) >= SKIP_PATTERN_MIN:
        findings.append(
            Finding(
                "high_score_skipped",
                {
                    "count": len(skipped),
                    "examples": [str(r["raw_title"])[:70] for r in skipped[:5]],
                    "categories": sorted({str(r["cat"]) for r in skipped if r["cat"]}),
                },
            )
        )

    negatives = list(
        conn.execute(
            """
            SELECT i.raw_title, json_extract(i.extract_json, '$.category') AS cat
            FROM claims c
            JOIN items i ON i.id = c.item_id
            WHERE c.worth_it = 0 AND c.updated_at >= ?
            """,
            (cutoff,),
        )
    )
    if len(negatives) >= WORTH_IT_NEG_MIN:
        findings.append(
            Finding(
                "worth_it_negative",
                {
                    "count": len(negatives),
                    "examples": [str(r["raw_title"])[:70] for r in negatives[:5]],
                    "categories": sorted({str(r["cat"]) for r in negatives if r["cat"]}),
                },
            )
        )
    return findings


def threshold_findings(conn: sqlite3.Connection, cfg: Config) -> list[Finding]:
    """If most pushes get skipped, the push threshold is too permissive."""
    cutoff = _window_cutoff(cfg)
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN state = 'skipped' THEN 1 ELSE 0 END) AS skipped,
          COUNT(*) AS total
        FROM claims WHERE created_at >= ?
        """,
        (cutoff,),
    ).fetchone()
    total = int(row["total"] or 0)
    skipped = int(row["skipped"] or 0)
    if total >= 10 and skipped / total > PUSH_SKIP_RATE_TRIGGER:
        proposed = min(95, cfg.thresholds.push + THRESHOLD_STEP)
        if proposed != cfg.thresholds.push:
            return [
                Finding(
                    "threshold_tune",
                    {
                        "pushes": total,
                        "skipped": skipped,
                        "skip_rate": round(skipped / total, 2),
                        "current_push": cfg.thresholds.push,
                        "proposed_push": proposed,
                    },
                )
            ]
    return []


def analyze(conn: sqlite3.Connection, cfg: Config) -> list[Finding]:
    findings = (
        source_findings(conn, cfg) + scoring_findings(conn, cfg) + threshold_findings(conn, cfg)
    )
    ledger.add_event(
        conn,
        "critic_analyzed",
        {
            "findings": [f.kind for f in findings],
            "raw": json.dumps([f.data for f in findings])[:2000],
        },
    )
    return findings
