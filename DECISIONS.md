# Decisions log

Running log of choices made while building freebie-agent, and why. Newest at
the bottom of each phase section.

## Phase 1 — skeleton

- **Package index fallback.** `pypi.auros.be` was unreachable from the build
  environment (HTTP 502 through the egress proxy) at initial lock time, and
  uv hard-fails resolution when a configured index is unreachable — so the
  active index in `pyproject.toml` is `pypi.org` and `uv.lock` is pinned
  against it. The Auros mirror is kept as a ready-to-uncomment
  `[[tool.uv.index]]` block marked `default = true`; on a network that can
  reach the mirror, uncomment it and run `uv lock` to re-pin.
- **Python 3.12 (not 3.13).** The spec says 3.12+; 3.12 is the boring choice
  and matches the widest wheel availability for pinned deps.
- **stdlib `sqlite3`, not `aiosqlite`.** The pipeline is synchronous batch
  work; the bot's DB touches are small and fast. WAL mode + busy_timeout
  handles the two-process (worker + bot) concurrency. One less dependency.
- **`outbox` and `questions` tables added beyond the spec's seven.** The
  worker and bot are separate processes sharing SQLite; the worker enqueues
  outbound notifications into `outbox` and the bot polls it (long-polling
  only, no IPC ports). `questions` implements the 24h question TTL. Both are
  in migration 0001.
- **Claim state timestamps as columns** (`proposed_at`, `approved_at`, ...)
  rather than a transitions table — "timestamps per transition" with the
  fewest moving parts; the append-only `events` table already records the
  full history for the critic.
- **Config precedence:** `OLLAMA_HOST` is a required env var with no silent
  default (fail-fast rule wins over the "default localhost:11434" wording in
  the LLM section); `.env.example` ships the conventional localhost value.
- **Counters live in `events`** (`kind='counter'`, `kind='llm_call'`) instead
  of a dedicated metrics table — `--stats` and `/metrics` aggregate over the
  last 24h, and the critic gets the same raw material.
- **Migration runner:** `schema_migrations` bookkeeping table is created by
  the connection helper itself (not a migration) so "pending migrations"
  can be detected on a virgin DB.

## Phase 2 — pipeline core

- **Injection boundary implementation:** instructions go verbatim as the
  system message (loaded only from `prompts/*.md`); untrusted payloads are
  JSON-serialized into the user message between `<<DATA-<random hex>>>`
  sentinels regenerated per call (`llm.build_messages` is pure so tests can
  assert the separation).
- **Structured output detection:** Ollama >= 0.5.0 (via `GET /api/version`)
  gets `format: <json schema>`; older or undetectable versions fall back to
  `format: "json"`. Detected once per client, at startup.
- **Push budget enforced at routing time**, not send time: when today's
  `push_sent` events plus unsent notify outbox rows reach the limit, further
  pushes are demoted to the digest and a `route_demoted` event is recorded
  (the critic can see demotions). Deadline nags bypass the router entirely.
- **Shadow sources** are fetched/extracted/scored normally but their would-be
  pushes/digests are recorded as drops with reason `shadow_source` — "scored
  and logged but never notified" with the simplest possible mechanism.
- **Schedule syntax in sources.yaml** is a plain interval (`30m`, `2h`, `1d`)
  rather than cron — boring, testable, and enough for feed polling.
- **`prompts_version`** = git short-sha of the last commit touching
  `prompts/` (falls back to a content hash outside git, prefixed `nogit-`).
- **http_page fetcher baseline:** the first fetch of a page stores the hash
  and emits nothing — only subsequent *changes* become items, avoiding a
  notification storm on first run.
- **imap fetcher** reads UNSEEN messages and flags them Seen; the message-id
  based pseudo-URL (`imap://INBOX/<message-id>`) feeds the normal dedupe.

## Phase 3 — Telegram bot

- **Worker ↔ bot transport is the DB.** The worker/critic enqueue outbound
  messages into `outbox`; the bot's job queue polls it every
  `[worker].outbox_poll_seconds`. No IPC, no ports, restart-safe.
- **Claim flow is two-step:** ✅ Claim marks the claim `approved` and edits
  the card into prepared human steps with 🎉 Claimed / ⚠️ Failed buttons; only
  🎉 moves it to `claimed` (which schedules the +14d worth-it follow-up and
  bumps the source's claim counter). The agent never executes a claim.
- **Trial-cancel derivation:** approved trial/membership claims take the
  extract's `deadline_iso` as the cancel-by date when present; otherwise the
  bot asks for the renewal date (a `questions` row with the 24h TTL) and an
  ISO date in any later owner message answers the newest open trial question.
- **shipping_check deadlines have no pre-nags:** at due time the deadline
  becomes the 👍/👎 worth-it question and is marked done, instead of the
  T-72/24/2 nag ladder (which stays for trial/coupon/points/custom kinds).
- **Sensitive-data refusal is pattern-based** (card-number runs, PAN/Aadhaar
  shapes, OTP/password phrasing) and runs before any event write; the same
  redaction filter scrubs all log records. Deliberately over-broad.
- **/pause demotes pushes to the digest** (rather than dropping them) via a
  `kv` row checked by the router; /mute drops the category outright. `kv`
  is migration 0002.
- **APScheduler jobs open their own SQLite connection per run** — the
  default executor is a thread pool and sharing one connection across
  threads is not worth the cleverness. WAL keeps this cheap.
- **add_source intent appends a `status: shadow` entry to sources.yaml and
  commits**; promotion to active is the critic's job after the 14-day shadow
  window.

## Phase 5 — critic

- **config.toml is committed** (it holds no secrets — those are env-only), so
  the critic's threshold_change proposals can be real git-committed diffs
  against the running config. `config.example.toml` remains the documented
  reference copy. The `.gitignore` entry from phase 1 was removed.
- **Unified diffs are applied by a small strict pure-Python applier**
  (`difftools.py`) instead of `git apply` — git's path resolution differs
  inside/outside a worktree and the staging dir used by the eval gate is not
  a repo. The applier refuses fuzzy matches: a stale proposal fails loudly.
- **Eval gate runs twice for prompt diffs:** at propose time (a regressing
  candidate never even becomes a pending proposal — recorded as a
  `proposal_gate_blocked` event) and again post-write at apply time (a
  regression reverts the file via git checkout and rejects the proposal).
- **Shadow promotion signal** = `route_demoted` events with reason
  `shadow_source` and score >= push threshold ("would-have-pushed"), >= 3 of
  them after >= 14 days in shadow.
- **Kill heuristic** = active source with >= 30 items and 0 claims in the
  lookback window. Conservative on purpose; the operator still approves.
- **Auto-apply tier** is a kv flag (`auto_apply_source_change`) flipped by a
  special proposal whose diff carries the `AUTO-APPLY-SOURCE-CHANGES` marker
  (a behavioral change has no file to diff). It is proposed automatically
  once >= 5 source_change proposals have been applied, per spec.
- **Critic LLM is optional and cosmetic:** all decisions are SQL heuristics;
  `[critic].llm_backend` (a model tag, or "none") only rewords rationales
  via prompts/critic.md, and any LLM failure falls back to the deterministic
  wording.
- **freebie-critic default mode is a long-running weekly scheduler** (three
  long-running processes per spec); `--once` gives the on-demand run.
