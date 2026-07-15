"""DataUpdateCoordinator for E.ON W1000 integration."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_EMAIL_SENDER,
    CONF_EMAIL_SUBJECT,
    CONF_IMAP_HOST,
    CONF_IMAP_PASS,
    CONF_IMAP_PORT,
    CONF_IMAP_USER,
    CONF_POLL_INTERVAL,
    DEFAULT_EMAIL_SENDER,
    DEFAULT_EMAIL_SUBJECT,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    STATISTIC_EXPORT_ID,
    STATISTIC_IMPORT_ID,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .imap_client import ImapClient
from .parser import build_statistics_payload, parse_eon_xlsx

_LOGGER = logging.getLogger(__name__)


class EonW1000Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch and process E.ON W1000 data."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_data: dict[str, Any],
    ) -> None:
        """Initialize the coordinator."""
        self._config_data = config_data
        self._tzinfo = datetime.now().astimezone().tzinfo

        poll_minutes = config_data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=poll_minutes),
        )

        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
        self._processed_uids: set[str] = set()

        # Initial meter values from config
        self._initial_import = float(
            config_data.get(CONF_INITIAL_IMPORT, DEFAULT_INITIAL_IMPORT)
        )
        self._initial_export = float(
            config_data.get(CONF_INITIAL_EXPORT, DEFAULT_INITIAL_EXPORT)
        )

    async def _async_setup(self) -> None:
        """Restore persisted processed UIDs."""
        stored = await self._store.async_load()
        if stored and "processed_uids" in stored:
            self._processed_uids = set(stored["processed_uids"])
            _LOGGER.debug("Loaded %d processed email UIDs", len(self._processed_uids))

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch new emails, parse XLSX, push to recorder."""
        try:
            return await self.hass.async_add_executor_job(self._sync_update)
        except Exception as err:
            raise UpdateFailed(f"Update failed: {err}") from err

    def _sync_update(self) -> dict[str, Any]:
        """Synchronous update logic (runs in executor thread)."""
        client = ImapClient(
            host=self._config_data.get(CONF_IMAP_HOST, ""),
            port=self._config_data.get(CONF_IMAP_PORT, 993),
            username=self._config_data.get(CONF_IMAP_USER, ""),
            password=self._config_data.get(CONF_IMAP_PASS, ""),
            sender_filter=self._config_data.get(CONF_EMAIL_SENDER, DEFAULT_EMAIL_SENDER),
            subject_filter=self._config_data.get(CONF_EMAIL_SUBJECT, DEFAULT_EMAIL_SUBJECT),
        )

        try:
            client.connect()
            emails = client.fetch_unseen_attachments()
        finally:
            client.disconnect()

        if not emails:
            _LOGGER.debug("No new E.ON emails to process")
            # On first run, return initial meter values so sensors show something
            if self.data is None or self.data.get("latest_import") is None:
                return {
                    "last_update": datetime.now(tz=self._tzinfo).isoformat(),
                    "latest_import": self._initial_import,
                    "latest_export": self._initial_export,
                }
            # Keep existing data
            return self.data

        # Load current state from HA
        self._hass_import_statistics: list[dict[str, Any]] = []

        all_calculated: list[dict[str, Any]] = []

        for email_data in emails:
            for path in email_data["attachment_paths"]:
                try:
                    calculated = parse_eon_xlsx(path, self._tzinfo)
                    all_calculated.extend(calculated)
                    _LOGGER.info(
                        "Processed %s: %d hourly rows", path, len(calculated)
                    )
                except Exception as exc:
                    _LOGGER.error("Failed to parse %s: %s", path, exc)
                finally:
                    # Clean up temp file
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

        if not all_calculated:
            _LOGGER.warning("No valid data extracted from emails")
            return {"last_update": datetime.now(tz=self._tzinfo).isoformat()}

        # Deduplicate: merge all calculated rows, sort by start, keep last value per timestamp
        deduped: dict[str, dict[str, Any]] = {}
        for row in all_calculated:
            deduped[row["start"]] = row

        calculated = sorted(deduped.values(), key=lambda r: r["start"])

        # Build statistics payloads
        import_stats = build_statistics_payload(calculated, "1_8_0")
        export_stats = build_statistics_payload(calculated, "2_8_0")

        latest_import = float(calculated[-1]["1_8_0"])
        latest_export = float(calculated[-1]["2_8_0"])

        return {
            "last_update": datetime.now(tz=self._tzinfo).isoformat(),
            "import_stats": import_stats,
            "export_stats": export_stats,
            "latest_import": latest_import,
            "latest_export": latest_export,
            "row_count": len(calculated),
        }

    async def async_push_statistics(
        self, import_stats: list[dict], export_stats: list[dict]
    ) -> None:
        """Push statistics to HA recorder via import_statistics service."""
        # Import statistics
        await self.hass.services.async_call(
            "recorder",
            "import_statistics",
            {
                "statistic_id": STATISTIC_IMPORT_ID,
                "source": DOMAIN,
                "unit_of_measurement": "kWh",
                "has_mean": False,
                "has_sum": True,
                "stats": import_stats,
            },
            blocking=True,
        )

        # Export statistics
        await self.hass.services.async_call(
            "recorder",
            "import_statistics",
            {
                "statistic_id": STATISTIC_EXPORT_ID,
                "source": DOMAIN,
                "unit_of_measurement": "kWh",
                "has_mean": False,
                "has_sum": True,
                "stats": export_stats,
            },
            blocking=True,
        )

        _LOGGER.info(
            "Pushed %d import + %d export statistics", len(import_stats), len(export_stats)
        )
