"""IMAP client for E.ON W1000 export mails.

Two rules this client exists for:

* **Nothing is consumed before it is imported.**  The messages are only marked
  ``\\Seen`` by an explicit :meth:`mark_seen` call *after* a successful import.
  A parse or anchor failure therefore leaves the mail for the next poll instead
  of losing the window forever.
* **Read state is not a filter.**  Searching only for ``UNSEEN`` mail silently
  loses data whenever any other consumer (an n8n Gmail trigger, a phone client)
  marks the mail read first.  The search is a bounded date range, and duplicates
  are filtered by ``Message-ID`` in the coordinator's ledger.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_date(value: date) -> str:
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year}"


@dataclass
class MailAttachment:
    """One saved attachment from one mail."""

    filename: str
    path: str
    size: int


@dataclass
class MailMessage:
    """A fetched E.ON export mail."""

    uid: str
    message_id: str
    subject: str
    date: str
    attachments: list[MailAttachment] = field(default_factory=list)

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)


class ImapError(Exception):
    """Raised for a connection/login failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ImapClient:
    """IMAP client for fetching E.ON export mails with XLSX attachments."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        sender_filter: str = "noreply@eon.com",
        subject_filter: str = "[EON-W1000]",
        max_attachment_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender_filter = sender_filter
        self._subject_filter = subject_filter
        self._max_attachment_bytes = max_attachment_bytes
        self._conn: imaplib.IMAP4_SSL | None = None

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        """Connect, log in and select the INBOX."""
        try:
            self._conn = imaplib.IMAP4_SSL(self._host, self._port)
        except OSError as err:
            raise ImapError("connect", str(err)) from err
        try:
            self._conn.login(self._username, self._password)
        except imaplib.IMAP4.error as err:
            raise ImapError("auth", str(err)) from err
        try:
            self._conn.select("INBOX")
        except imaplib.IMAP4.error as err:
            raise ImapError("connect", str(err)) from err

    def disconnect(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:  # noqa: BLE001 - never mask the real error
                pass
            self._conn = None

    def test_connection(self) -> tuple[bool, str]:
        """Return ``(ok, code)`` where code is a translation key, not prose."""
        try:
            connection = imaplib.IMAP4_SSL(self._host, self._port)
        except OSError:
            return False, "imap_connect"
        try:
            connection.login(self._username, self._password)
        except imaplib.IMAP4.error:
            return False, "imap_auth"
        except OSError:
            return False, "imap_connect"
        try:
            connection.select("INBOX")
        except imaplib.IMAP4.error:
            return False, "imap_connect"
        finally:
            try:
                connection.logout()
            except Exception:  # noqa: BLE001
                pass
        return True, "ok"

    # ------------------------------------------------------------------ #
    # Search / fetch
    # ------------------------------------------------------------------ #
    def build_criteria(self, since: date) -> str:
        return (
            f'(SINCE "{_imap_date(since)}" FROM "{self._sender_filter}" '
            f'SUBJECT "{self._subject_filter}")'
        )

    def search_uids(self, since: date) -> list[str]:
        """UIDs of matching mails since the given date (read state irrelevant)."""
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        criteria = self.build_criteria(since)
        _LOGGER.debug("IMAP search: %s", criteria)
        status, data = self._conn.uid("SEARCH", None, criteria)
        if status != "OK":
            raise ImapError("search", str(status))
        raw = data[0].decode() if data and data[0] else ""
        return raw.split()

    def fetch_message(self, uid: str, directory: str | None = None) -> MailMessage | None:
        """Fetch one mail and save its XLSX attachments under ``directory``."""
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        status, data = self._conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            _LOGGER.warning("Could not fetch mail uid=%s (%s)", uid, status)
            return None

        raw = data[0][1]
        if not isinstance(raw, (bytes, bytearray)):
            return None
        message = email.message_from_bytes(bytes(raw))

        message_id = (message.get("Message-ID") or "").strip() or f"uid:{uid}"

        target_dir = directory or tempfile.mkdtemp(prefix="eon_w1000_")
        attachments: list[MailAttachment] = []
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            filename = part.get_filename()
            if not filename:
                continue
            if not filename.lower().endswith((".xlsx", ".xls")):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if len(payload) > self._max_attachment_bytes:
                _LOGGER.error(
                    "Attachment %s from uid=%s is %d bytes; refusing to process",
                    filename,
                    uid,
                    len(payload),
                )
                continue
            handle, path = tempfile.mkstemp(
                prefix="eon_w1000_", suffix=Path(filename).suffix, dir=target_dir
            )
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
            attachments.append(MailAttachment(filename=filename, path=path, size=len(payload)))
            _LOGGER.debug("Saved attachment %s -> %s (uid=%s)", filename, path, uid)

        return MailMessage(
            uid=uid,
            message_id=message_id,
            subject=_decode_header(message.get("Subject")),
            date=str(message.get("Date") or ""),
            attachments=attachments,
        )

    def mark_seen(self, uids: list[str]) -> None:
        """Flag mails as read. Only ever called after a successful import."""
        if not uids:
            return
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        for uid in uids:
            try:
                self._conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            except imaplib.IMAP4.error as err:
                _LOGGER.warning("Could not mark uid=%s as seen: %s", uid, err)

    def latest_uid(self, since: date) -> str | None:
        """Highest UID of the matching mails — used by the manual button."""
        uids = self.search_uids(since)
        return uids[-1] if uids else None


def _decode_header(value: Any) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except (UnicodeDecodeError, LookupError, ValueError):
        return str(value)
