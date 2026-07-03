---
name: freebie-ledger
description: Query the freebie ledger — recent claims with outcomes and worth-it feedback, plus open deadlines (trial cancellations, expiring coupons).
version: 0.1.0
metadata:
  openclaw:
    requires:
      bins: [python3, uv]
      env: [TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, OLLAMA_HOST, OLLAMA_MODEL]
---

# freebie-ledger

Read-only view of the SQLite ledger: claims (with state and worth-it
feedback) and open deadlines.

## Steps

1. Run `echo '{"limit": 20}' | {baseDir}/scripts/ledger_query.sh` (limit is
   optional, max 200).
2. Summarize `claims` (state, worth_it) and flag anything in
   `open_deadlines` that is due soon — trial cancellations first.

## Rules

1. Read-only: this skill never mutates claims, deadlines, or items.
2. Claim/skip decisions and outcome marking happen only via the Telegram
   bot's buttons, where they are attributed to the operator.
3. Do not paste raw ledger rows containing URLs into public places.
