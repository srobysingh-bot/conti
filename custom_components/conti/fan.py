"""Fan platform for Conti.

Maps Tuya DPs to HA :class:`FanEntity`:
* On/off        — ``power`` or ``fan_power`` DP (bool)
* Speed         — ``fan_speed`` DP (enum strings or integer percentage)
* Direction     — ``fan_direction`` DP (string: ``"forward"`` / ``"reverse"``)
* Oscillation   — ``fan_oscillation`` DP (bool)

Speed handling
~~~~~~~~~~~~~~
If the dp_map entry for the speed DP includes a ``"values"`` list
(e.g. ``["low", "medium", "high"]``), the entity uses **enum-based**
speed and maps the list to HA percentages automatically.

If ``"values"`` is absent but ``"min"``/``"max"`` are present, the
entity uses **integer-percentage** speed — the raw DP value is scaled
linearly to 0-100 %.

Combo devices (fan + light)
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Fan devices whose dp_map also contains light-capable DPs (brightness,
color_temp, color_rgb) will automatically get light entities created by
the light platform.  Use ``"fan_power"`` as the DP key for the fan's
on/off switch to avoid conflicts with the light platform's ``"power"``
key.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
    DP_KEY_FAN_OSCILLATION,
    DP_KEY_FAN_POWER,
    DP_KEY_FAN_SPEED,
    DP_KEY_POWER,
    MANUFACTURER,
)
from .coordinator import ContiCoordinator

_LOGGER = logging.getLogger(__name__)

# Default speed list when none provided in dp_map
_DEFAULT_SPEED_LIST: list[str] = ["low", "medium", "high"]

# Speed mode — determined by dp_map content
_SPEED_ENUM = "enum"
_SPEED_PERCENT = "percent"


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
    """Representation of a Tuya fan.

    Capabilities are derived entirely from the dp_map — no per-model
    subclasses required.  Adding a new fan variant only requires
    providing the correct dp_map at configuration time.
    """

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
        self._entry = entry

        self._attr_unique_id = f"{DOMAIN}_{device_id}_fan"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": entry.title,
            "manufacturer": MANUFACTURER,
        }

        # DP resolution — "fan_power" for combo devices, fallback to "power"
        self._dp_power = (
            self._find_dp(DP_KEY_FAN_POWER) or self._find_dp(DP_KEY_POWER)
        )
        self._dp_speed = self._find_dp(DP_KEY_FAN_SPEED)
        self._dp_direction = self._find_dp(DP_KEY_FAN_DIRECTION)
        self._dp_oscillation = self._find_dp(DP_KEY_FAN_OSCILLATION)

        # Normalized cached state (updated in _update_cached_state)
        self._state_on: bool | None = None
        self._state_percentage: int | None = None
        self._state_oscillating: bool | None = None
        self._state_direction: str | None = None

        # Speed configuration: enum (string list) vs percentage (int range).
        # dp_map with "values" → enum;  "min"/"max" without "values" → percent.
        speed_info = (
            self._dp_map.get(self._dp_speed, {}) if self._dp_speed else {}
        )
        if not isinstance(speed_info, dict):
            speed_info = {}
        speed_values = speed_info.get("values")

        if speed_values and isinstance(speed_values, list):
            self._speed_type = _SPEED_ENUM
            self._speed_list: list[str] = speed_values
            self._speed_min = 0
            self._speed_max = 0
            self._attr_speed_count = len(self._speed_list)
        elif self._dp_speed:
            self._speed_type = _SPEED_PERCENT
            self._speed_list = []
            self._speed_min = int(speed_info.get("min", 1))
            self._speed_max = int(speed_info.get("max", 100))
            self._attr_speed_count = 100
        else:
            self._speed_type = _SPEED_ENUM
            self._speed_list = _DEFAULT_SPEED_LIST
            self._speed_min = 0
            self._speed_max = 0
            self._attr_speed_count = len(_DEFAULT_SPEED_LIST)

        # Supported features
        features = FanEntityFeature(0)
        if self._dp_speed:
            features |= FanEntityFeature.SET_SPEED
        if self._dp_direction:
            features |= FanEntityFeature.DIRECTION
        if self._dp_oscillation:
            features |= FanEntityFeature.OSCILLATE
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

    def _normalize_speed(self, raw: Any) -> int | None:
        """Convert raw speed DP to HA percentage (0-100)."""
        if raw is None:
            return None
        if self._speed_type == _SPEED_ENUM:
            speed_str = str(raw)
            if speed_str in self._speed_list:
                return ordered_list_item_to_percentage(
                    self._speed_list, speed_str
                )
            try:
                idx = int(raw)
                if 0 <= idx < len(self._speed_list):
                    return ordered_list_item_to_percentage(
                        self._speed_list, self._speed_list[idx]
                    )
            except (ValueError, TypeError):
                pass
            return None
        # Percentage-based: scale from device range to 0-100
        try:
            raw_int = int(raw)
            span = max(self._speed_max - self._speed_min, 1)
            return max(0, min(100, round(
                (raw_int - self._speed_min) / span * 100
            )))
        except (ValueError, TypeError):
            return None

    def _percentage_to_device(self, percentage: int) -> Any:
        """Convert HA percentage (0-100) to device native speed value."""
        if self._speed_type == _SPEED_ENUM:
            return percentage_to_ordered_list_item(
                self._speed_list, percentage
            )
        # Percentage-based: scale from 0-100 to device range
        span = max(self._speed_max - self._speed_min, 1)
        return int(self._speed_min + percentage / 100 * span)

    # -- Normalized state update ---------------------------------------------

    def _update_cached_state(self) -> None:
        """Read raw DPs and update normalized cached state."""
        power = self._dp_value(self._dp_power)
        if power is not None:
            self._state_on = bool(power)

        raw_speed = self._dp_value(self._dp_speed)
        if raw_speed is not None:
            self._state_percentage = self._normalize_speed(raw_speed)

        raw_osc = self._dp_value(self._dp_oscillation)
        if raw_osc is not None:
            self._state_oscillating = bool(raw_osc)

        raw_dir = self._dp_value(self._dp_direction)
        if raw_dir is not None:
            self._state_direction = str(raw_dir)

    # -- Coordinator update handling -----------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        """Normalize state, detect external changes, log to Activity panel."""
        prev_on = self._state_on
        prev_pct = self._state_percentage
        prev_osc = self._state_oscillating

        self._update_cached_state()
        self.async_write_ha_state()

        # --- External power change ---
        if (
            self._dp_power
            and prev_on is not None
            and self._state_on is not None
            and self._state_on != prev_on
            and not self.coordinator.is_dp_commanded(self._dp_power)
        ):
            action = "turned on" if self._state_on else "turned off"
            self.hass.bus.async_fire(
                "logbook_entry",
                {
                    "name": self._entry.title,
                    "message": f"{action} by external device",
                    "entity_id": self.entity_id,
                    "domain": "fan",
                },
            )

        # --- External speed change ---
        if (
            self._dp_speed
            and prev_pct is not None
            and self._state_percentage is not None
            and self._state_percentage != prev_pct
            and not self.coordinator.is_dp_commanded(self._dp_speed)
        ):
            self.hass.bus.async_fire(
                "logbook_entry",
                {
                    "name": self._entry.title,
                    "message": (
                        f"speed changed to {self._state_percentage}% "
                        f"by external device"
                    ),
                    "entity_id": self.entity_id,
                    "domain": "fan",
                },
            )

        # --- External oscillation change ---
        if (
            self._dp_oscillation
            and prev_osc is not None
            and self._state_oscillating is not None
            and self._state_oscillating != prev_osc
            and not self.coordinator.is_dp_commanded(self._dp_oscillation)
        ):
            state = "enabled" if self._state_oscillating else "disabled"
            self.hass.bus.async_fire(
                "logbook_entry",
                {
                    "name": self._entry.title,
                    "message": f"oscillation {state} by external device",
                    "entity_id": self.entity_id,
                    "domain": "fan",
                },
            )

    # -- State properties ----------------------------------------------------

    @property
    def available(self) -> bool:
        return self.coordinator.device_manager.is_online(self._device_id)

    @property
    def is_on(self) -> bool | None:
        return self._state_on

    @property
    def percentage(self) -> int | None:
        return self._state_percentage

    @property
    def oscillating(self) -> bool | None:
        return self._state_oscillating

    @property
    def current_direction(self) -> str | None:
        return self._state_direction

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
            self.coordinator.mark_dp_commanded(self._dp_power)
            dps[int(self._dp_power)] = True
        if percentage is not None and self._dp_speed:
            self.coordinator.mark_dp_commanded(self._dp_speed)
            dps[int(self._dp_speed)] = self._percentage_to_device(percentage)
        if dps:
            await mgr.set_dps(self._device_id, dps)
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._dp_power:
            self.coordinator.mark_dp_commanded(self._dp_power)
            await self.coordinator.device_manager.set_dp(
                self._device_id, int(self._dp_power), False
            )
            await self.coordinator.async_request_refresh()

    async def async_set_percentage(self, percentage: int) -> None:
        if self._dp_speed:
            self.coordinator.mark_dp_commanded(self._dp_speed)
            await self.coordinator.device_manager.set_dp(
                self._device_id,
                int(self._dp_speed),
                self._percentage_to_device(percentage),
            )
            await self.coordinator.async_request_refresh()

    async def async_set_direction(self, direction: str) -> None:
        if self._dp_direction:
            self.coordinator.mark_dp_commanded(self._dp_direction)
            await self.coordinator.device_manager.set_dp(
                self._device_id, int(self._dp_direction), direction
            )
            await self.coordinator.async_request_refresh()

    async def async_oscillate(self, oscillating: bool) -> None:
        """Turn oscillation on or off."""
        if self._dp_oscillation:
            self.coordinator.mark_dp_commanded(self._dp_oscillation)
            await self.coordinator.device_manager.set_dp(
                self._device_id, int(self._dp_oscillation), oscillating
            )
            await self.coordinator.async_request_refresh()
