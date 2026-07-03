"""Pipeline behavior: fixture RSS entries end-to-end with a mocked LLM produce
the correct DB states; dedupe; routing buckets; failure isolation."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest

from freebie_agent import ledger, pipeline
from freebie_agent.config import Config
from freebie_agent.fetchers.rss import RssFetcher
from freebie_agent.llm import LLMFormatError
from freebie_agent.models import ItemStatus, RawItem, SourceStatus
from freebie_agent.router import Bucket

from .mocks import FakeLLM, scores_by_title

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>deals</title>
<item><title>Gem: free 1-yr membership bundled with card</title>
  <link>https://deals.test/gem</link>
  <description>Worth Rs 1199, two minutes to activate.</description></item>
<item><title>Middling: ebook bundle for newsletter signup</title>
  <link>https://deals.test/mid</link>
  <description>Eight ebooks, adds you to a marketing list.</description></item>
<item><title>Junk: free t-shirt for 90-minute webinar</title>
  <link>https://deals.test/junk</link>
  <description>Business email required, decision makers only.</description></item>
</channel></rss>"""


@pytest.fixture
def rss_source(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> RssFetcher:
    ledger.sync_source(
        conn, "test-rss", "rss", "https://deals.test/feed", "1h", SourceStatus.ACTIVE
    )

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=RSS_XML.encode(), request=request)

    monkeypatch.setattr("freebie_agent.fetchers.rss.httpx.get", fake_get)
    return RssFetcher(source_id="test-rss", url="https://deals.test/feed")


def _run_pass(conn: sqlite3.Connection, cfg: Config, fetcher: RssFetcher) -> FakeLLM:
    llm = FakeLLM(
        score_fn=scores_by_title({"Gem": 92, "Middling": 63, "Junk": 8}),
    )
    pipeline.run_pass(conn, cfg, llm, [fetcher])  # type: ignore[arg-type]
    return llm


def test_three_rss_entries_end_to_end(
    conn: sqlite3.Connection, cfg: Config, rss_source: RssFetcher
) -> None:
    _run_pass(conn, cfg, rss_source)

    items = {r["url"]: r for r in conn.execute("SELECT * FROM items")}
    assert len(items) == 3
    gem = items["https://deals.test/gem"]
    mid = items["https://deals.test/mid"]
    junk = items["https://deals.test/junk"]

    assert gem["status"] == ItemStatus.QUEUED.value
    assert mid["status"] == ItemStatus.DIGESTED.value
    assert junk["status"] == ItemStatus.DROPPED.value

    # push: claim proposed + outbox notify
    claim = ledger.claim_for_item(conn, int(gem["id"]))
    assert claim is not None and claim["state"] == "proposed"
    outbox = ledger.unsent_outbox(conn)
    assert [o["kind"] for o in outbox] == ["notify"]
    assert json.loads(outbox[0]["payload"])["item_id"] == gem["id"]

    # scores stored with prompt version + model tag
    score = ledger.latest_score(conn, int(gem["id"]))
    assert score is not None and score["score"] == 92
    assert score["model_tag"] == "mock-model"
    assert score["prompt_version"]

    # counters
    counters = ledger.counters_since(conn, ledger.utcnow().replace(hour=0, minute=0))
    assert counters["items_fetched"] == 3
    assert counters["items_extracted"] == 3
    assert counters["items_scored"] == 3
    assert counters["items_queued"] == 1
    assert counters["items_digested"] == 1
    assert counters["items_dropped"] == 1

    # source stats
    source = ledger.get_source(conn, "test-rss")
    assert source is not None
    assert source["items_seen"] == 3
    assert source["items_queued"] == 1
    assert source["last_ok_at"] is not None


def test_dedupe_reseen_items_refresh_fetched_at_only(
    conn: sqlite3.Connection, cfg: Config, rss_source: RssFetcher
) -> None:
    _run_pass(conn, cfg, rss_source)
    first = {r["id"]: r["fetched_at"] for r in conn.execute("SELECT id, fetched_at FROM items")}

    import time

    time.sleep(0.01)
    _run_pass(conn, cfg, rss_source)

    rows = list(conn.execute("SELECT id, fetched_at, status FROM items"))
    assert len(rows) == 3  # no duplicates
    for row in rows:
        assert row["fetched_at"] >= first[row["id"]]
    # statuses were not reset to new; no second claim/outbox row appeared
    assert conn.execute("SELECT COUNT(*) AS n FROM claims").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"] == 1


def test_dedupe_hash_normalizes_url_and_title() -> None:
    a = ledger.dedupe_hash("https://X.test/Deal/", "  Free   Thing ")
    b = ledger.dedupe_hash("https://x.test/deal", "free thing")
    assert a == b
    assert ledger.dedupe_hash("https://x.test/deal", "other") != a


