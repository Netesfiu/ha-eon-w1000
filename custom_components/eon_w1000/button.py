"""Button platform for E.ON W1000 integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from .coordinator import EonW1000Coordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up E.ON W1000 button."""
    coordinator = entry.runtime_data
    async_add_entities([EonW1000ProcessButton(coordinator)])


class EonW1000ProcessButton(CoordinatorEntity["EonW1000Coordinator"], ButtonEntity):
    """Button to manually trigger email processing."""

    _attr_has_entity_name = True
    _attr_translation_key = "process_now"

    def __init__(
        self,
        coordinator: EonW1000Coordinator,  # noqa: F821
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._attr_unique_id = "eon_w1000_process_now"

    async def async_press(self) -> None:
        """Handle button press — fetch and process the latest email immediately."""
        self.coordinator._force_refresh_latest = True
        await self.coordinator.async_refresh()
        data = self.coordinator.data
        if data and data.get("import_stats"):
            await self.coordinator.async_push_statistics(
                data["import_stats"], data["export_stats"]
            )
