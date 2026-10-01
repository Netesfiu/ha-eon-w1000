"""E.ON W1000 — import the portal's XLSX export mails into HA statistics.

Replaces the previous external importer (n8n) with the same contract:

* the hourly series written is ``sensor.grid_energy_import`` /
  ``sensor.grid_energy_export`` (``source: recorder``) — the series the Energy
  dashboard already points at, so nothing needs reconfiguring;
* the cumulative value is anchored to the hour the recorder already holds
  immediately before the imported window and accumulated in integer Wh from
  there, which is what keeps an overlapping window from moving the seam;
* a mail is only marked read *after* its hours have been imported, and the
  search is a date range rather than ``UNSEEN``, so the read flag of the mailbox
  can never decide whether data is imported.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_INITIAL_EXPORT,
    CONF_INITIAL_IMPORT,
    DEFAULT_INITIAL_EXPORT,
    DEFAULT_INITIAL_IMPORT,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import EonW1000Coordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_PROCESS_NOW = "process_now"

# Keep in step with EonW1000ConfigFlow.VERSION / MINOR_VERSION.
ENTRY_VERSION = 2
ENTRY_MINOR_VERSION = 1

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an entry created before 2.0.

    The config schema is unchanged; 2.0 only added the two bootstrap keys the
    entry keeps for compatibility (the cumulative series is anchored to the
    recorder and is never seeded from them).  Home Assistant tolerates a minor
    mismatch on its own but refuses to load an entry whose major version it
    cannot migrate, so this handler has to exist for the 1.x entry to survive
    the upgrade — and for that failure mode to stay impossible later.
    """
    if entry.version > ENTRY_VERSION:
        _LOGGER.error(
            "E.ON W1000 entry %s was written by a newer version (%s.%s) than this "
            "integration implements (%s.%s); refusing to migrate it",
            entry.entry_id,
            entry.version,
            entry.minor_version,
            ENTRY_VERSION,
            ENTRY_MINOR_VERSION,
        )
        return False

    data = dict(entry.data)
    data.setdefault(CONF_INITIAL_IMPORT, DEFAULT_INITIAL_IMPORT)
    data.setdefault(CONF_INITIAL_EXPORT, DEFAULT_INITIAL_EXPORT)
    hass.config_entries.async_update_entry(
        entry, data=data, version=ENTRY_VERSION, minor_version=ENTRY_MINOR_VERSION
    )
    _LOGGER.info(
        "Migrated E.ON W1000 entry %s from version %s.%s to %s.%s",
        entry.entry_id,
        entry.version,
        entry.minor_version,
        ENTRY_VERSION,
        ENTRY_MINOR_VERSION,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = EonW1000Coordinator(hass, {**entry.data, **entry.options})
    # Restore the processed-mail ledger before the first poll, otherwise every
    # restart would re-import the whole search window.
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_process_now(call: ServiceCall) -> None:
        entry.async_create_task(hass, coordinator.async_import_now())

    if not hass.services.has_service(DOMAIN, SERVICE_PROCESS_NOW):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PROCESS_NOW,
            _async_process_now,
            supports_response=SupportsResponse.NONE,
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and not hass.config_entries.async_entries(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_PROCESS_NOW)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Pick up changed options without a restart."""
    await hass.config_entries.async_reload(entry.entry_id)


def _entry_data(entry: ConfigEntry) -> dict[str, Any]:
    return {**entry.data, **entry.options}
