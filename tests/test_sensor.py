"""Tests for paired temperature and humidity sensors (ha-smartcocoon#422).

The cloud reports each sensor inside the room it is paired to. These cover
the room coordinator that refreshes them and the entities that show them.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pysmartcocoon.errors import UnauthorizedError

from custom_components.smartcocoon.coordinator import SmartCocoonRoomCoordinator
from custom_components.smartcocoon.sensor import SENSOR_DESCRIPTIONS, SmartCocoonSensor
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

ROOM_ID = 93694
DESCRIPTIONS = {d.key: d for d in SENSOR_DESCRIPTIONS}


def _sensor(**overrides: Any) -> SimpleNamespace:
    """A paired sensor as the library exposes it."""
    values: dict[str, Any] = {
        "identifier": 13004,
        "room_id": ROOM_ID,
        "temperature": 19.85,
        "humidity": 52.95,
        "battery_voltage": 3.002,
        "last_connection": datetime(2026, 11, 12, 1, 49, 15, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _scmanager(sensor: SimpleNamespace | None = None) -> MagicMock:
    sensors = {ROOM_ID: sensor or _sensor()}
    return MagicMock(
        external_sensors=sensors,
        rooms={ROOM_ID: SimpleNamespace(name="Main Bathroom")},
        fans={"e6955c": SimpleNamespace(room_id=ROOM_ID)},
    )


def _entity(
    key: str, scmanager: MagicMock | None = None, last_update_success: bool = True
) -> SmartCocoonSensor:
    coordinator = MagicMock(last_update_success=last_update_success)
    return SmartCocoonSensor(
        coordinator, scmanager or _scmanager(), ROOM_ID, DESCRIPTIONS[key]
    )


# --- coordinator ------------------------------------------------------------


async def test_room_coordinator_refreshes_rooms(hass: HomeAssistant) -> None:
    """Each update asks the library for the rooms."""
    scmanager = MagicMock(async_refresh_rooms=AsyncMock(return_value=True))
    coordinator = SmartCocoonRoomCoordinator(hass, scmanager)

    await coordinator._async_update_data()  # pylint: disable=protected-access

    scmanager.async_refresh_rooms.assert_awaited_once()


async def test_room_coordinator_failure_is_an_update_failure(
    hass: HomeAssistant,
) -> None:
    """A failed refresh marks the sensors unavailable rather than stale."""
    scmanager = MagicMock(async_refresh_rooms=AsyncMock(return_value=False))
    coordinator = SmartCocoonRoomCoordinator(hass, scmanager)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()  # pylint: disable=protected-access


async def test_room_coordinator_rejected_login_asks_for_reauth(
    hass: HomeAssistant,
) -> None:
    """Rejected credentials start reauth, as for the fans."""
    scmanager = MagicMock(
        async_refresh_rooms=AsyncMock(side_effect=UnauthorizedError("401"))
    )
    coordinator = SmartCocoonRoomCoordinator(hass, scmanager)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()  # pylint: disable=protected-access


# --- entities ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("temperature", 19.85),
        ("humidity", 52.95),
        ("battery_voltage", 3.002),
        ("last_seen", datetime(2026, 11, 12, 1, 49, 15, tzinfo=timezone.utc)),
    ],
)
def test_entity_shows_the_reading(key: str, expected: Any) -> None:
    """Each entity reports its own field of the sensor."""
    assert _entity(key).native_value == expected


def test_entity_follows_a_refresh() -> None:
    """Values are read from the manager each time, not cached."""
    scmanager = _scmanager()
    entity = _entity("temperature", scmanager)
    scmanager.external_sensors[ROOM_ID] = _sensor(temperature=22.5)
    assert entity.native_value == 22.5


def test_unique_ids_are_stable_and_distinct() -> None:
    """Keyed on the sensor's own id, one per reading."""
    ids = {_entity(key).unique_id for key in DESCRIPTIONS}
    assert len(ids) == len(DESCRIPTIONS)
    assert "smartcocoon_sensor_13004_temperature" in ids


def test_unavailable_when_polling_fails() -> None:
    """A failed room refresh makes the readings unavailable."""
    assert _entity("temperature").available is True
    assert _entity("temperature", last_update_success=False).available is False


def test_unavailable_when_the_sensor_is_unpaired() -> None:
    """A sensor that disappears from its room is unavailable, not stale."""
    scmanager = _scmanager()
    entity = _entity("humidity", scmanager)
    scmanager.external_sensors.clear()
    assert entity.available is False
    assert entity.native_value is None


def test_readings_attach_to_the_room_fan() -> None:
    """With a fan in the room, the entities join the fan's device.

    Only the identifiers are given, so the fan's name is not overwritten.
    """
    info = _entity("temperature").device_info
    assert info == {"identifiers": {("smartcocoon", "smartcocoon_fan_e6955c")}}


def test_sensor_without_a_fan_gets_its_own_device() -> None:
    """A room with no fan still shows its sensor, as its own device."""
    scmanager = _scmanager()
    scmanager.fans = {}
    info = _entity("temperature", scmanager).device_info
    assert info["identifiers"] == {("smartcocoon", "smartcocoon_sensor_13004")}
    assert info["name"] == "Main Bathroom Sensor"
    assert info["suggested_area"] == "Main Bathroom"
    assert "via_device" not in info
