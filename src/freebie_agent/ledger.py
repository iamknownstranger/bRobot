"""All DB reads/writes for items, scores, claims, deadlines, sources, events,
proposals, outbox and questions. Every other module goes through here."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from freebie_agent.models import (
    ClaimState,
    DeadlineKind,
    DeadlineState,
    ItemStatus,
    ProposalKind,
    ProposalState,
    RawItem,
    SourceStatus,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def dedupe_hash(url: str, title: str) -> str:
    """Dedupe key: sha256 of normalized url + title."""
    norm_url = url.strip().lower().rstrip("/").split("#")[0]
    norm_title = " ".join(title.strip().lower().split())
    return hashlib.sha256(f"{norm_url}\n{norm_title}".encode()).hexdigest()


def _one(cur: sqlite3.Cursor) -> sqlite3.Row | None:
    row: sqlite3.Row | None = cur.fetchone()
    return row


# ---------------------------------------------------------------- items


def upsert_raw_item(conn: sqlite3.Connection, raw: RawItem) -> tuple[int, bool]:
    """Insert a fetched item, deduped by content hash.

    Returns (item_id, is_new). Re-seen items only refresh fetched_at.
    """
    h = dedupe_hash(raw.url, raw.title)
    now = iso(utcnow())
    row = conn.execute("SELECT id FROM items WHERE dedupe_hash = ?", (h,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE items SET fetched_at = ?, updated_at = ? WHERE id = ?",
            (now, now, row["id"]),
        )
        conn.commit()
        return int(row["id"]), False
    cur = conn.execute(
        "INSERT INTO items (source_id, url, dedupe_hash, raw_title, raw_body, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (raw.source_id, raw.url, h, raw.title, raw.body, now),
    )
    conn.commit()
    return int(cur.lastrowid or 0), True


def set_item_status(conn: sqlite3.Connection, item_id: int, status: ItemStatus) -> None:
    conn.execute(
        "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
        (status.value, iso(utcnow()), item_id),
    )
    conn.commit()


def set_extract(conn: sqlite3.Connection, item_id: int, extract: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE items SET extract_json = ?, status = ?, updated_at = ? WHERE id = ?",
        (
            json.dumps(extract, ensure_ascii=False),
            ItemStatus.EXTRACTED.value,
            iso(utcnow()),
            item_id,
        ),
    )
    conn.commit()


def set_extract_error(conn: sqlite3.Connection, item_id: int, error: str) -> None:
    conn.execute(
        "UPDATE items SET extract_error = ?, status = ?, updated_at = ? WHERE id = ?",
        (error[:500], ItemStatus.DROPPED.value, iso(utcnow()), item_id),
    )
    conn.commit()


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return _one(conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)))


def items_with_status(conn: sqlite3.Connection, status: ItemStatus) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM items WHERE status = ? ORDER BY id", (status.value,)))


# ---------------------------------------------------------------- scores


def add_score(
    conn: sqlite3.Connection,
    item_id: int,
    score: int,
    reason: str,
    prompt_version: str,
    model_tag: str,
) -> None:
    conn.execute(
        "INSERT INTO scores (item_id, score, reason, prompt_version, model_tag)"
        " VALUES (?, ?, ?, ?, ?)",
        (item_id, score, reason, prompt_version, model_tag),
    )
    conn.execute(
        "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
        (ItemStatus.SCORED.value, iso(utcnow()), item_id),
    )
    conn.commit()


def latest_score(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return _one(
        conn.execute("SELECT * FROM scores WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item_id,))
    )


# ---------------------------------------------------------------- claims

_CLAIM_TS_COLUMN = {
    ClaimState.PROPOSED: "proposed_at",
    ClaimState.APPROVED: "approved_at",
    ClaimState.SKIPPED: "skipped_at",
    ClaimState.CLAIMED: "claimed_at",
    ClaimState.ARRIVED: "arrived_at",
    ClaimState.FAILED: "failed_at",
}


def create_claim(conn: sqlite3.Connection, item_id: int) -> int:
    now = iso(utcnow())
    cur = conn.execute(
        "INSERT INTO claims (item_id, state, proposed_at) VALUES (?, 'proposed', ?)",
        (item_id, now),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def set_claim_state(
    conn: sqlite3.Connection,
    claim_id: int,
    state: ClaimState,
    operator_note: str | None = None,
) -> None:
    now = iso(utcnow())
    ts_col = _CLAIM_TS_COLUMN[state]
    conn.execute(
        f"UPDATE claims SET state = ?, {ts_col} = ?, updated_at = ?,"
        " operator_note = COALESCE(?, operator_note) WHERE id = ?",
        (state.value, now, now, operator_note, claim_id),
    )
    conn.commit()


def set_claim_worth_it(conn: sqlite3.Connection, claim_id: int, worth_it: bool) -> None:
    conn.execute(
        "UPDATE claims SET worth_it = ?, updated_at = ? WHERE id = ?",
        (1 if worth_it else 0, iso(utcnow()), claim_id),
    )
    conn.commit()


def get_claim(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
    return _one(conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)))


def claim_for_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return _one(
        conn.execute("SELECT * FROM claims WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item_id,))
    )


def claims_in_state(conn: sqlite3.Connection, state: ClaimState) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT c.*, i.raw_title, i.url FROM claims c JOIN items i ON i.id = c.item_id"
            " WHERE c.state = ? ORDER BY c.id",
            (state.value,),
        )
    )


def recent_claims(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT c.*, i.raw_title, i.url FROM claims c JOIN items i ON i.id = c.item_id"
            " ORDER BY c.updated_at DESC LIMIT ?",
            (limit,),
        )
    )


# ---------------------------------------------------------------- deadlines


def add_deadline(
    conn: sqlite3.Connection,
    kind: DeadlineKind,
    due_at: datetime,
    item_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO deadlines (item_id, kind, due_at, payload_json) VALUES (?, ?, ?, ?)",
        (item_id, kind.value, iso(due_at), json.dumps(payload or {}, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def open_deadlines(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM deadlines WHERE state IN ('pending','nagged') ORDER BY due_at")
    )


def record_deadline_nag(conn: sqlite3.Connection, deadline_id: int, hour_mark: int) -> None:
    row = conn.execute("SELECT nags_sent FROM deadlines WHERE id = ?", (deadline_id,)).fetchone()
    sent: list[int] = json.loads(row["nags_sent"]) if row else []
    if hour_mark not in sent:
        sent.append(hour_mark)
    conn.execute(
        "UPDATE deadlines SET nags_sent = ?, state = ?, updated_at = ? WHERE id = ?",
        (json.dumps(sent), DeadlineState.NAGGED.value, iso(utcnow()), deadline_id),
    )
    conn.commit()


def set_deadline_state(conn: sqlite3.Connection, deadline_id: int, state: DeadlineState) -> None:
    conn.execute(
        "UPDATE deadlines SET state = ?, updated_at = ? WHERE id = ?",
        (state.value, iso(utcnow()), deadline_id),
    )
    conn.commit()


# ---------------------------------------------------------------- sources


def sync_source(
    conn: sqlite3.Connection,
    source_id: str,
    type_: str,
    url: str,
    schedule: str,
    status: SourceStatus,
) -> None:
    """Mirror one sources.yaml entry into the sources table (yaml wins for
    config fields; runtime stats columns are preserved)."""
    now = iso(utcnow())
    shadow_since = now if status is SourceStatus.SHADOW else None
    conn.execute(
        "INSERT INTO sources (id, type, url, schedule, status, shadow_since)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET type=excluded.type, url=excluded.url,"
        " schedule=excluded.schedule, status=excluded.status,"
        " shadow_since=CASE WHEN excluded.status='shadow' AND sources.status!='shadow'"
        "   THEN excluded.shadow_since ELSE sources.shadow_since END,"
        " updated_at=?",
        (source_id, type_, url, schedule, status.value, shadow_since, now),
    )
    conn.commit()


def get_source(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return _one(conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)))


def record_source_ok(conn: sqlite3.Connection, source_id: str, items_seen: int) -> None:
    now = iso(utcnow())
    conn.execute(
        "UPDATE sources SET items_seen = items_seen + ?, last_ok_at = ?,"
        " last_error = NULL, updated_at = ? WHERE id = ?",
        (items_seen, now, now, source_id),
    )
    conn.commit()


def record_source_error(conn: sqlite3.Connection, source_id: str, error: str) -> None:
    conn.execute(
        "UPDATE sources SET last_error = ?, updated_at = ? WHERE id = ?",
        (error[:500], iso(utcnow()), source_id),
    )
    conn.commit()


def bump_source_counter(conn: sqlite3.Connection, source_id: str, column: str) -> None:
    if column not in ("items_queued", "items_claimed"):
        raise ValueError(f"not a source counter column: {column}")
    conn.execute(
        f"UPDATE sources SET {column} = {column} + 1, updated_at = ? WHERE id = ?",
        (iso(utcnow()), source_id),
    )
    conn.commit()


def set_source_status(conn: sqlite3.Connection, source_id: str, status: SourceStatus) -> None:
    now = iso(utcnow())
    conn.execute(
        "UPDATE sources SET status = ?, updated_at = ?,"
        " shadow_since = CASE WHEN ? = 'shadow' THEN ? ELSE shadow_since END WHERE id = ?",
        (status.value, now, status.value, now, source_id),
    )
    conn.commit()


def set_source_page_hash(conn: sqlite3.Connection, source_id: str, page_hash: str) -> None:
    conn.execute(
        "UPDATE sources SET last_page_hash = ?, updated_at = ? WHERE id = ?",
        (page_hash, iso(utcnow()), source_id),
    )
    conn.commit()


def all_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM sources ORDER BY id"))


# ---------------------------------------------------------------- events & counters


def add_event(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict[str, Any] | None = None,
    item_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (kind, item_id, payload) VALUES (?, ?, ?)",
        (kind, item_id, json.dumps(payload or {}, ensure_ascii=False)),
    )
    conn.commit()


def bump_counter(conn: sqlite3.Connection, name: str, n: int = 1, **extra: Any) -> None:
    """Pipeline stage counters land in events as kind='counter'."""
    add_event(conn, "counter", {"name": name, "n": n, **extra})


def counters_since(conn: sqlite3.Connection, since: datetime) -> dict[str, int]:
    rows = conn.execute(
        "SELECT payload FROM events WHERE kind = 'counter' AND created_at >= ?",
        (iso(since),),
    )
    totals: dict[str, int] = {}
    for row in rows:
        payload = json.loads(row["payload"])
        name = str(payload.get("name", "unknown"))
        totals[name] = totals.get(name, 0) + int(payload.get("n", 1))
    return totals


def llm_stats_since(conn: sqlite3.Connection, since: datetime) -> dict[str, float]:
    rows = list(
        conn.execute(
            "SELECT payload FROM events WHERE kind = 'llm_call' AND created_at >= ?",
            (iso(since),),
        )
    )
    if not rows:
        return {"llm_calls": 0, "llm_failures": 0, "llm_latency_ms_avg": 0.0}
    calls = 0
    failures = 0
    latency_total = 0.0
    for row in rows:
        payload = json.loads(row["payload"])
        calls += 1
        if not payload.get("ok", True):
            failures += 1
        latency_total += float(payload.get("ms", 0.0))
    return {
        "llm_calls": calls,
        "llm_failures": failures,
        "llm_latency_ms_avg": round(latency_total / calls, 1),
    }


def events_since(conn: sqlite3.Connection, since: datetime) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM events WHERE created_at >= ? ORDER BY id", (iso(since),))
    )


def pushes_sent_today(conn: sqlite3.Connection) -> int:
    day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'push_sent' AND created_at >= ?",
        (iso(day_start),),
    ).fetchone()
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------- outbox


def enqueue_outbox(conn: sqlite3.Connection, kind: str, payload: dict[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO outbox (kind, payload) VALUES (?, ?)",
        (kind, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def unsent_outbox(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT ?", (limit,))
    )


def mark_outbox_sent(conn: sqlite3.Connection, outbox_id: int) -> None:
    conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (iso(utcnow()), outbox_id))
    conn.commit()


# ---------------------------------------------------------------- questions


def ask_question(
    conn: sqlite3.Connection,
    topic: str,
    payload: dict[str, Any] | None = None,
    item_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO questions (topic, item_id, payload) VALUES (?, ?, ?)",
        (topic, item_id, json.dumps(payload or {}, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def resolve_question(conn: sqlite3.Connection, question_id: int, state: str) -> None:
    if state not in ("answered", "no_answer"):
        raise ValueError(f"invalid question resolution: {state}")
    conn.execute(
        "UPDATE questions SET state = ?, resolved_at = ? WHERE id = ?",
        (state, iso(utcnow()), question_id),
    )
    conn.commit()


def expire_stale_questions(conn: sqlite3.Connection, ttl_hours: int) -> list[int]:
    """Questions open past their TTL become no_answer (proceed conservatively)."""
    cutoff = iso(utcnow() - timedelta(hours=ttl_hours))
    rows = list(
        conn.execute("SELECT id FROM questions WHERE state = 'open' AND asked_at < ?", (cutoff,))
    )
    for row in rows:
        resolve_question(conn, int(row["id"]), "no_answer")
    return [int(r["id"]) for r in rows]


# ---------------------------------------------------------------- proposals


def add_proposal(
    conn: sqlite3.Connection,
    kind: ProposalKind,
    diff: str,
    rationale: str,
    eval_before: float | None = None,
    eval_after: float | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO proposals (kind, diff, rationale, eval_before, eval_after)"
        " VALUES (?, ?, ?, ?, ?)",
        (kind.value, diff, rationale, eval_before, eval_after),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row | None:
    return _one(conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)))


def proposals_in_state(conn: sqlite3.Connection, state: ProposalState) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM proposals WHERE state = ? ORDER BY id", (state.value,)))


def set_proposal_state(
    conn: sqlite3.Connection,
    proposal_id: int,
    state: ProposalState,
    git_commit: str | None = None,
    rationale_suffix: str | None = None,
) -> None:
    conn.execute(
        "UPDATE proposals SET state = ?, git_commit = COALESCE(?, git_commit),"
        " rationale = rationale || COALESCE(?, ''), updated_at = ? WHERE id = ?",
        (state.value, git_commit, rationale_suffix, iso(utcnow()), proposal_id),
    )
    conn.commit()


def approved_proposal_count(conn: sqlite3.Connection, kind: ProposalKind) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM proposals WHERE kind = ? AND state = 'applied'",
        (kind.value,),
    ).fetchone()
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------- kv state


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, iso(utcnow())),
    )
    conn.commit()


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = _one(conn.execute("SELECT value FROM kv WHERE key = ?", (key,)))
    return str(row["value"]) if row else None


def set_paused_until(conn: sqlite3.Connection, until: datetime | None) -> None:
    kv_set(conn, "paused_until", iso(until) if until else "")


def paused_until(conn: sqlite3.Connection) -> datetime | None:
    value = kv_get(conn, "paused_until")
    if not value:
        return None
    until = parse_iso(value)
    return until if until > utcnow() else None


def muted_categories(conn: sqlite3.Connection) -> set[str]:
    value = kv_get(conn, "muted_categories")
    return set(json.loads(value)) if value else set()


def mute_category(conn: sqlite3.Connection, category: str) -> set[str]:
    cats = muted_categories(conn)
    cats.add(category.strip().lower())
    kv_set(conn, "muted_categories", json.dumps(sorted(cats)))
    return cats


def unmute_category(conn: sqlite3.Connection, category: str) -> set[str]:
    cats = muted_categories(conn)
    cats.discard(category.strip().lower())
    kv_set(conn, "muted_categories", json.dumps(sorted(cats)))
    return cats
