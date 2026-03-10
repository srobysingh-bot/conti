"""Climate (AC) platform for Conti.

Maps Tuya DPs to HA :class:`ClimateEntity`:
* Power          — ``power`` DP (bool)
* Target temp    — ``target_temp`` DP (int)
* Current temp   — ``current_temp`` DP (int, read-only from device)
* HVAC mode      — ``hvac_mode`` DP (string enum)
* Fan mode       — ``fan_mode`` DP (string enum)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DP_MAP,
    DEVICE_TYPE_CLIMATE,
    DOMAIN,
    DP_KEY_CURRENT_TEMP,
    DP_KEY_FAN_MODE,
    DP_KEY_HVAC_MODE,
    DP_KEY_POWER,
    DP_KEY_TARGET_TEMP,
    MANUFACTURER,
)
from .coordinator import ContiCoordinator

_LOGGER = logging.getLogger(__name__)

# Mapping from common Tuya HVAC strings to HA modes
_HVAC_MAP: dict[str, HVACMode] = {
    "auto": HVACMode.AUTO,
    "cold": HVACMode.COOL,
    "cool": HVACMode.COOL,
    "hot": HVACMode.HEAT,
    "heat": HVACMode.HEAT,
    "wind": HVACMode.FAN_ONLY,
    "fan": HVACMode.FAN_ONLY,
    "dry": HVACMode.DRY,
}

_HVAC_REVERSE: dict[HVACMode, str] = {v: k for k, v in _HVAC_MAP.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_CLIMATE:
        return

    coordinator: ContiCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    dp_map: dict[str, Any] = json.loads(
        entry.options.get(CONF_DP_MAP) or entry.data.get(CONF_DP_MAP, "{}")
    )
    device_id: str = entry.data[CONF_DEVICE_ID]

    async_add_entities(
        [ContiClimate(coordinator, entry, device_id, dp_map)],
        update_before_add=True,
    )


class ContiClimate(CoordinatorEntity[ContiCoordinator], ClimateEntity):
    """Representation of a Tuya AC / climate device."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1.0

    def __init__(
        self,
        coordinator: ContiCoordinator,
        entry: ConfigEntry,
        device_id: str,
        dp_map: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._dp_map = dp_map
        self._entry = entry

        self._attr_unique_id = f"{DOMAIN}_{device_id}_climate"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": entry.title,
            "manufacturer": MANUFACTURER,
        }

        self._dp_power = self._find_dp(DP_KEY_POWER)
        self._dp_target_temp = self._find_dp(DP_KEY_TARGET_TEMP)
        self._dp_current_temp = self._find_dp(DP_KEY_CURRENT_TEMP)
        self._dp_hvac_mode = self._find_dp(DP_KEY_HVAC_MODE)
        self._dp_fan_mode = self._find_dp(DP_KEY_FAN_MODE)

        # Temperature range from dp_map
        temp_info = self._dp_map.get(self._dp_target_temp, {}) if self._dp_target_temp else {}
        self._attr_min_temp = float(temp_info.get("min", 16))
        self._attr_max_temp = float(temp_info.get("max", 30))

        # Supported features
        features = ClimateEntityFeature(0)
        if self._dp_target_temp:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._dp_fan_mode:
            features |= ClimateEntityFeature.FAN_MODE
        self._attr_supported_features = features

        # HVAC modes
        modes = [HVACMode.OFF]
        hvac_info = self._dp_map.get(self._dp_hvac_mode, {}) if self._dp_hvac_mode else {}
        for v in hvac_info.get("values", ["cool", "heat", "auto", "fan", "dry"]):
            ha_mode = _HVAC_MAP.get(v)
            if ha_mode and ha_mode not in modes:
                modes.append(ha_mode)
        self._attr_hvac_modes = modes

        # Fan modes
        fan_info = self._dp_map.get(self._dp_fan_mode, {}) if self._dp_fan_mode else {}
        self._attr_fan_modes = fan_info.get("values", ["auto", "low", "medium", "high"])

        # Track last-known power state for external-change detection
        self._last_power: bool | None = None

    # -- Coordinator update handling -----------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        """Detect external power changes and log to HA Activity panel."""
        prev_power = self._last_power
        current = self._dp_value(self._dp_power)
        if current is not None:
            self._last_power = bool(current)
        self.async_write_ha_state()

        # Fire logbook entry only for genuine external power changes
        if (
            self._dp_power
            and prev_power is not None
            and current is not None
            and bool(current) != prev_power
            and not self.coordinator.is_dp_commanded(self._dp_power)
        ):
            action = "turned on" if bool(current) else "turned off"
            self.hass.bus.async_fire(
                "logbook_entry",
                {
                    "name": self._entry.title,
                    "message": f"{action} by external device",
                    "entity_id": self.entity_id,
                    "domain": "climate",
                },
            )

    # -- Helpers -------------------------------------------------------------

    def _find_dp(self, key: str) -> str | None:
        for dp_id, info in self._dp_map.items():
            if isinstance(info, dict) and info.get("key") == key:
                return str(dp_id)
        return None

    def _dp_value(self, dp_id: str | None) -> Any:
        if dp_id is None:
            return None
        data = self.coordinator.data or {}
        return data.get(self._device_id, {}).get(dp_id)

    # -- State ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.coordinator.device_manager.is_online(self._device_id)

    @property
    def hvac_mode(self) -> HVACMode:
        power = self._dp_value(self._dp_power)
        if power is not None and not power:
            return HVACMode.OFF
        raw = self._dp_value(self._dp_hvac_mode)
        if raw is not None:
            return _HVAC_MAP.get(str(raw), HVACMode.AUTO)
        # Power is on but no mode DP — assume auto
        if power:
            return HVACMode.AUTO
        return HVACMode.OFF

    @property
    def current_temperature(self) -> float | None:
        raw = self._dp_value(self._dp_current_temp)
        if raw is None:
            return None
        scale = 1
        if self._dp_current_temp:
            info = self._dp_map.get(self._dp_current_temp, {})
            scale = info.get("scale", 1) if isinstance(info, dict) else 1
        try:
            return round(float(raw) / scale, 1)
        except (TypeError, ValueError):
            return None

    @property
    def target_temperature(self) -> float | None:
        raw = self._dp_value(self._dp_target_temp)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @property
    def fan_mode(self) -> str | None:
        raw = self._dp_value(self._dp_fan_mode)
        return str(raw) if raw is not None else None

    # -- Commands ------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        mgr = self.coordinator.device_manager
        if hvac_mode == HVACMode.OFF:
            if self._dp_power:
                self.coordinator.mark_dp_commanded(self._dp_power)
                await mgr.set_dp(self._device_id, int(self._dp_power), False)
        else:
            dps: dict[int, Any] = {}
            if self._dp_power:
                self.coordinator.mark_dp_commanded(self._dp_power)
                dps[int(self._dp_power)] = True
            if self._dp_hvac_mode:
                tuya_mode = _HVAC_REVERSE.get(hvac_mode, "auto")
                dps[int(self._dp_hvac_mode)] = tuya_mode
            if dps:
                await mgr.set_dps(self._device_id, dps)
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None and self._dp_target_temp:
            await self.coordinator.device_manager.set_dp(
                self._device_id, int(self._dp_target_temp), int(temp)
            )
            await self.coordinator.async_request_refresh()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if self._dp_fan_mode:
            await self.coordinator.device_manager.set_dp(
                self._device_id, int(self._dp_fan_mode), fan_mode
            )
            await self.coordinator.async_request_refresh()
