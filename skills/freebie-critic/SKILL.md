---
name: freebie-critic
description: Run the self-improvement critic on demand — analyze the ledger and create operator-approvable proposals (source changes, prompt diffs, thresholds).
version: 0.1.0
metadata:
  openclaw:
    requires:
      bins: [python3, uv, git]
      env: [TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, OLLAMA_HOST, OLLAMA_MODEL]
---

# freebie-critic

One on-demand critic pass: deterministic ledger analysis produces proposal
rows (source kill/promote, score-prompt cautions, threshold changes) that are
delivered to the operator on Telegram with Apply/Reject buttons.

## Steps

1. Ensure the repository working tree is clean (`git status`); the critic
   refuses to run otherwise.
2. Run `{baseDir}/scripts/critic_run.sh`. It prints
   `{"ok": true, "proposals_created": [ids...]}`.
3. Report the created proposal ids; the operator reviews them via the bot's
   `/proposals` command.

## Rules

1. Proposals never self-apply: every change requires operator approval via
   Telegram, except source pruning/promotion after the operator has
   explicitly enabled the auto-apply tier (itself a proposal).
2. Prompt-touching proposals must pass the eval gate (candidate accuracy >=
   current) both before being offered and again after being written.
3. Every applied proposal is a git commit with the rationale as message —
   never edit prompts/profile/sources outside this loop.
4. If the tree is dirty or the gate fails, report it; do not force anything.
