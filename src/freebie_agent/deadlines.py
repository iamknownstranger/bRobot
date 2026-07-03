"""Deadline engine: scan open deadlines and emit nags at the configured
hour-marks before due (default T-72h, T-24h, T-2h).

Nags bypass the daily push budget by design (they protect the operator from
paid trial rollovers). Each hour-mark fires at most once per deadline; past-due
pending deadlines flip to missed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from freebie_agent import ledger
from freebie_agent.config import Config
from freebie_agent.models import DeadlineKind, DeadlineState


def due_nag_mark(
    due_at: datetime,
    now: datetime,
    nag_hours: tuple[int, ...],
    already_sent: tuple[int, ...],
) -> int | None:
    """The most urgent unsent hour-mark whose window has started, or None.

    nag_hours must be sorted descending (config guarantees it). Only the most
    urgent applicable mark fires per scan, so a deadline created inside the
    72h window gets one nag, not three.
    """
    if now >= due_at:
        return None
    remaining_hours = (due_at - now).total_seconds() / 3600
    applicable = [h for h in nag_hours if remaining_hours <= h]
    if not applicable:
        return None
    mark = min(applicable)
    if mark in already_sent:
        return None
    return mark


def deadline_scan(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None) -> int:
    """One scan pass. Returns the number of nags enqueued."""
    now = now or ledger.utcnow()
    nags = 0
    for row in ledger.open_deadlines(conn):
        due_at = ledger.parse_iso(row["due_at"])
        sent = tuple(json.loads(row["nags_sent"]))
        if row["kind"] == DeadlineKind.SHIPPING_CHECK.value:
            # No pre-nags: at due time this becomes the worth-it question
            # (👍/👎), which writes claims.worth_it — the key training signal.
            if now >= due_at:
                payload = json.loads(row["payload_json"])
                ledger.enqueue_outbox(conn, "worth_it", payload)
                ledger.set_deadline_state(conn, int(row["id"]), DeadlineState.DONE)
                ledger.add_event(
                    conn,
                    "worth_it_question_sent",
                    {"claim_id": payload.get("claim_id")},
                    item_id=row["item_id"],
                )
                nags += 1
            continue
        if now >= due_at:
            ledger.set_deadline_state(conn, int(row["id"]), DeadlineState.MISSED)
            ledger.add_event(
                conn,
                "deadline_missed",
                {"deadline_id": int(row["id"]), "kind": row["kind"]},
                item_id=row["item_id"],
            )
            continue
        mark = due_nag_mark(due_at, now, cfg.nag_hours, sent)
        if mark is None:
            continue
        ledger.record_deadline_nag(conn, int(row["id"]), mark)
        ledger.enqueue_outbox(
            conn,
            "nag",
            {
                "deadline_id": int(row["id"]),
                "kind": row["kind"],
                "due_at": row["due_at"],
                "hours_left": mark,
                "item_id": row["item_id"],
                "payload": json.loads(row["payload_json"]),
            },
        )
        ledger.add_event(
            conn,
            "deadline_nag",
            {"deadline_id": int(row["id"]), "mark": mark},
            item_id=row["item_id"],
        )
        nags += 1
    return nags
