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
