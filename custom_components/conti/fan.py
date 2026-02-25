"""Fan platform for Conti.

Maps Tuya DPs to HA :class:`FanEntity`:
* On/off     — ``power`` DP (bool)
* Speed      — ``fan_speed`` DP (int/enum)
* Direction  — ``fan_direction`` DP (string: ``"forward"`` / ``"reverse"``)
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DP_MAP,
    DEVICE_TYPE_FAN,
    DOMAIN,
    DP_KEY_FAN_DIRECTION,
    DP_KEY_FAN_SPEED,
    DP_KEY_POWER,
    MANUFACTURER,
)
from .coordinator import ContiCoordinator

_LOGGER = logging.getLogger(__name__)

# Default speed list when none provided in dp_map
_DEFAULT_SPEED_LIST: list[str] = ["low", "medium", "high"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_FAN:
        return

    coordinator: ContiCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    dp_map: dict[str, Any] = json.loads(
        entry.options.get(CONF_DP_MAP) or entry.data.get(CONF_DP_MAP, "{}")
    )
    device_id: str = entry.data[CONF_DEVICE_ID]

    async_add_entities(
        [ContiFan(coordinator, entry, device_id, dp_map)],
        update_before_add=True,
    )


class ContiFan(CoordinatorEntity[ContiCoordinator], FanEntity):
    """Representation of a Tuya fan."""

    _attr_has_entity_name = True
    _attr_name = None

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

        self._attr_unique_id = f"{DOMAIN}_{device_id}_fan"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": entry.title,
            "manufacturer": MANUFACTURER,
        }

        self._dp_power = self._find_dp(DP_KEY_POWER)
        self._dp_speed = self._find_dp(DP_KEY_FAN_SPEED)
        self._dp_direction = self._find_dp(DP_KEY_FAN_DIRECTION)

        # Build speed list from dp_map or use default
        speed_info = self._dp_map.get(self._dp_speed, {}) if self._dp_speed else {}
        self._speed_list: list[str] = speed_info.get("values", _DEFAULT_SPEED_LIST)
        self._attr_speed_count = len(self._speed_list)

        features = FanEntityFeature(0)
        if self._dp_speed:
            features |= FanEntityFeature.SET_SPEED
        if self._dp_direction:
            features |= FanEntityFeature.DIRECTION
        self._attr_supported_features = features

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

    # -- State properties ----------------------------------------------------

    @property
    def available(self) -> bool:
        return self.coordinator.device_manager.is_online(self._device_id)

    @property
    def is_on(self) -> bool | None:
        val = self._dp_value(self._dp_power)
        return bool(val) if val is not None else None

    @property
    def percentage(self) -> int | None:
        raw = self._dp_value(self._dp_speed)
        if raw is None:
            return None
        speed_str = str(raw)
        if speed_str in self._speed_list:
            return ordered_list_item_to_percentage(self._speed_list, speed_str)
        # If it's a numeric value, assume 0-len(speed_list) range
        try:
            idx = int(raw)
            if 0 <= idx < len(self._speed_list):
                return ordered_list_item_to_percentage(
                    self._speed_list, self._speed_list[idx]
                )
        except (ValueError, TypeError):
            pass
        return None

    @property
    def current_direction(self) -> str | None:
        raw = self._dp_value(self._dp_direction)
        if raw is None:
            return None
        return str(raw)

    # -- Commands ------------------------------------------------------------

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        mgr = self.coordinator.device_manager
        dps: dict[int, Any] = {}
        if self._dp_power:
            dps[int(self._dp_power)] = True
        if percentage is not None and self._dp_speed:
            speed = percentage_to_ordered_list_item(self._speed_list, percentage)
            dps[int(self._dp_speed)] = speed
        if dps:
            await mgr.set_dps(self._device_id, dps)
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._dp_power:
            await self.coordinator.device_manager.set_dp(
                self._device_id, int(self._dp_power), False
            )
            await self.coordinator.async_request_refresh()

    async def async_set_percentage(self, percentage: int) -> None:
        if self._dp_speed:
            speed = percentage_to_ordered_list_item(self._speed_list, percentage)
            await self.coordinator.device_manager.set_dp(
                self._device_id, int(self._dp_speed), speed
            )
            await self.coordinator.async_request_refresh()

    async def async_set_direction(self, direction: str) -> None:
        if self._dp_direction:
            await self.coordinator.device_manager.set_dp(
                self._device_id, int(self._dp_direction), direction
            )
            await self.coordinator.async_request_refresh()
