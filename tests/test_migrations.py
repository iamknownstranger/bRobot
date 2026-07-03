"""Migration runner: refuses to start with pending migrations; applies only
via the explicit --migrate path; is idempotent."""

from __future__ import annotations

from pathlib import Path

import pytest

from freebie_agent import db as dbmod
from freebie_agent.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO_ROOT / "migrations"


def test_virgin_db_has_pending_migrations(cfg: Config) -> None:
    conn = dbmod.connect(cfg.paths.db)
    try:
        assert dbmod.pending_migrations(conn, MIGRATIONS)
        with pytest.raises(dbmod.PendingMigrationsError, match="--migrate"):
            dbmod.assert_migrated(conn, MIGRATIONS)
    finally:
        conn.close()


def test_migrate_then_clean(cfg: Config) -> None:
    conn = dbmod.connect(cfg.paths.db)
    try:
        applied = dbmod.migrate(conn, MIGRATIONS)
        assert "0001_init.sql" in applied
        dbmod.assert_migrated(conn, MIGRATIONS)  # no raise
        # idempotent: second run applies nothing
        assert dbmod.migrate(conn, MIGRATIONS) == []
    finally:
        conn.close()


def test_worker_refuses_run_with_pending_migration(cfg: Config, tmp_path: Path) -> None:
    """A new migration file appearing => worker run/stats path must refuse."""
    import shutil

    migrations = tmp_path / "migs"
    shutil.copytree(MIGRATIONS, migrations)
    conn = dbmod.connect(cfg.paths.db)
    try:
        dbmod.migrate(conn, migrations)
        (migrations / "0002_new_column.sql").write_text(
            "ALTER TABLE items ADD COLUMN test_col TEXT;"
        )
        with pytest.raises(dbmod.PendingMigrationsError, match="0002_new_column.sql"):
            dbmod.assert_migrated(conn, migrations)
    finally:
        conn.close()


def test_misnamed_migration_rejected(cfg: Config, tmp_path: Path) -> None:
    migrations = tmp_path / "migs"
    migrations.mkdir()
    (migrations / "init.sql").write_text("SELECT 1;")
    conn = dbmod.connect(cfg.paths.db)
    try:
        with pytest.raises(ValueError, match="NNNN_name.sql"):
            dbmod.discover_migrations(migrations)
    finally:
        conn.close()


def test_worker_cli_migrate_and_stats(
    cfg: Config, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: `freebie-worker --migrate && freebie-worker --stats` on an
    empty DB runs clean."""
    from freebie_agent.worker import main

    monkeypatch.chdir(repo_copy)
    assert main(["--config", str(repo_copy / "config.toml"), "--migrate"]) == 0
    assert main(["--config", str(repo_copy / "config.toml"), "--stats"]) == 0


def test_worker_cli_stats_refuses_unmigrated(
    cfg: Config, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from freebie_agent.worker import main

    monkeypatch.chdir(repo_copy)
    assert main(["--config", str(repo_copy / "config.toml"), "--stats"]) == 3
