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
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .const import (
    DEFAULT_PORT,
    DEFAULT_PROTOCOL_VERSION,
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


@dataclass
class DeviceDiagnostics:
    """Per-device diagnostic state exposed via `get_device_diagnostics`."""

    protocol_version: str = "unknown"
    auto_detected: bool = False
    last_handshake_ok: float = 0.0
    last_status_ok: float = 0.0
    last_error: str = ""
    last_error_class: str = ERR_NONE
    last_tx_hex: str = ""
    last_rx_hex: str = ""
    consecutive_failures: int = 0


class _ManagedDevice:
    """Internal wrapper around a single device connection."""

    __slots__ = (
        "client",
        "config",
        "reconnect_delay",
        "reconnect_task",
        "online",
        "lock",
        "diag",
    )

    def __init__(self, client: TinyTuyaDevice, config: dict[str, Any]) -> None:
        self.client = client
        self.config = config
        self.reconnect_delay: float = RECONNECT_BASE_DELAY
        self.reconnect_task: Optional[asyncio.Task[None]] = None
        self.online: bool = False
        self.lock: asyncio.Lock = asyncio.Lock()
        self.diag: DeviceDiagnostics = DeviceDiagnostics()


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
        """Close every connection and cancel pending reconnects."""
        self._running = False
        for dev in self._devices.values():
            if dev.reconnect_task and not dev.reconnect_task.done():
                dev.reconnect_task.cancel()
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

        client = TinyTuyaDevice(
            device_id=device_id,
            ip=config["host"],
            local_key=config["local_key"],
            version=config.get("protocol_version", DEFAULT_PROTOCOL_VERSION),
            port=config.get("port", DEFAULT_PORT),
        )

        managed = _ManagedDevice(client, config)
        self._devices[device_id] = managed

        # Wire callbacks
        client.set_dp_callback(
            lambda dps, _id=device_id: self._on_dp_update(_id, dps)
        )
        client.set_disconnect_callback(
            lambda _id=device_id: self._on_disconnect(_id)
        )

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
                    self._schedule_reconnect(device_id)
            else:
                managed.online = False
                managed.diag.consecutive_failures += 1
                if not managed.diag.last_error:
                    self._classify_error(managed, "initial connect failed")
                _LOGGER.warning(
                    "Conti device %s initial connect FAILED - will retry",
                    device_id,
                )
                self._schedule_reconnect(device_id)

        return managed.online

    async def remove_device(self, device_id: str) -> None:
        """Disconnect and forget a device."""
        managed = self._devices.pop(device_id, None)
        if managed is None:
            return
        if managed.reconnect_task and not managed.reconnect_task.done():
            managed.reconnect_task.cancel()
        await managed.client.close()
        self._state_callbacks.pop(device_id, None)
        _LOGGER.info("Removed device %s from manager", device_id)

    # -- Commands / queries --------------------------------------------------

    async def set_dp(self, device_id: str, dp_id: int, value: Any) -> bool:
        """Set a single DP on a device.  Returns `False` if offline."""
        managed = self._devices.get(device_id)
        if not managed or not managed.online:
            return False
        async with managed.lock:
            try:
                return await managed.client.set_dp(dp_id, value)
            except Exception as exc:
                managed.diag.consecutive_failures += 1
                self._classify_error(managed, f"set_dp failed: {exc!r}")
                _LOGGER.warning("Conti set_dp(%s, %s) failed: %s", device_id, dp_id, exc)
                return False

    async def set_dps(self, device_id: str, dps: dict[int, Any]) -> bool:
        """Set multiple DPs at once."""
        managed = self._devices.get(device_id)
        if not managed or not managed.online:
            return False
        async with managed.lock:
            try:
                return await managed.client.set_dps(dps)
            except Exception as exc:
                managed.diag.consecutive_failures += 1
                self._classify_error(managed, f"set_dps failed: {exc!r}")
                _LOGGER.warning("Conti set_dps(%s) failed: %s", device_id, exc)
                return False

    async def query_device(self, device_id: str) -> dict[str, Any]:
        """Query the current DP values.  Returns `{}` if offline.

        * If a reconnect task is already running, returns cached DPS.
        * After a successful connect, uses status_with_fallback().
        * Returns cached DPS when status is empty but connection is alive.
        * Only marks offline when the TCP connection is actually lost.
        """
        managed = self._devices.get(device_id)
        if not managed:
            _LOGGER.warning("Conti device %s not registered in manager", device_id)
            return {}

        # ---- Device is currently offline ----
        if not managed.online:
            if managed.reconnect_task and not managed.reconnect_task.done():
                _LOGGER.debug(
                    "Conti device %s offline, reconnect in progress - returning cached",
                    device_id,
                )
                return managed.client.cached_dps
            _LOGGER.info(
                "Conti device %s offline, attempting connect to %s",
                device_id,
                managed.config["host"],
            )
            async with managed.lock:
                managed.client.ip = managed.config["host"]
                try:
                    ok = await managed.client.connect()
                except Exception as exc:
                    ok = False
                    self._classify_error(managed, f"query connect: {exc!r}")

                if ok:
                    self._update_diag_on_connect(managed)
                    managed.online = True
                    managed.reconnect_delay = RECONNECT_BASE_DELAY
                    managed.diag.last_handshake_ok = time.monotonic()

                    try:
                        st = await managed.client.status_with_fallback()
                    except Exception as exc:
                        st = {}
                        _LOGGER.debug(
                            "Conti query post-connect fallback raised for %s: %s",
                            device_id, exc,
                        )

                    managed.diag.last_tx_hex = managed.client.last_tx_hex
                    managed.diag.last_rx_hex = managed.client.last_rx_hex

                    if st:
                        managed.diag.consecutive_failures = 0
                        managed.diag.last_status_ok = time.monotonic()
                        _LOGGER.info(
                            "Conti device %s re-connected with DPS (v%s)",
                            device_id,
                            managed.diag.protocol_version,
                        )
                        return st

                    # Connected but empty — check if connection survived
                    if managed.client.connected:
                        _LOGGER.info(
                            "Conti device %s re-connected but empty DPS — "
                            "staying online, returning cache (tx=%s rx=%s)",
                            device_id,
                            managed.client.last_tx_hex[:32],
                            managed.client.last_rx_hex[:32],
                        )
                        return managed.client.cached_dps

                    # Connection lost during probing
                    managed.online = False
                    managed.diag.consecutive_failures += 1
                    self._classify_error(
                        managed,
                        "query: connection lost during status probing "
                        f"(tx={managed.client.last_tx_hex[:16]} "
                        f"rx={managed.client.last_rx_hex[:16]})",
                    )
                    self._schedule_reconnect(device_id)
                    return managed.client.cached_dps
                else:
                    managed.diag.consecutive_failures += 1
                    if not managed.diag.last_error:
                        self._classify_error(managed, "connect failed in query")
                    _LOGGER.warning(
                        "Conti device %s connect failed (ip=%s)",
                        device_id,
                        managed.client.ip,
                    )
                    self._schedule_reconnect(device_id)
                    return managed.client.cached_dps

        # ---- Device is online - request fresh status ----
        managed.client.ip = managed.config["host"]

        async with managed.lock:
            try:
                status = await managed.client.status_with_fallback()
            except Exception as exc:
                status = None
                self._classify_error(managed, f"status failed: {exc!r}")
                _LOGGER.warning("Conti device %s status exception: %s", device_id, exc)

        managed.diag.last_tx_hex = managed.client.last_tx_hex
        managed.diag.last_rx_hex = managed.client.last_rx_hex

        if status:
            managed.diag.last_status_ok = time.monotonic()
            managed.diag.consecutive_failures = 0
        else:
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
                _LOGGER.warning(
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
        return managed.online if managed else False

    def device_ids(self) -> list[str]:
        return list(self._devices)

    def get_client(self, device_id: str) -> TinyTuyaDevice | None:
        """Return the raw client for a device (used for diagnostics)."""
        managed = self._devices.get(device_id)
        return managed.client if managed else None

    def seed_cached_dps(
        self, device_id: str, dps: dict[str, Any]
    ) -> None:
        """Pre-load cached DPS from persistent storage.

        Called during ``async_setup_entry`` so entities have data
        before the first live poll completes.
        """
        managed = self._devices.get(device_id)
        if managed and not managed.client.cached_dps:
            managed.client._cached_dps.update(dps)  # noqa: SLF001

    # -- Diagnostics ---------------------------------------------------------

    def get_device_diagnostics(self, device_id: str) -> dict[str, Any]:
        """Return diagnostic info for *device_id*."""
        managed = self._devices.get(device_id)
        if not managed:
            return {"device_id": device_id, "error": "not registered"}
        d = managed.diag
        client = managed.client
        return {
            "device_id": device_id,
            "host": client.ip,
            "online": managed.online,
            "protocol_version": d.protocol_version,
            "auto_detected": d.auto_detected,
            "detected_version": client.detected_version,
            "last_handshake_ok": d.last_handshake_ok,
            "last_status_ok": d.last_status_ok,
            "last_error": d.last_error,
            "last_error_class": d.last_error_class,
            "consecutive_failures": d.consecutive_failures,
            "reconnect_delay": managed.reconnect_delay,
            "last_tx_hex": d.last_tx_hex,
            "last_rx_hex": d.last_rx_hex,
        }

    # -- Internal callbacks --------------------------------------------------

    def _on_dp_update(self, device_id: str, dps: dict[str, Any]) -> None:
        callbacks = self._state_callbacks.get(device_id)
        if callbacks:
            for cb in list(callbacks):
                try:
                    cb(device_id, dps)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("State callback error for %s", device_id)

    def _on_disconnect(self, device_id: str) -> None:
        managed = self._devices.get(device_id)
        if not managed:
            return
        was_online = managed.online
        managed.online = False
        self._classify_error(managed, "disconnected")
        if was_online:
            _LOGGER.warning(
                "Conti device %s disconnected - scheduling reconnect", device_id
            )
        else:
            _LOGGER.debug(
                "Conti device %s disconnect callback while already offline", device_id
            )
        if self._running:
            self._schedule_reconnect(device_id)

    # -- Reconnect -----------------------------------------------------------

    def _schedule_reconnect(self, device_id: str) -> None:
        managed = self._devices.get(device_id)
        if not managed or not self._running:
            return
        if managed.reconnect_task and not managed.reconnect_task.done():
            return  # already scheduled
        managed.reconnect_task = asyncio.create_task(
            self._reconnect(device_id),
            name=f"conti-reconnect-{device_id}",
        )

    async def _reconnect(self, device_id: str) -> None:
        """Reconnect with exponential back-off + jitter, verifying status."""
        managed = self._devices.get(device_id)
        if not managed:
            return

        while self._running and not managed.online:
            jitter = random.uniform(0, managed.reconnect_delay * 0.3)
            delay = managed.reconnect_delay + jitter
            _LOGGER.info(
                "Reconnecting to %s in %.1fs (failures=%d, last_error_class=%s)",
                device_id,
                delay,
                managed.diag.consecutive_failures,
                managed.diag.last_error_class,
            )
            await asyncio.sleep(delay)

            if not self._running:
                return

            async with managed.lock:
                await managed.client.close()
                managed.client.ip = managed.config["host"]
                try:
                    ok = await managed.client.connect()
                except Exception as exc:
                    ok = False
                    self._classify_error(managed, f"reconnect connect: {exc!r}")

            if ok:
                self._update_diag_on_connect(managed)
                managed.online = True
                managed.reconnect_delay = RECONNECT_BASE_DELAY
                managed.diag.last_handshake_ok = time.monotonic()

                # Best-effort status — don't require non-empty for "online"
                async with managed.lock:
                    try:
                        st = await managed.client.status_with_fallback()
                    except Exception as exc:
                        st = {}
                        _LOGGER.debug(
                            "Reconnect status_with_fallback raised for %s: %s",
                            device_id, exc,
                        )

                managed.diag.last_tx_hex = managed.client.last_tx_hex
                managed.diag.last_rx_hex = managed.client.last_rx_hex

                if st:
                    managed.diag.consecutive_failures = 0
                    managed.diag.last_status_ok = time.monotonic()
                    _LOGGER.info(
                        "Reconnected to %s (v%s) - DPS available: %s",
                        device_id,
                        managed.diag.protocol_version,
                        list(st.keys()),
                    )
                    return

                # Connected but empty — check if TCP is alive
                if managed.client.connected:
                    managed.diag.consecutive_failures = 0
                    _LOGGER.info(
                        "Reconnected to %s (v%s) — empty DPS but "
                        "connection alive, staying online (tx=%s rx=%s)",
                        device_id,
                        managed.diag.protocol_version,
                        managed.client.last_tx_hex[:32],
                        managed.client.last_rx_hex[:32],
                    )
                    return

                # Connection dropped during probing
                managed.online = False
                managed.diag.consecutive_failures += 1
                self._classify_error(
                    managed,
                    "reconnect: connection lost during status probing "
                    f"(tx={managed.client.last_tx_hex[:16]} "
                    f"rx={managed.client.last_rx_hex[:16]})",
                )
                _LOGGER.warning(
                    "Reconnect to %s: connection dropped during status "
                    "probing - retrying (failures=%d)",
                    device_id,
                    managed.diag.consecutive_failures,
                )
            else:
                managed.diag.consecutive_failures += 1
                if not managed.diag.last_error:
                    self._classify_error(managed, "reconnect failed")

            # Exponential back-off (capped)
            managed.reconnect_delay = min(
                managed.reconnect_delay * 2, RECONNECT_MAX_DELAY
            )

    # -- Helpers -------------------------------------------------------------

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
