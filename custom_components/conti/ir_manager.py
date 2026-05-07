"""Local-first IR command execution for Conti."""

from __future__ import annotations

import asyncio
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


class IRRemoteIDNotResolved(IRSendError):
    """Tuya AC runtime remote_id is required but missing."""


class IRDeviceUnavailable(IRSendError):
    """Physical IR device is not currently reachable."""


class IRManager:
    """Execute IR commands from local storage with cloud fallback."""

    def __init__(
        self,
        storage: IRStorage,
        cloud: TuyaIRCloud | None = None,
        *,
        local_sender: IRLocalSender | None = None,
        host: str = "",
        port: int = 6668,
    ) -> None:
        self._storage = storage
        self._cloud = cloud
        self._local_sender = local_sender
        self._host = str(host or "").strip()
        self._port = int(port or 6668)
        self._local_support_cache: dict[str, bool] = {}
        self._availability: dict[str, bool] = {}
        self._last_emit: dict[str, Any] | None = None

    def supports_local_ir(self, device_id: str) -> bool:
        """Return whether local IR send should be attempted for this device."""
        if self._local_sender is None:
            self._local_support_cache[device_id] = False
            return False
        return self._local_support_cache.get(device_id, True)

    async def send_ir_command(
        self,
        device_id: str,
        action: str,
        *,
        cloud_fallback: bool = True,
    ) -> bool:
        """Send an IR command by action name.

        Cloud-sourced commands use cloud send. Learned commands try local send
        first, then fall back to cloud when possible.
        """
        if not await self.async_is_device_available(device_id):
            raise IRSendError("ir_device_unavailable")

        command = await self._storage.async_get_command(action)
        if not command:
            raw_command = _raw_command_from_action(action)
            if raw_command is not None:
                self._remember_emit(action, raw_command, "raw")
                _LOGGER.info(
                    "IR execution path device=%s action=%s path=raw payload_length=%s",
                    device_id,
                    action,
                    _payload_length(raw_command),
                )
                if self.supports_local_ir(device_id):
                    if await self._send_local(device_id, raw_command):
                        self._remember_emit(action, raw_command, "local")
                        return True
                if cloud_fallback and await self._send_cloud(device_id, raw_command):
                    self._remember_emit(action, raw_command, "cloud")
                    return True
                raise IRSendError("ir_send_failed")
            _LOGGER.warning(
                "IR command not configured device=%s action=%s",
                device_id,
                action,
            )
            raise IRCommandNotConfigured("command_not_configured")

        source = str(command.get("source", "")).strip() or "cloud"
        if source == "code_pack" and self.supports_local_ir(device_id):
            self._remember_emit(action, command, "code_pack")
            _LOGGER.info(
                "IR execution path device=%s action=%s path=code_pack_local "
                "payload_length=%s",
                device_id,
                action,
                _payload_length(command),
            )
            if await self._send_local(device_id, command):
                self._remember_emit(action, command, "local")
                return True
            if cloud_fallback:
                _LOGGER.warning(
                    "IR code pack local send failed; falling back to cloud "
                    "device=%s action=%s",
                    device_id,
                    action,
                )

        if source in {"cloud", "raw", "code_pack"}:
            self._remember_emit(action, command, source)
            _LOGGER.info(
                "IR execution path device=%s action=%s path=%s payload_length=%s",
                device_id,
                action,
                source,
                _payload_length(command),
            )
            if cloud_fallback and await self._send_cloud(device_id, command):
                return True
            raise IRSendError("ir_send_failed")

        if source == "learned":
            if not self.supports_local_ir(device_id):
                _LOGGER.info(
                    "IR execution path device=%s action=%s path=cloud "
                    "local_supported=false payload_length=%s",
                    device_id,
                    action,
                    _payload_length(command),
                )
                if cloud_fallback and await self._send_cloud(device_id, command):
                    return True
                raise IRSendError("ir_send_failed")

            _LOGGER.info(
                "IR execution path device=%s action=%s path=local payload_length=%s",
                device_id,
                action,
                _payload_length(command),
            )
            if await self._send_local(device_id, command):
                self._remember_emit(action, command, "local")
                return True

            _LOGGER.warning(
                "IR local send failed; falling back to cloud device=%s action=%s",
                device_id,
                action,
            )
            if cloud_fallback and await self._send_cloud(device_id, command):
                self._remember_emit(action, command, "cloud")
                return True
            raise IRSendError("ir_send_failed")

        raise IRSendError("ir_send_failed")

    async def send_ac_command(
        self,
        device_id: str,
        state_payload: dict[str, Any],
    ) -> bool:
        """Send structured AC state via Tuya runtime, when available."""
        if not await self.async_is_device_available(device_id):
            raise IRDeviceUnavailable("ir_device_unavailable")
        if self._cloud is None:
            return False
        remote_id = await self._storage.async_remote_id()
        infrared_id = await self._storage.async_infrared_id()
        if not remote_id:
            _LOGGER.debug(
                "IR AC runtime unavailable device=%s infrared_id=%s "
                "reason=missing_remote_id; using bundled raw fallback",
                device_id,
                infrared_id,
            )
            return False
        try:
            ok = await self._cloud.send_ac_runtime_command(
                device_id,
                state_payload,
                infrared_id=infrared_id,
                remote_id=remote_id,
            )
        except (IRRemoteIDNotResolved, IRDeviceUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("IR AC runtime send failed for %s: %s", device_id, exc)
            return False
        if ok:
            _LOGGER.info("IR execution path device=%s path=ac_runtime ok", device_id)
        return ok

    async def test_raw_emit(
        self,
        device_id: str,
        raw_payload: Any,
        *,
        transport_mode: str = "cloud",
        remote_id: str = "",
    ) -> bool:
        """Direct raw emit test service entrypoint for transport debugging."""
        command = {
            "source": "raw",
            "payload": raw_payload if isinstance(raw_payload, dict) else {"code": raw_payload},
        }
        if remote_id and isinstance(command["payload"], dict):
            command["payload"]["remote_id"] = remote_id
        self._remember_emit("test_ir_raw_emit", command, transport_mode)
        _LOGGER.info(
            "IR test raw emit device=%s transport=%s payload_length=%s",
            device_id,
            transport_mode,
            _payload_length(command),
        )
        if not await self.async_is_device_available(device_id):
            raise IRSendError("ir_device_unavailable")
        if transport_mode == "local":
            return await self._send_local(device_id, command)
        if transport_mode in {"cloud", "raw_runtime", "tuya"}:
            if await self._send_cloud(device_id, command):
                return True
            raise IRSendError("ir_send_failed")
        raise IRSendError(f"unsupported_transport:{transport_mode}")

    async def resend_last(self, device_id: str) -> bool:
        """Replay the last emitted payload through the cloud transport."""
        if not self._last_emit:
            raise IRCommandNotConfigured("last_emit_not_available")
        command = self._last_emit.get("command")
        if not isinstance(command, dict):
            raise IRCommandNotConfigured("last_emit_not_available")
        _LOGGER.info(
            "IR resend last device=%s action=%s transport=%s payload_length=%s",
            device_id,
            self._last_emit.get("action"),
            self._last_emit.get("transport"),
            _payload_length(command),
        )
        if await self._send_cloud(device_id, command):
            return True
        raise IRSendError("ir_send_failed")

    async def async_is_device_available(self, device_id: str) -> bool:
        """Probe the actual IR transport and log availability transitions."""
        online = await self._probe_device_available(device_id)
        previous = self._availability.get(device_id)
        if previous is None:
            _LOGGER.info(
                "IR device %s device=%s",
                "online" if online else "offline",
                device_id,
            )
        elif online and not previous:
            _LOGGER.info("IR device online device=%s reconnect success", device_id)
        elif previous and not online:
            _LOGGER.warning("IR device offline device=%s reconnect failed", device_id)
        self._availability[device_id] = online
        return online

    async def _probe_device_available(self, device_id: str) -> bool:
        if self._host:
            return await _async_probe_tcp(self._host, self._port)
        if self._cloud is not None:
            try:
                return bool(await self._cloud.is_device_online(device_id))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "IR cloud availability probe failed device=%s error=%s",
                    device_id,
                    exc,
                )
                return False
        return False

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
        command = await self._with_runtime_remote(command)
        try:
            ok = await self._cloud.send_command(device_id, command)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("IR cloud send failed for %s: %s", device_id, exc)
            return False
        if ok:
            _LOGGER.info("IR execution path device=%s path=cloud ok", device_id)
        return ok

    async def _with_runtime_remote(self, command: dict[str, Any]) -> dict[str, Any]:
        """Attach stored runtime remote metadata to raw command payloads."""
        payload = command.get("payload")
        if not isinstance(payload, dict):
            return command
        if payload.get("remote_id"):
            return command
        remote_id = await self._storage.async_remote_id()
        if not remote_id:
            return command
        return {
            **command,
            "payload": {
                **payload,
                "remote_id": remote_id,
            },
        }

    def _remember_emit(
        self,
        action: str,
        command: dict[str, Any],
        transport: str,
    ) -> None:
        self._last_emit = {
            "action": action,
            "command": command,
            "transport": transport,
            "payload_length": _payload_length(command),
        }


def _raw_command_from_action(action: str) -> dict[str, Any] | None:
    action = str(action).strip()
    if not action.startswith(("raw:", "base64:")):
        return None
    return {
        "source": "raw",
        "payload": {"code": action.split(":", 1)[1].strip()},
    }


def _payload_length(command: dict[str, Any]) -> int:
    payload = command.get("payload", command)
    if isinstance(payload, dict):
        for key in ("code", "base64", "data", "payload"):
            value = payload.get(key)
            if value not in (None, "", {}, []):
                return len(str(value))
    return len(str(payload))


async def _async_probe_tcp(host: str, port: int) -> bool:
    """Return whether a TCP connection can be opened to the IR hub."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=3,
        )
    except (OSError, TimeoutError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass
    return True
