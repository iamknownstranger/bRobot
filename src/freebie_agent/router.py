"""Routing: score -> push / digest / drop, with the daily push budget and
shadow-source suppression applied."""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from freebie_agent import ledger
from freebie_agent.config import Thresholds
from freebie_agent.models import SourceStatus


class Bucket(StrEnum):
    PUSH = "push"
    DIGEST = "digest"
    DROP = "drop"


def bucket_for_score(score: int, thresholds: Thresholds) -> Bucket:
    """Pure threshold mapping — this is what the eval set measures."""
    if score >= thresholds.push:
        return Bucket.PUSH
    if score >= thresholds.digest:
        return Bucket.DIGEST
    return Bucket.DROP


def effective_bucket(
    conn: sqlite3.Connection,
    source_id: str,
    score: int,
    thresholds: Thresholds,
    pushes_per_day: int,
    category: str | None = None,
) -> tuple[Bucket, str | None]:
    """Bucket after operational rules. Returns (bucket, demotion_reason).

    - Shadow sources are scored and logged but never notified: their pushes
      and digests are recorded as drop-with-reason.
    - Muted categories (/mute) are dropped outright.
    - While paused (/pause), pushes demote to the digest.
    - The daily real-time push budget demotes overflow pushes to the digest
      (deadline nags are exempt — they don't go through this router).
    """
    bucket = bucket_for_score(score, thresholds)
    source = ledger.get_source(conn, source_id)
    if source is not None and source["status"] == SourceStatus.SHADOW.value:
        if bucket is not Bucket.DROP:
            return Bucket.DROP, "shadow_source"
        return bucket, None
    if category and category.strip().lower() in ledger.muted_categories(conn):
        if bucket is not Bucket.DROP:
            return Bucket.DROP, "muted_category"
        return bucket, None
    if bucket is Bucket.PUSH:
        if ledger.paused_until(conn) is not None:
            return Bucket.DIGEST, "paused"
        pending = len([r for r in ledger.unsent_outbox(conn, limit=100) if r["kind"] == "notify"])
        if ledger.pushes_sent_today(conn) + pending >= pushes_per_day:
            return Bucket.DIGEST, "push_budget_exhausted"
    return bucket, None
