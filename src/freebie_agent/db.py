"""SQLite connection helper and migration runner.

One file (`data/ledger.db`), WAL mode. Schema changes only via numbered
migration scripts in `migrations/`. The worker refuses to start while
migrations are pending; they are applied only by an explicit
`freebie-worker --migrate`.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class PendingMigrationsError(Exception):
    """Raised when the DB schema is behind migrations/ and --migrate wasn't given."""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the ledger DB with WAL mode and foreign keys enforced."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
    )
    return conn


def discover_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """Return [(version, path)] sorted; reject misnamed files loudly."""
    found: list[tuple[int, Path]] = []
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {migrations_dir}")
    for path in sorted(migrations_dir.iterdir()):
        if path.suffix != ".sql":
            continue
        m = _MIGRATION_RE.match(path.name)
        if not m:
            raise ValueError(f"migration file {path.name!r} does not match NNNN_name.sql naming")
        found.append((int(m.group(1)), path))
    versions = [v for v, _ in found]
    if len(versions) != len(set(versions)):
        raise ValueError("duplicate migration version numbers in migrations/")
    return found


def pending_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[tuple[int, Path]]:
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    return [(v, p) for v, p in discover_migrations(migrations_dir) if v not in applied]


def assert_migrated(conn: sqlite3.Connection, migrations_dir: Path) -> None:
    """Fail fast if any migration is pending."""
    pending = pending_migrations(conn, migrations_dir)
    if pending:
        names = ", ".join(p.name for _, p in pending)
        raise PendingMigrationsError(
            f"{len(pending)} pending migration(s): {names}. Run `freebie-worker --migrate` first."
        )


def migrate(conn: sqlite3.Connection, migrations_dir: Path) -> list[str]:
    """Apply all pending migrations in order; returns the names applied."""
    applied: list[str] = []
    for version, path in pending_migrations(conn, migrations_dir):
        sql = path.read_text(encoding="utf-8")
        with conn:  # one transaction per migration
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (version, path.name),
            )
        applied.append(path.name)
    return applied


def default_migrations_dir() -> Path:
    """Prefer ./migrations (running from the repo root, per runbook), else the
    copy that sits next to the source checkout."""
    cwd_migrations = Path("migrations")
    if cwd_migrations.is_dir():
        return cwd_migrations
    return Path(__file__).resolve().parent.parent.parent / "migrations"
