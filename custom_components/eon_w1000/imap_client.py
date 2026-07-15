"""IMAP client for fetching E.ON W1000 export emails."""

from __future__ import annotations

import email
import imaplib
import logging
import tempfile
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


class ImapClient:
    """IMAP client for fetching E.ON export emails with XLSX attachments."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        sender_filter: str = "noreply@eon.com",
        subject_filter: str = "[EON-W1000]",
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender_filter = sender_filter
        self._subject_filter = subject_filter
        self._conn: imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        """Connect and login to the IMAP server."""
        _LOGGER.debug("Connecting to IMAP %s:%d", self._host, self._port)
        self._conn = imaplib.IMAP4_SSL(self._host, self._port)
        self._conn.login(self._username, self._password)
        self._conn.select("INBOX")

    def disconnect(self) -> None:
        """Logout and close the connection."""
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def _ensure_connected(self) -> None:
        if self._conn is None:
            self.connect()

    def fetch_unseen_attachments(self) -> list[dict[str, Any]]:
        """Fetch unseen emails matching the sender/subject filters.

        Returns list of dicts with keys:
          - msg_id: IMAP message UID
          - subject: email subject
          - date: email date (ISO string)
          - attachment_paths: list of paths to saved XLSX attachments
        """
        self._ensure_connected()
        assert self._conn is not None

        # Search for unseen messages from the sender with the subject
        search_criteria = f'(UNSEEN FROM "{self._sender_filter}" SUBJECT "{self._subject_filter}")'
        _LOGGER.debug("IMAP search: %s", search_criteria)

        typ, data = self._conn.uid("SEARCH", None, search_criteria)
        if typ != "OK":
            _LOGGER.error("IMAP search failed: %s", typ)
            return []

        uid_str = data[0].decode() if data and data[0] else ""
        if not uid_str.strip():
            _LOGGER.debug("No unseen E.ON emails found")
            return []

        uids = uid_str.split()
        _LOGGER.info("Found %d unseen E.ON email(s)", len(uids))

        results: list[dict[str, Any]] = []
        for uid in uids:
            result = self._fetch_and_save_attachments(uid.decode())
            if result is not None and result["attachment_paths"]:
                results.append(result)
                # Mark as seen after processing
                self._conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")

        return results

    def _fetch_and_save_attachments(self, uid: str) -> dict[str, Any] | None:
        """Fetch a single email by UID and save XLSX attachments to temp files."""
        assert self._conn is not None

        typ, data = self._conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not data or not data[0]:
            return None

        # Parse the raw email
        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = str(email.header.decode_header(msg["Subject"] or "")[0][0] or "")
        date_str = msg["Date"] or ""

        attachment_paths: list[str] = []
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue

            filename = part.get_filename()
            if not filename:
                continue

            lower = filename.lower()
            if not (lower.endswith(".xlsx") or lower.endswith(".xls")):
                continue

            # Save to temp file
            payload = part.get_payload(decode=True)
            if not payload:
                continue

            suffix = Path(filename).suffix
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.write(payload)
            tmp.close()
            attachment_paths.append(tmp.name)
            _LOGGER.info(
                "Saved attachment %s → %s (uid=%s)", filename, tmp.name, uid
            )

        if not attachment_paths:
            _LOGGER.debug("No XLSX attachment in email uid=%s", uid)
            return None

        return {
            "msg_uid": uid,
            "subject": subject,
            "date": date_str,
            "attachment_paths": attachment_paths,
        }

    def test_connection(self) -> tuple[bool, str]:
        """Test if the IMAP connection works. Returns (success, message)."""
        try:
            conn = imaplib.IMAP4_SSL(self._host, self._port)
            conn.login(self._username, self._password)
            conn.select("INBOX")
            conn.logout()
            return True, "Sikeres kapcsolódás"
        except imaplib.IMAP4.error as e:
            return False, f"IMAP hiba: {e}"
        except OSError as e:
            return False, f"Kapcsolódási hiba: {e}"
