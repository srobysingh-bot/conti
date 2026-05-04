"""Remote entities for Conti IR devices."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from homeassistant.components.remote import RemoteEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_ID,
    CONF_IR_BRAND,
    CONF_IR_CATEGORY,
    CONF_IR_MODEL,
    CONF_RUNTIME_CHANNEL,
    DOMAIN,
    MANUFACTURER,
    RUNTIME_CHANNEL_IR,
)
from .ir_manager import IRCommandNotConfigured, IRManager, IRSendError
from .ir_storage import IRStorage

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Conti remote entities."""
    if entry.data.get(CONF_RUNTIME_CHANNEL) != RUNTIME_CHANNEL_IR:
        return

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    storage = entry_data.get("ir_storage")
    manager = entry_data.get("ir_manager")
    device_id = str(entry.data.get(CONF_DEVICE_ID, "")).strip()
    if (
        not device_id
        or not isinstance(storage, IRStorage)
        or not isinstance(manager, IRManager)
    ):
        _LOGGER.warning(
            "Conti IR remote setup skipped for incomplete entry %s",
            entry.entry_id,
        )
        return

    async_add_entities([ContiIRRemote(entry, device_id, storage, manager)], True)


class ContiIRRemote(RemoteEntity):
    """Home Assistant remote entity backed by Conti IR command storage."""

    _attr_has_entity_name = True
    _attr_name = "IR Remote"

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        storage: IRStorage,
        manager: IRManager,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._storage = storage
        self._manager = manager
        self._commands: dict[str, dict[str, Any]] = {}
        self._attr_unique_id = f"{entry.entry_id}_ir_remote"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "manufacturer": MANUFACTURER,
            "name": entry.title,
            "model": str(entry.data.get(CONF_IR_MODEL, "IR Remote") or "IR Remote"),
        }

    @property
    def is_on(self) -> bool:
        """Return whether the remote is available for use."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose stored command names so the UI is never empty."""
        if not self._commands:
            return {
                "reason": "no_ir_commands_available",
                "message": "No IR library found. Use learning mode.",
                "commands": [],
                "command_count": 0,
                "category": self._entry.data.get(CONF_IR_CATEGORY, ""),
                "brand": self._entry.data.get(CONF_IR_BRAND, ""),
                "model": self._entry.data.get(CONF_IR_MODEL, ""),
            }
        return {
            "commands": sorted(self._commands),
            "command_count": len(self._commands),
            "category": self._entry.data.get(CONF_IR_CATEGORY, ""),
            "brand": self._entry.data.get(CONF_IR_BRAND, ""),
            "model": self._entry.data.get(CONF_IR_MODEL, ""),
        }

    async def async_added_to_hass(self) -> None:
        """Load commands when the entity is added."""
        await self.async_update()

    async def async_update(self) -> None:
        """Refresh the visible command list from storage."""
        self._commands = await self._storage.async_all_commands()
        self._attr_available = bool(self._commands)
        if not self._commands:
            _LOGGER.warning(
                "IR: No library found for device %s; use learning mode",
                self._device_id,
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Remote devices are always ready; no-op turn on."""

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Remote devices are always ready; no-op turn off."""

    async def async_send_command(
        self,
        command: Iterable[str] | str,
        **kwargs: Any,
    ) -> None:
        """Send one or more stored IR commands."""
        commands = [command] if isinstance(command, str) else list(command)
        for action in commands:
            try:
                await self._manager.send_ir_command(self._device_id, str(action))
            except IRCommandNotConfigured as exc:
                raise HomeAssistantError("ir_command_not_found") from exc
            except IRSendError as exc:
                raise HomeAssistantError("ir_send_failed") from exc

        await self.async_update()
        self.async_write_ha_state()
