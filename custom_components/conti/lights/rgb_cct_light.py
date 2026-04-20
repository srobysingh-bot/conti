"""RGB + CCT LED strip light (power + brightness + color_temp + color_rgb + mode).

Full-feature light that supports both RGB colour and tuneable-white
(colour temperature) via the Tuya ``mode`` DP.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
)

from .base_light import BaseContiLight, _rgb_to_tuya_hsv


class ContiRgbCctLight(BaseContiLight):
    """RGB + CCT strip (brightness + colour temperature + RGB colour)."""

    def _init_color_modes(self) -> None:
        modes: set[ColorMode] = set()
        if self._dp_rgb:
            modes.add(ColorMode.RGB)
        if self._dp_color_temp:
            modes.add(ColorMode.COLOR_TEMP)
        if self._dp_brightness and ColorMode.COLOR_TEMP not in modes:
            # Only add BRIGHTNESS if COLOR_TEMP isn't already present
            # (COLOR_TEMP implies brightness control).
            modes.add(ColorMode.BRIGHTNESS)
        if not modes:
            modes.add(ColorMode.ONOFF)
        self._attr_supported_color_modes = modes
        # Default to COLOR_TEMP; switch to RGB when user picks a colour.
        if ColorMode.COLOR_TEMP in modes:
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif ColorMode.RGB in modes:
            self._attr_color_mode = ColorMode.RGB
        else:
            self._attr_color_mode = next(iter(modes))

    # -- Commands ------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        power_dps: dict[int, Any] = {}
        dps: dict[int, Any] = {}
        optimistic: dict[str, Any] = {}

        if self._dp_power:
            power_dps[int(self._dp_power)] = True
            optimistic[self._dp_power] = True

        # Determine which mode we need based on the requested attributes.
        if ATTR_RGB_COLOR in kwargs and self._dp_rgb:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            hex_val = _rgb_to_tuya_hsv(r, g, b)
            dps[int(self._dp_rgb)] = hex_val
            optimistic[self._dp_rgb] = hex_val
            if self._dp_mode:
                dps[int(self._dp_mode)] = "colour"
                optimistic[self._dp_mode] = "colour"
            self._attr_color_mode = ColorMode.RGB
        else:
            # Brightness-only, color-temp, or plain ON → "white" mode.
            if self._dp_mode:
                dps[int(self._dp_mode)] = "white"
                optimistic[self._dp_mode] = "white"
            if ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
                self._attr_color_mode = ColorMode.COLOR_TEMP

        if ATTR_BRIGHTNESS in kwargs and self._dp_brightness:
            lo, hi = self._dp_range(self._dp_brightness)
            scaled = int(lo + kwargs[ATTR_BRIGHTNESS] / 255 * (hi - lo))
            dps[int(self._dp_brightness)] = scaled
            optimistic[self._dp_brightness] = scaled

        if ATTR_COLOR_TEMP_KELVIN in kwargs and self._dp_color_temp:
            lo, hi = self._dp_range(self._dp_color_temp)
            frac = (kwargs[ATTR_COLOR_TEMP_KELVIN] - 2000) / (6535 - 2000)
            ct_val = int(lo + frac * (hi - lo))
            dps[int(self._dp_color_temp)] = ct_val
            optimistic[self._dp_color_temp] = ct_val

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
