"""Button platform for the E.ON W1000 integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EonW1000Coordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([EonW1000ProcessNowButton(entry.runtime_data, entry)])


class EonW1000ProcessNowButton(CoordinatorEntity[EonW1000Coordinator], ButtonEntity):
    """Check the mailbox right now, ignoring the processed-mail ledger.

    Useful to verify the setup without waiting for the poll interval: it always
    looks at the newest matching mail, imports it if its hours are importable,
    and reports the outcome on the sensors.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "process_now"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:email-sync"

    def __init__(self, coordinator: EonW1000Coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_process_now"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="E.ON W1000",
            manufacturer="E.ON",
            model="W1000 portal export via IMAP",
        )

    async def async_press(self) -> None:
        await self.coordinator.async_import_now()
