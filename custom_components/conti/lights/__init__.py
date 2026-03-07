"""Conti light sub-types package.

Provides per-device-type light classes and a factory function used by
the ``light`` platform to instantiate the correct subclass based on the
DP map.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from ..const import (
    DP_KEY_BRIGHTNESS,
    DP_KEY_COLOR_RGB,
    DP_KEY_COLOR_TEMP,
    DP_KEY_POWER,
)
from ..coordinator import ContiCoordinator
from .base_light import BaseContiLight, DP_KEY_MODE
from .dimmer_light import ContiDimmerLight
from .rgb_cct_light import ContiRgbCctLight
from .rgb_strip_light import ContiRgbStripLight
from .triac_dimmer_light import ContiTriacDimmerLight
from .white_strip_light import ContiWhiteStripLight

__all__ = [
    "BaseContiLight",
    "ContiDimmerLight",
    "ContiRgbCctLight",
    "ContiRgbStripLight",
    "ContiTriacDimmerLight",
    "ContiWhiteStripLight",
    "create_conti_light",
]


def _has_dp(dp_map: dict[str, Any], key: str) -> bool:
    """Return ``True`` if *dp_map* contains a DP with the given role key."""
    for info in dp_map.values():
        if isinstance(info, dict) and info.get("key") == key:
            return True
    return False


def create_conti_light(
    coordinator: ContiCoordinator,
    entry: ConfigEntry,
    device_id: str,
    dp_map: dict[str, Any],
) -> BaseContiLight:
    """Instantiate the correct light subclass based on available DPs.

    Factory rules (checked most-specific first):
    1. power + brightness + color_temp + color_rgb + mode → RgbCctLight
    2. power + brightness + color_rgb + mode             → RgbStripLight
    3. power + brightness + color_temp + mode             → WhiteStripLight
    4. power + brightness (DP 20–29 range, triac-style)  → TriacDimmerLight
    5. power + brightness                                → DimmerLight
    6. fallback                                          → DimmerLight
    """
    has_power = _has_dp(dp_map, DP_KEY_POWER)
    has_brightness = _has_dp(dp_map, DP_KEY_BRIGHTNESS)
    has_color_temp = _has_dp(dp_map, DP_KEY_COLOR_TEMP)
    has_rgb = _has_dp(dp_map, DP_KEY_COLOR_RGB)
    has_mode = _has_dp(dp_map, DP_KEY_MODE)

    if has_power and has_brightness and has_color_temp and has_rgb and has_mode:
        return ContiRgbCctLight(coordinator, entry, device_id, dp_map)

    if has_power and has_brightness and has_rgb and has_mode:
        return ContiRgbStripLight(coordinator, entry, device_id, dp_map)

    if has_power and has_brightness and has_color_temp and has_mode:
        return ContiWhiteStripLight(coordinator, entry, device_id, dp_map)

    # Triac dimmers typically use DP ids in the 20–29 range for power.
    if has_power and has_brightness:
        power_dp_id = _find_dp_id(dp_map, DP_KEY_POWER)
        if power_dp_id is not None and 20 <= int(power_dp_id) <= 29:
            return ContiTriacDimmerLight(coordinator, entry, device_id, dp_map)
        return ContiDimmerLight(coordinator, entry, device_id, dp_map)

    # Fallback — at minimum power-only (OnOff).
    return ContiDimmerLight(coordinator, entry, device_id, dp_map)


def _find_dp_id(dp_map: dict[str, Any], key: str) -> str | None:
    """Return the string DP id for *key*, or ``None``."""
    for dp_id, info in dp_map.items():
        if isinstance(info, dict) and info.get("key") == key:
            return str(dp_id)
    return None
