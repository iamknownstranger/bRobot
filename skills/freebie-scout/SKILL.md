---
name: freebie-scout
description: Run one freebie discovery pass — fetch configured sources, extract and score items with the local LLM, and route them to push/digest/drop.
version: 0.1.0
metadata:
  openclaw:
    requires:
      bins: [python3, uv]
      env: [TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, OLLAMA_HOST, OLLAMA_MODEL]
---

# freebie-scout

Runs a single fetch → extract → score → route pass over the sources in
`sources.yaml`. Output is one JSON object with per-stage counts.

## Steps

1. Run `{baseDir}/scripts/scout.sh` (no stdin needed). It prints
   `{"ok": true, "stats": {"fetched": N, "extracted": N, "scored": N}}`.
2. If `ok` is false, report the `error` field to the operator verbatim and
   stop; do not retry more than once.
3. To inspect what the pass produced, use the freebie-ledger skill.

## Rules

1. Discovery only: this skill fetches public feeds/pages and the dedicated
   freebie inbox. It never logs into websites, never solves CAPTCHAs, never
   scrapes behind logins.
2. Notification decisions belong to the pipeline's router and rate limits;
   do not bypass them by messaging the operator directly.
3. Failures of a single source are recorded on that source and are not an
   error for the pass as a whole.
