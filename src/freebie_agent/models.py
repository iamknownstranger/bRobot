"""Dataclasses shared across the pipeline, bot, and critic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ItemStatus(StrEnum):
    NEW = "new"
    EXTRACTED = "extracted"
    SCORED = "scored"
    QUEUED = "queued"
    DIGESTED = "digested"
    DROPPED = "dropped"
    CLAIMED = "claimed"
    EXPIRED = "expired"


class ClaimState(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    SKIPPED = "skipped"
    CLAIMED = "claimed"
    ARRIVED = "arrived"
    FAILED = "failed"


class DeadlineKind(StrEnum):
    TRIAL_CANCEL = "trial_cancel"
    COUPON_EXPIRY = "coupon_expiry"
    POINTS_EXPIRY = "points_expiry"
    SHIPPING_CHECK = "shipping_check"
    CUSTOM = "custom"


class DeadlineState(StrEnum):
    PENDING = "pending"
    NAGGED = "nagged"
    DONE = "done"
    MISSED = "missed"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    SHADOW = "shadow"
    PAUSED = "paused"
    KILLED = "killed"


class ProposalKind(StrEnum):
    PROMPT_DIFF = "prompt_diff"
    PROFILE_DIFF = "profile_diff"
    SOURCE_CHANGE = "source_change"
    THRESHOLD_CHANGE = "threshold_change"


class ProposalState(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Intent(StrEnum):
    """Fixed enum for free-text classification; anything else asks back."""

    UPDATE_PROFILE = "update_profile"
    QUERY_LEDGER = "query_ledger"
    ADJUST_FILTER = "adjust_filter"
    EXPLAIN_DECISION = "explain_decision"
    PAUSE = "pause"
    CLAIM_STATUS = "claim_status"
    ADD_SOURCE = "add_source"
    CHITCHAT = "chitchat"


@dataclass(frozen=True)
class RawItem:
    """What every fetcher returns."""

    url: str
    title: str
    body: str
    source_id: str


@dataclass(frozen=True)
class Item:
    id: int
    source_id: str
    url: str
    dedupe_hash: str
    raw_title: str
    raw_body: str
    fetched_at: str
    extract_json: str | None
    extract_error: str | None
    status: ItemStatus


@dataclass(frozen=True)
class Score:
    item_id: int
    score: int
    reason: str
    prompt_version: str
    model_tag: str


@dataclass(frozen=True)
class ClaimTask:
    id: int
    item_id: int
    state: ClaimState
    operator_note: str | None = None
    worth_it: bool | None = None


@dataclass(frozen=True)
class Deadline:
    id: int
    item_id: int | None
    kind: DeadlineKind
    due_at: datetime
    state: DeadlineState
    nags_sent: tuple[int, ...] = ()
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Proposal:
    id: int
    kind: ProposalKind
    diff: str
    rationale: str
    eval_before: float | None
    eval_after: float | None
    state: ProposalState
    git_commit: str | None = None
