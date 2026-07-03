"""freebie-skill: JSON stdin/stdout subcommands backing the OpenClaw skills.

Every subcommand reads an optional JSON object from stdin and writes exactly
one JSON object to stdout, so skill scripts stay thin shims. Logic lives in
the freebie_agent package — the scripts add nothing.

No subcommand creates accounts, submits personal data, or transacts: claim
preparation is read-only output for a human to execute.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from freebie_agent import db as dbmod
from freebie_agent import ledger
from freebie_agent.config import Config, load_or_exit
from freebie_agent.logs import register_secret, setup_logging


def _read_stdin_json() -> dict[str, Any]:
    data = sys.stdin.read().strip()
    if not data:
        return {}
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise ValueError("stdin must be a JSON object")
    return parsed


def _emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def cmd_scout(cfg: Config, args: dict[str, Any]) -> dict[str, Any]:
    """One fetch->extract->score->route pass. Requires Ollama reachable."""
    from freebie_agent.worker import run_pipeline_once

    stats = run_pipeline_once(cfg)
    return {"ok": True, "stats": stats}


def cmd_ledger_query(cfg: Config, args: dict[str, Any]) -> dict[str, Any]:
    limit = min(int(args.get("limit", 20)), 200)
    conn = dbmod.connect(cfg.paths.db)
    try:
        dbmod.assert_migrated(conn, dbmod.default_migrations_dir())
        claims = [
            {
                "claim_id": r["id"],
                "item_id": r["item_id"],
                "state": r["state"],
                "worth_it": r["worth_it"],
                "title": r["raw_title"],
                "url": r["url"],
                "updated_at": r["updated_at"],
            }
            for r in ledger.recent_claims(conn, limit)
        ]
        deadlines = [
            {
                "deadline_id": r["id"],
                "kind": r["kind"],
                "due_at": r["due_at"],
                "state": r["state"],
            }
            for r in ledger.open_deadlines(conn)
        ]
    finally:
        conn.close()
    return {"ok": True, "claims": claims, "open_deadlines": deadlines}


def cmd_claim_prepare(cfg: Config, args: dict[str, Any]) -> dict[str, Any]:
    """Prepare (never execute) a claim: link, requirements, steps."""
    claim_id = args.get("claim_id")
    if claim_id is None:
        return {"ok": False, "error": "claim_id is required"}
    conn = dbmod.connect(cfg.paths.db)
    try:
        dbmod.assert_migrated(conn, dbmod.default_migrations_dir())
        claim = ledger.get_claim(conn, int(claim_id))
        if claim is None:
            return {"ok": False, "error": f"claim {claim_id} not found"}
        item = ledger.get_item(conn, int(claim["item_id"]))
        assert item is not None
        extract = json.loads(item["extract_json"] or "{}")
        score = ledger.latest_score(conn, int(claim["item_id"]))
    finally:
        conn.close()
    return {
        "ok": True,
        "claim_id": int(claim["id"]),
        "state": claim["state"],
        "what_you_get": extract.get("what_you_get", item["raw_title"]),
        "url": item["url"],
        "requirements": extract.get("requirements", []),
        "deadline_iso": extract.get("deadline_iso"),
        "score": score["score"] if score else None,
        "steps": [
            f"Open {item['url']}",
            "Check the requirements list; use the freebie identity kit from profile.md.",
            "Complete the claim manually — the agent never submits anything.",
            "Mark the outcome via the Telegram card (🎉 Claimed / ⚠️ Failed).",
        ],
        "boundaries": "No account creation, no personal data submission, no payment entry.",
    }


def cmd_critic_run(cfg: Config, args: dict[str, Any]) -> dict[str, Any]:
    from freebie_agent.critic.run import DirtyTreeError, run_critic_once

    config_path = Path(str(args.get("config", "config.toml")))
    try:
        created = run_critic_once(cfg, config_path)
    except DirtyTreeError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "proposals_created": created}


COMMANDS = {
    "scout": cmd_scout,
    "ledger-query": cmd_ledger_query,
    "claim-prepare": cmd_claim_prepare,
    "critic-run": cmd_critic_run,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="freebie-skill", description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--config", default="config.toml", type=Path)
    args = parser.parse_args(argv)

    cfg = load_or_exit(args.config)
    register_secret(cfg.telegram_bot_token)
    setup_logging(None, f"skill-{args.command}")  # logs go to stderr; stdout is JSON only

    try:
        stdin_args = _read_stdin_json()
    except (json.JSONDecodeError, ValueError) as exc:
        _emit({"ok": False, "error": f"invalid stdin JSON: {exc}"})
        return 2
    try:
        result = COMMANDS[args.command](cfg, stdin_args)
    except dbmod.PendingMigrationsError as exc:
        _emit({"ok": False, "error": str(exc)})
        return 3
    _emit(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
