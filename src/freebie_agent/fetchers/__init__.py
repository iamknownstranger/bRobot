"""Fetchers: one module per source type. `build_fetcher` maps a sources.yaml
entry to a Fetcher instance."""

from __future__ import annotations

from typing import Any

from freebie_agent.fetchers.base import Fetcher
from freebie_agent.fetchers.http_page import HttpPageFetcher
from freebie_agent.fetchers.imap_inbox import ImapInboxFetcher
from freebie_agent.fetchers.rss import RssFetcher


def build_fetcher(source: dict[str, Any]) -> Fetcher:
    type_ = str(source.get("type", ""))
    source_id = str(source["id"])
    url = str(source.get("url", ""))
    if type_ == "rss":
        return RssFetcher(source_id=source_id, url=url)
    if type_ == "imap":
        return ImapInboxFetcher(source_id=source_id, folder=str(source.get("folder", "INBOX")))
    if type_ == "http_page":
        return HttpPageFetcher(source_id=source_id, url=url)
    raise ValueError(f"unknown source type {type_!r} for source {source_id!r}")


__all__ = ["Fetcher", "HttpPageFetcher", "ImapInboxFetcher", "RssFetcher", "build_fetcher"]
