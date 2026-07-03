"""RSS/Atom fetcher."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import feedparser
import httpx

from freebie_agent.models import RawItem

_MAX_BODY_CHARS = 8000


def _entry_body(entry: feedparser.FeedParserDict) -> str:
    if entry.get("content"):
        parts = entry["content"]
        if parts and parts[0].get("value"):
            return str(parts[0]["value"])[:_MAX_BODY_CHARS]
    return str(entry.get("summary", ""))[:_MAX_BODY_CHARS]


@dataclass
class RssFetcher:
    source_id: str
    url: str
    timeout_seconds: float = 30.0

    def fetch(self, conn: sqlite3.Connection) -> list[RawItem]:
        resp = httpx.get(
            self.url,
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "freebie-agent/0.1 (+personal RSS reader)"},
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if feed.get("bozo") and not feed.get("entries"):
            raise ValueError(f"unparseable feed: {feed.get('bozo_exception')}")
        items: list[RawItem] = []
        for entry in feed.entries:
            link = str(entry.get("link", "")).strip()
            title = str(entry.get("title", "")).strip()
            if not link or not title:
                continue
            items.append(
                RawItem(url=link, title=title, body=_entry_body(entry), source_id=self.source_id)
            )
        return items