def test_extract_failure_drops_item_not_batch(
    conn: sqlite3.Connection, cfg: Config, rss_source: RssFetcher
) -> None:
    calls = {"n": 0}

    def flaky_extract(payload: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if "Gem" in str(payload["title"]):
            raise LLMFormatError("invalid JSON after 3 attempts")
        from .mocks import default_extract

        return default_extract(payload)

    llm = FakeLLM(extract_fn=flaky_extract, score_fn=lambda p: {"score": 55, "reason": "ok"})
    pipeline.run_pass(conn, cfg, llm, [rss_source])  # type: ignore[arg-type]

    rows = {r["url"]: r for r in conn.execute("SELECT * FROM items")}
    gem = rows["https://deals.test/gem"]
    assert gem["status"] == ItemStatus.DROPPED.value
    assert "invalid JSON" in gem["extract_error"]
    # the other two were still extracted and scored
    assert rows["https://deals.test/mid"]["status"] == ItemStatus.DIGESTED.value
    assert rows["https://deals.test/junk"]["status"] == ItemStatus.DIGESTED.value


def test_source_failure_is_isolated(conn: sqlite3.Connection, cfg: Config) -> None:
    ledger.sync_source(conn, "bad", "rss", "https://bad.test", "1h", SourceStatus.ACTIVE)
    ledger.sync_source(conn, "good", "rss", "https://good.test", "1h", SourceStatus.ACTIVE)

    class BadFetcher:
        source_id = "bad"

        def fetch(self, conn: sqlite3.Connection) -> list[RawItem]:
            raise RuntimeError("connection refused")

    class GoodFetcher:
        source_id = "good"

        def fetch(self, conn: sqlite3.Connection) -> list[RawItem]:
            return [RawItem("https://good.test/a", "A freebie", "body", "good")]

    llm = FakeLLM(score_fn=lambda p: {"score": 55, "reason": "ok"})
    stats = pipeline.run_pass(conn, cfg, llm, [BadFetcher(), GoodFetcher()])  # type: ignore[arg-type]
    assert stats["fetched"] == 1

    bad = ledger.get_source(conn, "bad")
    good = ledger.get_source(conn, "good")
    assert bad is not None and "connection refused" in bad["last_error"]
    assert good is not None and good["last_error"] is None and good["items_seen"] == 1


def test_push_budget_demotes_to_digest(conn: sqlite3.Connection, cfg: Config) -> None:
    ledger.sync_source(conn, "src", "rss", "u", "1h", SourceStatus.ACTIVE)
    # exhaust the daily budget with already-sent pushes
    for _ in range(cfg.pushes_per_day):
        ledger.add_event(conn, "push_sent", {})
    llm = FakeLLM(score_fn=lambda p: {"score": 95, "reason": "great"})

    class OneItem:
        source_id = "src"

        def fetch(self, conn: sqlite3.Connection) -> list[RawItem]:
            return [RawItem("https://x.test/1", "Great deal", "body", "src")]

    pipeline.run_pass(conn, cfg, llm, [OneItem()])  # type: ignore[arg-type]
    item = conn.execute("SELECT * FROM items").fetchone()
    assert item["status"] == ItemStatus.DIGESTED.value
    demoted = list(conn.execute("SELECT * FROM events WHERE kind='route_demoted'"))
    assert len(demoted) == 1
    assert json.loads(demoted[0]["payload"])["reason"] == "push_budget_exhausted"


def test_shadow_source_scored_but_never_notified(conn: sqlite3.Connection, cfg: Config) -> None:
    ledger.sync_source(conn, "shadow-src", "rss", "u", "1h", SourceStatus.SHADOW)
    llm = FakeLLM(score_fn=lambda p: {"score": 95, "reason": "great"})

    class OneItem:
        source_id = "shadow-src"

        def fetch(self, conn: sqlite3.Connection) -> list[RawItem]:
            return [RawItem("https://x.test/2", "Shadow deal", "body", "shadow-src")]

    pipeline.run_pass(conn, cfg, llm, [OneItem()])  # type: ignore[arg-type]
    item = conn.execute("SELECT * FROM items").fetchone()
    # scored and logged...
    assert ledger.latest_score(conn, int(item["id"])) is not None
    # ...but never notified
    assert item["status"] == ItemStatus.DROPPED.value
    assert ledger.unsent_outbox(conn) == []


def test_digest_collects_band_and_marks_included(
    conn: sqlite3.Connection, cfg: Config, rss_source: RssFetcher
) -> None:
    _run_pass(conn, cfg, rss_source)
    digest = pipeline.build_digest(conn, cfg)
    assert digest is not None
    assert [i["url"] for i in digest["items"]] == ["https://deals.test/mid"]
    # second digest run has nothing new
    assert pipeline.build_digest(conn, cfg) is None


def test_bucket_thresholds(cfg: Config) -> None:
    from freebie_agent.router import bucket_for_score

    assert bucket_for_score(80, cfg.thresholds) is Bucket.PUSH
    assert bucket_for_score(100, cfg.thresholds) is Bucket.PUSH
    assert bucket_for_score(79, cfg.thresholds) is Bucket.DIGEST
    assert bucket_for_score(50, cfg.thresholds) is Bucket.DIGEST
    assert bucket_for_score(49, cfg.thresholds) is Bucket.DROP
    assert bucket_for_score(0, cfg.thresholds) is Bucket.DROP
