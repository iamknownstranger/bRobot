# freebie-agent

A personal, self-improving freebie-hunting agent for a single Linux VM. It
discovers deals/promo merch/free memberships/trials from configurable sources,
extracts and scores them with a local LLM (Ollama) against your profile,
notifies you on Telegram with Claim/Skip/Why buttons, tracks deadlines
(trial cancellations, coupon expiry), and improves its own prompts/sources
weekly via an operator-approved proposal loop.

> **What this deliberately does not do:** no automated account creation, no
> automated form submission of personal data, no payment entry, no CAPTCHA
> handling, no scraping behind logins, no auto-claiming. The agent prepares
> claims; a human executes the final step.

*README stub — full setup, runbook, and architecture land in phase 6.*

## Quick start (development)

```bash
uv sync
cp config.example.toml config.toml
cp .env.example .env      # fill in TELEGRAM_BOT_TOKEN etc.
uv run freebie-worker --migrate
uv run freebie-worker --stats
uv run pytest
```
