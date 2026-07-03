"""Small git helpers: the bot commits profile/source edits, the critic
applies proposals as commits and refuses to run on a dirty tree."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def is_repo(repo_root: Path) -> bool:
    try:
        return _git(repo_root, "rev-parse", "--is-inside-work-tree") == "true"
    except GitError:
        return False


def is_dirty(repo_root: Path) -> bool:
    return bool(_git(repo_root, "status", "--porcelain"))


def head_short_sha(repo_root: Path) -> str:
    return _git(repo_root, "rev-parse", "--short", "HEAD")


def commit_paths(repo_root: Path, paths: list[Path], message: str) -> str:
    """Stage the given paths and commit; returns the new short sha."""
    if not is_repo(repo_root):
        raise GitError(f"{repo_root} is not a git repository")
    _git(repo_root, "add", "--", *(str(p) for p in paths))
    _git(repo_root, "commit", "-m", message, "--", *(str(p) for p in paths))
    return head_short_sha(repo_root)
