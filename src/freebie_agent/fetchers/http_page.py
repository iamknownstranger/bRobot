"""Simple page-diff watcher: fetch a page (no JS rendering), strip tags, and
emit one RawItem whenever the content hash changes since the last fetch.

State (the last content hash) lives on the source's row in the `sources`
table, so restarts don't re-notify unchanged pages.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass

import httpx

from freebie_agent import ledger
from freebie_agent.models import RawItem

_MAX_BODY_CHARS = 8000
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.DOTALL | re.IGNORECASE)


def strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    return " ".join(text.split())


@dataclass
class HttpPageFetcher:
    source_id: str
    url: str
    timeout_seconds: float = 30.0

    def fetch(self, conn: sqlite3.Connection) -> list[RawItem]:
        resp = httpx.get(
            self.url,
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "freebie-agent/0.1 (+personal page watcher)"},
        )
        resp.raise_for_status()
        text = strip_html(resp.text)[:_MAX_BODY_CHARS]
        page_hash = hashlib.sha256(text.encode()).hexdigest()

        source = ledger.get_source(conn, self.source_id)
        previous = source["last_page_hash"] if source else None
        if page_hash == previous:
            return []
        ledger.set_source_page_hash(conn, self.source_id, page_hash)
        if previous is None:
            # First observation is a baseline, not a change worth an item.
            return []
        return [
            RawItem(
                url=f"{self.url}#changed-{page_hash[:12]}",
                title=f"Page changed: {self.url}",
                body=text,
                source_id=self.source_id,
            )
        ]
