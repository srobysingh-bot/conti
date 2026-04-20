"""RGB LED strip light (power + brightness + color_rgb + mode).

Supports RGB strips that use ``mode`` DP set to ``"colour"`` when
showing an RGB value and (optionally) ``"white"`` for plain brightness.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ColorMode,
)

from .base_light import BaseContiLight, _rgb_to_tuya_hsv


class ContiRgbStripLight(BaseContiLight):
    """RGB strip (brightness + RGB colour)."""

    def _init_color_modes(self) -> None:
        modes: set[ColorMode] = set()
        if self._dp_rgb:
            modes.add(ColorMode.RGB)
        # Only add BRIGHTNESS when RGB is absent (HA rule:
        # BRIGHTNESS cannot coexist with other color modes).
        if self._dp_brightness and not modes:
            modes.add(ColorMode.BRIGHTNESS)
        if not modes:
            modes.add(ColorMode.ONOFF)
        self._attr_supported_color_modes = modes
        # Prefer RGB when available.
        if ColorMode.RGB in modes:
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

        # RGB colour requested → mode "colour"
        if ATTR_RGB_COLOR in kwargs and self._dp_rgb:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            hex_val = _rgb_to_tuya_hsv(r, g, b)
            dps[int(self._dp_rgb)] = hex_val
            optimistic[self._dp_rgb] = hex_val
            if self._dp_mode:
                dps[int(self._dp_mode)] = "colour"
                optimistic[self._dp_mode] = "colour"
        elif self._dp_mode:
            # Plain ON or brightness-only → "white" mode
            dps[int(self._dp_mode)] = "white"
            optimistic[self._dp_mode] = "white"

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
