"""freebie-worker entrypoint: migrations, stats, and the scheduler pipeline.

The worker refuses to start with pending migrations; `--migrate` is the only
code path that applies them.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

from freebie_agent import db as dbmod
from freebie_agent import ledger
from freebie_agent.config import Config, load_or_exit
from freebie_agent.logs import register_secret, setup_logging


def cmd_migrate(cfg: Config) -> int:
    conn = dbmod.connect(cfg.paths.db)
    try:
        applied = dbmod.migrate(conn, dbmod.default_migrations_dir())
    finally:
        conn.close()
    if applied:
        print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("no pending migrations")
    return 0


def cmd_stats(cfg: Config) -> int:
    conn = dbmod.connect(cfg.paths.db)
    try:
        dbmod.assert_migrated(conn, dbmod.default_migrations_dir())
        since = ledger.utcnow() - timedelta(hours=24)
        counters = ledger.counters_since(conn, since)
        llm = ledger.llm_stats_since(conn, since)
    finally:
        conn.close()
    print("pipeline counters (last 24h):")
    for name in (
        "items_fetched",
        "items_extracted",
        "items_scored",
        "items_queued",
        "items_digested",
        "items_dropped",
        "items_notified",
    ):
        print(f"  {name:20s} {counters.get(name, 0)}")
    for name in sorted(
        set(counters)
        - {
            "items_fetched",
            "items_extracted",
            "items_scored",
            "items_queued",
            "items_digested",
            "items_dropped",
            "items_notified",
        }
    ):
        print(f"  {name:20s} {counters[name]}")
    print(f"  {'llm_calls':20s} {int(llm['llm_calls'])}")
    print(f"  {'llm_failures':20s} {int(llm['llm_failures'])}")
    print(f"  {'llm_latency_ms_avg':20s} {llm['llm_latency_ms_avg']}")
    return 0


def cmd_run(cfg: Config, once: bool) -> int:
    conn = dbmod.connect(cfg.paths.db)
    try:
        dbmod.assert_migrated(conn, dbmod.default_migrations_dir())
    finally:
        conn.close()
    # Scheduler wiring lands in phase 2 (see build plan); until then the run
    # mode is a clear error, not a silent no-op.
    print("fatal: pipeline scheduler not implemented yet (phase 2)", file=sys.stderr)
    return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="freebie-worker")
    parser.add_argument("--config", default="config.toml", type=Path)
    parser.add_argument("--migrate", action="store_true", help="apply pending migrations")
    parser.add_argument("--stats", action="store_true", help="print last-24h counters")
    parser.add_argument("--once", action="store_true", help="run one pipeline pass and exit")
    args = parser.parse_args(argv)

    cfg = load_or_exit(args.config)
    register_secret(cfg.telegram_bot_token)
    setup_logging(cfg.paths.logs, "worker")

    try:
        if args.migrate:
            return cmd_migrate(cfg)
        if args.stats:
            return cmd_stats(cfg)
        return cmd_run(cfg, once=args.once)
    except dbmod.PendingMigrationsError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
