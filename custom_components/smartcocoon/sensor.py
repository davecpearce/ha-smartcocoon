"""Support for SmartCocoon paired temperature and humidity sensors.

SmartCocoon's external sensor is paired to a room, and the cloud reports it
inside that room's entry in the `rooms` response (ha-smartcocoon#422). Its
temperature, humidity, battery voltage and last-seen entities are attached
to the room's fan device; a sensor in a room without a fan gets its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from pysmartcocoon.manager import SmartCocoonManager

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SmartCocoonController
from .const import DOMAIN
from .coordinator import SmartCocoonRoomCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SmartCocoonSensorDescription(SensorEntityDescription):  # type: ignore[misc]
    """Describe one reading of a paired sensor."""

    value_fn: Callable[[Any], float | datetime | None]


SENSOR_DESCRIPTIONS: tuple[SmartCocoonSensorDescription, ...] = (
    SmartCocoonSensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda sensor: sensor.temperature,
    ),
    SmartCocoonSensorDescription(
        key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        value_fn=lambda sensor: sensor.humidity,
    ),
    SmartCocoonSensorDescription(
        # The API's battery_level reads as millivolts; the library converts.
        # It is a voltage rather than a percentage because the discharge
        # curve of the cell is unknown.
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda sensor: sensor.battery_voltage,
    ),
    SmartCocoonSensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda sensor: sensor.last_connection,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Callable[[list[SmartCocoonSensor]], None],
) -> None:
    """Add an entity for each reading of each paired sensor.

    Sensors are found at setup. One paired later appears after the
    integration is reloaded.
    """
    smartcocoon: SmartCocoonController = hass.data[DOMAIN][config_entry.entry_id]
    scmanager: SmartCocoonManager | None = smartcocoon.scmanager
    coordinator = smartcocoon.room_coordinator
    if scmanager is None or coordinator is None:
        return

    entities = [
        SmartCocoonSensor(coordinator, scmanager, room_id, description)
        for room_id in scmanager.external_sensors
        for description in SENSOR_DESCRIPTIONS
    ]
    _LOGGER.debug(
        "Adding %d entities for %d paired sensor(s)",
        len(entities),
        len(scmanager.external_sensors),
    )
    async_add_entities(entities)


class SmartCocoonSensor(
    CoordinatorEntity[SmartCocoonRoomCoordinator],  # type: ignore[misc]
    SensorEntity,  # type: ignore[misc]
):
    """One reading of a SmartCocoon paired sensor."""

    _attr_has_entity_name = True
    entity_description: SmartCocoonSensorDescription

    def __init__(
        self,
        coordinator: SmartCocoonRoomCoordinator,
        scmanager: SmartCocoonManager,
        room_id: int,
        description: SmartCocoonSensorDescription,
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._scmanager = scmanager
        self._room_id = room_id

        sensor = scmanager.external_sensors[room_id]
        # The sensor's own id identifies it; the room id is the fallback
        # for a payload without one.
        self._sensor_key = str(sensor.identifier or f"room_{room_id}")
        self._attr_unique_id = f"{DOMAIN}_sensor_{self._sensor_key}_{description.key}"

    def _sensor(self) -> Any:
        """Return the sensor's latest data, or None if it is gone."""
        return self._scmanager.external_sensors.get(self._room_id)

    @property
    def available(self) -> bool:
        """Available while polling works and the sensor is still paired."""
        return bool(super().available) and self._sensor() is not None

    @property
    def native_value(self) -> float | datetime | None:
        """Return the reading."""
        sensor = self._sensor()
        if sensor is None:
            return None
        return self.entity_description.value_fn(sensor)

    def _room_fan_id(self) -> str | None:
        """Return the id of the fan in this sensor's room, if there is one."""
        return next(
            (
                fan_id
                for fan_id, fan in self._scmanager.fans.items()
                if getattr(fan, "room_id", None) == self._room_id
            ),
            None,
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the readings to the room's fan device.

        The sensor's readings describe the room the fan serves, so they sit
        on the fan's device page beside its controls. Only the identifiers
        are given, so the fan's own name and details are left untouched.
        A sensor in a room without a fan gets a device of its own.
        """
        fan_id = self._room_fan_id()
        if fan_id is not None:
            return {"identifiers": {(DOMAIN, f"smartcocoon_fan_{fan_id}")}}

        room = self._scmanager.rooms.get(self._room_id)
        room_name = getattr(room, "name", None) or "SmartCocoon"
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, f"smartcocoon_sensor_{self._sensor_key}")},
            "name": f"{room_name} Sensor",
            "manufacturer": "SmartCocoon",
            "model": "External Sensor",
        }
        if room is not None:
            info["suggested_area"] = room_name
        return info
