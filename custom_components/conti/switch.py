"""Switch platform for Conti.

Creates a :class:`SwitchEntity` for every boolean DP in the device's dp_map.
This supports single-switch devices, multi-gang devices, and power strips
whose dp_map contains multiple bool DPs (e.g. ``socket_1`` … ``socket_4``).
"""

from __future__ import annotations

import asyncio
import json
import logging
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
        val = self._dp_value()
        return bool(val) if val is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.device_manager.set_dp(
            self._device_id, int(self._dp_id), True
        )
        # Optimistic: reflect new state in UI immediately
        self.coordinator.apply_optimistic_update(
            self._device_id, self._dp_id, True
        )
        # Non-blocking delayed refresh to reconcile with device
        self.hass.async_create_task(self._delayed_refresh())

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.device_manager.set_dp(
            self._device_id, int(self._dp_id), False
        )
        self.coordinator.apply_optimistic_update(
            self._device_id, self._dp_id, False
        )
        self.hass.async_create_task(self._delayed_refresh())

    async def _delayed_refresh(self) -> None:
        """Reconcile with device after a short delay to avoid flapping."""
        await asyncio.sleep(1.0)
        await self.coordinator.async_request_refresh()
