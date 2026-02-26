"""Light platform for Conti.

Maps Tuya DPs to HA :class:`LightEntity` features:

* On/off        — ``power`` DP (bool)
* Brightness    — ``brightness`` DP (int, scaled to 0-255)
* Color temp    — ``color_temp`` DP (int, scaled to mireds)
* RGB colour    — ``color_rgb`` DP (string ``"rrggbb"`` hex)

Commands use optimistic state updates for instant UI feedback and
batch related DPs into a single ``set_dps`` call.
"""

from __future__ import annotations

import asyncio
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

_DP_KEY_MODE = "mode"  # Tuya light mode DP key (e.g. "white", "colour")


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
        self._dp_mode = self._find_dp(_DP_KEY_MODE)

        # Command coalescing / debounce state
        self._pending_dps: dict[int, Any] = {}
        self._send_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

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
        # Prefer cached DP or coordinator health so entities don't flap
        # to "unknown" on transient poll failures.
        return (
            self._dp_value(self._dp_power) is not None
            or self.coordinator.last_update_success
            or self.coordinator.device_manager.is_online(self._device_id)
        )

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

    # -- Helpers (optimistic) -------------------------------------------------

    def _apply_optimistic(self, updates: dict[str, Any]) -> None:
        """Push *updates* into coordinator data and notify HA immediately."""
        for dp_id, value in updates.items():
            self.coordinator.apply_optimistic_update(
                self._device_id, dp_id, value
            )

    # -- Command coalescing --------------------------------------------------

    def _schedule_send(self, dps: dict[int, Any]) -> None:
        """Merge *dps* into the pending batch and ensure a send is scheduled."""
        self._pending_dps.update(dps)
        if self._send_task is None or self._send_task.done():
            self._send_task = self.hass.async_create_task(
                self._debounced_send()
            )

    async def _debounced_send(self) -> None:
        """Wait briefly, coalesce all pending DPs, and send once.

        If new DPs arrive while the device call is in-flight the loop
        runs one more iteration so the latest values are always sent.
        """
        while True:
            await asyncio.sleep(0.2)
            if not self._pending_dps:
                break
            # Snapshot and clear
            batch = dict(self._pending_dps)
            self._pending_dps.clear()
            async with self._send_lock:
                try:
                    await self.coordinator.device_manager.set_dps(
                        self._device_id, batch
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "Debounced set_dps failed for %s: %s",
                        self._device_id,
                        batch,
                        exc_info=True,
                    )
            # Schedule a single delayed refresh after successful send
            self._schedule_refresh()
            # If nothing new accumulated while we were sending, we're done
            if not self._pending_dps:
                break

    def _schedule_refresh(self) -> None:
        """Cancel any pending refresh and schedule a new one."""
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = self.hass.async_create_task(
            self._delayed_refresh()
        )

    async def _delayed_refresh(self) -> None:
        """Reconcile with the device after a short delay."""
        await asyncio.sleep(1.0)
        await self.coordinator.async_request_refresh()

    # -- Commands ------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        mgr = self.coordinator.device_manager
        dps: dict[int, Any] = {}
        optimistic: dict[str, Any] = {}  # str dp_id → value

        # Always ensure power is on
        if self._dp_power:
            dps[int(self._dp_power)] = True
            optimistic[self._dp_power] = True

        # Set mode to "white" when turning on or adjusting colour temp
        if self._dp_mode and (
            ATTR_COLOR_TEMP_KELVIN in kwargs
            or ATTR_BRIGHTNESS in kwargs
            or not kwargs  # plain ON
        ):
            dps[int(self._dp_mode)] = "white"
            optimistic[self._dp_mode] = "white"

        if ATTR_BRIGHTNESS in kwargs and self._dp_brightness:
            lo, hi = self._dp_range(self._dp_brightness)
            scaled = int(lo + kwargs[ATTR_BRIGHTNESS] / 255 * (hi - lo))
            dps[int(self._dp_brightness)] = scaled
            optimistic[self._dp_brightness] = scaled

        if ATTR_COLOR_TEMP_KELVIN in kwargs and self._dp_color_temp:
            lo, hi = self._dp_range(self._dp_color_temp)
            frac = (kwargs[ATTR_COLOR_TEMP_KELVIN] - 2000) / (6535 - 2000)
            ct_val = int(frac * hi)
            dps[int(self._dp_color_temp)] = ct_val
            optimistic[self._dp_color_temp] = ct_val

        if ATTR_RGB_COLOR in kwargs and self._dp_rgb:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            hex_val = f"{r:02x}{g:02x}{b:02x}"
            dps[int(self._dp_rgb)] = hex_val
            optimistic[self._dp_rgb] = hex_val
            # Switch mode to colour when RGB is requested
            if self._dp_mode:
                dps[int(self._dp_mode)] = "colour"
                optimistic[self._dp_mode] = "colour"

        if not dps:
            return

        # Optimistic: update HA state instantly before the network call
        self._apply_optimistic(optimistic)

        # Coalesce into the debounced send pipeline (non-blocking)
        self._schedule_send(dps)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if not self._dp_power:
            return

        # Optimistic: reflect OFF in UI immediately
        self._apply_optimistic({self._dp_power: False})

        # Coalesce into the debounced send pipeline (non-blocking)
        self._schedule_send({int(self._dp_power): False})
