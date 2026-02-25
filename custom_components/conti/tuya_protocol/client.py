"""Async Tuya device TCP client.

Clean, high-level interface for a single Tuya-firmware device over the
local LAN.  Protocol details (encryption, framing, session negotiation)
are fully encapsulated.  No Home Assistant dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

from .base import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    HEARTBEAT_INTERVAL,
    PROTO_31,
    PROTO_33,
    PROTO_34,
    PROTO_35,
    PROTO_AUTO,
    READ_TIMEOUT,
    TuyaCommand,
    TuyaProtocol,
)
from .packet import extract_frames
from .v31 import TuyaV31
from .v33 import TuyaV33
from .v34 import TuyaV34
from .v35 import TuyaV35

_LOGGER = logging.getLogger(__name__)


def _create_protocol(
    version: str, local_key: bytes, device_id: str
) -> TuyaProtocol:
    """Instantiate the correct protocol handler for *version*."""
    if version == PROTO_35:
        return TuyaV35(local_key, device_id)
    if version == PROTO_34:
        return TuyaV34(local_key, device_id)
    if version == PROTO_31:
        return TuyaV31(local_key, device_id)
    return TuyaV33(local_key, device_id)


class TuyaDeviceClient:
    """Async TCP client for a single Tuya device."""

    def __init__(
        self,
        device_id: str,
        ip: str,
        local_key: str,
        version: str = PROTO_33,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._device_id = device_id
        self._ip = ip
        self._local_key = local_key.encode() if isinstance(local_key, str) else local_key
        self._version = version
        self._port = port
        self._timeout = timeout

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._seqno: int = 0
        self._connected: bool = False
        self._last_seen: float = 0.0
        self._cached_dps: dict[str, Any] = {}

        self._protocol: Optional[TuyaProtocol] = None
        # Initialize a default protocol so decode/encode work before connect.
        # For "auto" mode, default to v3.3; connect() will replace as needed.
        self._protocol = _create_protocol(
            PROTO_33 if version == PROTO_AUTO else version,
            self._local_key,
            self._device_id,
        )
        self._read_task: Optional[asyncio.Task[None]] = None
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._on_dp_update: Optional[Callable[[dict[str, Any]], None]] = None
        self._on_disconnect: Optional[Callable[[], None]] = None
        # TCP stream reassembly buffer — frames may be split across reads
        self._recv_buf: bytes = b""
        # Auto-detect locking: once detected, reuse on reconnect
        self._detected_version: Optional[str] = None
        self._detect_fail_count: int = 0
        # Diagnostics: first 32 bytes of last TX/RX frames
        self._last_tx_hex: str = ""
        self._last_rx_hex: str = ""

    # -- Properties ----------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def ip(self) -> str:
        return self._ip

    @ip.setter
    def ip(self, value: str) -> None:
        self._ip = value

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_seen(self) -> float:
        return self._last_seen

    @property
    def cached_dps(self) -> dict[str, Any]:
        """Return the most recent DP snapshot (keys are *string* DP ids)."""
        return dict(self._cached_dps)

    @property
    def detected_version(self) -> Optional[str]:
        """Protocol version locked after auto-detection, or ``None``."""
        return self._detected_version

    @property
    def protocol_version(self) -> str:
        """Current protocol version string."""
        if self._protocol:
            return self._protocol.version
        return self._version

    @property
    def last_tx_hex(self) -> str:
        """Hex of first 32 bytes of last transmitted frame."""
        return self._last_tx_hex

    @property
    def last_rx_hex(self) -> str:
        """Hex of first 32 bytes of last received frame."""
        return self._last_rx_hex

    # -- Callbacks -----------------------------------------------------------

    def set_dp_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback invoked whenever DPs are updated."""
        self._on_dp_update = callback

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when the connection drops."""
        self._on_disconnect = callback

    # -- Connection lifecycle ------------------------------------------------

    async def connect(self) -> bool:
        """Open TCP, negotiate session if required, start background loops.

        When version is 'auto', tries 3.3 → 3.1 → 3.4 → 3.5.
        Once a version succeeds, it is locked for future reconnects.
        Returns True on success.
        """
        versions = self._versions_to_try()
        _LOGGER.info(
            "Conti connect starting for %s (%s:%d), versions to try: %s",
            self._device_id, self._ip, self._port, versions,
        )

        for version in versions:
            ok = await self._attempt_connect(version)
            if ok:
                if self._version == PROTO_AUTO:
                    self._detected_version = version
                    self._detect_fail_count = 0
                    _LOGGER.info(
                        "Auto-detected and locked protocol v%s for %s",
                        version,
                        self._device_id,
                    )
                return True

        # Track consecutive failures for locked version re-detection
        if self._detected_version:
            self._detect_fail_count += 1

        _LOGGER.error(
            "Conti FAILED to connect %s (%s:%d) with ALL protocol versions tried: %s",
            self._device_id,
            self._ip,
            self._port,
            versions,
        )
        return False

    async def _attempt_connect(self, version: str) -> bool:
        """Try to connect using a specific protocol version."""
        # Cancel any leftover read task from a prior attempt
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None

        await self._close_socket()
        self._recv_buf = b""

        try:
            _LOGGER.info(
                "Conti opening TCP to %s:%s (v%s, device=%s)",
                self._ip, self._port, version, self._device_id,
            )
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._ip, self._port),
                timeout=self._timeout,
            )
            _LOGGER.info(
                "Conti TCP socket opened for %s (%s:%s)",
                self._device_id, self._ip, self._port,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            _LOGGER.warning(
                "Conti TCP connect FAILED for %s (%s:%d, v%s): %s",
                self._device_id,
                self._ip,
                self._port,
                version,
                exc,
            )
            return False

        protocol = _create_protocol(version, self._local_key, self._device_id)

        if protocol.needs_handshake:
            ok = await protocol.perform_handshake(
                self._reader, self._writer, self._timeout
            )
            if not ok:
                _LOGGER.warning(
                    "Handshake failed for %s (v%s) — skipping",
                    self._device_id, version,
                )
                await self._close_socket()
                return False

        self._protocol = protocol
        self._connected = True
        self._last_seen = time.monotonic()
        # Skip sequence numbers already used by handshake
        self._seqno = 2 if protocol.needs_handshake else 0

        # Brief stabilisation delay after TCP open / handshake.
        # Some devices need a moment before they accept commands.
        await asyncio.sleep(0.2)

        self._read_task = asyncio.create_task(
            self._read_loop(), name=f"conti-read-{self._device_id}"
        )

        # Validate the protocol version with a lightweight heartbeat.
        # A heartbeat is universally supported and won't trigger the socket
        # resets that DP_QUERY causes on some devices (especially RGB lights).
        hb_ok = await self._send(TuyaCommand.HEARTBEAT, b"")
        if hb_ok:
            await asyncio.sleep(0.3)

        if not self._connected:
            # Device closed the connection — wrong protocol version?
            if self._version == PROTO_AUTO and not self._detected_version:
                _LOGGER.info(
                    "Conti auto-detect: v%s dropped after heartbeat for %s "
                    "— trying next version",
                    version, self._device_id,
                )
                return False
            # Explicit or locked version — accept anyway; reconnect loop
            # will recover.  Do NOT reject the connection here.
            _LOGGER.warning(
                "Conti device %s dropped after heartbeat (v%s)"
                " — accepting, will reconnect at runtime",
                self._device_id, version,
            )

        # Start heartbeat loop only if the connection is still alive
        if self._connected:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name=f"conti-hb-{self._device_id}"
            )

        _LOGGER.info(
            "Connected to Tuya device %s at %s:%d (v%s)",
            self._device_id,
            self._ip,
            self._port,
            protocol.version,
        )
        return True

    def _versions_to_try(self) -> list[str]:
        """Return protocol versions to attempt, in order.

        For auto-detect, if a version has been locked, use it directly.
        After 3 consecutive failures with the locked version, clear the
        lock and re-detect across all versions.
        """
        if self._version == PROTO_AUTO:
            if self._detected_version:
                if self._detect_fail_count >= 3:
                    _LOGGER.info(
                        "Clearing locked protocol v%s for %s after %d failures "
                        "— will re-detect",
                        self._detected_version,
                        self._device_id,
                        self._detect_fail_count,
                    )
                    self._detected_version = None
                    self._detect_fail_count = 0
                else:
                    return [self._detected_version]
            return [PROTO_33, PROTO_34, PROTO_35, PROTO_31]
        return [self._version]

    async def _close_socket(self) -> None:
        """Close the raw TCP socket without touching background tasks."""
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None
        self._reader = None

    async def close(self) -> None:
        """Gracefully tear down the connection."""
        self._connected = False
        for task in (self._read_task, self._heartbeat_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._read_task = None
        self._heartbeat_task = None

        if self._protocol:
            self._protocol.reset_session()

        await self._close_socket()
        self._recv_buf = b""
        _LOGGER.debug("Closed connection to %s", self._device_id)

    # -- Public commands -----------------------------------------------------

    async def status(self, retries: int = 2) -> dict[str, Any]:
        """Query all DPs with retry logic.

        Tries the appropriate DP_QUERY command up to *retries* times,
        waiting progressively longer between each attempt.
        Returns string DP id → value, or {} on failure.
        """
        payload = self._build_query_payload()

        # v3.4+ use the "new" DP query command; v3.1/3.3 use the legacy one.
        if self._protocol and self._protocol.version in (PROTO_34, PROTO_35):
            cmd = TuyaCommand.DP_QUERY_NEW
        else:
            cmd = TuyaCommand.DP_QUERY

        for attempt in range(1, retries + 1):
            if not self._connected:
                break

            _LOGGER.info(
                "Conti sending DP query (cmd=0x%02X, attempt %d/%d) to %s (v%s)",
                cmd, attempt, retries, self._device_id,
                self._protocol.version if self._protocol else "?",
            )

            ok = await self._send(cmd, payload)
            if not ok:
                _LOGGER.warning(
                    "Conti DP query send failed for %s (attempt %d)",
                    self._device_id, attempt,
                )
                continue

            # Give the read loop time to receive the response.
            # Increase wait on later attempts.
            await asyncio.sleep(0.5 + 0.3 * (attempt - 1))

            if self._cached_dps:
                result = dict(self._cached_dps)
                _LOGGER.info(
                    "Conti status result for %s (attempt %d): %s",
                    self._device_id, attempt, result,
                )
                return result

            _LOGGER.debug(
                "Conti status empty for %s after attempt %d/%d",
                self._device_id, attempt, retries,
            )

        result = dict(self._cached_dps)
        _LOGGER.info(
            "Conti status final for %s after %d attempts: %s",
            self._device_id, retries, result if result else "(empty)",
        )
        return result

    async def status_with_fallback(self) -> dict[str, Any]:
        """Query DPs with multiple fallback strategies.

        Some Tuya devices (especially RGB lights) close the socket when
        they receive an unsupported DP_QUERY command.  This method uses
        a graduated approach:

        1. Send a heartbeat first to "warm up" the connection.
        2. Try the primary query command with retry (DP_QUERY or DP_QUERY_NEW).
        3. If empty, try the alternate command.
        4. Try a CONTROL probe with common DPs to trigger a STATUS push.
        5. If still empty, wait briefly for push-based DPS updates.
        6. Return whatever is in `cached_dps` (may be empty on first connect).

        Unlike `status()`, this method **never** returns {} due to a
        connection reset — it catches the exception, logs diagnostics,
        and returns the cache.
        """
        # Step 1: Heartbeat warms up the connection
        if self._connected:
            await self._send(TuyaCommand.HEARTBEAT, b"")
            await asyncio.sleep(0.2)

        if not self._connected:
            _LOGGER.debug(
                "Conti status_with_fallback: connection lost after heartbeat "
                "for %s (tx=%s rx=%s)",
                self._device_id, self._last_tx_hex[:16], self._last_rx_hex[:16],
            )
            return dict(self._cached_dps)

        payload = self._build_query_payload()

        # Step 2: Primary query command — try twice with increasing wait
        if self._protocol and self._protocol.version in (PROTO_34, PROTO_35):
            primary_cmd = TuyaCommand.DP_QUERY_NEW
            fallback_cmd = TuyaCommand.DP_QUERY
        else:
            primary_cmd = TuyaCommand.DP_QUERY
            fallback_cmd = TuyaCommand.DP_QUERY_NEW

        for attempt in range(1, 3):
            if not self._connected:
                break
            _LOGGER.debug(
                "Conti status_with_fallback: primary cmd=0x%02X attempt %d for %s",
                primary_cmd, attempt, self._device_id,
            )
            try:
                ok = await self._send(primary_cmd, payload)
                if ok:
                    await asyncio.sleep(0.5 + 0.3 * attempt)
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Primary query send raised for %s (attempt %d)",
                    self._device_id, attempt,
                )

            if self._cached_dps:
                _LOGGER.info(
                    "Conti status_with_fallback: got DPS from primary query "
                    "(attempt %d) for %s: %s",
                    attempt, self._device_id, self._cached_dps,
                )
                return dict(self._cached_dps)

        # Step 3: Fallback to alternate command
        if self._connected:
            _LOGGER.debug(
                "Conti status_with_fallback: primary empty, trying fallback "
                "cmd=0x%02X for %s",
                fallback_cmd, self._device_id,
            )
            try:
                ok = await self._send(fallback_cmd, payload)
                if ok:
                    await asyncio.sleep(0.8)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Fallback query send raised for %s", self._device_id)

            if self._cached_dps:
                _LOGGER.info(
                    "Conti status_with_fallback: got DPS from fallback query "
                    "for %s: %s",
                    self._device_id, self._cached_dps,
                )
                return dict(self._cached_dps)

        # Step 4: Probe with a no-op CONTROL to trigger a STATUS push.
        # Some devices only report DPS in response to a CONTROL frame.
        if self._connected and not self._cached_dps:
            _LOGGER.debug(
                "Conti status_with_fallback: sending CONTROL probe for %s",
                self._device_id,
            )
            try:
                probe_payload = self._build_control_payload({})
                await self._send(TuyaCommand.CONTROL, probe_payload)
                await asyncio.sleep(1.0)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("CONTROL probe raised for %s", self._device_id)

            if self._cached_dps:
                _LOGGER.info(
                    "Conti status_with_fallback: got DPS from CONTROL probe "
                    "for %s: %s",
                    self._device_id, self._cached_dps,
                )
                return dict(self._cached_dps)

        # Step 5: Wait for push-based DPS (some devices push after connect)
        if self._connected and not self._cached_dps:
            _LOGGER.debug(
                "Conti status_with_fallback: all queries empty for %s, "
                "waiting for push data...",
                self._device_id,
            )
            await asyncio.sleep(2.0)

        result = dict(self._cached_dps)
        _LOGGER.info(
            "Conti status_with_fallback final for %s: %s (connected=%s, "
            "tx=%s, rx=%s)",
            self._device_id,
            result if result else "(empty)",
            self._connected,
            self._last_tx_hex[:32],
            self._last_rx_hex[:32],
        )
        return result

    def _build_query_payload(self) -> bytes:
        """Build the standard JSON payload for a DP query."""
        return json.dumps(
            {
                "gwId": self._device_id,
                "devId": self._device_id,
                "uid": self._device_id,
                "t": str(int(time.time())),
            },
            separators=(",", ":"),
        ).encode()

    def _build_control_payload(self, dps: dict[int | str, Any]) -> bytes:
        """Build the standard JSON payload for a CONTROL command."""
        return json.dumps(
            {
                "gwId": self._device_id,
                "devId": self._device_id,
                "uid": self._device_id,
                "t": str(int(time.time())),
                "dps": {str(k): v for k, v in dps.items()},
            },
            separators=(",", ":"),
        ).encode()

    async def set_dp(self, dp_id: int, value: Any) -> bool:
        """Set a single DP value."""
        return await self.set_dps({dp_id: value})

    async def set_dps(self, dps: dict[int, Any]) -> bool:
        """Set multiple DP values at once."""
        payload = self._build_control_payload(dps)
        return await self._send(TuyaCommand.CONTROL, payload)

    async def heartbeat(self) -> bool:
        """Send a heartbeat frame."""
        return await self._send(TuyaCommand.HEARTBEAT, b"")

    async def detect_dps(self) -> dict[str, Any]:
        """Auto-discover which DPs the device supports.

        Combines multiple strategies to build the most complete picture:

        1. Standard DP_QUERY (0x0A) — works on most devices.
        2. DP_QUERY_NEW (0x10) — works on v3.4+ devices.
        3. CONTROL probe with common DP ids (1–28) — some devices only
           report DPs in their STATUS push after receiving a CONTROL.

        Returns the discovered DP dict ``{"1": <value>, "2": <value>, …}``.
        The result is also merged into ``cached_dps``.
        """
        if not self._connected:
            _LOGGER.warning("detect_dps called but not connected for %s", self._device_id)
            return dict(self._cached_dps)

        _LOGGER.info("Starting DPS auto-detection for %s", self._device_id)

        # Save whatever we already know
        before = set(self._cached_dps.keys())

        # --- Strategy 1: DP_QUERY ------------------------------------------
        payload = self._build_query_payload()
        try:
            await self._send(TuyaCommand.DP_QUERY, payload)
            await asyncio.sleep(1.0)
        except Exception:  # noqa: BLE001
            pass

        # --- Strategy 2: DP_QUERY_NEW --------------------------------------
        if self._connected:
            try:
                await self._send(TuyaCommand.DP_QUERY_NEW, payload)
                await asyncio.sleep(1.0)
            except Exception:  # noqa: BLE001
                pass

        # --- Strategy 3: CONTROL probe with empty values -------------------
        # Some devices only report their DPs in a STATUS push after a
        # CONTROL frame, even if the values are unchanged.  We send a
        # no-op CONTROL with common DP ids set to ``None`` which most
        # devices will ignore but respond with a STATUS push containing
        # their real DP values.
        if self._connected and len(self._cached_dps) == 0:
            _LOGGER.debug(
                "detect_dps: no DPs found yet for %s — trying CONTROL probe",
                self._device_id,
            )
            # Probe a range of common DP ids
            probe_dps: dict[str, Any] = {}
            for dp_id in range(1, 29):
                probe_dps[str(dp_id)] = None
            try:
                probe_payload = json.dumps(
                    {
                        "gwId": self._device_id,
                        "devId": self._device_id,
                        "uid": self._device_id,
                        "t": str(int(time.time())),
                        "dps": probe_dps,
                    },
                    separators=(",", ":"),
                ).encode()
                await self._send(TuyaCommand.CONTROL, probe_payload)
                await asyncio.sleep(1.5)
            except Exception:  # noqa: BLE001
                pass

        # Wait briefly for any push frames still in-flight
        if self._connected and not self._cached_dps:
            await asyncio.sleep(1.0)

        discovered = dict(self._cached_dps)
        after = set(discovered.keys())
        new_dps = after - before

        _LOGGER.info(
            "DPS auto-detection for %s complete: %d DPs total, %d newly discovered: %s",
            self._device_id,
            len(discovered),
            len(new_dps),
            sorted(new_dps) if new_dps else "(none)",
        )
        return discovered

    # -- Internal I/O --------------------------------------------------------

    async def _send(self, cmd: TuyaCommand, payload: bytes) -> bool:
        if not self._connected or not self._writer or not self._protocol:
            _LOGGER.warning(
                "Conti _send skipped for %s: connected=%s writer=%s protocol=%s",
                self._device_id, self._connected,
                self._writer is not None, self._protocol is not None,
            )
            return False
        try:
            frame = self._protocol.encode(
                cmd=cmd,
                payload=payload,
                seqno=self._next_seq(),
            )
        except RuntimeError as exc:
            _LOGGER.error("Encode failed for %s: %s", self._device_id, exc)
            await self._handle_disconnect()
            return False

        try:
            self._last_tx_hex = frame[:32].hex()
            self._writer.write(frame)
            await self._writer.drain()
            return True
        except (OSError, ConnectionError) as exc:
            _LOGGER.error("Send failed for %s: %s", self._device_id, exc)
            await self._handle_disconnect()
            return False

    def _next_seq(self) -> int:
        self._seqno += 1
        return self._seqno

    # -- Background loops ----------------------------------------------------

    async def _read_loop(self) -> None:
        """Continuously read and parse frames from the device.

        Uses a reassembly buffer because TCP is a stream protocol —
        a single ``read()`` may deliver partial frames, multiple
        frames, or a mix of both.
        """
        self._recv_buf = b""
        try:
            while self._connected and self._reader:
                try:
                    data = await asyncio.wait_for(
                        self._reader.read(4096), timeout=READ_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    continue
                except (ConnectionResetError, ConnectionAbortedError, OSError) as exc:
                    _LOGGER.warning(
                        "Conti connection lost for %s: %s", self._device_id, exc
                    )
                    break

                if not data:
                    _LOGGER.warning(
                        "Conti device %s closed the TCP connection (EOF)", self._device_id
                    )
                    break

                self._recv_buf += data
                frames, self._recv_buf = extract_frames(self._recv_buf)
                for frame_bytes in frames:
                    self._last_rx_hex = frame_bytes[:32].hex()
                    self._process_data(frame_bytes)

        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Read loop error for %s", self._device_id)

        await self._handle_disconnect()

    def _process_data(self, data: bytes) -> None:
        """Parse raw bytes into a frame and update cached DPs."""
        if not self._protocol:
            return

        frame = self._protocol.decode(data)
        if frame is None:
            return

        self._last_seen = time.monotonic()

        # Heartbeat response — nothing else to do.
        if frame.cmd == TuyaCommand.HEARTBEAT:
            return

        # Try to extract DPS from JSON payload.
        try:
            parsed = json.loads(frame.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        dps = parsed.get("dps")
        if dps and isinstance(dps, dict):
            self._cached_dps.update(dps)
            if self._on_dp_update:
                try:
                    self._on_dp_update(dict(self._cached_dps))
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("DP callback error for %s", self._device_id)

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats to keep the connection alive."""
        try:
            while self._connected:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not await self.heartbeat():
                    break
        except asyncio.CancelledError:
            return

    async def _handle_disconnect(self) -> None:
        """Mark as disconnected and invoke the callback."""
        if not self._connected:
            return
        _LOGGER.warning("Conti connection lost for device %s", self._device_id)
        self._connected = False
        # Never reuse session key across reconnects
        if self._protocol:
            self._protocol.reset_session()
        # Clean up socket so writer doesn't linger as non-None
        await self._close_socket()
        if self._on_disconnect:
            try:
                self._on_disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Disconnect callback error for %s", self._device_id)
