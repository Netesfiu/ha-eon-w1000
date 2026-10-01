"""DataUpdateCoordinator for the E.ON W1000 integration."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
import logging
import shutil
import tempfile
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EMAIL_SENDER,
    CONF_EMAIL_SUBJECT,
    CONF_IMAP_HOST,
    CONF_IMAP_PASS,
    CONF_IMAP_PORT,
    CONF_IMAP_USER,
    CONF_INITIAL_EXPORT,
    CONF_INITIAL_IMPORT,
    CONF_POLL_INTERVAL,
    CONF_SEARCH_DAYS,
    DEFAULT_EMAIL_SENDER,
    DEFAULT_EMAIL_SUBJECT,
    DEFAULT_INITIAL_EXPORT,
    DEFAULT_INITIAL_IMPORT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SEARCH_DAYS,
    DOMAIN,
    LEDGER_MAX_ENTRIES,
    MAX_ATTACHMENT_BYTES,
    STATISTIC_EXPORT_ID,
    STATISTIC_IMPORT_ID,
    STATISTIC_SOURCE,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .hourly import (
    RunSelection,
    StatRow,
    select_run,
    source_history,
)
from .imap_client import ImapClient, ImapError, MailAttachment, MailMessage
from .parser import ParsedHour, parse_eon_xlsx

_LOGGER = logging.getLogger(__name__)

STATUS_IDLE = "idle"
STATUS_OK = "ok"
STATUS_NO_MAIL = "no_mail"
STATUS_NO_DATA = "no_data"
STATUS_NO_ANCHOR = "no_anchor"
STATUS_PARSE_ERROR = "parse_error"
STATUS_MAIL_ERROR = "mail_error"


class EonW1000Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch E.ON export mails, compute hourly energy and import statistics."""

    def __init__(self, hass: HomeAssistant, config_data: dict[str, Any]) -> None:
        self._config = config_data
        poll_minutes = int(config_data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL) or DEFAULT_POLL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=max(poll_minutes, 5)),
        )
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._source_hours: dict[datetime, ParsedHour] = {}
        self._source_lock = asyncio.Lock()
        self._ledger: dict[str, str] = {}
        self._state: dict[str, Any] = {}
        self._force_refresh_latest = False

        self._initial_import = float(config_data.get(CONF_INITIAL_IMPORT, DEFAULT_INITIAL_IMPORT) or 0.0)
        self._initial_export = float(config_data.get(CONF_INITIAL_EXPORT, DEFAULT_INITIAL_EXPORT) or 0.0)

    # ------------------------------------------------------------------ #
    # Setup / persistence
    # ------------------------------------------------------------------ #
    async def async_setup(self) -> None:
        """Restore the processed-mail ledger and the last known totals."""
        stored = await self._store.async_load() or {}
        self._ledger = dict(stored.get("ledger") or {})
        self._state = dict(stored.get("state") or {})
        for row in stored.get("source_hours", []):
            hour = ParsedHour(**{**row, "start": dt_util.parse_datetime(row["start"])})
            self._source_hours[hour.start] = hour
        _LOGGER.debug("Restored %d ledger entries", len(self._ledger))

    async def _async_save(self) -> None:
        if len(self._ledger) > LEDGER_MAX_ENTRIES:
            for key in sorted(self._ledger, key=self._ledger.get)[: len(self._ledger) - LEDGER_MAX_ENTRIES]:
                self._ledger.pop(key, None)
        await self._store.async_save({"ledger": self._ledger, "state": self._state,
            "source_hours": [{**asdict(h), "start": h.start.isoformat()}
                             for h in self._source_hours.values()]})

    # ------------------------------------------------------------------ #
    # Public helpers
    # ------------------------------------------------------------------ #
    async def async_import_now(self) -> None:
        """Manual trigger: look at the newest mail regardless of the ledger."""
        self._force_refresh_latest = True
        await self.async_refresh()

    def _client(self) -> ImapClient:
        return ImapClient(
            host=self._config.get(CONF_IMAP_HOST, ""),
            port=int(self._config.get(CONF_IMAP_PORT, 993) or 993),
            username=self._config.get(CONF_IMAP_USER, ""),
            password=self._config.get(CONF_IMAP_PASS, ""),
            sender_filter=self._config.get(CONF_EMAIL_SENDER, DEFAULT_EMAIL_SENDER),
            subject_filter=self._config.get(CONF_EMAIL_SUBJECT, DEFAULT_EMAIL_SUBJECT),
            max_attachment_bytes=MAX_ATTACHMENT_BYTES,
        )

    # ------------------------------------------------------------------ #
    # Coordinator entry point
    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> dict[str, Any]:
        async with self._source_lock:
            return await self._async_update_source()

    async def _async_update_source(self) -> dict[str, Any]:
        force = self._force_refresh_latest
        self._force_refresh_latest = False

        workdir = await self.hass.async_add_executor_job(
            tempfile.mkdtemp, None, "eon_w1000_"
        )
        try:
            try:
                selection, meta = await self.hass.async_add_executor_job(
                    self._collect, workdir, force
                )
            except ImapError as err:
                _LOGGER.error("IMAP failure (%s): %s", err.code, err.detail)
                return self._payload(
                    status=STATUS_MAIL_ERROR,
                    error=f"IMAP {err.code}: {err.detail}",
                )

            return await self._import_selection(selection, meta)
        finally:
            await self.hass.async_add_executor_job(shutil.rmtree, workdir, True)

    async def _import_selection(self, selection, meta):
        # Persist source material first so interrupted writes can be replayed.
        await self._async_save()
        if not selection.hours:
            return self._payload(
                status=STATUS_NO_DATA,
                error=meta.get("error"),
                skipped=selection.skipped,
                meta=meta,
            )

        accumulation = source_history(selection)
        totals = accumulation.totals

        await self._push(STATISTIC_IMPORT_ID, accumulation.import_rows)
        await self._push(STATISTIC_EXPORT_ID, accumulation.export_rows)

        imported_mails = meta.get("imported_mails") or []
        await self.hass.async_add_executor_job(self._acknowledge, imported_mails)

        now = dt_util.now()
        self._state.update(
            {
                "last_processing": now.isoformat(),
                "latest_import": totals["import_total"],
                "latest_export": totals["export_total"],
                "last_window_from": selection.first.isoformat(),
                "last_window_to": selection.last.isoformat(),
                "last_raw_m180": meta.get("raw_m180"),
                "last_raw_m280": meta.get("raw_m280"),
            }
        )
        await self._async_save()

        _LOGGER.info(
            "Imported %d hourly statistics for %s..%s (%d..%d kWh import, %d..%d kWh export)",
            len(selection.hours),
            selection.first,
            selection.last,
            totals["import_energy"],
            totals["import_total"],
            totals["export_energy"],
            totals["export_total"],
        )

        return self._payload(
            status=STATUS_OK,
            skipped=selection.skipped,
            meta=meta,
            totals=totals,
        )
    async def async_import_files(self, paths: list[str]) -> dict[str, Any]:
        """Backfill local XLSX files using the same source archive as mail."""
        async with self._source_lock:
            selection, meta = await self.hass.async_add_executor_job(self._collect_files, paths)
            result = await self._import_selection(selection, meta)
            self.async_set_updated_data(result)
            return result

    def _collect_files(self, paths):
        hours = dict(self._source_hours)
        meta = {}
        for path in paths:
            self._ingest(MailAttachment(filename=Path(path).name, path=path, size=Path(path).stat().st_size), hours, meta)
        return self._select_source(hours, meta), meta

    def _select_source(self, hours, meta):
        self._source_hours = hours
        ordered = sorted(hours.values(), key=lambda hour: hour.start)
        selection = select_run(ordered, 4)
        meta["hours_seen"] = len(ordered)
        meta["hours_importable"] = len(selection.hours)
        meta.setdefault("parse_failures", 0)
        # Registers come from the archive as a whole, so a file backfill reports
        # them exactly like the mail path does.
        meta["raw_m180"] = _last_register(hours, "m180")
        meta["raw_m280"] = _last_register(hours, "m280")
        if len(selection.hours) != len(ordered) or selection.duplicates:
            meta["error"] = "Excel archive has a gap, incomplete or ambiguous hour; supply complete overlapping files"
            return RunSelection(skipped=selection.skipped)
        return selection

    # ------------------------------------------------------------------ #
    # Blocking helpers (executor thread)
    # ------------------------------------------------------------------ #
    def _collect(self, workdir: str, force: bool) -> tuple[RunSelection, dict[str, Any]]:
        """Fetch mails, parse attachments, merge hours, pick the importable run.

        ``force`` (the manual button) means *re-process the newest export*, so the
        ledger must not skip it — otherwise the button cannot repair a window that
        an earlier, broken run wrote, which is the one thing it is for.
        """
        client = self._client()
        meta: dict[str, Any] = {}
        try:
            client.connect()
            since = dt_util.now().date() - timedelta(
                days=int(self._config.get(CONF_SEARCH_DAYS, DEFAULT_SEARCH_DAYS) or DEFAULT_SEARCH_DAYS)
            )
            if force:
                latest = client.latest_uid(since)
                uids = [latest] if latest else []
            else:
                uids = client.search_uids(since)

            messages: list[MailMessage] = []
            for uid in uids:
                message = client.fetch_message(uid, workdir)
                if message is None:
                    continue
                if not force and message.message_id in self._ledger:
                    _LOGGER.debug("Skipping already processed mail %s", message.message_id)
                    meta.setdefault("known_mails", 0)
                    meta["known_mails"] += 1
                    continue
                if not message.has_attachments:
                    continue
                messages.append(message)

            meta["mails"] = len(messages)
            if not messages:
                return RunSelection(), {"mails": 0}

            hours: dict[datetime, ParsedHour] = dict(self._source_hours)
            imported_mails: list[dict[str, str]] = []
            for message in messages:
                parsed_any = False
                for attachment in message.attachments:
                    parsed_any = self._ingest(attachment, hours, meta) or parsed_any
                if parsed_any:
                    imported_mails.append({"uid": message.uid, "message_id": message.message_id})

            meta["imported_mails"] = imported_mails

            selection = self._select_source(hours, meta)
            return selection, meta
        finally:
            client.disconnect()

    def _ingest(
        self, attachment: MailAttachment, hours: dict[datetime, ParsedHour], meta: dict[str, Any]
    ) -> bool:
        try:
            result = parse_eon_xlsx(attachment.path, dt_util.DEFAULT_TIME_ZONE)
        except Exception as err:  # noqa: BLE001 - reported, mail stays unread
            _LOGGER.error("Failed to parse %s: %s", attachment.filename, err)
            meta.setdefault("parse_errors", []).append(f"{attachment.filename}: {err}")
            meta["parse_failures"] = int(meta.get("parse_failures") or 0) + 1
            return False
        meta["expected_quarters"] = max(int(meta.get("expected_quarters") or 1), result.expected_quarters)
        for hour in result.hours:
            hours[hour.start] = hour  # newer attachment wins for the same hour
        _LOGGER.debug(
            "Accepted %s: %d hourly buckets (%s)",
            attachment.filename,
            len(result.hours),
            result.export_format,
        )
        return True

    def _acknowledge(self, mails: list[dict[str, str]]) -> None:
        """Mark the imported mails as seen and record them in the ledger."""
        if not mails:
            return
        stamp = dt_util.now().isoformat()
        for entry in mails:
            self._ledger[entry["message_id"]] = stamp
        client = self._client()
        try:
            client.connect()
            client.mark_seen([entry["uid"] for entry in mails])
        except ImapError as err:
            _LOGGER.warning("Imported, but could not mark mail as seen: %s", err)
        finally:
            client.disconnect()

    # ------------------------------------------------------------------ #
    # Recorder interaction
    # ------------------------------------------------------------------ #
    async def _push(self, statistic_id: str, stats: list[StatRow]) -> None:
        """Write hourly rows into an existing recorder statistics series.

        The primary path is the ``recorder.import_statistics`` service — the very
        call the importer this replaces makes from outside, so the payload shape
        is known to be accepted.  If the service is not available (it is newer
        than some HA releases, and it can also be missing on a build without the
        recorder's service registration), fall back to the recorder's internal
        ``async_import_statistics``, which needs aware ``datetime`` starts rather
        than the ISO strings the service takes.
        """
        rows = numeric_rows(statistic_id, stats)
        payload = {
            "statistic_id": statistic_id,
            "source": STATISTIC_SOURCE,
            "unit_of_measurement": "kWh",
            "has_mean": False,
            "has_sum": True,
            "stats": rows,
        }
        if self.hass.services.has_service("recorder", "import_statistics"):
            await self.hass.services.async_call(
                "recorder", "import_statistics", payload, blocking=True
            )
            return

        # The recorder registers its services late in startup, so an import that
        # runs during startup legitimately takes this path: only warn when the
        # recorder itself is missing, since then nothing will write the series.
        log = _LOGGER.debug
        if "recorder" not in self.hass.config.components:
            log = _LOGGER.warning
        log(
            "The recorder.import_statistics service is not registered; "
            "writing %s through the recorder API instead",
            statistic_id,
        )
        await self._push_via_recorder_api(statistic_id, rows)

    async def _push_via_recorder_api(
        self, statistic_id: str, stats: list[StatRow]
    ) -> None:
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import async_import_statistics

        rows = [
            {
                "start": dt_util.parse_datetime(row["start"]),
                "state": float(row["state"]),
                "sum": float(row["sum"]),
            }
            for row in stats
        ]
        if any(row["start"] is None for row in rows):
            raise ValueError("unparsable statistics timestamp")
        metadata = {
            "has_mean": False,
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": f"E.ON W1000 {statistic_id}",
            "source": STATISTIC_SOURCE,
            "statistic_id": statistic_id,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        }
        # ``async_import_statistics`` is a plain callback that only enqueues the
        # write, so it must run on the event loop, not in the executor.
        async_import_statistics(self.hass, metadata, rows)

    # ------------------------------------------------------------------ #
    # Data payload for the entities
    # ------------------------------------------------------------------ #
    def _payload(
        self,
        *,
        status: str,
        error: str | None = None,
        skipped: list[tuple[datetime, str]] | None = None,
        meta: dict[str, Any] | None = None,
        totals: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        meta = meta or {}
        skipped = skipped or []
        payload: dict[str, Any] = {
            "status": status,
            "last_update": dt_util.now().isoformat(),
            "last_processing": self._state.get("last_processing"),
            "latest_import": self._state.get("latest_import", self._initial_import),
            "latest_export": self._state.get("latest_export", self._initial_export),
            "last_window_from": self._state.get("last_window_from"),
            "last_window_to": self._state.get("last_window_to"),
            "raw_m180": self._state.get("last_raw_m180"),
            "raw_m280": self._state.get("last_raw_m280"),
            "mails": meta.get("mails", 0),
            "hours_seen": meta.get("hours_seen", 0),
            "hours_importable": meta.get("hours_importable", 0),
            "skipped_hours": len(skipped),
            "skipped_detail": [f"{hour.isoformat()}: {reason}" for hour, reason in skipped[:5]],
            "last_error": _compose_error(error, meta),
            "parse_failures": meta.get("parse_failures", 0),
        }
        if totals:
            payload.update(totals)
        if self.data:
            for key in ("last_window_from", "last_window_to"):
                payload[key] = payload[key] or self.data.get(key)
        return payload


def _compose_error(error: str | None, meta: dict[str, Any]) -> str | None:
    """Report the selection problem *and* the parse failure that caused it.

    A run whose archive has a gap often also had a file that would not parse;
    showing only the gap ("supply complete overlapping files") hides the real
    cause, which is the unreadable attachment.
    """
    parts = [part for part in (error, meta.get("error")) if part]
    parts.extend(meta.get("parse_errors") or [])
    return "; ".join(dict.fromkeys(parts)) or None


def numeric_rows(statistic_id: str, stats: list[StatRow]) -> list[StatRow]:
    """Return statistics rows whose ``state``/``sum`` are real numbers.

    ``recorder.import_statistics`` validates them as ``float`` or ``int``, so a
    formatted string never reaches the recorder: the service schema rejects the
    whole call with ``expected float or int at 'stats[0].state'``, which says
    nothing about where the string came from.  Raise here instead, naming the
    row and the offending value.
    """
    rows: list[StatRow] = []
    for row in stats:
        state = row["state"]
        total = row["sum"]
        for label, value in (("state", state), ("sum", total)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"{statistic_id}: row {row.get('start')!r} has a non-numeric "
                    f"{label} ({value!r}, {type(value).__name__}); the statistics "
                    "writer accepts only float/int"
                )
        rows.append(
            {"start": str(row.get("start")), "state": float(state), "sum": float(total)}
        )
    return rows


def _last_register(hours: dict[datetime, ParsedHour], attribute: str) -> float | None:
    for key in sorted(hours, reverse=True):
        value = getattr(hours[key], attribute)
        if value is not None:
            return value
    return None
