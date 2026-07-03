"""freebie-worker entrypoint: migrations, stats, and the scheduler pipeline.

The worker refuses to start with pending migrations; `--migrate` is the only
code path that applies them.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from freebie_agent import db as dbmod
from freebie_agent import ledger, pipeline, sourcecfg
from freebie_agent.config import Config, load_or_exit
from freebie_agent.deadlines import deadline_scan
from freebie_agent.fetchers import build_fetcher
from freebie_agent.llm import OllamaClient
from freebie_agent.logs import register_secret, setup_logging
from freebie_agent.models import SourceStatus


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


def _build_llm(cfg: Config) -> OllamaClient:
    return OllamaClient(
        host=cfg.ollama_host,
        model=cfg.ollama_model,
        prompts_dir=cfg.paths.prompts,
        temperature=cfg.ollama_temperature,
        timeout_seconds=cfg.ollama_timeout_seconds,
        retries=cfg.ollama_retries,
    )


def _sync_sources(conn: sqlite3.Connection, cfg: Config) -> list[dict[str, object]]:
    entries = sourcecfg.load_sources(cfg.paths.sources)
    for entry in entries:
        ledger.sync_source(
            conn,
            source_id=str(entry["id"]),
            type_=str(entry["type"]),
            url=str(entry.get("url", "")),
            schedule=str(entry.get("schedule", "")),
            status=SourceStatus(str(entry.get("status", "active"))),
        )
    return entries


def _fetchable(entry: dict[str, object]) -> bool:
    """active and shadow sources are fetched; paused/killed are not."""
    return str(entry.get("status", "active")) in (
        SourceStatus.ACTIVE.value,
        SourceStatus.SHADOW.value,
    )


def run_pipeline_once(cfg: Config) -> dict[str, int]:
    conn = dbmod.connect(cfg.paths.db)
    llm = _build_llm(cfg)
    try:
        entries = _sync_sources(conn, cfg)
        fetchers = [build_fetcher(e) for e in entries if _fetchable(e)]
        return pipeline.run_pass(conn, cfg, llm, fetchers)
    finally:
        llm.close()
        conn.close()


class _MetricsHandler(BaseHTTPRequestHandler):
    """Plain-text last-24h counters on localhost (opt-in via [metrics])."""

    db_path: Path  # set on the subclass created in _start_metrics_server

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/metrics":
            self.send_error(404)
            return
        conn = dbmod.connect(self.db_path)
        try:
            since = ledger.utcnow() - timedelta(hours=24)
            counters = ledger.counters_since(conn, since)
            llm = ledger.llm_stats_since(conn, since)
        finally:
            conn.close()
        lines = [f"freebie_{name} {value}" for name, value in sorted(counters.items())]
        lines += [f"freebie_{name} {value}" for name, value in sorted(llm.items())]
        body = "\n".join(lines) + "\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: object) -> None:
        logging.getLogger("metrics").info(format % args)


def _start_metrics_server(cfg: Config) -> None:
    handler = type("Handler", (_MetricsHandler,), {"db_path": cfg.paths.db})
    server = HTTPServer(("127.0.0.1", cfg.metrics_port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="metrics")
    thread.start()
    logging.getLogger("worker").info(
        "metrics endpoint up", extra={"ctx": {"port": cfg.metrics_port}}
    )


def run_scheduler(cfg: Config) -> None:
    """APScheduler wiring: per-source fetch jobs, extract+score sweep, daily
    digest, question-TTL expiry, deadline nag scan.

    APScheduler runs jobs on a thread pool, so every job opens its own
    short-lived SQLite connection (WAL mode makes this cheap and safe).
    """
    log = logging.getLogger("worker")
    setup_conn = dbmod.connect(cfg.paths.db)
    try:
        entries = _sync_sources(setup_conn, cfg)
    finally:
        setup_conn.close()
    llm = _build_llm(cfg)
    scheduler = BlockingScheduler(timezone="UTC")

    def fetch_job(entry: dict[str, object]) -> None:
        conn = dbmod.connect(cfg.paths.db)
        try:
            fetcher = build_fetcher(entry)
            pipeline.fetch_source(conn, fetcher)
            profile_text = cfg.paths.profile.read_text(encoding="utf-8")
            pipeline.extract_stage(conn, llm)
            pipeline.score_stage(conn, cfg, llm, profile_text)
        finally:
            conn.close()

    def digest_job() -> None:
        conn = dbmod.connect(cfg.paths.db)
        try:
            pipeline.build_digest(conn, cfg)
        finally:
            conn.close()

    def question_ttl_job() -> None:
        conn = dbmod.connect(cfg.paths.db)
        try:
            ledger.expire_stale_questions(conn, cfg.question_ttl_hours)
        finally:
            conn.close()

    def deadline_job() -> None:
        conn = dbmod.connect(cfg.paths.db)
        try:
            deadline_scan(conn, cfg)
        finally:
            conn.close()

    for entry in entries:
        if not _fetchable(entry):
            continue
        minutes = sourcecfg.parse_schedule_minutes(
            str(entry.get("schedule", "")), cfg.default_fetch_minutes
        )
        scheduler.add_job(
            fetch_job,
            "interval",
            minutes=minutes,
            args=[entry],
            id=f"fetch:{entry['id']}",
            next_run_time=ledger.utcnow(),
            max_instances=1,
            coalesce=True,
        )

    digest_hour, digest_minute = (int(p) for p in cfg.digest_send_at.split(":"))
    scheduler.add_job(digest_job, "cron", hour=digest_hour, minute=digest_minute, id="digest")
    scheduler.add_job(question_ttl_job, "interval", hours=1, id="question-ttl")
    scheduler.add_job(
        deadline_job,
        "interval",
        minutes=10,
        id="deadline-scan",
        max_instances=1,
        coalesce=True,
    )

    if cfg.metrics_enabled:
        _start_metrics_server(cfg)

    log.info(
        "worker scheduler starting",
        extra={"ctx": {"sources": [str(e["id"]) for e in entries if _fetchable(e)]}},
    )
    try:
        scheduler.start()
    finally:
        llm.close()


def cmd_run(cfg: Config, once: bool) -> int:
    conn = dbmod.connect(cfg.paths.db)
    try:
        dbmod.assert_migrated(conn, dbmod.default_migrations_dir())
    finally:
        conn.close()
    if once:
        stats = run_pipeline_once(cfg)
        print(json.dumps(stats))
        return 0
    run_scheduler(cfg)
    return 0


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
