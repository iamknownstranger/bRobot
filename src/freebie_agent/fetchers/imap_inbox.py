"""IMAP fetcher for a dedicated freebie inbox.

Reads only; never sends, never deletes. Credentials come exclusively from
environment variables (FREEBIE_IMAP_HOST/USER/PASSWORD) — never from
sources.yaml or the DB. Processed messages are flagged Seen so re-fetches
only pick up new mail.
"""

from __future__ import annotations

import email.policy
import imaplib
import os
import sqlite3
from dataclasses import dataclass
from email.message import EmailMessage
from email.parser import BytesParser

from freebie_agent.models import RawItem

_MAX_BODY_CHARS = 8000


class ImapConfigError(Exception):
    """Missing IMAP env vars for a configured imap source (fail fast)."""


def _plain_body(msg: EmailMessage) -> str:
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    content = body.get_content()
    return str(content)[:_MAX_BODY_CHARS]


@dataclass
class ImapInboxFetcher:
    source_id: str
    folder: str = "INBOX"

    def _credentials(self) -> tuple[str, str, str]:
        host = os.environ.get("FREEBIE_IMAP_HOST", "").strip()
        user = os.environ.get("FREEBIE_IMAP_USER", "").strip()
        password = os.environ.get("FREEBIE_IMAP_PASSWORD", "").strip()
        if not (host and user and password):
            raise ImapConfigError(
                "imap source configured but FREEBIE_IMAP_HOST/USER/PASSWORD not set"
            )
        return host, user, password

    def fetch(self, conn: sqlite3.Connection) -> list[RawItem]:
        host, user, password = self._credentials()
        items: list[RawItem] = []
        with imaplib.IMAP4_SSL(host) as imap:
            imap.login(user, password)
            imap.select(self.folder)
            status, data = imap.search(None, "UNSEEN")
            if status != "OK":
                raise ValueError(f"imap search failed: {status}")
            for num in data[0].split():
                status, fetched = imap.fetch(num, "(RFC822)")
                if status != "OK" or not fetched or fetched[0] is None:
                    continue
                payload = fetched[0]
                raw_bytes = payload[1] if isinstance(payload, tuple) else bytes(payload)
                msg = BytesParser(EmailMessage, policy=email.policy.default).parsebytes(raw_bytes)
                subject = str(msg.get("Subject", "")).strip()
                message_id = str(msg.get("Message-ID", "")).strip() or subject
                if not subject:
                    continue
                items.append(
                    RawItem(
                        url=f"imap://{self.folder}/{message_id}",
                        title=subject,
                        body=_plain_body(msg),
                        source_id=self.source_id,
                    )
                )
                imap.store(num, "+FLAGS", "\\Seen")
        return items
