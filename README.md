# freebie-agent

A personal, self-improving freebie-hunting agent for a single Linux VM. It
discovers deals, promo merch, free memberships, bundled entitlements and
trials from configurable sources; extracts and scores each item with a local
LLM (Ollama, `gemma3` family) against your profile; notifies you on Telegram
with inline **Claim / Skip / Why** buttons; tracks a claim queue, an outcome
ledger, and deadlines (trial cancellations, coupon expiry); and improves its
own prompts, profile and source list weekly through an operator-approved
proposal loop, applied as git commits and gated by a fixed eval set.

## What this deliberately does not do

- **No auto-claiming.** No code path submits forms, creates accounts, or
  transacts. The agent prepares claims; a human executes the final step.
- No automated form submission of personal data, no payment entry, no CAPTCHA
  handling, no scraping behind logins.
- The bot never asks for or stores passwords, OTPs, card numbers, or
  government IDs — if you send one, it refuses and does not persist it.
- Prompts never receive scraped text as instructions: untrusted content is
  fenced behind random per-call sentinels (tested).

## Architecture

```
 sources.yaml                      profile.md            prompts/*.md
      │                                 │                     │
      ▼                                 ▼                     ▼
┌──────────────────────────── freebie-worker ─────────────────────────────┐
│ APScheduler:                                                            │
│  per-source fetch ──► dedupe ──► extract (LLM) ──► score (LLM) ──► route│
│  (rss / imap / http_page)         │ strict JSON        │ 0-100          │
│                                   ▼                    ▼                │
│  deadline scan (T-72/24/2)     items table          scores table        │
│  daily digest, question TTL        └──────────┬─────────┘               │
└───────────────────────────────────────────────┼─────────────────────────┘
                                                ▼
                    ┌── SQLite data/ledger.db (WAL) ──┐
                    │ items scores claims deadlines   │
                    │ sources events proposals outbox │
                    └───────────────┬─────────────────┘
                                    │ outbox polling        long-polling
┌── freebie-critic (weekly) ──┐     ▼                            │
│ ledger -> findings ->       │  ┌───────── freebie-bot ─────────┴──┐
│ proposals (diffs+rationale) │  │ owner-only gateway                │
│ eval gate on prompt diffs   │──► item cards ✅/❌/🤔, digests,     │
│ apply = git commit          │  │ nags, worth-it 👍/👎, /commands,  │
└─────────────────────────────┘  │ proposal Apply/Reject             │
                                 └────────────────────────► Telegram │
```

