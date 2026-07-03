"""Deadline engine: nag ladder (T-72/24/2), single-fire per mark, missed
transitions, and the shipping_check -> worth-it special case."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from freebie_agent import ledger
from freebie_agent.config import Config
from freebie_agent.deadlines import deadline_scan, due_nag_mark
from freebie_agent.models import DeadlineKind, DeadlineState


def test_due_nag_mark_ladder() -> None:
    nag_hours = (72, 24, 2)
    now = ledger.utcnow()

    # far out: nothing yet
    assert due_nag_mark(now + timedelta(hours=100), now, nag_hours, ()) is None
    # inside 72h window: 72-mark fires
    assert due_nag_mark(now + timedelta(hours=71), now, nag_hours, ()) == 72
    # 72 already sent, still outside 24h: nothing
    assert due_nag_mark(now + timedelta(hours=48), now, nag_hours, (72,)) is None
    # inside 24h: 24-mark fires even if 72 was sent
    assert due_nag_mark(now + timedelta(hours=23), now, nag_hours, (72,)) == 24
    # inside 2h: 2-mark
    assert due_nag_mark(now + timedelta(hours=1), now, nag_hours, (72, 24)) == 2
    # created late (inside all windows, nothing sent): only the most urgent
    assert due_nag_mark(now + timedelta(hours=1), now, nag_hours, ()) == 2
    # past due: no nag (handled as missed)
    assert due_nag_mark(now - timedelta(hours=1), now, nag_hours, ()) is None


def test_scan_emits_nags_once_per_mark(conn: sqlite3.Connection, cfg: Config) -> None:
    now = ledger.utcnow()
    deadline_id = ledger.add_deadline(
        conn,
        DeadlineKind.TRIAL_CANCEL,
        now + timedelta(hours=23),
        payload={"what": "FitTrackr trial"},
    )

    assert deadline_scan(conn, cfg, now=now) == 1
    outbox = ledger.unsent_outbox(conn)
    assert len(outbox) == 1
    row = conn.execute("SELECT * FROM deadlines WHERE id = ?", (deadline_id,)).fetchone()
    assert row["state"] == DeadlineState.NAGGED.value
    assert row["nags_sent"] == "[24]"

    # same window again: no duplicate nag
    assert deadline_scan(conn, cfg, now=now + timedelta(minutes=30)) == 0

    # 2h window later: second nag
    assert deadline_scan(conn, cfg, now=now + timedelta(hours=21, minutes=30)) == 1
    row = conn.execute("SELECT * FROM deadlines WHERE id = ?", (deadline_id,)).fetchone()
    assert row["nags_sent"] == "[24, 2]"


def test_scan_marks_past_due_as_missed(conn: sqlite3.Connection, cfg: Config) -> None:
    ledger.add_deadline(conn, DeadlineKind.COUPON_EXPIRY, ledger.utcnow() - timedelta(hours=1))
    deadline_scan(conn, cfg)
    row = conn.execute("SELECT * FROM deadlines").fetchone()
    assert row["state"] == DeadlineState.MISSED.value
    events = [r["kind"] for r in conn.execute("SELECT kind FROM events")]
    assert "deadline_missed" in events


def test_shipping_check_fires_worth_it_question_at_due(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    ledger.add_deadline(
        conn,
        DeadlineKind.SHIPPING_CHECK,
        ledger.utcnow() - timedelta(minutes=5),
        payload={"claim_id": 7, "what": "steel bottle"},
    )
    # no pre-nags ever fired for shipping_check
    deadline_scan(conn, cfg)
    outbox = ledger.unsent_outbox(conn)
    assert [o["kind"] for o in outbox] == ["worth_it"]
    row = conn.execute("SELECT * FROM deadlines").fetchone()
    assert row["state"] == DeadlineState.DONE.value  # not missed


def test_shipping_check_has_no_pre_nags(conn: sqlite3.Connection, cfg: Config) -> None:
    ledger.add_deadline(
        conn,
        DeadlineKind.SHIPPING_CHECK,
        ledger.utcnow() + timedelta(hours=1),
        payload={"claim_id": 7, "what": "steel bottle"},
    )
    assert deadline_scan(conn, cfg) == 0
    assert ledger.unsent_outbox(conn) == []
