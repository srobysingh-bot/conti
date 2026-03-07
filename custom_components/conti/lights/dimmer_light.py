"""Standard dimmer light (power + brightness).

Supports devices with a simple boolean power DP and an integer brightness
DP (e.g. Tuya Wi-Fi dimmers using DP 1/2).  No mode, color-temp, or RGB
handling.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
)

from .base_light import BaseContiLight


class ContiDimmerLight(BaseContiLight):
    """On/Off + Brightness dimmer."""

    def _init_color_modes(self) -> None:
        if self._dp_brightness:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF

    # -- Commands ------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        power_dps: dict[int, Any] = {}
        dps: dict[int, Any] = {}
        optimistic: dict[str, Any] = {}

        if self._dp_power:
            power_dps[int(self._dp_power)] = True
            optimistic[self._dp_power] = True

        if ATTR_BRIGHTNESS in kwargs and self._dp_brightness:
            lo, hi = self._dp_range(self._dp_brightness)
            scaled = int(lo + kwargs[ATTR_BRIGHTNESS] / 255 * (hi - lo))
            dps[int(self._dp_brightness)] = scaled
            optimistic[self._dp_brightness] = scaled

        if not power_dps and not dps:
            return

        self._track_sent(optimistic)
        self._apply_optimistic(optimistic)

        if power_dps:
            self.hass.async_create_task(self._send_immediately(power_dps))
        if dps:
            self._schedule_send(dps)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if not self._dp_power:
            return

        self._track_sent({self._dp_power: False})
        self._apply_optimistic({self._dp_power: False})
        self.hass.async_create_task(
            self._send_immediately({int(self._dp_power): False})
        )
