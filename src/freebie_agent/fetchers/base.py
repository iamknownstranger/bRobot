"""Fetcher protocol: fetch() -> list[RawItem]."""

from __future__ import annotations

import sqlite3
from typing import Protocol, runtime_checkable

from freebie_agent.models import RawItem


@runtime_checkable
class Fetcher(Protocol):
    """One configured source. Implementations must be side-effect-free apart
    from network reads and (for stateful fetchers) their own bookkeeping via
    the connection passed in."""

    source_id: str

    def fetch(self, conn: sqlite3.Connection) -> list[RawItem]:
        """Return newly seen raw items. Raise on failure; the pipeline
        isolates per-source errors."""
        ...
