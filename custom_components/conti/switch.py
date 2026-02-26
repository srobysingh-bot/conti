"""Switch platform for Conti.

Creates a :class:`SwitchEntity` for every boolean DP in the device's dp_map.
This supports single-switch devices, multi-gang devices, and power strips
whose dp_map contains multiple bool DPs (e.g. ``socket_1`` … ``socket_4``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DP_MAP,
    DEVICE_TYPE_SWITCH,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import ContiCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_SWITCH:
        return

    coordinator: ContiCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    dp_map: dict[str, Any] = json.loads(
        entry.options.get(CONF_DP_MAP) or entry.data.get(CONF_DP_MAP, "{}")
    )
    device_id: str = entry.data[CONF_DEVICE_ID]

    # Collect ALL DPs whose type is "bool" — covers single-switch, multi-gang,
    # and power-strip devices without requiring a specific "power" key.
    bool_dps: list[tuple[str, str]] = [
        (str(dp_id), info.get("key", f"switch_{dp_id}"))
        for dp_id, info in dp_map.items()
        if isinstance(info, dict) and info.get("type") == "bool"
    ]

    if not bool_dps:
        _LOGGER.warning(
            "Switch device %s has no bool DPs in dp_map — no entities created",
            device_id,
        )
        return

    entities: list[ContiSwitch] = []
    for dp_id, key_name in sorted(bool_dps, key=lambda x: x[0]):
        entities.append(
            ContiSwitch(coordinator, entry, device_id, dp_id, key_name)
        )

    _LOGGER.debug(
        "Creating %d switch entit(y/ies) for %s (DPs: %s)",
        len(entities), device_id, [dp for dp, _ in bool_dps],
    )
    async_add_entities(entities, update_before_add=True)


class ContiSwitch(CoordinatorEntity[ContiCoordinator], SwitchEntity):
    """Representation of a Tuya switch / smart plug (single channel)."""

    _attr_has_entity_name = True

    # Seconds after a command during which contradicting poll values are
    # ignored (stale-data guard).
    _COOLDOWN_SECS: float = 1.5

    def __init__(
        self,
        coordinator: ContiCoordinator,
        entry: ConfigEntry,
        device_id: str,
        dp_id: str,
        key_name: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._dp_id = dp_id

        # Anti-bounce / cooldown state
        self._last_state: bool | None = None
        self._desired: bool | None = None
        self._cooldown_until: float = 0.0
        self._refresh_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()

        self._attr_unique_id = f"{DOMAIN}_{device_id}_switch_{dp_id}"
        self._attr_name = key_name or None
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": entry.title,
            "manufacturer": MANUFACTURER,
        }

    def _dp_value(self) -> Any:
        data = self.coordinator.data or {}
        return data.get(self._device_id, {}).get(self._dp_id)

    @property
    def available(self) -> bool:
        # Prefer cached DP or coordinator health so entities don't flap
        # to "unknown" on transient poll failures.
        return (
            self._dp_value() is not None
            or self.coordinator.last_update_success
            or self.coordinator.device_manager.is_online(self._device_id)
        )

    @property
    def is_on(self) -> bool | None:
        polled = self._dp_value()
        now = time.monotonic()

        # --- Missing DP: fall back to last known state ---
        if polled is None:
            return self._last_state

        polled_bool = bool(polled)

        # --- Cooldown guard: ignore stale contradictions ---
        if now < self._cooldown_until and self._desired is not None:
            if polled_bool != self._desired:
                # Poll contradicts the command we just sent — keep desired
                return self._desired
            # Poll agrees with command — accept & end cooldown early
            self._cooldown_until = 0.0

        self._last_state = polled_bool
        return polled_bool

    # -- Coordinator update filtering ----------------------------------------

    def _handle_coordinator_update(self) -> None:
        """Accept coordinator data but let ``is_on`` filter stale values."""
        self.async_write_ha_state()

    # -- Commands ------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._desired = True
        self._last_state = True
        self._cooldown_until = time.monotonic() + self._COOLDOWN_SECS

        # Optimistic: reflect new state in UI immediately
        self.coordinator.apply_optimistic_update(
            self._device_id, self._dp_id, True
        )
        self.async_write_ha_state()

        # Send in background so the service call returns instantly
        self.hass.async_create_task(self._async_send_dp(True))

        # Debounced delayed refresh
        self._schedule_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._desired = False
        self._last_state = False
        self._cooldown_until = time.monotonic() + self._COOLDOWN_SECS

        self.coordinator.apply_optimistic_update(
            self._device_id, self._dp_id, False
        )
        self.async_write_ha_state()

        self.hass.async_create_task(self._async_send_dp(False))
        self._schedule_refresh()

    # -- Background helpers --------------------------------------------------

    async def _async_send_dp(self, value: bool) -> None:
        """Send the DP to the device; log but do not raise on failure."""
        async with self._send_lock:
            try:
                await self.coordinator.device_manager.set_dp(
                    self._device_id, int(self._dp_id), value
                )
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Background set_dp failed for %s dp=%s value=%s",
                    self._device_id,
                    self._dp_id,
                    value,
                    exc_info=True,
                )

    def _schedule_refresh(self) -> None:
        """Cancel any pending refresh and schedule a new one."""
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = self.hass.async_create_task(
            self._delayed_refresh()
        )

    async def _delayed_refresh(self) -> None:
        """Reconcile with device after a short delay to avoid flapping."""
        await asyncio.sleep(1.0)
        await self.coordinator.async_request_refresh()
