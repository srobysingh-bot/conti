"""Light platform for Conti.

Maps Tuya DPs to HA :class:`LightEntity` features:

* On/off        — ``power`` DP (bool)
* Brightness    — ``brightness`` DP (int, scaled to 0-255)
* Color temp    — ``color_temp`` DP (int, scaled to mireds)
* RGB colour    — ``color_rgb`` DP (string ``"rrggbb"`` hex)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DP_MAP,
    DEVICE_TYPE_LIGHT,
    DOMAIN,
    DP_KEY_BRIGHTNESS,
    DP_KEY_COLOR_RGB,
    DP_KEY_COLOR_TEMP,
    DP_KEY_POWER,
    MANUFACTURER,
)
from .coordinator import ContiCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Conti light entities from a config entry."""
    if entry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_LIGHT:
        return

    coordinator: ContiCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    dp_map: dict[str, Any] = json.loads(
        entry.options.get(CONF_DP_MAP) or entry.data.get(CONF_DP_MAP, "{}")
    )
    device_id: str = entry.data[CONF_DEVICE_ID]

    async_add_entities(
        [ContiLight(coordinator, entry, device_id, dp_map)],
        update_before_add=True,
    )


class ContiLight(CoordinatorEntity[ContiCoordinator], LightEntity):
    """Representation of a Tuya light controlled via Conti."""

    _attr_has_entity_name = True
    _attr_name = None  # use device name

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

        self._attr_unique_id = f"{DOMAIN}_{device_id}_light"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": entry.title,
            "manufacturer": MANUFACTURER,
        }

        # Resolve DP ids for each capability
        self._dp_power = self._find_dp(DP_KEY_POWER)
        self._dp_brightness = self._find_dp(DP_KEY_BRIGHTNESS)
        self._dp_color_temp = self._find_dp(DP_KEY_COLOR_TEMP)
        self._dp_rgb = self._find_dp(DP_KEY_COLOR_RGB)

        # Determine supported color modes
        modes: set[ColorMode] = set()
        if self._dp_brightness:
            modes.add(ColorMode.BRIGHTNESS)
        if self._dp_color_temp:
            modes.add(ColorMode.COLOR_TEMP)
        if self._dp_rgb:
            modes.add(ColorMode.RGB)
        if not modes:
            modes.add(ColorMode.ONOFF)
        self._attr_supported_color_modes = modes
        self._attr_color_mode = next(iter(modes))

    # -- Helpers -------------------------------------------------------------

    def _find_dp(self, key: str) -> str | None:
        """Return the string DP id whose ``key`` matches, or ``None``."""
        for dp_id, info in self._dp_map.items():
            if isinstance(info, dict) and info.get("key") == key:
                return str(dp_id)
        return None

    def _dp_value(self, dp_id: str | None) -> Any:
        if dp_id is None:
            return None
        data = self.coordinator.data or {}
        device_data = data.get(self._device_id, {})
        return device_data.get(dp_id)

    def _dp_range(self, dp_id: str | None) -> tuple[int, int]:
        """Return (min, max) from the dp_map, defaulting to (10, 1000)."""
        if dp_id is None:
            return (10, 1000)
        info = self._dp_map.get(dp_id, {})
        return (info.get("min", 10), info.get("max", 1000))

    # -- State properties ----------------------------------------------------

    @property
    def available(self) -> bool:
        return self.coordinator.device_manager.is_online(self._device_id)

    @property
    def is_on(self) -> bool | None:
        val = self._dp_value(self._dp_power)
        if val is None:
            return None
        return bool(val)

    @property
    def brightness(self) -> int | None:
        raw = self._dp_value(self._dp_brightness)
        if raw is None:
            return None
        lo, hi = self._dp_range(self._dp_brightness)
        # Scale Tuya range → HA 0-255
        return max(1, int((int(raw) - lo) / max(hi - lo, 1) * 255))

    @property
    def color_temp_kelvin(self) -> int | None:
        raw = self._dp_value(self._dp_color_temp)
        if raw is None:
            return None
        lo, hi = self._dp_range(self._dp_color_temp)
        # Tuya 0-1000 → HA kelvin 2000-6535
        frac = int(raw) / max(hi, 1)
        return int(2000 + frac * (6535 - 2000))

    @property
    def min_color_temp_kelvin(self) -> int:
        return 2000

    @property
    def max_color_temp_kelvin(self) -> int:
        return 6535

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        raw = self._dp_value(self._dp_rgb)
        if not raw or not isinstance(raw, str) or len(raw) < 6:
            return None
        try:
            r = int(raw[0:2], 16)
            g = int(raw[2:4], 16)
            b = int(raw[4:6], 16)
            return (r, g, b)
        except ValueError:
            return None

    # -- Commands ------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        mgr = self.coordinator.device_manager
        dps: dict[int, Any] = {}

        if self._dp_power:
            dps[int(self._dp_power)] = True

        if ATTR_BRIGHTNESS in kwargs and self._dp_brightness:
            lo, hi = self._dp_range(self._dp_brightness)
            scaled = int(lo + kwargs[ATTR_BRIGHTNESS] / 255 * (hi - lo))
            dps[int(self._dp_brightness)] = scaled

        if ATTR_COLOR_TEMP_KELVIN in kwargs and self._dp_color_temp:
            lo, hi = self._dp_range(self._dp_color_temp)
            frac = (kwargs[ATTR_COLOR_TEMP_KELVIN] - 2000) / (6535 - 2000)
            dps[int(self._dp_color_temp)] = int(frac * hi)

        if ATTR_RGB_COLOR in kwargs and self._dp_rgb:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            dps[int(self._dp_rgb)] = f"{r:02x}{g:02x}{b:02x}"

        if dps:
            await mgr.set_dps(self._device_id, dps)
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._dp_power:
            await self.coordinator.device_manager.set_dp(
                self._device_id, int(self._dp_power), False
            )
            await self.coordinator.async_request_refresh()
