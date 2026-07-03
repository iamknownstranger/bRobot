"""Bot behavior: owner-only auth, claim flow (card -> claim -> ledger ->
follow-up scheduling), commands, sensitive-data refusal, intents."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ApplicationHandlerStop

from freebie_agent import ledger
from freebie_agent.bot.gateway import REFUSAL_TEXT, Gateway
from freebie_agent.config import Config
from freebie_agent.deadlines import deadline_scan
from freebie_agent.models import (
    ClaimState,
    DeadlineKind,
    ItemStatus,
    RawItem,
    SourceStatus,
)

from .mocks import FakeBot, FakeIntentLLM

OWNER_ID = 424242
STRANGER_ID = 666


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def gateway(cfg: Config, conn: sqlite3.Connection) -> Gateway:
    return Gateway(cfg, conn, FakeIntentLLM())  # type: ignore[arg-type]


def make_context(bot: FakeBot, args: list[str] | None = None) -> Any:
    return SimpleNamespace(bot=bot, args=args or [])


def make_text_update(text: str, user_id: int = OWNER_ID) -> Update:
    user = User(id=user_id, is_bot=False, first_name="Op")
    chat = Chat(id=user_id, type="private")
    msg = Message(message_id=1, date=datetime.now(), chat=chat, from_user=user, text=text)
    return Update(update_id=1, message=msg)


def seed_pushed_item(
    conn: sqlite3.Connection,
    category: str = "merch",
    deadline_iso: str | None = None,
) -> tuple[int, int]:
    """One queued item with score, claim and outbox notify — as the pipeline
    leaves it after a push routing decision."""
    ledger.sync_source(conn, "seed-src", "rss", "u", "1h", SourceStatus.ACTIVE)
    item_id, _ = ledger.upsert_raw_item(
        conn, RawItem("https://x.test/deal", "Nice freebie", "body", "seed-src")
    )
    ledger.set_extract(
        conn,
        item_id,
        {
            "what_you_get": "Nice freebie",
            "estimated_value_inr": 999,
            "effort_minutes": 3,
            "deadline_iso": deadline_iso,
            "requirements": ["existing account"],
            "region": "IN",
            "category": category,
            "red_flags": [],
            "is_lead_gen_trap": False,
        },
    )
    ledger.add_score(conn, item_id, 91, "great value", "abc1234", "gemma3:test")
    ledger.set_item_status(conn, item_id, ItemStatus.QUEUED)
    claim_id = ledger.create_claim(conn, item_id)
    ledger.enqueue_outbox(conn, "notify", {"item_id": item_id, "claim_id": claim_id})
    return item_id, claim_id


# ------------------------------------------------------------------- auth


async def test_guard_drops_non_owner_silently(
    gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection
) -> None:
    update = make_text_update("hello", user_id=STRANGER_ID)
    with pytest.raises(ApplicationHandlerStop):
        await gateway.guard(update, make_context(bot))
    # never replied
    assert bot.sent == [] and bot.edited == []
    # logged-and-dropped
    events = list(conn.execute("SELECT * FROM events WHERE kind='auth_rejected'"))
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["from_id"] == STRANGER_ID


async def test_guard_passes_owner(gateway: Gateway, bot: FakeBot) -> None:
    update = make_text_update("hello", user_id=OWNER_ID)
    await gateway.guard(update, make_context(bot))  # no raise


# ------------------------------------------------------------- claim flow


async def test_outbox_notify_renders_item_card(
    gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection
) -> None:
    seed_pushed_item(conn)
    await gateway.poll_outbox(make_context(bot))
    assert len(bot.sent) == 1
    card = bot.sent[0]
    assert "Nice freebie" in card["text"]
    assert "₹999" in card["text"]
    assert "91/100" in card["text"]
    buttons = [b.text for row in card["reply_markup"].inline_keyboard for b in row]
    assert buttons == ["✅ Claim", "❌ Skip", "🤔 Why?"]
    # marked sent + push counted
    assert ledger.unsent_outbox(conn) == []
    assert ledger.pushes_sent_today(conn) == 1


async def _tap(gateway: Gateway, bot: FakeBot, data: str, message_id: int = 10) -> None:
    user = User(id=OWNER_ID, is_bot=False, first_name="Op")
    chat = Chat(id=OWNER_ID, type="private")
    msg = Message(message_id=message_id, date=datetime.now(), chat=chat, from_user=user)
    query = CallbackQuery(id="1", from_user=user, chat_instance="ci", data=data, message=msg)
    query.set_bot(bot)  # type: ignore[arg-type]
    update = Update(update_id=2, callback_query=query)
    await gateway.on_callback(update, make_context(bot))


async def test_claim_approve_then_claimed_schedules_followup(
    gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection
) -> None:
    """Integration: claim -> ledger -> follow-up scheduling."""
    item_id, claim_id = seed_pushed_item(conn)

    await _tap(gateway, bot, f"claim:{claim_id}:approve")
    claim = ledger.get_claim(conn, claim_id)
    assert claim is not None and claim["state"] == ClaimState.APPROVED.value
    assert claim["approved_at"] is not None
    # card edited in place with prepared claim steps
    assert "you do these steps yourself" in bot.edited[-1]["text"]

    await _tap(gateway, bot, f"claim:{claim_id}:claimed")
    claim = ledger.get_claim(conn, claim_id)
    assert claim is not None and claim["state"] == ClaimState.CLAIMED.value
    item = ledger.get_item(conn, item_id)
    assert item is not None and item["status"] == ItemStatus.CLAIMED.value

    # +14d shipping_check follow-up exists
    deadlines = ledger.open_deadlines(conn)
    assert [d["kind"] for d in deadlines] == [DeadlineKind.SHIPPING_CHECK.value]
    due = ledger.parse_iso(deadlines[0]["due_at"])
    assert abs((due - ledger.utcnow()) - timedelta(days=14)) < timedelta(minutes=1)

    # source claim counter for the critic
    src = ledger.get_source(conn, "seed-src")
    assert src is not None and src["items_claimed"] == 1


async def test_worth_it_roundtrip(
    gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection, cfg: Config
) -> None:
    """The +14d follow-up fires as a 👍/👎 question and writes worth_it."""
    item_id, claim_id = seed_pushed_item(conn)
    ledger.set_claim_state(conn, claim_id, ClaimState.CLAIMED)
    ledger.add_deadline(
        conn,
        DeadlineKind.SHIPPING_CHECK,
        ledger.utcnow() - timedelta(minutes=1),
        item_id=item_id,
        payload={"claim_id": claim_id, "what": "Nice freebie"},
    )
    deadline_scan(conn, cfg)
    await gateway.poll_outbox(make_context(bot))
    question = bot.sent[-1]
    assert "worth it" in question["text"]

    await _tap(gateway, bot, f"worthit:{claim_id}:yes")
    claim = ledger.get_claim(conn, claim_id)
    assert claim is not None and claim["worth_it"] == 1
    assert claim["state"] == ClaimState.ARRIVED.value


async def test_skip_and_why(gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection) -> None:
    _, claim_id = seed_pushed_item(conn)
    await _tap(gateway, bot, f"claim:{claim_id}:why")
    assert "Why 91/100" in bot.sent[-1]["text"]
    assert "great value" in bot.sent[-1]["text"]
    claim = ledger.get_claim(conn, claim_id)
    assert claim is not None and claim["state"] == ClaimState.PROPOSED.value  # why != decision

    await _tap(gateway, bot, f"claim:{claim_id}:skip")
    claim = ledger.get_claim(conn, claim_id)
    assert claim is not None and claim["state"] == ClaimState.SKIPPED.value


async def test_trial_approval_creates_cancel_deadline(
    gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection
) -> None:
    _, claim_id = seed_pushed_item(conn, category="trial", deadline_iso="2027-03-31")
    await _tap(gateway, bot, f"claim:{claim_id}:approve")
    kinds = [d["kind"] for d in ledger.open_deadlines(conn)]
    assert DeadlineKind.TRIAL_CANCEL.value in kinds


async def test_trial_approval_without_date_asks_and_answer_sets_deadline(
    gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection
) -> None:
    _, claim_id = seed_pushed_item(conn, category="trial", deadline_iso=None)
    await _tap(gateway, bot, f"claim:{claim_id}:approve")
    assert any("When does it renew" in t for t in bot.texts)
    assert ledger.open_deadlines(conn) == []

    # operator answers with a date -> trial_cancel deadline + question answered
    await gateway.on_text(make_text_update("it renews 2027-01-15"), make_context(bot))
    deadlines = ledger.open_deadlines(conn)
    assert [d["kind"] for d in deadlines] == [DeadlineKind.TRIAL_CANCEL.value]
    q = conn.execute("SELECT state FROM questions").fetchone()
    assert q["state"] == "answered"


# --------------------------------------------------------------- commands


async def test_cmd_floor_updates_profile_and_commits(
    gateway: Gateway, bot: FakeBot, cfg: Config, conn: sqlite3.Connection
) -> None:
    await gateway.cmd_floor(make_text_update("/floor 500"), make_context(bot, ["500"]))
    assert "₹500" in cfg.paths.profile.read_text(encoding="utf-8")
    assert "₹500" in bot.texts[-1]


async def test_cmd_mute_drops_category(
    gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection, cfg: Config
) -> None:
    await gateway.cmd_mute(make_text_update("/mute merch"), make_context(bot, ["merch"]))
    assert "merch" in ledger.muted_categories(conn)
    from freebie_agent.router import Bucket, effective_bucket

    ledger.sync_source(conn, "s2", "rss", "u", "1h", SourceStatus.ACTIVE)
    bucket, reason = effective_bucket(
        conn, "s2", 95, cfg.thresholds, cfg.pushes_per_day, category="merch"
    )
    assert bucket is Bucket.DROP and reason == "muted_category"


async def test_cmd_pause_demotes_pushes(
    gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection, cfg: Config
) -> None:
    await gateway.cmd_pause(make_text_update("/pause 3"), make_context(bot, ["3"]))
    assert ledger.paused_until(conn) is not None
    from freebie_agent.router import Bucket, effective_bucket

    ledger.sync_source(conn, "s3", "rss", "u", "1h", SourceStatus.ACTIVE)
    bucket, reason = effective_bucket(conn, "s3", 95, cfg.thresholds, cfg.pushes_per_day)
    assert bucket is Bucket.DIGEST and reason == "paused"

    await gateway.cmd_resume(make_text_update("/resume"), make_context(bot))
    assert ledger.paused_until(conn) is None


async def test_cmd_queue_ledger_due_status(
    gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection
) -> None:
    seed_pushed_item(conn)
    ctx = make_context(bot)
    await gateway.cmd_queue(make_text_update("/queue"), ctx)
    assert "Nice freebie" in bot.texts[-1]
    await gateway.cmd_ledger(make_text_update("/ledger"), ctx)
    assert "claims" in bot.texts[-1] or "Ledger" in bot.texts[-1]
    await gateway.cmd_due(make_text_update("/due"), ctx)
    assert "deadline" in bot.texts[-1].lower()
    await gateway.cmd_status(make_text_update("/status"), ctx)
    assert "llm_calls" in bot.texts[-1]


async def test_digest_message_render(
    gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection, cfg: Config
) -> None:
    ledger.sync_source(conn, "dsrc", "rss", "u", "1h", SourceStatus.ACTIVE)
    item_id, _ = ledger.upsert_raw_item(
        conn, RawItem("https://x.test/mid", "Mid deal", "b", "dsrc")
    )
    ledger.set_extract(conn, item_id, {"what_you_get": "Mid deal", "category": "other"})
    ledger.add_score(conn, item_id, 60, "decent", "v1", "m")
    ledger.set_item_status(conn, item_id, ItemStatus.DIGESTED)
    await gateway.cmd_digest(make_text_update("/digest"), make_context(bot))
    assert any("Daily digest" in t and "Mid deal" in t for t in bot.texts)


# -------------------------------------------------------------- free text


async def test_sensitive_message_refused_and_not_persisted(
    gateway: Gateway, bot: FakeBot, conn: sqlite3.Connection
) -> None:
    secret = "my card is 4111 1111 1111 1111 please save it"
    await gateway.on_text(make_text_update(secret), make_context(bot))
    assert bot.texts[-1] == REFUSAL_TEXT
    # nothing containing the number was persisted anywhere in events
    rows = list(conn.execute("SELECT kind, payload FROM events"))
    assert all("4111" not in r["payload"] for r in rows)
    assert any(r["kind"] == "sensitive_refused" for r in rows)


async def test_update_profile_intent_appends_note_and_commits(
    cfg: Config, conn: sqlite3.Connection, bot: FakeBot
) -> None:
    llm = FakeIntentLLM(
        intent="update_profile", confidence=0.95, args={"note": "cancelled Zomato Gold"}
    )
    gateway = Gateway(cfg, conn, llm)  # type: ignore[arg-type]
    await gateway.on_text(make_text_update("I cancelled Zomato Gold btw"), make_context(bot))
    profile = cfg.paths.profile.read_text(encoding="utf-8")
    assert "cancelled Zomato Gold" in profile
    assert profile.rstrip().endswith("cancelled Zomato Gold")
    import subprocess

    log_output = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=cfg.paths.profile.parent,
        capture_output=True,
        text=True,
    ).stdout
    assert "operator note" in log_output


async def test_low_confidence_intent_asks_clarifying_question(
    cfg: Config, conn: sqlite3.Connection, bot: FakeBot
) -> None:
    llm = FakeIntentLLM(intent="adjust_filter", confidence=0.3)
    gateway = Gateway(cfg, conn, llm)  # type: ignore[arg-type]
    await gateway.on_text(make_text_update("hmm do the thing"), make_context(bot))
    assert "not sure" in bot.texts[-1]
    # nothing was changed
    assert ledger.paused_until(conn) is None
    assert ledger.muted_categories(conn) == set()
