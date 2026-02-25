"""Switch platform for Conti.

Maps Tuya ``power`` DP (bool) to HA :class:`SwitchEntity`.
Supports multi-gang devices: if the dp_map contains multiple DPs
with ``"key": "power"``, each one becomes a separate switch entity.
"""

from __future__ import annotations

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
    if entry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_SWITCH:
        return

    coordinator: ContiCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    dp_map: dict[str, Any] = json.loads(
        entry.options.get(CONF_DP_MAP) or entry.data.get(CONF_DP_MAP, "{}")
    )
    device_id: str = entry.data[CONF_DEVICE_ID]

    # Collect ALL DPs with key="power" for multi-gang support.
    power_dps: list[str] = [
        str(dp_id)
        for dp_id, info in dp_map.items()
        if isinstance(info, dict) and info.get("key") == DP_KEY_POWER
    ]

    if not power_dps:
        _LOGGER.warning(
            "Switch device %s has no power DP in dp_map — no entities created",
            device_id,
        )
        return

    multi = len(power_dps) > 1
    entities: list[ContiSwitch] = []
    for idx, dp_id in enumerate(sorted(power_dps), start=1):
        suffix = f" {idx}" if multi else ""
        entities.append(
            ContiSwitch(coordinator, entry, device_id, dp_id, suffix)
        )

    _LOGGER.debug(
        "Creating %d switch entit(y/ies) for %s (DPs: %s)",
        len(entities), device_id, power_dps,
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
        name_suffix: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._dp_id = dp_id

        self._attr_unique_id = f"{DOMAIN}_{device_id}_switch_{dp_id}"
        self._attr_name = f"Switch{name_suffix}" if name_suffix else None
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
        return self.coordinator.device_manager.is_online(self._device_id)

    @property
    def is_on(self) -> bool | None:
        val = self._dp_value()
        return bool(val) if val is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.device_manager.set_dp(
            self._device_id, int(self._dp_id), True
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.device_manager.set_dp(
            self._device_id, int(self._dp_id), False
        )
        await self.coordinator.async_request_refresh()
