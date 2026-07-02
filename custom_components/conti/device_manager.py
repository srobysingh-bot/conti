"""Device manager - singleton that manages connections to all Tuya devices.

This is the **only** layer that owns `TinyTuyaDevice` instances.
HA entities never touch sockets directly; they read state through
the :class:DeviceManager and issue commands through it.

Singleton
~~~~~~~~~
One `DeviceManager` is created at `hass.data[DOMAIN]["manager"]`
and shared across **all** config entries.  Devices are keyed by
`device_id` - each device independently selects its protocol
version and maintains its own connection lifecycle.

Responsibilities
~~~~~~~~~~~~~~~~
* Open / maintain / reconnect TCP connections.
* Per-device `asyncio.Lock` to prevent overlapping I/O.
* Per-device diagnostic state (protocol version, errors, frame hex).
* Exponential back-off with jitter for reconnect attempts.
* Error classification for diagnostics.
* Verify non-empty status after every connect to confirm "fully online".
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Optional

from .const import (
    DEFAULT_ENABLE_AUTO_RECONNECT,
    DEFAULT_PORT,
    DEFAULT_PROTOCOL_VERSION,
    ERROR_LOG_COOLDOWN,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
)
from .dp_mapping import mask_key
from .tinytuya_client import TinyTuyaDevice

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error classification strings (for diagnostics)
# ---------------------------------------------------------------------------
ERR_NONE = ""
ERR_PORT_CLOSED = "port_closed"
ERR_TIMEOUT = "timeout"
ERR_HANDSHAKE = "handshake_mismatch"
ERR_DECRYPT = "decrypt_failed"
ERR_RESET = "reset_by_peer"
ERR_MALFORMED = "malformed_payload"
ERR_SUBNET = "subnet_unreachable"
ERR_EOF = "eof"
ERR_EMPTY_STATUS = "empty_status"
ERR_UNKNOWN = "unknown"

# Minimum seconds between consecutive WARNING-level disconnect messages
# for the same device (covers both the push-listener and the disconnect
# callback paths).  Repeated drops within this window are demoted to
# DEBUG.  The window runs from the last WARNING; it is NOT reset on
# reconnect so flapping devices are suppressed across quick reconnect
# cycles.  A fresh WARNING fires naturally once the device has been
# stable for ≥ 600 s since the last WARNING.
_PUSH_WARN_COOLDOWN: float = ERROR_LOG_COOLDOWN


@dataclass
class DeviceDiagnostics:
    """Per-device diagnostic state exposed via `get_device_diagnostics`."""

    protocol_version: str = "unknown"
    auto_detected: bool = False
    last_handshake_ok: float = 0.0
    last_status_ok: float = 0.0
    last_error: str = ""
    last_local_error: str = ""
    last_error_class: str = ERR_NONE
    last_tx_hex: str = ""
    last_rx_hex: str = ""
    consecutive_failures: int = 0
    last_local_update: str | None = None
    reconnect_attempts: int = 0


class _ManagedDevice:
    """Internal wrapper around a single device connection."""

    __slots__ = (
        "client",
        "config",
        "reconnect_delay",
        "reconnect_task",
        "listener_task",
        "online",
        "lock",
        "diag",
        "cloud_fallback",
        "local_status",
        "control_path",
        "diagnostic_reason",
        "cloud_refresh_task",
        "_last_push_warn_time",
        "next_retry_time",
        "error_log_times",
        "cloud_error",
        "cloud_error_code",
        "cloud_error_message",
    )

    def __init__(self, client: TinyTuyaDevice, config: dict[str, Any]) -> None:
        self.client = client
        self.config = config
        self.reconnect_delay: float = RECONNECT_BASE_DELAY
        self.reconnect_task: Optional[asyncio.Task[None]] = None
        self.listener_task: Optional[asyncio.Task[None]] = None
        self.online: bool = False
        self.lock: asyncio.Lock = asyncio.Lock()
        self.diag: DeviceDiagnostics = DeviceDiagnostics()
        self.cloud_fallback: Any | None = None
        self.local_status: str = "unknown"
        self.control_path: str = "local"
        self.diagnostic_reason: str = ""
        self.cloud_refresh_task: Optional[asyncio.Task[None]] = None
        self._last_push_warn_time: float = 0.0
        self.next_retry_time: str | None = None
        self.error_log_times: dict[str, float] = {}
        self.cloud_error: str = ""
        self.cloud_error_code: str = ""
        self.cloud_error_message: str = ""


class DeviceManager:
    """Manages all Tuya device connections for the integration.

    **Singleton**: one instance per `hass`, shared across all config entries.
    Each device independently selects and locks its protocol version.
    """

    def __init__(self) -> None:
        self._devices: dict[str, _ManagedDevice] = {}
        # Per-device: *set* of callbacks so multiple listeners can register.
        self._state_callbacks: dict[
            str, set[Callable[[str, dict[str, Any]], None]]
        ] = {}
        self._running = False

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start the manager (called once during first entry setup)."""
        self._running = True

    async def stop(self) -> None:
        """Close every connection and cancel pending reconnects/listeners."""
        self._running = False
        cancelled: list[asyncio.Task[None]] = []
        for dev in self._devices.values():
            if dev.listener_task and not dev.listener_task.done():
                dev.listener_task.cancel()
                cancelled.append(dev.listener_task)
            if dev.reconnect_task and not dev.reconnect_task.done():
                dev.reconnect_task.cancel()
                cancelled.append(dev.reconnect_task)
            if dev.cloud_refresh_task and not dev.cloud_refresh_task.done():
                dev.cloud_refresh_task.cancel()
                cancelled.append(dev.cloud_refresh_task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        for dev in self._devices.values():
            await dev.client.close()
        self._devices.clear()

    # -- State callbacks (per-device, supports multiple listeners) -----------

    def register_state_callback(
        self, device_id: str, callback: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """Register a push callback for state pushes.  Multiple allowed."""
        self._state_callbacks.setdefault(device_id, set()).add(callback)

    def unregister_state_callback(
        self,
        device_id: str,
        callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        """Remove a specific callback, or *all* callbacks if callback is None."""
        if callback is None:
            self._state_callbacks.pop(device_id, None)
        else:
            s = self._state_callbacks.get(device_id)
            if s:
                s.discard(callback)
                if not s:
                    del self._state_callbacks[device_id]

    # -- Device registration -------------------------------------------------

    @staticmethod
    def _new_client(config: dict[str, Any]) -> TinyTuyaDevice:
        """Build a completely fresh local client from saved entry config."""
        client = TinyTuyaDevice(
            device_id=config["device_id"],
            ip=config["host"],
            local_key=config["local_key"],
            version=config.get("protocol_version", DEFAULT_PROTOCOL_VERSION),
            port=config.get("port", DEFAULT_PORT),
        )
        dp_map = config.get("dp_map")
        if isinstance(dp_map, dict) and dp_map:
            client.set_monitored_dp_ids(list(dp_map.keys()))
        return client

    def _wire_client(self, managed: _ManagedDevice) -> None:
        """Attach manager callbacks to the currently active client."""
        device_id = managed.config["device_id"]
        client = managed.client
        managed.client.set_dp_callback(
            lambda dps, _id=device_id, _client=client: self._on_dp_update(
                _id, dps, source_client=_client
            )
        )
        managed.client.set_disconnect_callback(
            lambda _id=device_id, _client=client: self._on_disconnect(
                _id, source_client=_client
            )
        )

    async def add_device(self, config: dict[str, Any]) -> bool:
        """Add a device and attempt to connect.

        Returns `True` if the initial connection **and** status succeeded.
        """
        device_id: str = config["device_id"]
        if device_id in self._devices:
            _LOGGER.info("Device %s already managed - updating config", device_id)
            managed = self._devices[device_id]
            managed.config = config
            managed.client.ip = config["host"]
            return managed.online

        client = self._new_client(config)

        managed = _ManagedDevice(client, config)
        self._devices[device_id] = managed

        # Wire callbacks
        self._wire_client(managed)

        _LOGGER.info(
            "Conti add_device: %s at %s:%s (v%s, key=%s)",
            device_id,
            config["host"],
            config.get("port", DEFAULT_PORT),
            config.get("protocol_version", DEFAULT_PROTOCOL_VERSION),
            mask_key(config["local_key"]),
        )

        async with managed.lock:
            try:
                ok = await client.connect()
            except Exception as exc:
                ok = False
                self._classify_error(managed, f"initial connect: {exc!r}")

            if ok:
                self._clear_cloud_fallback(managed)
                managed.reconnect_delay = RECONNECT_BASE_DELAY
                managed.diag.consecutive_failures = 0
                self._update_diag_on_connect(managed)

                # Mark online immediately after TCP + handshake.
                # The device is reachable; empty initial status is OK —
                # it just means the device hasn't pushed DPS yet or
                # doesn't support the query command we sent.
                managed.online = True
                managed.diag.last_handshake_ok = time.monotonic()

                # Best-effort status fetch using fallback strategies.
                # This will NOT close the connection on empty/reset.
                try:
                    st = await client.status_with_fallback()
                except Exception as exc:
                    st = {}
                    _LOGGER.debug(
                        "Conti device %s: initial status_with_fallback raised: %s",
                        device_id, exc,
                    )

                if st:
                    managed.diag.last_status_ok = time.monotonic()
                    managed.diag.last_local_update = self._utc_now()
                    _LOGGER.info(
                        "Conti device %s is ONLINE with DPS (v%s): %s",
                        device_id,
                        managed.diag.protocol_version,
                        list(st.keys()),
                    )
                else:
                    _LOGGER.warning(
                        "Conti device %s connected (v%s) but initial status "
                        "empty — staying online, will get DPS from push or "
                        "next poll (tx=%s rx=%s)",
                        device_id,
                        managed.diag.protocol_version,
                        client.last_tx_hex[:32],
                        client.last_rx_hex[:32],
                    )

                # Update diagnostics with latest frame data
                managed.diag.last_tx_hex = client.last_tx_hex
                managed.diag.last_rx_hex = client.last_rx_hex

                # If connection was lost during fallback probing,
                # schedule reconnect but don't clear online yet —
                # let the coordinator poll decide.
                if not client.connected:
                    _LOGGER.info(
                        "Conti device %s: connection dropped during initial "
                        "status probing — scheduling reconnect",
                        device_id,
                    )
                    managed.online = False
                    self._classify_error(
                        managed, "connection lost during initial status probing"
                    )
                    self._mark_unavailable(managed)
                    self._schedule_reconnect(device_id)
                else:
                    # TCP alive — start push listener for near-instant
                    # RF / physical-button state updates.
                    self._start_listener(device_id)
            else:
                managed.online = False
                managed.diag.consecutive_failures += 1
                reason = client.last_failure_reason or "unknown"
                detail = client.last_failure_detail
                self._classify_error(
                    managed,
                    f"initial connect failed: {reason}: {detail}",
                )
                self._mark_unavailable(managed)
                retry_note = (
                    "automatic retry enabled"
                    if config.get("enable_auto_reconnect", False)
                    else "automatic retry disabled; use conti.reconnect_device"
                )
                _LOGGER.warning(
                    "Conti device %s initial connect FAILED ip=%s "
                    "protocol=%s reason=%s detail=%s attempts=%s - %s",
                    device_id,
                    client.ip,
                    config.get("protocol_version", DEFAULT_PROTOCOL_VERSION),
                    reason,
                    detail,
                    client.attempt_failures,
                    retry_note,
                )
                self._schedule_reconnect(device_id)

        return managed.online

    async def remove_device(self, device_id: str) -> None:
        """Disconnect and forget a device."""
        managed = self._devices.pop(device_id, None)
        if managed is None:
            return
        if managed.listener_task and not managed.listener_task.done():
            managed.listener_task.cancel()
        if managed.reconnect_task and not managed.reconnect_task.done():
            managed.reconnect_task.cancel()
        if managed.cloud_refresh_task and not managed.cloud_refresh_task.done():
            managed.cloud_refresh_task.cancel()
        await managed.client.close()
        self._state_callbacks.pop(device_id, None)
        _LOGGER.info("Removed device %s from manager", device_id)

    # -- Commands / queries --------------------------------------------------

    async def set_dp(self, device_id: str, dp_id: int, value: Any) -> bool:
        """Set a single DP on a device.  Returns `False` if offline."""
        managed = self._devices.get(device_id)
        allow_deferred = bool(managed and managed.control_path == "cloud_fallback")
        if not managed or (not managed.online and not allow_deferred):
            return False
        async with managed.lock:
            if managed.control_path == "cloud_fallback":
                return await self._set_dp_cloud(managed, dp_id, value)
            try:
                result = await managed.client.set_dp(dp_id, value)
                _LOGGER.info(
                    "Conti direct command device=%s protocol=%s dp=%s "
                    "value=%r deferred_local=%s success=%s",
                    device_id,
                    managed.client.protocol_version,
                    dp_id,
                    value,
                    allow_deferred,
                    result,
                )
                if result:
                    return True
                if self._cloud_fallback_failure(managed):
                    self._activate_cloud_fallback(managed)
                    if managed.control_path == "cloud_fallback":
                        return await self._set_dp_cloud(managed, dp_id, value)
                return False
            except Exception as exc:
                managed.diag.consecutive_failures += 1
                self._classify_error(managed, f"set_dp failed: {exc!r}")
                self._log_device_error(
                    managed,
                    f"set_dp:{dp_id}:{type(exc).__name__}",
                    "Conti set_dp(%s, %s) failed: %s",
                    device_id,
                    dp_id,
                    exc,
                    exc=exc,
                )
                return False

    async def set_dps(self, device_id: str, dps: dict[int, Any]) -> bool:
        """Set multiple DPs at once."""
        managed = self._devices.get(device_id)
        allow_deferred = bool(managed and managed.control_path == "cloud_fallback")
        if not managed or (not managed.online and not allow_deferred):
            return False
        async with managed.lock:
            if managed.control_path == "cloud_fallback":
                results = [
                    await self._set_dp_cloud(managed, dp_id, value)
                    for dp_id, value in dps.items()
                ]
                return all(results)
            try:
                result = await managed.client.set_dps(dps)
                _LOGGER.info(
                    "Conti direct multi-command device=%s protocol=%s "
                    "dps=%s deferred_local=%s success=%s",
                    device_id,
                    managed.client.protocol_version,
                    dps,
                    allow_deferred,
                    result,
                )
                if result:
                    return True
                if self._cloud_fallback_failure(managed):
                    self._activate_cloud_fallback(managed)
                    if managed.control_path == "cloud_fallback":
                        results = [
                            await self._set_dp_cloud(managed, dp_id, value)
                            for dp_id, value in dps.items()
                        ]
                        return all(results)
                return False
            except Exception as exc:
                managed.diag.consecutive_failures += 1
                self._classify_error(managed, f"set_dps failed: {exc!r}")
                self._log_device_error(
                    managed,
                    f"set_dps:{type(exc).__name__}",
                    "Conti set_dps(%s) failed: %s",
                    device_id,
                    exc,
                    exc=exc,
                )
                return False

    async def query_device(self, device_id: str) -> dict[str, Any] | None:
        """Query the current DP values.  Returns `{}` if offline.

        Returns ``None`` when the poll is intentionally **skipped** because
        a command (``set_dp``/``set_dps``) currently holds the per-device
        lock.  The caller should treat ``None`` as "no new data, but not
        a failure" and fall back to cached DPS without incrementing any
        failure counter.

        * Offline polling never creates a fresh client by itself.
        * Optional auto-reconnect is scheduled in the background.
        * Returns cached DPS when status is empty but connection is alive.
        * Only marks offline when the TCP connection is actually lost.
        """
        managed = self._devices.get(device_id)
        if not managed:
            _LOGGER.warning("Conti device %s not registered in manager", device_id)
            return {}

        # ---- Device is currently offline ----
        if not managed.online:
            # Polling must not be a hidden reconnect path. It can only ensure
            # the explicitly enabled background task exists and serve cache.
            self._schedule_reconnect(device_id)
            return managed.client.cached_dps

        # ---- Device is online - request fresh status ----
        managed.client.ip = managed.config["host"]

        # If the lock is already held (command in-flight), skip this poll
        # so user commands are never blocked behind polling I/O.
        if managed.lock.locked():
            _LOGGER.debug(
                "Conti device %s: lock busy (command in-flight), "
                "skipping poll — returning None (poll skipped)",
                device_id,
            )
            return None

        async with managed.lock:
            try:
                # Short timeout (2 s) for routine polls on an already-
                # connected persistent socket so the lock is released
                # quickly and the push listener is not starved when the
                # device is slow to answer a DP_QUERY.
                status = await managed.client.status_with_fallback(
                    timeout=2.0
                )
            except Exception as exc:
                status = None
                self._classify_error(managed, f"status failed: {exc!r}")
                self._log_device_error(
                    managed,
                    f"status:{type(exc).__name__}",
                    "Conti device %s status exception: %s",
                    device_id,
                    exc,
                    exc=exc,
                )

        managed.diag.last_tx_hex = managed.client.last_tx_hex
        managed.diag.last_rx_hex = managed.client.last_rx_hex

        if status:
            managed.diag.last_status_ok = time.monotonic()
            managed.diag.last_local_update = self._utc_now()
            managed.diag.consecutive_failures = 0
        else:
            if self._cloud_fallback_failure(managed):
                self._activate_cloud_fallback(managed)
            # Distinguish between "empty response" and "connection lost"
            if not managed.client.connected:
                managed.diag.consecutive_failures += 1
                managed.online = False
                self._classify_error(
                    managed,
                    "connection lost during status query "
                    f"(tx={managed.client.last_tx_hex[:16]} "
                    f"rx={managed.client.last_rx_hex[:16]})",
                )
                self._mark_unavailable(managed)
                self._log_device_error(
                    managed,
                    "poll_connection_lost",
                    "Conti device %s: connection lost during poll "
                    "(failures=%d) — scheduling reconnect",
                    device_id,
                    managed.diag.consecutive_failures,
                )
                self._schedule_reconnect(device_id)
            else:
                # Still connected but empty — not a hard failure.
                # Return cached DPS so entities don't flap.
                _LOGGER.debug(
                    "Conti device %s: empty status but still connected "
                    "(tx=%s rx=%s) — returning cache",
                    device_id,
                    managed.client.last_tx_hex[:16],
                    managed.client.last_rx_hex[:16],
                )

        return status or managed.client.cached_dps or {}

    def get_cached_dps(self, device_id: str) -> dict[str, Any]:
        """Return the cached DPs without hitting the network."""
        managed = self._devices.get(device_id)
        if not managed:
            return {}
        return managed.client.cached_dps

    async def detect_dps(self, device_id: str) -> dict[str, Any]:
        """Auto-detect which DPs a device supports.

        Uses multiple query strategies (DP_QUERY, DP_QUERY_NEW, CONTROL
        probe) to discover all available data-points.  Returns the
        discovered DP dict ``{"1": <value>, ...}``.

        The device must already be registered via `add_device()`.
        If it's offline, an automatic connect attempt is made.
        """
        managed = self._devices.get(device_id)
        if not managed:
            _LOGGER.warning("detect_dps: device %s not registered", device_id)
            return {}

        # Ensure the device is connected
        if not managed.online or not managed.client.connected:
            _LOGGER.info("detect_dps: connecting to %s first", device_id)
            async with managed.lock:
                try:
                    ok = await managed.client.connect()
                except Exception as exc:
                    _LOGGER.warning("detect_dps connect failed for %s: %s", device_id, exc)
                    return {}
                if ok:
                    managed.online = True
                    self._update_diag_on_connect(managed)
                else:
                    return {}

        async with managed.lock:
            discovered = await managed.client.detect_dps()

        managed.diag.last_tx_hex = managed.client.last_tx_hex
        managed.diag.last_rx_hex = managed.client.last_rx_hex
        if discovered:
            managed.diag.last_status_ok = time.monotonic()
            managed.diag.consecutive_failures = 0

        _LOGGER.info(
            "detect_dps for %s: discovered %d DPs: %s",
            device_id, len(discovered), sorted(discovered.keys()),
        )
        return discovered

    def is_online(self, device_id: str) -> bool:
        managed = self._devices.get(device_id)
        if not managed:
            return False
        if managed.control_path == "unavailable":
            return False
        if managed.control_path == "cloud_fallback":
            return bool(managed.client.cached_dps)
        return managed.online

    def device_ids(self) -> list[str]:
        return list(self._devices)

    def get_client(self, device_id: str) -> TinyTuyaDevice | None:
        """Return the raw client for a device (used for diagnostics)."""
        managed = self._devices.get(device_id)
        return managed.client if managed else None

    def seed_cached_dps(
        self,
        device_id: str,
        dps: dict[str, Any],
        *,
        overwrite: bool = False,
    ) -> None:
        """Pre-load cached DPS from persistent storage.

        Called during ``async_setup_entry`` so entities have data
        before the first live poll completes.
        """
        managed = self._devices.get(device_id)
        if managed and (overwrite or not managed.client.cached_dps):
            managed.client._cached_dps.update(dps)  # noqa: SLF001

    # -- Diagnostics ---------------------------------------------------------

    def get_device_diagnostics(self, device_id: str) -> dict[str, Any]:
        """Return diagnostic info for *device_id*."""
        managed = self._devices.get(device_id)
        if not managed:
            return {"device_id": device_id, "error": "not registered"}
        d = managed.diag
        client = managed.client
        monitored = getattr(client, "_monitored_dp_ids", None)
        monitored_dp_ids = (
            list(monitored)
            if isinstance(monitored, dict)
            else list((managed.config.get("dp_map") or {}).keys())
        )
        return {
            "device_id": device_id,
            "host": client.ip,
            "device_ip": managed.config.get("host", client.ip),
            "dp_map_source": managed.config.get("dp_map_source", "discovered"),
            "monitored_dp_ids": monitored_dp_ids,
            "ha_host_ip": managed.config.get("ha_host_ip", "unknown"),
            "ha_host_subnet": managed.config.get("ha_host_subnet", "unknown"),
            "online": managed.online,
            "local_status_available": managed.online,
            "local_status_reason": (
                "" if managed.online else client.last_failure_reason
            ),
            "local_status": managed.local_status,
            "control_path": managed.control_path,
            "diagnostic_reason": managed.diagnostic_reason,
            "cloud_error": managed.cloud_error,
            "cloud_error_code": managed.cloud_error_code,
            "cloud_error_message": managed.cloud_error_message,
            "protocol_version": d.protocol_version,
            "auto_detected": d.auto_detected,
            "detected_version": client.detected_version,
            "last_handshake_ok": d.last_handshake_ok,
            "last_status_ok": d.last_status_ok,
            "last_successful_local_update": d.last_local_update,
            "last_error": d.last_error,
            "last_local_error": d.last_local_error,
            "last_error_class": d.last_error_class,
            "consecutive_failures": d.consecutive_failures,
            "reconnect_delay": managed.reconnect_delay,
            "reconnect_attempts": d.reconnect_attempts,
            "next_retry_time": managed.next_retry_time,
            "last_tx_hex": d.last_tx_hex,
            "last_rx_hex": d.last_rx_hex,
            "last_probe_failure_reason": client.last_failure_reason,
            "last_probe_failure_detail": client.last_failure_detail,
            "protocol_attempt_failures": client.attempt_failures,
        }

    def log_device_error(
        self,
        device_id: str,
        error_key: str,
        message: str,
        *args: Any,
        exc: BaseException | None = None,
    ) -> None:
        """Log a rate-limited error for a managed device."""
        managed = self._devices.get(device_id)
        if managed:
            self._log_device_error(
                managed, error_key, message, *args, exc=exc
            )

    def configure_dali_cloud_fallback(
        self, device_id: str, cloud_runtime: Any
    ) -> bool:
        """Attach cloud control only to an exact DALI/CCT device mapping."""
        managed = self._devices.get(device_id)
        if (
            not managed
            or managed.config.get("device_type") != "light"
            or not cloud_runtime.supports_dali_cct_fallback()
        ):
            return False
        managed.cloud_fallback = cloud_runtime
        if self._cloud_fallback_failure(managed):
            self._activate_cloud_fallback(managed)
        return managed.control_path == "cloud_fallback"

    def record_cloud_fallback_diagnostics(
        self, device_id: str, cloud_runtime: Any
    ) -> bool:
        """Record cloud fallback errors and return True for permission errors."""
        managed = self._devices.get(device_id)
        getter = getattr(cloud_runtime, "get_connection_diagnostics", None)
        if not managed or getter is None:
            return False
        diagnostics = getter() or {}
        managed.cloud_error = str(diagnostics.get("cloud_error", ""))
        managed.cloud_error_code = str(diagnostics.get("cloud_error_code", ""))
        managed.cloud_error_message = str(
            diagnostics.get("cloud_error_message", "")
        )
        if managed.cloud_error != "cloud_permission_error":
            return False

        managed.control_path = "unavailable"
        managed.diagnostic_reason = "cloud_permission_error"
        self._log_device_error(
            managed,
            "cloud_permission_error",
            "Conti cloud_permission_error device=%s code=%s message=%s",
            device_id,
            managed.cloud_error_code or "none",
            managed.cloud_error_message or "Tuya cloud permission unavailable",
        )
        return True

    @staticmethod
    def _cloud_fallback_failure(managed: _ManagedDevice) -> str:
        reason = str(managed.client.last_failure_reason or "")
        detail = str(managed.client.last_failure_detail or "")
        failures = [(reason, detail)]
        failures.extend(
            (
                str(item.get("reason", "")),
                str(item.get("detail", "")),
            )
            for item in managed.client.attempt_failures
            if isinstance(item, dict)
        )
        for failure_reason, failure_detail in failures:
            if failure_reason == "local_key_or_version_914" or (
                failure_reason == "empty_payload" and "914" in failure_detail
            ):
                return "914"
            if failure_reason == "malformed_payload_904" or (
                failure_reason == "empty_payload" and "904" in failure_detail
            ):
                return "904"
        return ""

    def _activate_cloud_fallback(self, managed: _ManagedDevice) -> None:
        if managed.cloud_fallback is None:
            return
        error_code = self._cloud_fallback_failure(managed)
        if not error_code:
            return
        managed.local_status = f"failed_{error_code}"
        managed.control_path = "cloud_fallback"
        managed.diagnostic_reason = (
            "local_key_or_version_914_cloud_fallback"
            if error_code == "914"
            else "local_payload_904_cloud_fallback"
        )
        self._log_device_error(
            managed,
            f"cloud_fallback_activated:{error_code}",
            "Conti local Err %s detected device=%s; cloud fallback activated",
            error_code,
            managed.config["device_id"],
        )

    def _clear_cloud_fallback(self, managed: _ManagedDevice) -> None:
        if managed.control_path == "cloud_fallback":
            _LOGGER.info(
                "Conti local status recovered device=%s; disabling cloud fallback",
                managed.config["device_id"],
            )
        managed.local_status = "healthy"
        managed.control_path = "local"
        managed.diagnostic_reason = ""

    async def _set_dp_cloud(
        self, managed: _ManagedDevice, dp_id: int, value: Any
    ) -> bool:
        device_id = managed.config["device_id"]
        _LOGGER.info(
            "Conti cloud fallback command device=%s dp=%s value=%r",
            device_id,
            dp_id,
            value,
        )
        try:
            ok = bool(await managed.cloud_fallback.async_set_dp(dp_id, value))
        except Exception as exc:  # noqa: BLE001
            ok = False
            self._log_device_error(
                managed,
                "cloud_command_exception",
                "Conti cloud fallback command failed device=%s dp=%s "
                "value=%r exception=%r",
                device_id,
                dp_id,
                value,
                exc,
                exc=exc,
            )
        self.record_cloud_fallback_diagnostics(
            device_id, managed.cloud_fallback
        )
        if not ok:
            self._log_device_error(
                managed,
                "cloud_command_failure",
                "Conti cloud fallback command failure device=%s dp=%s value=%r",
                device_id,
                dp_id,
                value,
            )
            return False

        updates = {str(dp_id): value}
        if dp_id in {22, 23}:
            updates["21"] = "white"
        managed.client._cached_dps.update(updates)  # noqa: SLF001
        self._on_dp_update(device_id, updates, local=False)
        _LOGGER.info(
            "Conti cloud fallback command success device=%s dp=%s value=%r",
            device_id,
            dp_id,
            value,
        )
        if managed.cloud_refresh_task and not managed.cloud_refresh_task.done():
            managed.cloud_refresh_task.cancel()
        managed.cloud_refresh_task = asyncio.create_task(
            self._refresh_cloud_after_command(managed),
            name=f"conti-cloud-refresh-{device_id}",
        )
        return True

    async def _refresh_cloud_after_command(self, managed: _ManagedDevice) -> None:
        await asyncio.sleep(1.0)
        try:
            dps = await managed.cloud_fallback.async_get_dps()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Cloud refresh failed after command: %s", exc)
            return
        if dps:
            managed.client._cached_dps.update(dps)  # noqa: SLF001
            self._on_dp_update(managed.config["device_id"], dps, local=False)

    # -- Internal callbacks --------------------------------------------------

    def _on_dp_update(
        self,
        device_id: str,
        dps: dict[str, Any],
        *,
        local: bool = True,
        source_client: TinyTuyaDevice | None = None,
    ) -> None:
        managed = self._devices.get(device_id)
        if managed and source_client is not None and source_client is not managed.client:
            _LOGGER.debug("Ignoring stale-client DP callback device=%s", device_id)
            return
        if managed and local and dps:
            managed.diag.last_local_update = self._utc_now()
        callbacks = self._state_callbacks.get(device_id)
        if callbacks:
            for cb in list(callbacks):
                try:
                    cb(device_id, dps)
                except Exception as exc:  # noqa: BLE001
                    if managed:
                        self._log_device_error(
                            managed,
                            f"state_callback:{type(exc).__name__}",
                            "State callback error for %s: %s",
                            device_id,
                            exc,
                            exc=exc,
                        )

    # -- Push listener -------------------------------------------------------

    def _start_listener(self, device_id: str) -> None:
        """Start the unsolicited-data listener for *device_id*."""
        managed = self._devices.get(device_id)
        if not managed:
            return
        if managed.listener_task and not managed.listener_task.done():
            return  # already running
        managed.listener_task = asyncio.create_task(
            self._listen_loop(device_id),
            name=f"conti-listener-{device_id}",
        )

    def _stop_listener(self, device_id: str) -> None:
        """Cancel the listener task for *device_id* if running."""
        managed = self._devices.get(device_id)
        if managed and managed.listener_task and not managed.listener_task.done():
            managed.listener_task.cancel()
            managed.listener_task = None

    async def _listen_loop(self, device_id: str) -> None:
        """Background: poll the persistent socket for unsolicited pushes.

        Runs every ~0.15 s between polls/commands.  When the per-device
        lock is held (command or poll in flight) the loop yields so it
        never blocks real I/O.  A short 50 ms socket timeout inside
        ``receive_nowait`` keeps each iteration fast.
        """
        managed = self._devices.get(device_id)
        if not managed:
            return

        _LOGGER.debug("Push listener started for %s", device_id)
        try:
            while self._running and managed.online:
                # Yield to commands / polls that hold the lock
                if managed.lock.locked():
                    await asyncio.sleep(0.05)
                    continue

                async with managed.lock:
                    try:
                        dps = await managed.client.receive_nowait()
                    except Exception:  # noqa: BLE001
                        dps = None

                # Socket died during receive?
                if not managed.client.connected:
                    managed.online = False
                    self._classify_error(
                        managed, "disconnected during push listen"
                    )
                    self._mark_unavailable(managed)
                    _now = time.monotonic()
                    if _now - managed._last_push_warn_time >= _PUSH_WARN_COOLDOWN:
                        managed._last_push_warn_time = _now
                        _LOGGER.warning(
                            "Conti device %s: connection lost (push listener) "
                            "— scheduling reconnect",
                            device_id,
                        )
                    else:
                        _LOGGER.debug(
                            "Conti device %s: connection lost (push listener) "
                            "— scheduling reconnect (repeated within cooldown)",
                            device_id,
                        )
                    self._schedule_reconnect(device_id)
                    break

                if dps:
                    _LOGGER.debug(
                        "Unsolicited push from %s: %s",
                        device_id,
                        list(dps.keys()),
                    )
                    self._on_dp_update(device_id, dps)

                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            pass

        _LOGGER.debug("Push listener stopped for %s", device_id)

    def _on_disconnect(
        self,
        device_id: str,
        *,
        source_client: TinyTuyaDevice | None = None,
    ) -> None:
        managed = self._devices.get(device_id)
        if not managed:
            return
        if source_client is not None and source_client is not managed.client:
            _LOGGER.debug("Ignoring stale-client disconnect device=%s", device_id)
            return
        self._stop_listener(device_id)
        was_online = managed.online
        managed.online = False
        self._classify_error(managed, "disconnected")
        self._mark_unavailable(managed)
        if was_online:
            _now = time.monotonic()
            if _now - managed._last_push_warn_time >= _PUSH_WARN_COOLDOWN:
                managed._last_push_warn_time = _now
                _LOGGER.warning(
                    "Conti device %s disconnected - scheduling reconnect", device_id
                )
            else:
                _LOGGER.debug(
                    "Conti device %s disconnected - scheduling reconnect "
                    "(repeated within cooldown)",
                    device_id,
                )
        else:
            _LOGGER.debug(
                "Conti device %s disconnect callback while already offline", device_id
            )
        if self._running:
            self._schedule_reconnect(device_id)

    # -- Reconnect -----------------------------------------------------------

    def is_degraded(self, device_id: str) -> bool:
        """Return whether reconnecting this local device is appropriate."""
        managed = self._devices.get(device_id)
        if not managed:
            return False
        if self._cloud_fallback_is_healthy(managed):
            return False
        return not (
            managed.online
            and bool(managed.client.connected)
            and managed.control_path == "local"
            and managed.diag.consecutive_failures == 0
        )

    @staticmethod
    def _cloud_fallback_is_healthy(managed: _ManagedDevice) -> bool:
        """Return True when cached cloud fallback is currently usable."""
        if (
            managed.control_path != "cloud_fallback"
            or managed.cloud_fallback is None
            or not managed.client.cached_dps
            or managed.cloud_error
        ):
            return False
        return getattr(managed.cloud_fallback, "last_online_state", None) is not False

    @staticmethod
    def _needs_local_reconnect(managed: _ManagedDevice) -> bool:
        """Return whether optional background local recovery should continue."""
        return not (
            managed.online
            and bool(managed.client.connected)
            and managed.control_path == "local"
            and managed.diag.consecutive_failures == 0
        )

    def degraded_device_ids(self) -> list[str]:
        """Return only unavailable/degraded devices, preserving entry order."""
        return [device_id for device_id in self._devices if self.is_degraded(device_id)]

    async def reconnect_device(self, device_id: str) -> bool:
        """Immediately replace a stale client and verify the fresh session."""
        managed = self._devices.get(device_id)
        if not managed:
            return False

        current = asyncio.current_task()
        existing = managed.reconnect_task
        if existing and existing is not current and not existing.done():
            existing.cancel()
            with suppress(asyncio.CancelledError):
                await existing

        managed.reconnect_task = current
        try:
            try:
                success = await self._attempt_reconnect(managed)
            except Exception as exc:  # noqa: BLE001
                success = False
                managed.online = False
                self._classify_error(managed, f"reconnect lifecycle: {exc!r}")
                self._mark_unavailable(managed)
                self._log_device_error(
                    managed,
                    f"reconnect_lifecycle:{type(exc).__name__}",
                    "Conti reconnect lifecycle failed device=%s error=%s",
                    device_id,
                    exc,
                    exc=exc,
                )
        finally:
            if managed.reconnect_task is current:
                managed.reconnect_task = None

        if not success:
            self._schedule_reconnect(device_id)
        return success

    async def reconnect_all(self) -> dict[str, bool]:
        """Reconnect degraded devices serially and isolate per-device errors."""
        results: dict[str, bool] = {}
        for device_id in self.degraded_device_ids():
            try:
                results[device_id] = await self.reconnect_device(device_id)
            except Exception as exc:  # noqa: BLE001
                results[device_id] = False
                managed = self._devices.get(device_id)
                if managed:
                    self._classify_error(managed, f"manual reconnect failed: {exc!r}")
                    self._log_device_error(
                        managed,
                        "manual_reconnect_exception",
                        "Conti reconnect failed device=%s error=%s",
                        device_id,
                        exc,
                        exc=exc,
                    )
        return results

    def _schedule_reconnect(self, device_id: str) -> None:
        managed = self._devices.get(device_id)
        if not managed or not self._running:
            return
        if not managed.config.get(
            "enable_auto_reconnect", DEFAULT_ENABLE_AUTO_RECONNECT
        ):
            return
        if managed.reconnect_task and not managed.reconnect_task.done():
            return  # already scheduled
        managed.reconnect_task = asyncio.create_task(
            self._reconnect(device_id),
            name=f"conti-reconnect-{device_id}",
        )

    async def _reconnect(self, device_id: str) -> None:
        """Reconnect in the background with capped exponential backoff."""
        managed = self._devices.get(device_id)
        if not managed:
            return

        while self._running and self._needs_local_reconnect(managed):
            delay = managed.reconnect_delay
            managed.next_retry_time = self._utc_after(delay)
            _LOGGER.info(
                "Reconnecting to %s in %.1fs (failures=%d, last_error_class=%s)",
                device_id,
                delay,
                managed.diag.consecutive_failures,
                managed.diag.last_error_class,
            )
            try:
                await asyncio.sleep(delay)
            finally:
                managed.next_retry_time = None
            if not self._running:
                return
            try:
                success = await self._attempt_reconnect(managed)
            except Exception as exc:  # noqa: BLE001
                success = False
                managed.online = False
                self._classify_error(managed, f"auto reconnect lifecycle: {exc!r}")
                self._mark_unavailable(managed)
                self._log_device_error(
                    managed,
                    f"auto_reconnect_lifecycle:{type(exc).__name__}",
                    "Conti auto-reconnect lifecycle failed device=%s error=%s",
                    device_id,
                    exc,
                    exc=exc,
                )
            if success:
                return
            managed.reconnect_delay = min(
                managed.reconnect_delay * 2, RECONNECT_MAX_DELAY
            )

    async def _attempt_reconnect(self, managed: _ManagedDevice) -> bool:
        """Close the stale client, build a new one, and verify local status."""
        device_id = managed.config["device_id"]
        self._stop_listener(device_id)
        cached = dict(getattr(managed.client, "cached_dps", {}) or {})
        managed.diag.reconnect_attempts += 1

        async with managed.lock:
            stale_client = managed.client
            try:
                await stale_client.close()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "Closing stale Conti client failed device=%s: %s",
                    device_id,
                    exc,
                    exc_info=exc if _LOGGER.isEnabledFor(logging.DEBUG) else None,
                )

            fresh_client = self._new_client(managed.config)
            managed.client = fresh_client
            self._wire_client(managed)
            fresh_cache = getattr(fresh_client, "_cached_dps", None)
            if isinstance(fresh_cache, dict):
                fresh_cache.update(cached)

            try:
                connected = bool(await fresh_client.connect())
            except Exception as exc:  # noqa: BLE001
                connected = False
                self._classify_error(managed, f"reconnect connect: {exc!r}")
                self._log_device_error(
                    managed,
                    "reconnect_connect",
                    "Conti reconnect connect failed device=%s error=%s",
                    device_id,
                    exc,
                    exc=exc,
                )

            status: dict[str, Any] = {}
            if connected:
                try:
                    raw_status = await fresh_client.status_with_fallback()
                    status = raw_status if isinstance(raw_status, dict) else {}
                    if status and isinstance(fresh_cache, dict):
                        fresh_cache.update(status)
                except Exception as exc:  # noqa: BLE001
                    self._classify_error(managed, f"reconnect status: {exc!r}")
                    self._log_device_error(
                        managed,
                        "reconnect_status",
                        "Conti reconnect status failed device=%s error=%s",
                        device_id,
                        exc,
                        exc=exc,
                    )

        managed.diag.last_tx_hex = getattr(fresh_client, "last_tx_hex", "")
        managed.diag.last_rx_hex = getattr(fresh_client, "last_rx_hex", "")
        if connected and bool(fresh_client.connected) and status:
            managed.online = True
            managed.reconnect_delay = RECONNECT_BASE_DELAY
            managed.next_retry_time = None
            managed.diag.consecutive_failures = 0
            self._update_diag_on_connect(managed)
            self._clear_cloud_fallback(managed)
            managed.diag.last_status_ok = time.monotonic()
            managed.diag.last_local_update = self._utc_now()
            self._on_dp_update(device_id, status)
            self._start_listener(device_id)
            _LOGGER.info(
                "Conti reconnect succeeded device=%s protocol=%s dps=%s",
                device_id,
                managed.diag.protocol_version,
                list(status),
            )
            return True

        if connected and bool(fresh_client.connected):
            managed.online = False
            managed.diag.consecutive_failures += 1
            self._classify_error(
                managed, "empty status after reconnect; local recovery not verified"
            )
            managed.diag.last_error_class = ERR_EMPTY_STATUS
            self._mark_unavailable(managed)
            self._log_device_error(
                managed,
                "reconnect_empty_status",
                "Conti reconnect device=%s returned empty_status; "
                "keeping cached state and existing cloud fallback",
                device_id,
            )
            return False

        managed.online = False
        managed.diag.consecutive_failures += 1
        reason = str(getattr(fresh_client, "last_failure_reason", "") or "unknown")
        detail = str(getattr(fresh_client, "last_failure_detail", "") or "")
        self._classify_error(managed, f"reconnect failed: {reason}: {detail}")
        self._mark_unavailable(managed)
        self._log_device_error(
            managed,
            f"reconnect_failed:{reason}",
            "Conti reconnect failed device=%s reason=%s detail=%s",
            device_id,
            reason,
            detail,
        )
        return False

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _utc_after(seconds: float) -> str:
        return datetime.fromtimestamp(time.time() + seconds, UTC).isoformat()

    @staticmethod
    def _mark_unavailable(managed: _ManagedDevice) -> None:
        """Mark the local path down without overriding an active fallback."""
        managed.local_status = "unavailable"
        if managed.control_path != "cloud_fallback":
            managed.control_path = "unavailable"
            managed.diagnostic_reason = managed.diag.last_error_class

    @staticmethod
    def _log_device_error(
        managed: _ManagedDevice,
        error_key: str,
        message: str,
        *args: Any,
        exc: BaseException | None = None,
    ) -> None:
        """Rate-limit a repeated device error; traces are debug-only."""
        now = time.monotonic()
        last = managed.error_log_times.get(error_key, 0.0)
        if now - last < ERROR_LOG_COOLDOWN:
            _LOGGER.debug(message + " (suppressed by 10-minute cooldown)", *args)
            return
        managed.error_log_times[error_key] = now
        _LOGGER.warning(message, *args)
        if exc is not None and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Detailed Conti device exception",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _update_diag_on_connect(self, managed: _ManagedDevice) -> None:
        """Refresh diagnostics from the client after a successful connect."""
        client = managed.client
        managed.diag.protocol_version = client.protocol_version
        managed.diag.auto_detected = client.detected_version is not None
        managed.diag.last_handshake_ok = time.monotonic()
        managed.diag.last_tx_hex = client.last_tx_hex
        managed.diag.last_rx_hex = client.last_rx_hex
        managed.diag.last_error = ""
        managed.diag.last_error_class = ERR_NONE

    def _classify_error(self, managed: _ManagedDevice, msg: str) -> None:
        """Set last_error and auto-classify last_error_class."""
        managed.diag.last_error = msg
        managed.diag.last_local_error = msg
        lower = msg.lower()
        if "reset by peer" in lower or "connection reset" in lower:
            managed.diag.last_error_class = ERR_RESET
        elif "timed out" in lower or "timeout" in lower:
            managed.diag.last_error_class = ERR_TIMEOUT
        elif "refused" in lower or "port" in lower:
            managed.diag.last_error_class = ERR_PORT_CLOSED
        elif "handshake" in lower:
            managed.diag.last_error_class = ERR_HANDSHAKE
        elif "decrypt" in lower or "wrong key" in lower:
            managed.diag.last_error_class = ERR_DECRYPT
        elif "invalid_key" in lower:
            managed.diag.last_error_class = ERR_DECRYPT
        elif "no_response" in lower:
            managed.diag.last_error_class = ERR_EOF
        elif "empty_payload" in lower:
            managed.diag.last_error_class = ERR_MALFORMED
        elif "empty status" in lower:
            managed.diag.last_error_class = ERR_EMPTY_STATUS
        elif "eof" in lower or "closed" in lower or "disconnect" in lower:
            managed.diag.last_error_class = ERR_EOF
        elif "unreachable" in lower or "subnet" in lower or "no route" in lower:
            managed.diag.last_error_class = ERR_SUBNET
        elif "malformed" in lower or "json" in lower:
            managed.diag.last_error_class = ERR_MALFORMED
        else:
            managed.diag.last_error_class = ERR_UNKNOWN
