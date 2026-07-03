"""Rendering: item cards, digests, nags, proposal cards. Pure functions from
DB rows / payloads to (text, inline keyboard) pairs — no I/O, fully testable."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from freebie_agent import ledger


def _fmt_value(extract: dict[str, Any]) -> str:
    value = extract.get("estimated_value_inr")
    return f"₹{value}" if isinstance(value, int) else "value unknown"


def _fmt_effort(extract: dict[str, Any]) -> str:
    effort = extract.get("effort_minutes")
    return f"~{effort} min" if isinstance(effort, int) else "effort unknown"


def item_card(
    item: sqlite3.Row, score: sqlite3.Row, claim_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """The push notification card: what, value, effort, deadline, source,
    score+reason, link — with Claim/Skip/Why buttons."""
    extract: dict[str, Any] = json.loads(item["extract_json"] or "{}")
    deadline = extract.get("deadline_iso") or "none stated"
    lines = [
        f"🎁 {extract.get('what_you_get', item['raw_title'])}",
        "",
        f"💰 {_fmt_value(extract)}   ⏱ {_fmt_effort(extract)}   📅 deadline: {deadline}",
        f"📡 {item['source_id']}   🏷 {extract.get('category', '?')}",
        f"⭐ {score['score']}/100 — {score['reason']}",
        "",
        str(item["url"]),
    ]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Claim", callback_data=f"claim:{claim_id}:approve"),
                InlineKeyboardButton("❌ Skip", callback_data=f"claim:{claim_id}:skip"),
                InlineKeyboardButton("🤔 Why?", callback_data=f"claim:{claim_id}:why"),
            ]
        ]
    )
    return "\n".join(lines), keyboard


def claim_steps_card(item: sqlite3.Row, claim_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """After ✅: the prepared claim (link, requirements, steps). The human
    executes these — the agent never does."""
    extract: dict[str, Any] = json.loads(item["extract_json"] or "{}")
    requirements = extract.get("requirements") or []
    req_lines = [f"  • {r}" for r in requirements] or ["  • nothing special"]
    lines = [
        f"✅ Approved: {extract.get('what_you_get', item['raw_title'])}",
        "",
        "Prepared claim — you do these steps yourself:",
        f"1. Open {item['url']}",
        "2. You'll need:",
        *req_lines,
        "3. Use your freebie identity kit (see profile), never real payment data.",
        "",
        "Tell me how it went:",
    ]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎉 Claimed it", callback_data=f"claim:{claim_id}:claimed"),
                InlineKeyboardButton("⚠️ Failed", callback_data=f"claim:{claim_id}:failed"),
            ]
        ]
    )
    return "\n".join(lines), keyboard


def why_text(item: sqlite3.Row, score: sqlite3.Row) -> str:
    extract = json.loads(item["extract_json"] or "{}")
    summary = json.dumps(extract, indent=2, ensure_ascii=False)
    if len(summary) > 1500:
        summary = summary[:1500] + "\n… (truncated)"
    return f"🤔 Why {score['score']}/100:\n{score['reason']}\n\nExtract:\n{summary}"


def digest_message(payload: dict[str, Any]) -> str:
    lines = ["📰 Daily digest"]
    items = payload.get("items", [])
    if items:
        lines.append("")
        for i, entry in enumerate(items, 1):
            lines.append(
                f"{i}. [{entry['score']}] {entry['title']}\n   {entry['reason']}\n   {entry['url']}"
            )
    questions = payload.get("questions", [])
    if questions:
        lines += ["", "❓ Open questions (answer within 24h or I'll proceed without):"]
        for q in questions:
            lines.append(f"  • {q['topic']}: {json.dumps(q['payload'], ensure_ascii=False)}")
    if not items and not questions:
        lines.append("Nothing new today.")
    return "\n".join(lines)


def nag_message(payload: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup | None]:
    kind = payload.get("kind", "custom")
    hours = payload.get("hours_left")
    due = payload.get("due_at", "?")
    inner = payload.get("payload", {})
    label = {
        "trial_cancel": "⚠️ Trial needs cancelling",
        "coupon_expiry": "🏷 Coupon expiring",
        "points_expiry": "💳 Points expiring",
        "custom": "⏰ Deadline",
    }.get(str(kind), "⏰ Deadline")
    what = inner.get("what", inner.get("title", ""))
    text = f"{label}: {what}\nDue {due} — about {hours}h left."
    deadline_id = payload.get("deadline_id")
    keyboard = None
    if deadline_id is not None:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✔️ Done", callback_data=f"deadline:{deadline_id}:done")]]
        )
    return text, keyboard


def worth_it_question(payload: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    claim_id = payload["claim_id"]
    what = payload.get("what", "your claim")
    text = f"📦 Two weeks ago you claimed: {what}\nDid it arrive / was it worth it?"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👍 Worth it", callback_data=f"worthit:{claim_id}:yes"),
                InlineKeyboardButton("👎 Not worth it", callback_data=f"worthit:{claim_id}:no"),
            ]
        ]
    )
    return text, keyboard


def proposal_card(proposal: sqlite3.Row) -> tuple[str, InlineKeyboardMarkup]:
    eval_note = ""
    if proposal["eval_before"] is not None and proposal["eval_after"] is not None:
        eval_note = f"\n📊 eval: {proposal['eval_before']:.1%} → {proposal['eval_after']:.1%}"
    diff = str(proposal["diff"])
    if len(diff) > 2000:
        diff = diff[:2000] + "\n… (truncated)"
    text = (
        f"🔧 Proposal #{proposal['id']} ({proposal['kind']})\n\n"
        f"{proposal['rationale']}{eval_note}\n\n{diff}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Apply", callback_data=f"proposal:{proposal['id']}:apply"),
                InlineKeyboardButton(
                    "❌ Reject", callback_data=f"proposal:{proposal['id']}:reject"
                ),
            ]
        ]
    )
    return text, keyboard


def queue_text(conn: sqlite3.Connection) -> str:
    from freebie_agent.models import ClaimState

    lines = ["📋 Claim queue"]
    for state in (ClaimState.PROPOSED, ClaimState.APPROVED):
        rows = ledger.claims_in_state(conn, state)
        if rows:
            lines.append(f"\n{state.value}:")
            for r in rows:
                lines.append(f"  #{r['id']} {r['raw_title'][:60]}\n    {r['url']}")
    if len(lines) == 1:
        lines.append("empty — nothing waiting on you.")
    return "\n".join(lines)


def ledger_text(conn: sqlite3.Connection, limit: int) -> str:
    rows = ledger.recent_claims(conn, limit)
    if not rows:
        return "📒 Ledger is empty."
    lines = [f"📒 Last {len(rows)} claims:"]
    worth_icon = {1: "👍", 0: "👎", None: "·"}
    for r in rows:
        lines.append(
            f"  #{r['id']} [{r['state']}] {worth_icon[r['worth_it']]} {r['raw_title'][:60]}"
        )
    return "\n".join(lines)


def due_text(conn: sqlite3.Connection) -> str:
    rows = ledger.open_deadlines(conn)
    if not rows:
        return "⏰ No open deadlines."
    lines = ["⏰ Open deadlines:"]
    for r in rows:
        lines.append(f"  #{r['id']} {r['kind']} due {r['due_at']} ({r['state']})")
    return "\n".join(lines)


def status_text(counters: dict[str, int], llm_stats: dict[str, float]) -> str:
    lines = ["📈 Pipeline (last 24h):"]
    for name in sorted(counters):
        lines.append(f"  {name}: {counters[name]}")
    lines.append(f"  llm_calls: {int(llm_stats['llm_calls'])}")
    lines.append(f"  llm_failures: {int(llm_stats['llm_failures'])}")
    lines.append(f"  llm_latency_ms_avg: {llm_stats['llm_latency_ms_avg']}")
    return "\n".join(lines)
