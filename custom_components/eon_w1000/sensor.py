"""Sensor platform for E.ON W1000 integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import STATISTIC_EXPORT_ID, STATISTIC_IMPORT_ID

if TYPE_CHECKING:
    from .coordinator import EonW1000Coordinator


@dataclass(frozen=True, kw_only=True)
class EonW1000SensorDescription(SensorEntityDescription):
    """Description for E.ON W1000 energy sensors."""

    statistic_id: str = ""
    data_key: str = ""


ENERGY_SENSORS: tuple[EonW1000SensorDescription, ...] = (
    EonW1000SensorDescription(
        key="grid_import",
        statistic_id=STATISTIC_IMPORT_ID,
        data_key="latest_import",
        translation_key="grid_import",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    EonW1000SensorDescription(
        key="grid_export",
        statistic_id=STATISTIC_EXPORT_ID,
        data_key="latest_export",
        translation_key="grid_export",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)

DIAG_SENSORS: tuple[EonW1000SensorDescription, ...] = (
    EonW1000SensorDescription(
        key="last_update",
        data_key="last_update",
        translation_key="last_update",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    EonW1000SensorDescription(
        key="last_processing",
        data_key="last_processing",
        translation_key="last_processing",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up E.ON W1000 sensors."""
    coordinator = entry.runtime_data

    entities: list[SensorEntity] = []
    for desc in ENERGY_SENSORS:
        entities.append(EonW1000Sensor(coordinator, desc))
    for desc in DIAG_SENSORS:
        entities.append(EonW1000DiagSensor(coordinator, desc))

    async_add_entities(entities)


class EonW1000Sensor(CoordinatorEntity["EonW1000Coordinator"], SensorEntity):
    """Sensor for E.ON W1000 energy meter readings."""

    entity_description: EonW1000SensorDescription

    def __init__(
        self,
        coordinator: EonW1000Coordinator,  # noqa: F821 — TYPE_CHECKING
        description: EonW1000SensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"eon_w1000_{description.key}"

    @property
    def native_value(self) -> float | None:
        """Return the current meter reading."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.data_key)


class EonW1000DiagSensor(CoordinatorEntity["EonW1000Coordinator"], SensorEntity):
    """Diagnostic sensor for E.ON W1000 timestamps."""

    entity_description: EonW1000SensorDescription

    def __init__(
        self,
        coordinator: EonW1000Coordinator,  # noqa: F821
        description: EonW1000SensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"eon_w1000_{description.key}"

    @property
    def native_value(self) -> datetime | None:
        """Return the timestamp."""
        if self.coordinator.data is None:
            return None
        raw = self.coordinator.data.get(self.entity_description.data_key)
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str):
            return datetime.fromisoformat(raw)
        return None
