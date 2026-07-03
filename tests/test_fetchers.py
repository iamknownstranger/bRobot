"""IMAP and http page-diff fetchers, mocked transports only."""

from __future__ import annotations

import sqlite3
from email.message import EmailMessage
from typing import Any

import httpx
import pytest

from freebie_agent import ledger
from freebie_agent.fetchers.http_page import HttpPageFetcher, strip_html
from freebie_agent.fetchers.imap_inbox import ImapConfigError, ImapInboxFetcher
from freebie_agent.models import SourceStatus

# ---------------------------------------------------------------- http_page


def _serve_page(monkeypatch: pytest.MonkeyPatch, html: str) -> None:
    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, text=html, request=httpx.Request("GET", url))

    monkeypatch.setattr("freebie_agent.fetchers.http_page.httpx.get", fake_get)


def test_page_diff_baseline_then_change(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.sync_source(conn, "page", "http_page", "https://p.test", "1d", SourceStatus.ACTIVE)
    fetcher = HttpPageFetcher(source_id="page", url="https://p.test")

    _serve_page(monkeypatch, "<html><body>Offer A</body></html>")
    assert fetcher.fetch(conn) == []  # first observation = baseline, no item

    assert fetcher.fetch(conn) == []  # unchanged: nothing

    _serve_page(monkeypatch, "<html><body>Offer B is new!</body></html>")
    items = fetcher.fetch(conn)
    assert len(items) == 1
    assert items[0].source_id == "page"
    assert "Offer B" in items[0].body
    assert items[0].title.startswith("Page changed:")

    assert fetcher.fetch(conn) == []  # stable again


def test_strip_html_removes_scripts_and_tags() -> None:
    html = "<html><script>evil()</script><style>x{}</style><p>Real <b>text</b></p></html>"
    assert strip_html(html) == "Real text"


# --------------------------------------------------------------------- imap


class FakeImap:
    """Just enough of imaplib.IMAP4_SSL for the fetcher."""

    def __init__(self, messages: list[EmailMessage]) -> None:
        self.messages = messages
        self.logged_in: tuple[str, str] | None = None
        self.stored_flags: list[tuple[bytes, str, str]] = []

    def __enter__(self) -> FakeImap:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def select(self, folder: str) -> None:
        self.folder = folder

    def search(self, charset: None, criteria: str) -> tuple[str, list[bytes]]:
        assert criteria == "UNSEEN"
        nums = b" ".join(str(i + 1).encode() for i in range(len(self.messages)))
        return "OK", [nums]

    def fetch(self, num: bytes, spec: str) -> tuple[str, list[Any]]:
        msg = self.messages[int(num) - 1]
        return "OK", [(b"1 (RFC822 ...)", msg.as_bytes())]

    def store(self, num: bytes, op: str, flags: str) -> None:
        self.stored_flags.append((num, op, flags))


def _email(subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "deals@newsletter.test"
    msg["Message-ID"] = f"<{subject.replace(' ', '-')}@test>"
    msg.set_content(body)
    return msg


def test_imap_fetch_reads_unseen_and_marks_seen(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FREEBIE_IMAP_HOST", "imap.test")
    monkeypatch.setenv("FREEBIE_IMAP_USER", "freebies@test")
    monkeypatch.setenv("FREEBIE_IMAP_PASSWORD", "hunter2secret")

    fake = FakeImap(
        [
            _email("Free coffee sampler inside", "Claim your 100g sampler worth Rs 450."),
            _email("Weekly deals digest", "Ten new freebies this week."),
        ]
    )
    monkeypatch.setattr("freebie_agent.fetchers.imap_inbox.imaplib.IMAP4_SSL", lambda host: fake)

    fetcher = ImapInboxFetcher(source_id="inbox", folder="INBOX")
    items = fetcher.fetch(conn)

    assert fake.logged_in == ("freebies@test", "hunter2secret")
    assert [i.title for i in items] == ["Free coffee sampler inside", "Weekly deals digest"]
    assert "sampler" in items[0].body
    assert items[0].url.startswith("imap://INBOX/")
    # marked Seen so re-fetch won't re-read
    assert [f[2] for f in fake.stored_flags] == ["\\Seen", "\\Seen"]


def test_imap_missing_credentials_fails_fast(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("FREEBIE_IMAP_HOST", "FREEBIE_IMAP_USER", "FREEBIE_IMAP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    fetcher = ImapInboxFetcher(source_id="inbox")
    with pytest.raises(ImapConfigError, match="FREEBIE_IMAP"):
        fetcher.fetch(conn)
