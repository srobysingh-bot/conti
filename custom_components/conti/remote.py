"""Remote entities for Conti IR devices."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from homeassistant.components.remote import RemoteEntity

try:
    from homeassistant.components.remote import RemoteEntityFeature
except ImportError:  # pragma: no cover - older Home Assistant versions
    RemoteEntityFeature = None  # type: ignore[assignment]
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_ID,
    CONF_IR_BRAND,
    CONF_IR_CATEGORY,
    CONF_IR_MODEL,
    CONF_IR_REMOTE_ID,
    CONF_RUNTIME_CHANNEL,
    DOMAIN,
    MANUFACTURER,
    RUNTIME_CHANNEL_IR,
)
from .ir_learning import IRLearningError, IRLearningSession
from .ir_manager import IRCommandNotConfigured, IRManager, IRSendError
from .ir_storage import IRStorage

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)


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

    _LOGGER.info("Registering Conti IR remote entity for device %s", device_id)
    async_add_entities([ContiIRRemote(entry, device_id, storage, manager)], True)


class ContiIRRemote(RemoteEntity):
    """Home Assistant remote entity backed by Conti IR command storage."""

    _attr_has_entity_name = True
    _attr_name = "IR Remote"
    _attr_available = False
    _attr_should_poll = True
    if RemoteEntityFeature is not None and hasattr(
        RemoteEntityFeature, "LEARN_COMMAND"
    ):
        _attr_supported_features = RemoteEntityFeature.LEARN_COMMAND

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
        self._library_supported = True
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
        self._attr_available = await self._manager.async_is_device_available(
            self._device_id
        )
        self._library_supported = bool(
            any(
                str(command.get("source", "")).strip() == "cloud"
                for command in self._commands.values()
                if isinstance(command, dict)
            )
        )
        if not self._commands:
            _LOGGER.warning(
                "IR cloud library unavailable, continuing in raw mode device=%s",
                self._device_id,
            )
            self._library_supported = False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Remote devices are always ready; no-op turn on."""

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Remote devices are always ready; no-op turn off."""

    async def async_send_command(
        self,
        command: Iterable[str] | str,
        **kwargs: Any,
    ) -> None:
        """Send one or more stored or raw IR commands."""
        commands = [command] if isinstance(command, str) else list(command)
        for action in commands:
            try:
                await self._manager.send_ir_command(self._device_id, str(action))
            except IRCommandNotConfigured as exc:
                if await self._async_send_raw_command(str(action), kwargs):
                    continue
                raise HomeAssistantError("ir_command_not_found") from exc
            except IRSendError as exc:
                raise HomeAssistantError("ir_send_failed") from exc

        await self.async_update()
        self.async_write_ha_state()

    async def async_learn_command(
        self,
        device: str | None = None,
        command: Iterable[str] | str | None = None,
        **kwargs: Any,
    ) -> None:
        """Learn or store one or more IR commands without requiring a library."""
        if command is None:
            raise HomeAssistantError("ir_command_required")

        commands = [command] if isinstance(command, str) else list(command)
        session = IRLearningSession(
            self._storage,
            cloud=getattr(self._manager, "_cloud", None),
            remote_id="",
        )
        provided_payload = _raw_payload_from_kwargs(kwargs)
        for action in commands:
            try:
                payload = provided_payload
                if payload is None:
                    learning_time = await session.start_learning(self._device_id)
                    payload = await session.capture_learned_payload(
                        self._device_id,
                        learning_time,
                    )
                await session.learn_command(
                    self._device_id,
                    str(action),
                    payload,
                    overwrite=bool(kwargs.get("overwrite", False)),
                )
            except IRLearningError as exc:
                raise HomeAssistantError("ir_learn_failed") from exc

        await self.async_update()
        self.async_write_ha_state()

    async def _async_send_raw_command(
        self,
        action: str,
        kwargs: dict[str, Any],
    ) -> bool:
        """Send raw IR payloads directly through the IR cloud raw endpoint."""
        raw_payload = _raw_payload_from_command(action) or _raw_payload_from_kwargs(kwargs)
        if raw_payload is None:
            return False
        if not await self._manager.async_is_device_available(self._device_id):
            return False

        cloud = getattr(self._manager, "_cloud", None)
        if cloud is None or not hasattr(cloud, "send_raw_command"):
            return False

        remote_id = str(
            kwargs.get("remote_id")
            or self._entry.data.get(CONF_IR_REMOTE_ID)
            or await self._storage.async_remote_id()
            or ""
        ).strip()
        try:
            return bool(
                await cloud.send_raw_command(
                    self._device_id,
                    raw_payload,
                    remote_id=remote_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("IR raw command failed device=%s: %s", self._device_id, exc)
            return False


def _raw_payload_from_command(command: str) -> Any | None:
    """Return a raw payload when the service command itself is raw IR data."""
    command = str(command).strip()
    if not command:
        return None
    if command.startswith(("raw:", "base64:")):
        return {"code": command.split(":", 1)[1].strip()}
    return None


def _raw_payload_from_kwargs(kwargs: dict[str, Any]) -> Any | None:
    """Extract raw IR payload from common service-call fields."""
    for key in ("raw", "code", "payload", "data"):
        value = kwargs.get(key)
        if value not in (None, "", {}, []):
            return value
    return None
