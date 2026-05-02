"""Local-first IR command execution for Conti."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .ir_cloud import TuyaIRCloud
from .ir_storage import IRStorage

_LOGGER = logging.getLogger(__name__)

IRLocalSender = Callable[[str, dict[str, Any]], Awaitable[bool]]


class IRCommandError(Exception):
    """Base IR command execution error."""


class IRCommandNotConfigured(IRCommandError):
    """Requested IR action is not configured."""


class IRSendError(IRCommandError):
    """IR command failed on all available execution paths."""


class IRManager:
    """Execute IR commands from local storage with cloud fallback."""

    def __init__(
        self,
        storage: IRStorage,
        cloud: TuyaIRCloud | None = None,
        *,
        local_sender: IRLocalSender | None = None,
    ) -> None:
        self._storage = storage
        self._cloud = cloud
        self._local_sender = local_sender
        self._local_support_cache: dict[str, bool] = {}

    def supports_local_ir(self, device_id: str) -> bool:
        """Return whether local IR send should be attempted for this device."""
        if self._local_sender is None:
            self._local_support_cache[device_id] = False
            return False
        return self._local_support_cache.get(device_id, True)

    async def send_ir_command(self, device_id: str, action: str) -> bool:
        """Send an IR command by action name.

        Cloud-sourced commands use cloud send. Learned commands try local send
        first, then fall back to cloud when possible.
        """
        command = await self._storage.async_get_command(action)
        if not command:
            _LOGGER.warning(
                "IR command not configured device=%s action=%s",
                device_id,
                action,
            )
            raise IRCommandNotConfigured("command_not_configured")

        source = str(command.get("source", "")).strip() or "cloud"
        if source == "cloud":
            _LOGGER.info(
                "IR execution path device=%s action=%s path=cloud",
                device_id,
                action,
            )
            if await self._send_cloud(device_id, command):
                return True
            raise IRSendError("ir_send_failed")

        if source == "learned":
            if not self.supports_local_ir(device_id):
                _LOGGER.info(
                    "IR execution path device=%s action=%s path=cloud local_supported=false",
                    device_id,
                    action,
                )
                if await self._send_cloud(device_id, command):
                    return True
                raise IRSendError("ir_send_failed")

            _LOGGER.info(
                "IR execution path device=%s action=%s path=local",
                device_id,
                action,
            )
            if await self._send_local(device_id, command):
                return True

            _LOGGER.warning(
                "IR local send failed; falling back to cloud device=%s action=%s",
                device_id,
                action,
            )
            if await self._send_cloud(device_id, command):
                return True
            raise IRSendError("ir_send_failed")

        raise IRSendError("ir_send_failed")

    async def _send_local(self, device_id: str, command: dict[str, Any]) -> bool:
        if self._local_sender is None:
            self._local_support_cache[device_id] = False
            return False
        try:
            ok = bool(await self._local_sender(device_id, command))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("IR local send failed for %s: %s", device_id, exc)
            ok = False
        self._local_support_cache[device_id] = ok
        return ok

    async def _send_cloud(self, device_id: str, command: dict[str, Any]) -> bool:
        if self._cloud is None:
            return False
        try:
            ok = await self._cloud.send_command(device_id, command)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("IR cloud send failed for %s: %s", device_id, exc)
            return False
        if ok:
            _LOGGER.info("IR execution path device=%s path=cloud ok", device_id)
        return ok
