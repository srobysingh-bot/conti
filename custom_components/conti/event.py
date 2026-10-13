"""Explicitly mapped gateway reports for physical remote automations.

Button meanings must be verified and supplied in the DP map's ``events`` dict.
Neither a poll result nor a saved setting is treated as a physical press.
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CONNECTION_TYPE,
    CONF_DEVICE_ID,
    CONF_DEVICE_METADATA,
    CONF_DP_MAP,
    CONF_GATEWAY_DEVICE_ID,
    CONNECTION_TYPE_GATEWAY_CHILD,
    DOMAIN,
)
from .coordinator import ContiCoordinator
from .gateway import GatewayChild


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    """Create events only from explicit gateway-child event mappings."""
    if entry.data.get(CONF_CONNECTION_TYPE) != CONNECTION_TYPE_GATEWAY_CHILD:
        return
    raw = entry.options.get(CONF_DP_MAP) or entry.data.get(CONF_DP_MAP, "{}")
    mapping = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(mapping, dict):
        return
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    client = coordinator.device_manager.get_client(entry.data[CONF_DEVICE_ID])
    if not isinstance(client, GatewayChild):
        return
    entities = []
    for dp_id, info in mapping.items():
        if not isinstance(info, dict) or info.get("type") != "event":
            continue
        events = info.get("events")
        if not isinstance(events, dict) or not events or not all(
            isinstance(value, str) and value for value in events.values()
        ):
            continue
        entities.append(ContiGatewayEvent(coordinator, client, entry, str(dp_id), events))
    if entities:
        async_add_entities(entities)


class ContiGatewayEvent(CoordinatorEntity[ContiCoordinator], EventEntity):
    """A physical report mapped to an action, including repeated equal reports."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = EventDeviceClass.BUTTON

    def __init__(
        self, coordinator: ContiCoordinator, client: GatewayChild, entry: ConfigEntry,
        dp_id: str, events: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._client = client
        self._dp_id = dp_id
        self._events = events
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_DEVICE_ID]}_event_{dp_id}"
        self._attr_name = f"Button reports DP {dp_id}"
        self._attr_event_types = list(dict.fromkeys(events.values()))
        metadata = entry.data.get(CONF_DEVICE_METADATA, {})
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.data[CONF_DEVICE_ID])},
            "name": entry.title,
            "manufacturer": metadata.get("manufacturer") or "Tuya",
            "model": metadata.get("model") or metadata.get("product_name") or None,
            "via_device": (DOMAIN, entry.data[CONF_GATEWAY_DEVICE_ID]),
        }

    @property
    def available(self) -> bool:
        return self._coordinator.is_device_available()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._client.subscribe_reports(self._handle_report))

    def _handle_report(self, dps: dict[str, Any]) -> None:
        if self._dp_id not in dps:
            return
        value = dps[self._dp_id]
        action = self._events.get(str(value))
        if action is not None:
            self._trigger_event(action, {"dp_id": self._dp_id, "value": value})
            self.async_write_ha_state()
