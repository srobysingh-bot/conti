"""Persistent storage for Conti IR command libraries."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .ir_code_packs import (
    PACK_DIR_NAME,
    PACK_SCHEMA_VERSION,
    async_export_code_pack,
    async_load_code_pack,
    normalize_code_pack,
)
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
                    "type": "",
                    "category": "",
                    "brand": "",
                    "model": "",
                    "infrared_id": "",
                    "category_id": "",
                    "brand_id": "",
                    "remote_id": "",
                    "commands": {},
                }
            data.setdefault("device_id", self._device_id)
            data.setdefault("type", "")
            data.setdefault("infrared_id", "")
            data.setdefault("category_id", "")
            data.setdefault("brand_id", "")
            data.setdefault("remote_id", "")
            data.setdefault("commands", {})
            self._data = data
            await self._async_import_default_pack_if_empty()
            _LOGGER.debug(
                "Loaded IR library for %s (%d commands)",
                self._device_id,
                len(self._data.get("commands", {})),
            )
            return self._data

    async def async_save_library(
        self,
        *,
        category: str,
        brand: str,
        model: str,
        commands: dict[str, dict[str, Any]],
        profile_type: str = "",
        infrared_id: str = "",
        category_id: str = "",
        brand_id: str = "",
        remote_id: str = "",
    ) -> None:
        """Persist a complete cloud-fetched command library."""
        normalized_commands: dict[str, dict[str, Any]] = {}
        for action, command in commands.items():
            normalized_commands[normalize_ir_action(action)] = command
        async with self._lock:
            self._data = {
                "device_id": self._device_id,
                "type": profile_type,
                "category": category,
                "brand": brand,
                "model": model,
                "infrared_id": infrared_id,
                "category_id": category_id,
                "brand_id": brand_id,
                "remote_id": remote_id,
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

    async def async_update_runtime_metadata(
        self,
        *,
        infrared_id: str = "",
        remote_id: str = "",
        category_id: str = "",
        brand_id: str = "",
    ) -> None:
        """Persist runtime Tuya IR identifiers without replacing commands."""
        await self.async_load()
        async with self._lock:
            data = self._data or {"device_id": self._device_id, "commands": {}}
            if infrared_id:
                data["infrared_id"] = infrared_id
            if remote_id:
                data["remote_id"] = remote_id
            if category_id:
                data["category_id"] = category_id
            if brand_id:
                data["brand_id"] = brand_id
            self._data = data
            await self._store.async_save(data)

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
                "type": "",
                "category": "",
                "brand": "",
                "model": "",
                "infrared_id": "",
                "category_id": "",
                "brand_id": "",
                "remote_id": "",
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

    async def async_import_code_pack(
        self,
        pack: dict[str, Any],
        *,
        overwrite: bool = False,
    ) -> int:
        """Import raw IR commands from a normalized local code pack."""
        normalized = normalize_code_pack(pack)
        imported = 0
        await self.async_load()
        async with self._lock:
            data = self._data or {
                "device_id": self._device_id,
                "type": "",
                "category": "",
                "brand": "",
                "model": "",
                "infrared_id": "",
                "category_id": "",
                "brand_id": "",
                "remote_id": "",
                "commands": {},
            }
            commands = data.setdefault("commands", {})
            if not isinstance(commands, dict):
                commands = {}
                data["commands"] = commands
            for action, command in normalized["commands"].items():
                if action in commands and not overwrite:
                    continue
                commands[action] = command
                imported += 1
            if normalized.get("type"):
                data["type"] = normalized["type"]
            if normalized.get("manufacturer"):
                data["brand"] = normalized["manufacturer"]
            if normalized.get("model"):
                data["model"] = normalized["model"]
            self._data = data
            await self._store.async_save(data)
        _LOGGER.info(
            "Imported IR code pack for %s commands=%d overwrite=%s",
            self._device_id,
            imported,
            overwrite,
        )
        return imported

    async def async_import_code_pack_file(
        self,
        path: str | Path,
        *,
        overwrite: bool = False,
    ) -> int:
        """Import a JSON/YAML raw IR code pack from disk."""
        pack = await async_load_code_pack(Path(path))
        return await self.async_import_code_pack(pack, overwrite=overwrite)

    async def async_export_code_pack_file(self, path: str | Path) -> None:
        """Export stored commands as a JSON raw IR code pack."""
        data = await self.async_load()
        commands = data.get("commands", {})
        await async_export_code_pack(
            Path(path),
            {
                "schema_version": PACK_SCHEMA_VERSION,
                "manufacturer": data.get("brand", ""),
                "model": data.get("model", ""),
                "type": data.get("type", ""),
                "commands": commands if isinstance(commands, dict) else {},
            },
        )

    async def async_all_commands(self) -> dict[str, dict[str, Any]]:
        """Return all commands for this device."""
        data = await self.async_load()
        commands = data.get("commands", {})
        return commands if isinstance(commands, dict) else {}

    async def async_profile_type(self) -> str:
        """Return the stored IR profile type, such as ``ac``."""
        data = await self.async_load()
        profile_type = str(data.get("type") or "").strip().lower()
        if profile_type:
            return profile_type
        category = str(data.get("category") or "").strip().lower().replace("-", "_")
        if category in {"ac", "air_conditioner", "air conditioner", "airconditioner", "kt", "5"}:
            return "ac"
        return ""

    async def async_remote_id(self) -> str:
        """Return the Tuya remote_id created for this IR library."""
        data = await self.async_load()
        return str(data.get("remote_id") or "").strip()

    async def async_infrared_id(self) -> str:
        """Return the Tuya infrared_id for this IR hub."""
        data = await self.async_load()
        return str(data.get("infrared_id") or "").strip()

    async def _async_import_default_pack_if_empty(self) -> None:
        """Auto-import a local pack named after the device when storage is empty."""
        if self._data is None:
            return
        commands = self._data.get("commands", {})
        if isinstance(commands, dict) and commands:
            return

        pack_dir = Path(self._hass.config.path(PACK_DIR_NAME))
        for suffix in (".json", ".yaml", ".yml"):
            path = pack_dir / f"{self._device_id}{suffix}"
            if not path.exists():
                continue
            try:
                pack = await async_load_code_pack(path)
                commands = self._data.setdefault("commands", {})
                if not isinstance(commands, dict):
                    commands = {}
                    self._data["commands"] = commands
                commands.update(pack["commands"])
                if pack.get("type"):
                    self._data["type"] = pack["type"]
                if pack.get("manufacturer"):
                    self._data["brand"] = pack["manufacturer"]
                if pack.get("model"):
                    self._data["model"] = pack["model"]
                await self._store.async_save(self._data)
                _LOGGER.info("Auto-imported IR code pack %s", path)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("IR code pack import failed path=%s error=%s", path, exc)
            return
