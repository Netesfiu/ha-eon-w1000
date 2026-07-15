"""E.ON W1000 integration for Home Assistant.

Fetches E.ON energy meter data from email XLSX attachments and imports
them into Home Assistant's energy statistics.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN, PLATFORMS
from .coordinator import EonW1000Coordinator

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

    class EonW1000ConfigEntry(ConfigEntry):
        """Typed config entry for E.ON W1000."""

        runtime_data: EonW1000Coordinator


_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: "ConfigType") -> bool:
    """Set up the E.ON W1000 component (YAML-based setup not supported)."""
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: "EonW1000ConfigEntry"
) -> bool:
    """Set up E.ON W1000 from a config entry."""
    coordinator = EonW1000Coordinator(hass, entry.data)
    await coordinator._async_setup()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Perform initial refresh (coordinator auto-pushes statistics)
    await coordinator.async_config_entry_first_refresh()

    # Update listener for options changes
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # Register services
    async def _handle_process_now(call: ServiceCall) -> None:
        """Manually trigger a data refresh."""
        _LOGGER.info("Manual refresh triggered")
        await coordinator.async_refresh()

    hass.services.async_register(DOMAIN, "process_now", _handle_process_now)

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: "EonW1000ConfigEntry"
) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, "process_now")
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant, entry: "EonW1000ConfigEntry"
) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
