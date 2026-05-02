"""Persistent storage for Conti IR command libraries."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .ir_actions import ir_action_aliases, normalize_ir_action

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}_ir"


class IRCommandExistsError(ValueError):
    """Raised when learning would overwrite an existing IR command."""


class IRStorage:
    """Async HA Store-backed cache for one IR device."""

    def __init__(self, hass: HomeAssistant, device_id: str) -> None:
        self._hass = hass
        self._device_id = device_id
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{device_id}"
        )
        self._data: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    async def async_load(self) -> dict[str, Any]:
        """Load the IR command library once and return cached data."""
        if self._data is not None:
            return self._data

        async with self._lock:
            if self._data is not None:
                return self._data

            data = await self._store.async_load()
            if not isinstance(data, dict):
                data = {
                    "device_id": self._device_id,
                    "category": "",
                    "brand": "",
                    "model": "",
                    "commands": {},
                }
            data.setdefault("device_id", self._device_id)
            data.setdefault("commands", {})
            self._data = data
            _LOGGER.debug(
                "Loaded IR library for %s (%d commands)",
                self._device_id,
                len(data.get("commands", {})),
            )
            return self._data

    async def async_save_library(
        self,
        *,
        category: str,
        brand: str,
        model: str,
        commands: dict[str, dict[str, Any]],
    ) -> None:
        """Persist a complete cloud-fetched command library."""
        normalized_commands: dict[str, dict[str, Any]] = {}
        for action, command in commands.items():
            normalized_commands[normalize_ir_action(action)] = command
        async with self._lock:
            self._data = {
                "device_id": self._device_id,
                "category": category,
                "brand": brand,
                "model": model,
                "commands": normalized_commands,
            }
            await self._store.async_save(self._data)
            _LOGGER.info(
                "Stored IR library for %s (%s/%s/%s, %d commands)",
                self._device_id,
                category,
                brand,
                model,
                len(normalized_commands),
            )

    async def async_get_command(
        self, action: str
    ) -> dict[str, Any] | None:
        """Return a command payload by action name."""
        data = await self.async_load()
        commands = data.get("commands", {})
        if not isinstance(commands, dict):
            return None
        for candidate in ir_action_aliases(action):
            command = commands.get(candidate)
            if isinstance(command, dict):
                return command
        return None

    async def async_set_command(
        self,
        action: str,
        payload: Any,
        *,
        source: str = "learned",
        overwrite: bool = False,
    ) -> None:
        """Persist or overwrite one IR command."""
        if payload in (None, "", {}, []):
            raise ValueError("IR payload is empty")

        normalized_action = normalize_ir_action(action)
        await self.async_load()
        async with self._lock:
            data = self._data or {
                "device_id": self._device_id,
                "category": "",
                "brand": "",
                "model": "",
                "commands": {},
            }
            commands = data.setdefault("commands", {})
            if not isinstance(commands, dict):
                commands = {}
                data["commands"] = commands
            if normalized_action in commands and not overwrite:
                raise IRCommandExistsError(
                    f"IR command already exists: {normalized_action}"
                )
            commands[normalized_action] = {"source": source, "payload": payload}
            self._data = data
            await self._store.async_save(data)
            _LOGGER.info(
                "Stored %s IR command for %s action=%s",
                source,
                self._device_id,
                normalized_action,
            )

    async def async_all_commands(self) -> dict[str, dict[str, Any]]:
        """Return all commands for this device."""
        data = await self.async_load()
        commands = data.get("commands", {})
        return commands if isinstance(commands, dict) else {}
