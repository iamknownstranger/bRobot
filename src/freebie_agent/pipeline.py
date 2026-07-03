"""Pipeline orchestration: dedupe -> extract -> score -> route.

Each stage is a separate function so tests can drive them with fixture data
and a mocked LLM. Every stage emits counters into the events table.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from freebie_agent import ledger
from freebie_agent.config import Config
from freebie_agent.fetchers.base import Fetcher
from freebie_agent.llm import LLMError, LLMFormatError, OllamaClient, prompts_version
from freebie_agent.models import ItemStatus
from freebie_agent.router import Bucket, effective_bucket

log = logging.getLogger("pipeline")

EXTRACT_KEYS = frozenset(
    {
        "what_you_get",
        "estimated_value_inr",
        "effort_minutes",
        "deadline_iso",
        "requirements",
        "region",
        "category",
        "red_flags",
        "is_lead_gen_trap",
    }
)

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "what_you_get": {"type": "string"},
        "estimated_value_inr": {"type": ["integer", "null"]},
        "effort_minutes": {"type": ["integer", "null"]},
        "deadline_iso": {"type": ["string", "null"]},
        "requirements": {"type": "array", "items": {"type": "string"}},
        "region": {"type": ["string", "null"]},
        "category": {"type": "string"},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "is_lead_gen_trap": {"type": "boolean"},
    },
    "required": sorted(EXTRACT_KEYS),
}

SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
}


def _llm_call(
    conn: sqlite3.Connection,
    llm: OllamaClient,
    prompt_name: str,
    payload: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    """chat_json + the llm_call event (latency, ok/failed) in one place."""
    try:
        result = llm.chat_json(prompt_name, payload, schema=schema)
    except LLMError:
        ledger.add_event(
            conn,
            "llm_call",
            {"prompt": prompt_name, "ms": llm.last_latency_ms, "ok": False},
        )
        raise
    ledger.add_event(
        conn,
        "llm_call",
        {"prompt": prompt_name, "ms": llm.last_latency_ms, "ok": True},
    )
    return result


# ------------------------------------------------------------------ fetch


def fetch_source(conn: sqlite3.Connection, fetcher: Fetcher) -> int:
    """Fetch one source in isolation: failures are recorded on the sources
    row and never propagate to other sources."""
    try:
        raw_items = fetcher.fetch(conn)
    except Exception as exc:  # noqa: BLE001 - isolation boundary by design
        log.warning(
            "source fetch failed",
            extra={"ctx": {"source": fetcher.source_id, "error": str(exc)[:200]}},
        )
        ledger.record_source_error(conn, fetcher.source_id, str(exc))
        return 0
    new_count = 0
    for raw in raw_items:
        _, is_new = ledger.upsert_raw_item(conn, raw)
        if is_new:
            new_count += 1
    ledger.record_source_ok(conn, fetcher.source_id, items_seen=new_count)
    ledger.bump_counter(conn, "items_fetched", new_count, source=fetcher.source_id)
    return new_count


# ------------------------------------------------------------------ extract


def validate_extract(extract: dict[str, Any]) -> dict[str, Any]:
    missing = EXTRACT_KEYS - extract.keys()
    if missing:
        raise LLMFormatError(f"extract missing keys: {sorted(missing)}")
    if not isinstance(extract["is_lead_gen_trap"], bool):
        raise LLMFormatError("extract.is_lead_gen_trap must be boolean")
    if not isinstance(extract["requirements"], list):
        raise LLMFormatError("extract.requirements must be a list")
    return extract


def extract_stage(conn: sqlite3.Connection, llm: OllamaClient) -> int:
    """LLM-extract every item in status new. Bad JSON drops the item with an
    extract_error — never the batch."""
    extracted = 0
    for item in ledger.items_with_status(conn, ItemStatus.NEW):
        payload = {
            "title": item["raw_title"],
            "body": item["raw_body"],
            "url": item["url"],
            "source": item["source_id"],
        }
        try:
            extract = validate_extract(_llm_call(conn, llm, "extract", payload, EXTRACT_SCHEMA))
        except LLMError as exc:
            ledger.set_extract_error(conn, int(item["id"]), str(exc))
            ledger.bump_counter(conn, "extract_failures")
            ledger.bump_counter(conn, "items_dropped")
            continue
        ledger.set_extract(conn, int(item["id"]), extract)
        extracted += 1
    if extracted:
        ledger.bump_counter(conn, "items_extracted", extracted)
    return extracted


# ------------------------------------------------------------------ score


def clamp_score(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise LLMFormatError(f"score must be a number, got {type(value).__name__}")
    return max(0, min(100, int(value)))


def score_stage(conn: sqlite3.Connection, cfg: Config, llm: OllamaClient, profile_text: str) -> int:
    """Score every extracted item against the profile, then route it."""
    version = prompts_version(cfg.paths.prompts)
    scored = 0
    for item in ledger.items_with_status(conn, ItemStatus.EXTRACTED):
        extract = json.loads(item["extract_json"])
        payload = {"extract": extract, "profile": profile_text}
        try:
            result = _llm_call(conn, llm, "score", payload, SCORE_SCHEMA)
            score = clamp_score(result.get("score"))
            reason = str(result.get("reason", ""))[:500]
        except LLMError as exc:
            ledger.set_extract_error(conn, int(item["id"]), f"score: {exc}")
            ledger.bump_counter(conn, "score_failures")
            ledger.bump_counter(conn, "items_dropped")
            continue
        ledger.add_score(conn, int(item["id"]), score, reason, version, llm.model)
        scored += 1
        route_item(
            conn,
            cfg,
            int(item["id"]),
            item["source_id"],
            score,
            reason,
            category=str(extract.get("category", "")) or None,
        )
    if scored:
        ledger.bump_counter(conn, "items_scored", scored)
    return scored


# ------------------------------------------------------------------ route


def route_item(
    conn: sqlite3.Connection,
    cfg: Config,
    item_id: int,
    source_id: str,
    score: int,
    reason: str,
    category: str | None = None,
) -> Bucket:
    bucket, demotion = effective_bucket(
        conn, source_id, score, cfg.thresholds, cfg.pushes_per_day, category=category
    )
    if demotion:
        ledger.add_event(
            conn, "route_demoted", {"reason": demotion, "score": score}, item_id=item_id
        )
    if bucket is Bucket.PUSH:
        claim_id = ledger.create_claim(conn, item_id)
        ledger.set_item_status(conn, item_id, ItemStatus.QUEUED)
        ledger.bump_source_counter(conn, source_id, "items_queued")
        ledger.enqueue_outbox(conn, "notify", {"item_id": item_id, "claim_id": claim_id})
        ledger.bump_counter(conn, "items_queued")
    elif bucket is Bucket.DIGEST:
        ledger.set_item_status(conn, item_id, ItemStatus.DIGESTED)
        ledger.bump_counter(conn, "items_digested")
    else:
        ledger.set_item_status(conn, item_id, ItemStatus.DROPPED)
        ledger.bump_counter(conn, "items_dropped")
    ledger.add_event(
        conn,
        "routed",
        {"bucket": bucket.value, "score": score, "reason": reason},
        item_id=item_id,
    )
    return bucket


# ------------------------------------------------------------------ digest


def build_digest(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any] | None:
    """Collect digested items not yet included in a digest, top N by score,
    plus open batched questions; enqueue one digest outbox message."""
    rows = list(
        conn.execute(
            """
            SELECT i.id, i.raw_title, i.url, s.score, s.reason
            FROM items i JOIN scores s ON s.item_id = i.id
            WHERE i.status = 'digested'
              AND NOT EXISTS (
                SELECT 1 FROM events e
                WHERE e.kind = 'digest_included' AND e.item_id = i.id)
            ORDER BY s.score DESC
            LIMIT ?
            """,
            (cfg.digest_top_n,),
        )
    )
    questions = list(conn.execute("SELECT id, topic, payload FROM questions WHERE state = 'open'"))
    if not rows and not questions:
        return None
    payload = {
        "items": [
            {
                "item_id": int(r["id"]),
                "title": r["raw_title"],
                "url": r["url"],
                "score": int(r["score"]),
                "reason": r["reason"],
            }
            for r in rows
        ],
        "questions": [
            {"id": int(q["id"]), "topic": q["topic"], "payload": json.loads(q["payload"])}
            for q in questions
        ],
    }
    for r in rows:
        ledger.add_event(conn, "digest_included", item_id=int(r["id"]))
    ledger.enqueue_outbox(conn, "digest", payload)
    return payload


# ------------------------------------------------------------------ full pass


def run_pass(
    conn: sqlite3.Connection,
    cfg: Config,
    llm: OllamaClient,
    fetchers: list[Fetcher],
) -> dict[str, int]:
    """One full fetch->extract->score->route pass over the given fetchers."""
    profile_text = cfg.paths.profile.read_text(encoding="utf-8")
    fetched = sum(fetch_source(conn, f) for f in fetchers)
    extracted = extract_stage(conn, llm)
    scored = score_stage(conn, cfg, llm, profile_text)
    return {"fetched": fetched, "extracted": extracted, "scored": scored}
