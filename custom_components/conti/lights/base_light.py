"""Base light entity for all Conti light types.

Contains all shared infrastructure:
* DP lookup helpers
* Optimistic UI + ``async_write_ha_state`` gating
* Send lock, debounce/coalesce pipeline
* Stale-protect window (anti-bounce)
* Delayed refresh scheduling

Subclasses override:
* ``_init_color_modes()``  — declare supported HA ``ColorMode`` set.
* ``_process_coordinator_data()`` — map incoming DPs to cached state.
* ``_apply_optimistic()`` — mirror optimistic values into cached state.
* ``async_turn_on()`` / ``async_turn_off()`` — build DPs & optimistic dict.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.light import (
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import (
    DOMAIN,
    DP_KEY_BRIGHTNESS,
    DP_KEY_COLOR_RGB,
    DP_KEY_COLOR_TEMP,
    DP_KEY_POWER,
    MANUFACTURER,
)
from ..coordinator import ContiCoordinator

_LOGGER = logging.getLogger(__name__)

# Well-known DP role key for Tuya light mode (not in const.py).
DP_KEY_MODE = "mode"

# ---------------------------------------------------------------------------
# Stale-protect: ignore contradictory poll data for this many seconds after
# a command is sent.  Must survive the delayed-refresh round-trip (1.8 s)
# *and* the first regular coordinator poll (~10-30 s) that may still carry
# pre-command values, while remaining short enough to accept real external
# changes (RF remote, wall switch) on subsequent polls.
# ---------------------------------------------------------------------------
STALE_PROTECT_SECONDS: float = 6.0


class BaseContiLight(CoordinatorEntity[ContiCoordinator], LightEntity):
    """Abstract base for all Conti light entities."""

    _attr_has_entity_name = True
    _attr_name = None  # use device name

    # Subclasses may set these class-level defaults; they are also
    # computed dynamically in ``__init__`` via ``_init_color_modes``.
    _attr_supported_color_modes: set[ColorMode]
    _attr_color_mode: ColorMode

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

        # Resolve well-known DP ids
        self._dp_power: str | None = self._find_dp(DP_KEY_POWER)
        self._dp_brightness: str | None = self._find_dp(DP_KEY_BRIGHTNESS)
        self._dp_color_temp: str | None = self._find_dp(DP_KEY_COLOR_TEMP)
        self._dp_rgb: str | None = self._find_dp(DP_KEY_COLOR_RGB)
        self._dp_mode: str | None = self._find_dp(DP_KEY_MODE)

        # Command coalescing / debounce
        self._pending_dps: dict[int, Any] = {}
        self._send_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

        # Anti-bounce tracking
        self._last_sent_dps: dict[str, Any] = {}
        self._last_sent_ts: float = 0.0

        # Cached entity state — authoritative for HA properties.
        self._state_on: bool = False
        self._state_brightness: int | None = None
        self._state_color_temp_kelvin: int | None = None
        self._state_rgb: tuple[int, int, int] | None = None

        # Let each subclass declare its color modes.
        self._init_color_modes()

    # -- Subclass hooks (override as needed) ---------------------------------

    def _init_color_modes(self) -> None:
        """Determine ``_attr_supported_color_modes`` and ``_attr_color_mode``.

        Default implementation auto-detects from available DPs.
        """
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

    # -- DP helpers ----------------------------------------------------------

    def _find_dp(self, key: str) -> str | None:
        """Return the string DP id whose ``key`` matches, or ``None``."""
        for dp_id, info in self._dp_map.items():
            if isinstance(info, dict) and info.get("key") == key:
                return str(dp_id)
        return None

    def _dp_value(self, dp_id: str | None) -> Any:
        """Read raw value for *dp_id* from coordinator data."""
        if dp_id is None:
            return None
        data = self.coordinator.data or {}
        device_data = data.get(self._device_id, {})
        return device_data.get(dp_id)

    def _dp_range(self, dp_id: str | None) -> tuple[int, int]:
        """Return ``(min, max)`` from the dp_map, defaulting to ``(10, 1000)``."""
        if dp_id is None:
            return (10, 1000)
        info = self._dp_map.get(dp_id, {})
        return (info.get("min", 10), info.get("max", 1000))

    # -- HA state properties -------------------------------------------------

    @property
    def available(self) -> bool:
        return (
            self._dp_value(self._dp_power) is not None
            or self.coordinator.last_update_success
            or self.coordinator.device_manager.is_online(self._device_id)
        )

    @property
    def is_on(self) -> bool:
        return self._state_on

    @property
    def brightness(self) -> int | None:
        return self._state_brightness

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._state_color_temp_kelvin

    @property
    def min_color_temp_kelvin(self) -> int:
        return 2000

    @property
    def max_color_temp_kelvin(self) -> int:
        return 6535

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._state_rgb

    # -- Coordinator update handling -----------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        """Only write HA state when the coordinator brings a real change."""
        prev_on = self._state_on
        if self._process_coordinator_data():
            self.async_write_ha_state()
            # Log external power state changes to the HA Activity panel
            if (
                self._dp_power
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
                        "domain": "light",
                    },
                )

    def _is_stale(self, dp_id: str, incoming: Any) -> bool:
        """Return ``True`` if *incoming* for *dp_id* should be ignored.

        Inside the stale-protect window we reject values that contradict
        what we last sent; outside the window everything is accepted.
        """
        if (
            dp_id in self._last_sent_dps
            and (time.monotonic() - self._last_sent_ts) < STALE_PROTECT_SECONDS
            and incoming != self._last_sent_dps[dp_id]
        ):
            return True
        return False

    def _process_coordinator_data(self) -> bool:
        """Update cached entity state from coordinator data.

        Returns ``True`` if at least one cached attribute actually changed.
        Default implementation handles power, brightness, color temp, RGB.
        Subclasses can override for device-specific DP parsing.
        """
        device_data = (self.coordinator.data or {}).get(self._device_id, {})
        if not device_data:
            return False

        changed = False

        # --- Power ---
        if self._dp_power is not None and self._dp_power in device_data:
            incoming = device_data[self._dp_power]
            if not self._is_stale(self._dp_power, incoming):
                new_on = bool(incoming)
                if self._state_on != new_on:
                    self._state_on = new_on
                    changed = True

        # --- Brightness ---
        if self._dp_brightness is not None and self._dp_brightness in device_data:
            raw_br = device_data[self._dp_brightness]
            if not self._is_stale(self._dp_brightness, raw_br):
                lo, hi = self._dp_range(self._dp_brightness)
                new_br = max(1, int((int(raw_br) - lo) / max(hi - lo, 1) * 255))
                if self._state_brightness != new_br:
                    self._state_brightness = new_br
                    changed = True

        # --- Color temperature ---
        if self._dp_color_temp is not None and self._dp_color_temp in device_data:
            raw_ct = device_data[self._dp_color_temp]
            if not self._is_stale(self._dp_color_temp, raw_ct):
                lo, hi = self._dp_range(self._dp_color_temp)
                frac = (int(raw_ct) - lo) / max(hi - lo, 1)
                new_ct = int(2000 + frac * (6535 - 2000))
                if self._state_color_temp_kelvin != new_ct:
                    self._state_color_temp_kelvin = new_ct
                    changed = True

        # --- RGB ---
        if self._dp_rgb is not None and self._dp_rgb in device_data:
            raw_rgb = device_data[self._dp_rgb]
            if not self._is_stale(self._dp_rgb, raw_rgb):
                if isinstance(raw_rgb, str) and len(raw_rgb) >= 6:
                    try:
                        new_rgb = (
                            int(raw_rgb[0:2], 16),
                            int(raw_rgb[2:4], 16),
                            int(raw_rgb[4:6], 16),
                        )
                        if self._state_rgb != new_rgb:
                            self._state_rgb = new_rgb
                            changed = True
                    except ValueError:
                        pass

        # --- Mode → color_mode (RGB-capable lights) ---
        if self._dp_mode is not None and self._dp_mode in device_data:
            raw_mode = device_data[self._dp_mode]
            if not self._is_stale(self._dp_mode, raw_mode):
                new_cm = (
                    ColorMode.RGB
                    if str(raw_mode) == "colour"
                    else ColorMode.COLOR_TEMP
                    if ColorMode.COLOR_TEMP in self._attr_supported_color_modes
                    else self._attr_color_mode
                )
                if self._attr_color_mode != new_cm:
                    self._attr_color_mode = new_cm
                    changed = True

        return changed

    # -- Optimistic helpers --------------------------------------------------

    def _apply_optimistic(self, updates: dict[str, Any]) -> None:
        """Push *updates* into coordinator data, update cache, notify HA."""
        for dp_id, value in updates.items():
            self.coordinator.apply_optimistic_update(
                self._device_id, dp_id, value
            )
            if dp_id == self._dp_power:
                self._state_on = bool(value)
            elif dp_id == self._dp_brightness:
                lo, hi = self._dp_range(self._dp_brightness)
                self._state_brightness = max(
                    1, int((int(value) - lo) / max(hi - lo, 1) * 255)
                )
            elif dp_id == self._dp_color_temp:
                lo, hi = self._dp_range(self._dp_color_temp)
                frac = (int(value) - lo) / max(hi - lo, 1)
                self._state_color_temp_kelvin = int(
                    2000 + frac * (6535 - 2000)
                )
            elif (
                dp_id == self._dp_rgb
                and isinstance(value, str)
                and len(value) >= 6
            ):
                try:
                    self._state_rgb = (
                        int(value[0:2], 16),
                        int(value[2:4], 16),
                        int(value[4:6], 16),
                    )
                except ValueError:
                    pass
        self.async_write_ha_state()

    def _track_sent(self, optimistic: dict[str, Any]) -> None:
        """Record sent DP values for stale-protect."""
        self._last_sent_dps.update(optimistic)
        self._last_sent_ts = time.monotonic()

    # -- Command coalescing / debounce ---------------------------------------

    def _schedule_send(self, dps: dict[int, Any]) -> None:
        """Merge *dps* and (re)start the debounce timer."""
        self._pending_dps.update(dps)
        if self._send_task is not None and not self._send_task.done():
            self._send_task.cancel()
        self._send_task = self.hass.async_create_task(self._debounced_send())

    async def _debounced_send(self) -> None:
        """Wait for the debounce window, then send all accumulated DPs."""
        try:
            await asyncio.sleep(0.12)
        except asyncio.CancelledError:
            return

        while self._pending_dps:
            batch = dict(self._pending_dps)
            self._pending_dps.clear()
            async with self._send_lock:
                try:
                    await self.coordinator.device_manager.set_dps(
                        self._device_id, batch
                    )
                except asyncio.CancelledError:
                    self._pending_dps.update(batch)
                    return
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "Debounced set_dps failed for %s: %s",
                        self._device_id,
                        batch,
                        exc_info=True,
                    )
            self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        """Cancel any pending refresh and schedule a new one."""
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = self.hass.async_create_task(self._delayed_refresh())

    async def _delayed_refresh(self) -> None:
        """Reconcile with the device after a short delay."""
        await asyncio.sleep(1.8)
        await self.coordinator.async_request_refresh()

    async def _send_immediately(self, dps: dict[int, Any]) -> None:
        """Send DPs right now, bypassing debounce."""
        async with self._send_lock:
            try:
                await self.coordinator.device_manager.set_dps(
                    self._device_id, dps
                )
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Immediate set_dps failed for %s: %s",
                    self._device_id,
                    dps,
                    exc_info=True,
                )
        self._schedule_refresh()
