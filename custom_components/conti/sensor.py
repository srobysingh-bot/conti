"""Sensor platform for Conti.

Creates one HA sensor entity per sensor-type DP in the device's DP map:
temperature, humidity, power usage, battery, motion.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CONNECTION_TYPE,
    CONF_DEVICE_ID,
    CONF_DEVICE_METADATA,
    CONF_DEVICE_TYPE,
    CONF_DP_MAP,
    CONF_GATEWAY_DEVICE_ID,
    CONNECTION_TYPE_GATEWAY_CHILD,
    DEVICE_TYPE_SENSOR,
    DOMAIN,
    DP_KEY_BATTERY,
    DP_KEY_CONTACT,
    DP_KEY_CURRENT,
    DP_KEY_DOOR_STATE,
    DP_KEY_ENERGY_TOTAL,
    DP_KEY_HUMIDITY,
    DP_KEY_MOTION,
    DP_KEY_POWER_USAGE,
    DP_KEY_TEMPERATURE,
    DP_KEY_VOLTAGE,
    MANUFACTURER,
)
from .coordinator import ContiCoordinator

_LOGGER = logging.getLogger(__name__)

# Mapping from DP key → (device_class, unit, state_class, icon)
_SENSOR_META: dict[str, tuple[SensorDeviceClass | None, str | None, SensorStateClass | None, str | None]] = {
    DP_KEY_TEMPERATURE: (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS, SensorStateClass.MEASUREMENT, None),
    DP_KEY_HUMIDITY: (SensorDeviceClass.HUMIDITY, PERCENTAGE, SensorStateClass.MEASUREMENT, None),
    DP_KEY_POWER_USAGE: (SensorDeviceClass.POWER, UnitOfPower.WATT, SensorStateClass.MEASUREMENT, None),
    DP_KEY_BATTERY: (SensorDeviceClass.BATTERY, PERCENTAGE, SensorStateClass.MEASUREMENT, None),
    DP_KEY_MOTION: (None, None, None, "mdi:motion-sensor"),
    DP_KEY_CONTACT: (None, None, None, "mdi:door-closed"),
    DP_KEY_DOOR_STATE: (None, None, None, "mdi:door-closed"),
    # Energy monitoring (switches / plugs)
    DP_KEY_ENERGY_TOTAL: (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.TOTAL_INCREASING, None),
    DP_KEY_CURRENT: (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.MILLIAMPERE, SensorStateClass.MEASUREMENT, None),
    DP_KEY_VOLTAGE: (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, SensorStateClass.MEASUREMENT, None),
    # Alarm-capable contact sensor alarm settings
    "alarm_switch": (None, None, None, "mdi:bell"),
    "delay_alarm": (None, None, None, "mdi:timer"),
    "time_alarm": (None, None, None, "mdi:clock"),
    "alarm_volume": (None, PERCENTAGE, None, "mdi:volume-high"),
}


def _is_raw_child_dp(entry: ConfigEntry, dp_id: str, info: Any) -> bool:
    """Recognize the existing raw fallback without guessing a DP's meaning."""
    return (
        entry.data.get(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_GATEWAY_CHILD
        and isinstance(info, dict)
        and info.get("key") == f"dp_{dp_id}"
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device_type = entry.data.get(CONF_DEVICE_TYPE)

    if device_type != DEVICE_TYPE_SENSOR:
        # Combo support: create sensor entities when a non-sensor device
        # (e.g. switch with power monitoring) has sensor-capable DPs.
        raw = entry.options.get(CONF_DP_MAP) or entry.data.get(CONF_DP_MAP, "{}")
        dp_map_check = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if not isinstance(dp_map_check, dict) or not any(
            isinstance(v, dict)
            and (v.get("key") in _SENSOR_META or _is_raw_child_dp(entry, str(dp), v))
            for dp, v in dp_map_check.items()
        ):
            return

    coordinator: ContiCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    raw = entry.options.get(CONF_DP_MAP) or entry.data.get(CONF_DP_MAP, "{}")
    dp_map: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else (raw or {})
    device_id: str = entry.data[CONF_DEVICE_ID]

    entities: list[ContiSensor] = []
    for dp_id, info in dp_map.items():
        if not isinstance(info, dict):
            continue
        key = info.get("key", "")
        if key in _SENSOR_META or _is_raw_child_dp(entry, str(dp_id), info):
            entities.append(
                ContiSensor(coordinator, entry, device_id, str(dp_id), info)
            )

    if entities:
        async_add_entities(entities, update_before_add=True)


class ContiSensor(CoordinatorEntity[ContiCoordinator], SensorEntity):
    """A single sensor value from a Tuya device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ContiCoordinator,
        entry: ConfigEntry,
        device_id: str,
        dp_id: str,
        dp_info: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._dp_id = dp_id
        self._dp_info = dp_info
        self._key: str = dp_info.get("key", dp_id)
        self._raw_child_dp = _is_raw_child_dp(entry, dp_id, dp_info)
        self._scale: int = 1 if self._raw_child_dp else dp_info.get("scale", 1)

        meta = _SENSOR_META.get(self._key, (None, None, None, None))
        dev_class, unit, state_class, icon = meta

        self._attr_unique_id = f"{DOMAIN}_{device_id}_{self._key}"
        self._attr_name = self._key.replace("_", " ").title()
        self._attr_device_class = dev_class
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = state_class
        if icon:
            self._attr_icon = icon
        if self._raw_child_dp:
            self._attr_name = f"DP {dp_id}"
            self._attr_icon = "mdi:code-braces"

        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": entry.title,
            "manufacturer": MANUFACTURER,
        }
        if entry.data.get(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_GATEWAY_CHILD:
            metadata = entry.data.get(CONF_DEVICE_METADATA, {})
            self._attr_device_info.update({
                "via_device": (DOMAIN, entry.data[CONF_GATEWAY_DEVICE_ID]),
                "model": metadata.get("model") or metadata.get("product_name") or None,
                "manufacturer": metadata.get("manufacturer") or "Tuya",
            })

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Keep the full raw value when it cannot fit in an HA state string."""
        if not self._raw_child_dp:
            return None
        raw = (self.coordinator.data or {}).get(self._device_id, {}).get(self._dp_id)
        attributes: dict[str, Any] = {"dp_id": self._dp_id, "read_only": True}
        if isinstance(raw, (dict, list)) or isinstance(raw, str) and len(raw) > 255:
            attributes["raw_value"] = raw
        return attributes

    @property
    def available(self) -> bool:
        return self.coordinator.is_device_available()

    @property
    def native_value(self) -> float | int | str | None:
        data = self.coordinator.data or {}
        raw = data.get(self._device_id, {}).get(self._dp_id)
        if raw is None:
            return None
        if self._raw_child_dp:
            if isinstance(raw, (dict, list)):
                return json.dumps(raw, ensure_ascii=False)[:255]
            if isinstance(raw, str):
                return raw[:255]
            if isinstance(raw, (int, float)):
                return raw
            return str(raw)[:255]
        if self._scale and self._scale != 1:
            try:
                return round(float(raw) / self._scale, 2)
            except (TypeError, ValueError):
                pass
        return raw