## 5-minute setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), git, and a running
[Ollama](https://ollama.com) with a `gemma3` model pulled.

```bash
git clone <this repo> freebie-agent && cd freebie-agent
uv sync                      # installs pinned deps from uv.lock
cp .env.example .env         # fill in all four required values
$EDITOR profile.md           # replace the TODO placeholders — this drives scoring
$EDITOR sources.yaml         # enable/replace the starter sources
uv run freebie-worker --migrate
uv run freebie-worker --stats     # sanity check: prints zeroed counters
uv run pytest                     # full suite, no network needed
```

Required environment (all four are mandatory; the processes exit non-zero
otherwise): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `OLLAMA_HOST`,
`OLLAMA_MODEL`. Optional: `FREEBIE_IMAP_HOST/USER/PASSWORD` for the inbox
fetcher. Secrets live only in `.env` (git-ignored) — never in `config.toml`,
the DB, logs, or git.

`config.toml` (committed, no secrets) holds thresholds, schedules, rate
limits and the opt-in localhost `/metrics` endpoint. Start from
`config.example.toml` if yours is missing.

## Runbook: the three processes

| Process          | What it does                                              |
|------------------|-----------------------------------------------------------|
| `freebie-worker` | APScheduler pipeline: fetch → extract → score → route, deadline nags, daily digest |
| `freebie-bot`    | Telegram gateway (long-polling, owner-only), delivers outbox, records taps |
| `freebie-critic` | Weekly self-improvement pass (also `--once` on demand)    |

Run them under systemd (adjust paths/user):

```ini
# /etc/systemd/system/freebie-worker.service
[Unit]
Description=freebie-agent worker
After=network-online.target ollama.service

[Service]
User=freebie
WorkingDirectory=/home/freebie/freebie-agent
ExecStart=/home/freebie/.local/bin/uv run freebie-worker
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Create `freebie-bot.service` and `freebie-critic.service` identically with
`ExecStart=... uv run freebie-bot` / `... uv run freebie-critic`, then:

```bash
sudo systemctl enable --now freebie-worker freebie-bot freebie-critic
```

Useful commands:

```bash
uv run freebie-worker --once      # single pipeline pass, prints stats JSON
uv run freebie-worker --stats     # last-24h counters from the DB
uv run freebie-critic --once      # on-demand critic pass
uv run python eval/run_eval.py    # score current prompts against the eval set
curl localhost:9187/metrics       # if [metrics].enabled = true
```

Bot commands: `/queue` `/digest` `/ledger [n]` `/due` `/pause <days>`
`/resume` `/floor <inr>` `/mute <category>` `/status` `/proposals`. Free text
is classified into a fixed intent set; when unsure the bot asks instead of
guessing, and open questions expire to "no answer" after 24h.

## How to add a source

1. Edit `sources.yaml` and add an entry:

   ```yaml
   - id: my-deals-feed
     type: rss            # rss | imap | http_page
     url: https://example.com/deals.rss
     schedule: 2h         # 30m / 2h / 1d
     status: shadow       # start in shadow: scored + logged, never notified
   ```

2. Commit it (`git add sources.yaml && git commit -m "sources: add my-deals-feed"`)
   and restart `freebie-worker`.
3. Leave it in `shadow` for 14 days. If it earns it (items that would have
   pushed), the critic proposes promotion to `active`; approve from Telegram.
   You can also just tell the bot "watch https://example.com/deals.rss" —
   the add_source intent appends a shadow entry and commits it for you.

## The proposal / approval loop

1. Weekly (or `freebie-critic --once`), the critic reads the last weeks of
   events, claims, scores and source stats. It refuses to run on a dirty git
   tree.
2. Deterministic heuristics produce findings: noisy zero-claim sources,
   shadow sources worth promoting, high-score-but-skipped patterns, 👎
   worth-it feedback, push-threshold skew.
3. Findings become `proposals` rows with unified diffs against
   `sources.yaml`, `prompts/score.md`, `profile.md` or `config.toml`, plus a
   rationale citing the ledger numbers.
4. **Eval gate:** a proposal touching `prompts/` is only offered if the
   candidate's bucket accuracy on `eval/items.jsonl` is >= the current
   prompts' accuracy. Both numbers are stored on the proposal.
5. Each proposal lands on Telegram with **Apply / Reject** buttons. Apply
   writes the diff, re-runs the eval gate post-write (reverting on
   regression), commits with the rationale as the message, and records the
   sha. Reject is recorded too — the critic learns from both.
6. Source pruning/promotion can graduate to auto-apply after you've approved
   five of them — and that graduation is itself a proposal.

## OpenClaw skills

`skills/` packages the agent's capabilities as OpenClaw-compatible skill
folders (`SKILL.md` + `scripts/`): `freebie-scout`, `freebie-claim`,
`freebie-ledger`, `freebie-critic`. Every script is a thin shim over the
`freebie-skill` CLI (JSON stdin/stdout), so logic lives in one place. The
autonomy rules in each SKILL.md are binding — notably freebie-claim: never
create accounts, submit personal data, or enter payment details; prepare the
claim and request operator approval; unapproved after 24h means skipped.

## Development

```bash
uv run pytest             # behavior tests; Ollama + Telegram always mocked
uv run ruff check src tests eval
uv run mypy --strict src/
```

CI (`.gitlab-ci.yml`) runs ruff, mypy --strict and pytest; all three must
pass. Schema changes go through numbered files in `migrations/` — the worker
refuses to start with pending migrations (`freebie-worker --migrate` applies
them explicitly). Design decisions and their reasons live in `DECISIONS.md`.
