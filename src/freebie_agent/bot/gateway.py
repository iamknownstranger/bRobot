"""freebie-bot entrypoint: Telegram <-> event queue translation ONLY.

No business logic here beyond ledger writes and rendering; the only LLM call
is intent classification. Long-polling only — no webhooks, no public ports.

Auth: every update whose from.id != TELEGRAM_OWNER_ID is logged and dropped,
never replied to.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from freebie_agent import db as dbmod
from freebie_agent import gitops, ledger
from freebie_agent.bot import intents, render
from freebie_agent.config import Config, load_or_exit
from freebie_agent.llm import OllamaClient
from freebie_agent.logs import contains_sensitive, redact, register_secret, setup_logging
from freebie_agent.models import ClaimState, DeadlineKind, DeadlineState, Intent, ItemStatus

log = logging.getLogger("bot")

_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

REFUSAL_TEXT = (
    "That looks like a credential, OTP, card number, or ID. I can't store or "
    "use those — I never submit personal data anywhere. I haven't saved it."
)


class Gateway:
    """Holds config + DB and implements every handler. All outbound traffic
    goes through context.bot so tests can substitute a recorder."""

    def __init__(self, cfg: Config, conn: sqlite3.Connection, llm: OllamaClient) -> None:
        self.cfg = cfg
        self.conn = conn
        self.llm = llm
        self.repo_root = cfg.paths.profile.resolve().parent

    # ---------------------------------------------------------------- auth

    async def guard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Group -1 gatekeeper: silently drop anything not from the owner."""
        user = update.effective_user
        if user is None or user.id != self.cfg.telegram_owner_id:
            log.warning(
                "dropped non-owner update",
                extra={"ctx": {"from_id": user.id if user else None}},
            )
            ledger.add_event(self.conn, "auth_rejected", {"from_id": user.id if user else None})
            raise ApplicationHandlerStop

    # ------------------------------------------------------------- buttons

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return
        await query.answer()
        kind, _, rest = query.data.partition(":")
        ident_s, _, action = rest.partition(":")
        ident = int(ident_s)
        ledger.add_event(self.conn, "button_tap", {"data": query.data})
        chat_id = update.effective_chat.id if update.effective_chat else 0
        message_id = query.message.message_id if query.message else None

        if kind == "claim":
            await self._on_claim_button(context, chat_id, message_id, ident, action)
        elif kind == "worthit":
            await self._on_worth_it(context, chat_id, message_id, ident, action)
        elif kind == "deadline":
            ledger.set_deadline_state(self.conn, ident, DeadlineState.DONE)
            await self._edit_or_send(context, chat_id, message_id, "✔️ Deadline marked done.")
        elif kind == "proposal":
            await self._on_proposal_button(context, chat_id, message_id, ident, action)

    async def _edit_or_send(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int | None,
        text: str,
        reply_markup: Any = None,
    ) -> None:
        if message_id is not None:
            await context.bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(chat_id, text, reply_markup=reply_markup)

    async def _on_claim_button(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int | None,
        claim_id: int,
        action: str,
    ) -> None:
        claim = ledger.get_claim(self.conn, claim_id)
        if claim is None:
            await context.bot.send_message(chat_id, f"Claim #{claim_id} not found.")
            return
        item = ledger.get_item(self.conn, int(claim["item_id"]))
        assert item is not None
        extract: dict[str, Any] = json.loads(item["extract_json"] or "{}")

        if action == "approve":
            ledger.set_claim_state(self.conn, claim_id, ClaimState.APPROVED)
            text, keyboard = render.claim_steps_card(item, claim_id)
            await self._edit_or_send(context, chat_id, message_id, text, keyboard)
            if str(extract.get("category", "")) in ("trial", "membership"):
                await self._ensure_trial_cancel_deadline(
                    context, chat_id, int(item["id"]), claim_id, extract
                )
        elif action == "skip":
            ledger.set_claim_state(self.conn, claim_id, ClaimState.SKIPPED)
            await self._edit_or_send(
                context, chat_id, message_id, f"❌ Skipped: {item['raw_title'][:80]}"
            )
        elif action == "why":
            score = ledger.latest_score(self.conn, int(item["id"]))
            if score is None:
                await context.bot.send_message(chat_id, "No stored score for this item.")
                return
            await context.bot.send_message(chat_id, render.why_text(item, score))
        elif action == "claimed":
            ledger.set_claim_state(self.conn, claim_id, ClaimState.CLAIMED)
            ledger.set_item_status(self.conn, int(item["id"]), ItemStatus.CLAIMED)
            ledger.bump_source_counter(self.conn, str(item["source_id"]), "items_claimed")
            due = ledger.utcnow() + timedelta(days=14)
            ledger.add_deadline(
                self.conn,
                DeadlineKind.SHIPPING_CHECK,
                due,
                item_id=int(item["id"]),
                payload={
                    "claim_id": claim_id,
                    "what": extract.get("what_you_get", item["raw_title"]),
                },
            )
            await self._edit_or_send(
                context,
                chat_id,
                message_id,
                "🎉 Logged as claimed. I'll ask in two weeks whether it was worth it.",
            )
        elif action == "failed":
            ledger.set_claim_state(self.conn, claim_id, ClaimState.FAILED)
            await self._edit_or_send(
                context, chat_id, message_id, f"⚠️ Logged as failed: {item['raw_title'][:80]}"
            )

    async def _ensure_trial_cancel_deadline(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        item_id: int,
        claim_id: int,
        extract: dict[str, Any],
    ) -> None:
        """Approved trial/membership claims MUST get a trial_cancel deadline:
        derived from the extract when possible, otherwise asked."""
        deadline_iso = extract.get("deadline_iso")
        if isinstance(deadline_iso, str) and _ISO_DATE_RE.search(deadline_iso):
            ledger.add_deadline(
                self.conn,
                DeadlineKind.TRIAL_CANCEL,
                ledger.parse_iso(
                    deadline_iso if "T" in deadline_iso else deadline_iso + "T00:00:00Z"
                ),
                item_id=item_id,
                payload={"claim_id": claim_id, "what": extract.get("what_you_get", "")},
            )
            return
        question_id = ledger.ask_question(
            self.conn,
            "trial_end_date",
            {"claim_id": claim_id, "what": extract.get("what_you_get", "")},
            item_id=item_id,
        )
        await context.bot.send_message(
            chat_id,
            "⚠️ This is a trial/membership but I couldn't find its end date. "
            "When does it renew? Reply with a date like 2026-08-01. "
            f"(question #{question_id}, expires in {self.cfg.question_ttl_hours}h)",
        )

    async def _on_worth_it(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int | None,
        claim_id: int,
        action: str,
    ) -> None:
        worth_it = action == "yes"
        ledger.set_claim_worth_it(self.conn, claim_id, worth_it)
        ledger.set_claim_state(self.conn, claim_id, ClaimState.ARRIVED)
        ledger.add_event(
            self.conn, "worth_it_feedback", {"claim_id": claim_id, "worth_it": worth_it}
        )
        await self._edit_or_send(
            context,
            chat_id,
            message_id,
            "👍 Noted — that helps me score future finds."
            if worth_it
            else "👎 Noted — I'll penalize similar items.",
        )

    async def _on_proposal_button(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int | None,
        proposal_id: int,
        action: str,
    ) -> None:
        from freebie_agent.critic import apply as critic_apply

        if action == "apply":
            outcome = critic_apply.apply_proposal(self.conn, self.cfg, proposal_id)
        else:
            outcome = critic_apply.reject_proposal(self.conn, proposal_id)
        await self._edit_or_send(context, chat_id, message_id, outcome)

    # ------------------------------------------------------------ commands

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, context, render.queue_text(self.conn))

    async def cmd_digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from freebie_agent import pipeline

        payload = pipeline.build_digest(self.conn, self.cfg)
        if payload is None:
            await self._reply(update, context, "📰 Nothing pending for a digest.")
        else:
            # build_digest enqueued it; deliver immediately as well.
            await self._flush_outbox(context)

    async def cmd_ledger(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        n = 10
        if context.args and context.args[0].isdigit():
            n = min(int(context.args[0]), 50)
        await self._reply(update, context, render.ledger_text(self.conn, n))

    async def cmd_due(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, context, render.due_text(self.conn))

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        days = 1
        if context.args and context.args[0].isdigit():
            days = int(context.args[0])
        until = ledger.utcnow() + timedelta(days=days)
        ledger.set_paused_until(self.conn, until)
        ledger.add_event(self.conn, "paused", {"days": days})
        await self._reply(
            update, context, f"⏸ Paused pushes for {days} day(s). Digest continues. /resume undoes."
        )

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ledger.set_paused_until(self.conn, None)
        ledger.add_event(self.conn, "resumed", {})
        await self._reply(update, context, "▶️ Resumed real-time pushes.")

    async def cmd_floor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args or not context.args[0].isdigit():
            await self._reply(update, context, "Usage: /floor <inr>, e.g. /floor 500")
            return
        floor = int(context.args[0])
        sha = intents.set_value_floor(self.cfg.paths.profile, floor, self.repo_root)
        ledger.add_event(self.conn, "floor_changed", {"floor_inr": floor, "commit": sha})
        await self._reply(update, context, f"💰 Value floor set to ₹{floor} (commit {sha}).")

    async def cmd_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await self._reply(update, context, "Usage: /mute <category>, e.g. /mute merch")
            return
        cats = ledger.mute_category(self.conn, context.args[0])
        ledger.add_event(self.conn, "muted", {"category": context.args[0]})
        await self._reply(update, context, f"🔇 Muted. Currently muted: {', '.join(sorted(cats))}")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        since = ledger.utcnow() - timedelta(hours=24)
        text = render.status_text(
            ledger.counters_since(self.conn, since),
            ledger.llm_stats_since(self.conn, since),
        )
        await self._reply(update, context, text)

    async def cmd_proposals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from freebie_agent.models import ProposalState

        rows = ledger.proposals_in_state(self.conn, ProposalState.PENDING)
        if not rows:
            await self._reply(update, context, "🔧 No pending proposals.")
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        for row in rows:
            text, keyboard = render.proposal_card(row)
            await context.bot.send_message(chat_id, text, reply_markup=keyboard)

    # ----------------------------------------------------------- free text

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return
        text = message.text
        chat_id = update.effective_chat.id if update.effective_chat else 0

        # Never persist credentials/OTPs/IDs — refuse before anything is written.
        if contains_sensitive(text):
            await context.bot.send_message(chat_id, REFUSAL_TEXT)
            ledger.add_event(self.conn, "sensitive_refused", {})
            return

        ledger.add_event(self.conn, "message", {"text": redact(text)[:1000]})

        # An open trial-end question + a date in the message answers it.
        if await self._maybe_answer_trial_question(context, chat_id, text):
            return

        result = intents.classify(self.llm, text)
        ledger.add_event(
            self.conn,
            "intent_resolved",
            {
                "intent": result.intent.value if result.intent else "unknown",
                "confidence": result.confidence,
            },
        )
        if result.intent is None:
            await context.bot.send_message(
                chat_id,
                "I'm not sure what you want me to do with that. Rephrase, or use "
                "/queue /ledger /due /pause /floor /mute /status /proposals.",
            )
            return
        await self._dispatch_intent(context, chat_id, result, text)

    async def _maybe_answer_trial_question(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str
    ) -> bool:
        match = _ISO_DATE_RE.search(text)
        if not match:
            return False
        row = self.conn.execute(
            "SELECT * FROM questions WHERE state='open' AND topic='trial_end_date'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return False
        payload = json.loads(row["payload"])
        due = ledger.parse_iso(match.group(1) + "T00:00:00Z")
        ledger.add_deadline(
            self.conn,
            DeadlineKind.TRIAL_CANCEL,
            due,
            item_id=row["item_id"],
            payload=payload,
        )
        ledger.resolve_question(self.conn, int(row["id"]), "answered")
        await context.bot.send_message(
            chat_id, f"📅 Got it — trial cancel deadline set for {match.group(1)}."
        )
        return True

    async def _dispatch_intent(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        result: intents.Classified,
        original_text: str,
    ) -> None:
        assert result.intent is not None
        bot = context.bot
        args = result.args
        if result.intent is Intent.UPDATE_PROFILE:
            note = str(args.get("note") or original_text)
            sha = intents.append_operator_note(self.cfg.paths.profile, note, self.repo_root)
            await bot.send_message(chat_id, f"📝 Noted in profile (commit {sha}).")
        elif result.intent is Intent.QUERY_LEDGER:
            await bot.send_message(chat_id, render.ledger_text(self.conn, 10))
        elif result.intent is Intent.ADJUST_FILTER:
            floor = args.get("floor_inr")
            if isinstance(floor, int):
                sha = intents.set_value_floor(self.cfg.paths.profile, floor, self.repo_root)
                await bot.send_message(chat_id, f"💰 Value floor set to ₹{floor} (commit {sha}).")
            else:
                await bot.send_message(
                    chat_id, "Use /floor <inr> or /mute <category> for filter changes."
                )
        elif result.intent is Intent.EXPLAIN_DECISION:
            await bot.send_message(chat_id, self._explain_latest())
        elif result.intent is Intent.PAUSE:
            days = args.get("days") if isinstance(args.get("days"), int) else 1
            until = ledger.utcnow() + timedelta(days=int(days or 1))
            ledger.set_paused_until(self.conn, until)
            await bot.send_message(chat_id, f"⏸ Paused pushes for {days} day(s).")
        elif result.intent is Intent.CLAIM_STATUS:
            await bot.send_message(chat_id, render.queue_text(self.conn))
        elif result.intent is Intent.ADD_SOURCE:
            url = str(args.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                await bot.send_message(
                    chat_id, "Send the feed/page URL and I'll add it as a shadow source."
                )
                return
            await bot.send_message(chat_id, self._add_shadow_source(url))
        else:  # chitchat
            await bot.send_message(chat_id, "🤖 Here and hunting. /status for numbers.")

    def _explain_latest(self) -> str:
        row = self.conn.execute(
            "SELECT s.score, s.reason, i.raw_title FROM scores s"
            " JOIN items i ON i.id = s.item_id ORDER BY s.id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return "Nothing scored yet."
        return (
            f"Latest decision — {row['raw_title'][:70]}:\n"
            f"score {row['score']}/100: {row['reason']}\n"
            "Tap 🤔 Why? on any card for its full extract."
        )

    def _add_shadow_source(self, url: str) -> str:
        """Append a shadow source to sources.yaml and commit. Shadow sources
        are scored but never notified until the critic proposes promotion."""
        from urllib.parse import urlparse

        slug = re.sub(r"[^a-z0-9]+", "-", urlparse(url).netloc.lower()).strip("-")
        source_id = f"{slug}-shadow"
        sources_path = self.cfg.paths.sources
        if source_id in sources_path.read_text(encoding="utf-8"):
            return f"Source {source_id} is already configured."
        entry = (
            f"\n  - id: {source_id}\n"
            f"    type: rss\n"
            f"    url: {url}\n"
            f"    schedule: 6h\n"
            f"    status: shadow\n"
        )
        with sources_path.open("a", encoding="utf-8") as f:
            f.write(entry)
        sha = gitops.commit_paths(
            self.repo_root, [sources_path], f"sources: add shadow source {source_id}"
        )
        ledger.add_event(self.conn, "source_added", {"source_id": source_id, "commit": sha})
        return (
            f"👻 Added {source_id} as a shadow source (commit {sha}). It'll be scored "
            "silently for 14 days; the critic will propose promotion if it earns it."
        )

    # -------------------------------------------------------------- outbox

    async def poll_outbox(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._flush_outbox(context)

    async def _flush_outbox(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = self.cfg.telegram_owner_id
        for row in ledger.unsent_outbox(self.conn):
            payload = json.loads(row["payload"])
            kind = row["kind"]
            try:
                if kind == "notify":
                    await self._send_notify(context, chat_id, payload)
                    ledger.add_event(self.conn, "push_sent", {"item_id": payload.get("item_id")})
                elif kind == "digest":
                    await context.bot.send_message(chat_id, render.digest_message(payload))
                    ledger.add_event(self.conn, "digest_sent", {"n": len(payload.get("items", []))})
                elif kind == "nag":
                    text, keyboard = render.nag_message(payload)
                    await context.bot.send_message(chat_id, text, reply_markup=keyboard)
                    ledger.add_event(
                        self.conn, "nag_sent", {"deadline_id": payload.get("deadline_id")}
                    )
                elif kind == "worth_it":
                    text, keyboard = render.worth_it_question(payload)
                    await context.bot.send_message(chat_id, text, reply_markup=keyboard)
                elif kind == "proposal":
                    proposal = ledger.get_proposal(self.conn, int(payload["proposal_id"]))
                    if proposal is not None:
                        text, keyboard = render.proposal_card(proposal)
                        await context.bot.send_message(chat_id, text, reply_markup=keyboard)
                else:
                    await context.bot.send_message(chat_id, json.dumps(payload)[:1000])
            except Exception:
                log.exception("outbox send failed", extra={"ctx": {"outbox_id": row["id"]}})
                continue
            ledger.mark_outbox_sent(self.conn, int(row["id"]))

    async def _send_notify(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, payload: dict[str, Any]
    ) -> None:
        item = ledger.get_item(self.conn, int(payload["item_id"]))
        if item is None:
            return
        score = ledger.latest_score(self.conn, int(payload["item_id"]))
        if score is None:
            return
        text, keyboard = render.item_card(item, score, int(payload["claim_id"]))
        await context.bot.send_message(chat_id, text, reply_markup=keyboard)

    # --------------------------------------------------------------- reply

    async def _reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        chat_id = update.effective_chat.id if update.effective_chat else 0
        await context.bot.send_message(chat_id, text)


def build_application(cfg: Config, conn: sqlite3.Connection, llm: OllamaClient) -> Application:  # type: ignore[type-arg]
    gateway = Gateway(cfg, conn, llm)
    app = Application.builder().token(cfg.telegram_bot_token).build()
    app.add_handler(TypeHandler(Update, gateway.guard), group=-1)
    for name, handler in (
        ("queue", gateway.cmd_queue),
        ("digest", gateway.cmd_digest),
        ("ledger", gateway.cmd_ledger),
        ("due", gateway.cmd_due),
        ("pause", gateway.cmd_pause),
        ("resume", gateway.cmd_resume),
        ("floor", gateway.cmd_floor),
        ("mute", gateway.cmd_mute),
        ("status", gateway.cmd_status),
        ("proposals", gateway.cmd_proposals),
    ):
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(gateway.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, gateway.on_text))
    if app.job_queue is not None:
        app.job_queue.run_repeating(gateway.poll_outbox, interval=cfg.outbox_poll_seconds, first=2)
    return app


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="freebie-bot")
    parser.add_argument("--config", default="config.toml", type=Path)
    args = parser.parse_args()

    cfg = load_or_exit(args.config)
    register_secret(cfg.telegram_bot_token)
    setup_logging(cfg.paths.logs, "bot")

    conn = dbmod.connect(cfg.paths.db)
    try:
        dbmod.assert_migrated(conn, dbmod.default_migrations_dir())
    except dbmod.PendingMigrationsError as exc:
        print(f"fatal: {exc}")
        return 3
    llm = OllamaClient(
        host=cfg.ollama_host,
        model=cfg.ollama_model,
        prompts_dir=cfg.paths.prompts,
        temperature=cfg.ollama_temperature,
        timeout_seconds=cfg.ollama_timeout_seconds,
        retries=cfg.ollama_retries,
    )
    app = build_application(cfg, conn, llm)
    log.info("bot starting (long polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
