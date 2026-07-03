---
name: freebie-claim
description: Prepare a claim for a queued freebie — link, requirements, and manual steps for the operator. Never executes any claim itself.
version: 0.1.0
metadata:
  openclaw:
    requires:
      bins: [python3, uv]
      env: [TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, OLLAMA_HOST, OLLAMA_MODEL]
---

# freebie-claim

Turns a claim id into a prepared, human-executable claim package (URL,
requirements, step list, deadline). The human performs the claim; the agent
only prepares and tracks it.

## Steps

1. Find the claim id: run the freebie-ledger skill or ask the operator.
2. Run `echo '{"claim_id": <id>}' | {baseDir}/scripts/claim_prepare.sh`.
3. Relay the returned `steps`, `requirements` and `url` to the operator.
4. Approval and outcome are recorded via the Telegram bot buttons
   (✅ Claim → 🎉 Claimed / ⚠️ Failed); do not mark outcomes yourself.

## Rules

1. Never create accounts, submit personal data, or enter payment details.
2. Prepare the claim (link, code, steps) and request operator approval.
3. If approval is not granted within 24h, mark skipped.
4. Never ask for or store passwords, OTPs, card numbers, or government IDs;
   if the operator sends one, reply that it cannot be stored and do not
   persist it.
