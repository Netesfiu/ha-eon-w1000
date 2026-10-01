"""Historical Energy entities, with exactly one writer: the XLSX importer.

Live state stays unknown intentionally: publishing a delayed cumulative total
as a current numeric state would make Recorder manufacture processing-time
consumption. The latest Excel total is exposed as historical_total instead.
The imported has_sum/kWh metadata makes the own series Energy-selectable.
HA may show an unavailable-live-state warning; historical graphs still work.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    SENSOR_GRID_EXPORT,
    SENSOR_GRID_IMPORT,
    SENSOR_LAST_PROCESSING,
    SENSOR_LAST_UPDATE,
    STATISTIC_EXPORT_ID,
    STATISTIC_IMPORT_ID,
)
from .coordinator import EonW1000Coordinator

_ATTRIBUTE_KEYS = (
    "status",
    "last_update",
    "last_processing",
    "last_window_from",
    "last_window_to",
    "mails",
    "hours_seen",
    "hours_importable",
    "skipped_hours",
    "duplicate_hours",
    "parse_failures",
    "last_error",
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EonW1000Coordinator = entry.runtime_data
    async_add_entities(
        [
            EonW1000EnergySensor(coordinator, entry, SENSOR_GRID_IMPORT),
            EonW1000EnergySensor(coordinator, entry, SENSOR_GRID_EXPORT),
            EonW1000TimestampSensor(coordinator, entry, SENSOR_LAST_UPDATE),
            EonW1000TimestampSensor(coordinator, entry, SENSOR_LAST_PROCESSING),
        ]
    )


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="E.ON W1000",
        manufacturer="E.ON",
        model="W1000 portal export via IMAP",
    )


class EonW1000EnergySensor(CoordinatorEntity[EonW1000Coordinator], SensorEntity):
    """The last imported cumulative meter total, with import diagnostics."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "kWh"
    _attr_entity_category = None

    def __init__(
        self, coordinator: EonW1000Coordinator, entry: ConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = _device_info(entry)
        self._value_key = (
            "latest_import" if key == SENSOR_GRID_IMPORT else "latest_export"
        )
        self._register_key = "raw_m180" if key == SENSOR_GRID_IMPORT else "raw_m280"
        self._statistic_id = (
            STATISTIC_IMPORT_ID if key == SENSOR_GRID_IMPORT else STATISTIC_EXPORT_ID
        )

    @property
    def native_value(self) -> float | None:
        return None  # history is not a measurement at the current time

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data: dict[str, Any] = self.coordinator.data or {}
        attributes: dict[str, Any] = {
            "statistic_id": self._statistic_id,
            "historical_total": data.get(self._value_key),
            "history_source": "excel_only",
            "raw_meter_register": data.get(self._register_key),
        }
        for key in _ATTRIBUTE_KEYS:
            attributes[key] = data.get(key)
        attributes["skipped_detail"] = data.get("skipped_detail") or []
        return attributes


class EonW1000TimestampSensor(CoordinatorEntity[EonW1000Coordinator], SensorEntity):
    """When the mailbox was last polled, and when the last import succeeded."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: EonW1000Coordinator, entry: ConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> Any:
        raw = (self.coordinator.data or {}).get(self._key)
        if raw is None or not isinstance(raw, str):
            return raw
        return dt_util.parse_datetime(raw)
