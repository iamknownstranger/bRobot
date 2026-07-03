"""freebie-skill CLI: JSON stdin/stdout contract used by the skill shims."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from freebie_agent import ledger
from freebie_agent.config import Config
from freebie_agent.models import ItemStatus, RawItem, SourceStatus
from freebie_agent.skills_cli import main


def _run(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    stdin: str = "",
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = main(argv)
    out = capsys.readouterr().out.strip()
    return code, json.loads(out)


def _seed_claim(conn: sqlite3.Connection) -> int:
    ledger.sync_source(conn, "s", "rss", "u", "1h", SourceStatus.ACTIVE)
    item_id, _ = ledger.upsert_raw_item(
        conn, RawItem("https://x.test/deal", "Skill test freebie", "b", "s")
    )
    ledger.set_extract(
        conn,
        item_id,
        {
            "what_you_get": "Skill test freebie",
            "requirements": ["existing account"],
            "deadline_iso": None,
            "category": "other",
        },
    )
    ledger.add_score(conn, item_id, 88, "nice", "v1", "m")
    ledger.set_item_status(conn, item_id, ItemStatus.QUEUED)
    return ledger.create_claim(conn, item_id)


def test_ledger_query_json_roundtrip(
    conn: sqlite3.Connection,
    cfg: Config,
    repo_copy: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_claim(conn)
    monkeypatch.chdir(repo_copy)
    code, out = _run(
        capsys,
        monkeypatch,
        ["ledger-query", "--config", str(repo_copy / "config.toml")],
        stdin='{"limit": 5}',
    )
    assert code == 0 and out["ok"] is True
    claims = out["claims"]
    assert isinstance(claims, list) and claims[0]["title"] == "Skill test freebie"  # type: ignore[index]


def test_claim_prepare_never_executes(
    conn: sqlite3.Connection,
    cfg: Config,
    repo_copy: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id = _seed_claim(conn)
    monkeypatch.chdir(repo_copy)
    code, out = _run(
        capsys,
        monkeypatch,
        ["claim-prepare", "--config", str(repo_copy / "config.toml")],
        stdin=json.dumps({"claim_id": claim_id}),
    )
    assert code == 0 and out["ok"] is True
    assert out["url"] == "https://x.test/deal"
    assert "boundaries" in out and "No account creation" in str(out["boundaries"])
    steps = " ".join(str(s) for s in out["steps"])  # type: ignore[arg-type]
    assert "manually" in steps
    # preparation changed nothing in the ledger
    claim = ledger.get_claim(conn, claim_id)
    assert claim is not None and claim["state"] == "proposed"


def test_missing_claim_id_errors(
    conn: sqlite3.Connection,
    cfg: Config,
    repo_copy: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(repo_copy)
    code, out = _run(
        capsys,
        monkeypatch,
        ["claim-prepare", "--config", str(repo_copy / "config.toml")],
    )
    assert code == 1 and out["ok"] is False


def test_invalid_stdin_json_errors(
    conn: sqlite3.Connection,
    cfg: Config,
    repo_copy: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(repo_copy)
    code, out = _run(
        capsys,
        monkeypatch,
        ["ledger-query", "--config", str(repo_copy / "config.toml")],
        stdin="not json",
    )
    assert code == 2 and out["ok"] is False
