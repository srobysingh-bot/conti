"""Async wrapper around tinytuya.Device for the Conti integration.

Provides a drop-in replacement for the former custom ``TuyaDeviceClient``,
using the battle-tested `TinyTuya <https://github.com/jasonacox/tinytuya>`_
library for Tuya local-LAN protocol handling.

All synchronous TinyTuya I/O is wrapped with ``asyncio.to_thread()`` so
callers can ``await`` every operation without blocking the HA event loop.

Key design decisions
~~~~~~~~~~~~~~~~~~~~
* **Protocol version as float** — ``tinytuya.Device.set_version()``
  requires a *float* (e.g. ``3.5``), never a string.
* **local_key used as-is** — only ``.strip()`` is applied; no JSON
  parsing or character removal.
* **Persistent socket** — ``set_socketPersistent(True)`` keeps TCP
  alive between calls; the coordinator polls every 10 s.
* **Push via receive_nowait** — a background listener in
  ``DeviceManager`` calls :meth:`receive_nowait` every ~0.15 s to pick
  up unsolicited status updates (RF remote, physical button) with
  sub-200 ms latency between coordinator polls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import tinytuya

from .const import AUTO_DETECT_ORDER

_LOGGER = logging.getLogger(__name__)


class TinyTuyaDevice:
    """Async wrapper around :class:`tinytuya.Device`.

    The public API mirrors the former ``TuyaDeviceClient`` so that
    ``DeviceManager`` and ``config_flow`` require only import changes.
    """

    def __init__(
        self,
        device_id: str,
        ip: str,
        local_key: str,
        version: str = "auto",
        port: int = 6668,
    ) -> None:
        self._device_id = device_id
        self._ip = ip
        self._local_key = local_key.strip()  # Only .strip(), nothing else
        self._version_str = version
        self._port = port

        self._device: tinytuya.Device | None = None
        self._connected: bool = False
        self._cached_dps: dict[str, Any] = {}
        self._detected_version: str | None = None
        self._protocol_version: str = version if version != "auto" else "3.3"
        self._monitored_dp_ids: dict[str, None] | None = None

        # Diagnostics (kept for compatibility; TinyTuya doesn't expose hex)
        self._last_tx_hex: str = ""
        self._last_rx_hex: str = ""

        # Callback stubs — no-op in TinyTuya mode (coordinator polls)
        self._dp_callback: Callable[..., Any] | None = None
        self._disconnect_callback: Callable[..., Any] | None = None

    # -- Properties ----------------------------------------------------------

    @property
    def ip(self) -> str:
        return self._ip

    @ip.setter
    def ip(self, value: str) -> None:
        self._ip = value
        if self._device:
            self._device.address = value

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def cached_dps(self) -> dict[str, Any]:
        return dict(self._cached_dps)

    @property
    def detected_version(self) -> str | None:
        return self._detected_version

    @property
    def protocol_version(self) -> str:
        return self._protocol_version

    @property
    def last_tx_hex(self) -> str:
        return self._last_tx_hex

    @last_tx_hex.setter
    def last_tx_hex(self, value: str) -> None:
        self._last_tx_hex = value

    @property
    def last_rx_hex(self) -> str:
        return self._last_rx_hex

    @last_rx_hex.setter
    def last_rx_hex(self, value: str) -> None:
        self._last_rx_hex = value

    # -- Callback registration (compatibility stubs) -------------------------

    def set_monitored_dp_ids(self, dp_ids: list[str]) -> None:
        """Tell TinyTuya which DPs to include in status queries.

        Must be called before :meth:`connect`.  The IDs are passed to
        ``tinytuya.Device.set_dpsUsed()`` so that multi-gang / multi-DP
        devices report all their DPs, not just the default subset.
        """
        self._monitored_dp_ids = {str(dp): None for dp in dp_ids}

    def set_dp_callback(self, callback: Callable[..., Any]) -> None:
        """Register a push callback (no-op — TinyTuya is polled)."""
        self._dp_callback = callback

    def set_disconnect_callback(self, callback: Callable[..., Any]) -> None:
        """Register a disconnect callback (no-op — detected via poll)."""
        self._disconnect_callback = callback

    # -- Connection ----------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to the device.

        When ``version`` is ``"auto"``, versions are tried in the order
        defined by :data:`AUTO_DETECT_ORDER` (3.3 → 3.4 → 3.5 → 3.1).
        The first version that returns a successful ``status()`` wins.
        """
        if self._version_str == "auto":
            for ver_str in AUTO_DETECT_ORDER:
                ver = float(ver_str)
                ok = await asyncio.to_thread(self._try_connect_version, ver)
                if ok:
                    self._detected_version = ver_str
                    _LOGGER.info(
                        "Conti auto-detected protocol v%s for %s",
                        ver_str,
                        self._device_id,
                    )
                    return True
            _LOGGER.warning(
                "Conti auto-detect failed for %s — tried %s",
                self._device_id,
                AUTO_DETECT_ORDER,
            )
            self._connected = False
            return False

        ver = float(self._version_str)
        return await asyncio.to_thread(self._try_connect_version, ver)

    def _try_connect_version(self, version: float) -> bool:
        """Synchronous connection attempt for a single protocol version."""
        # Close any prior connection
        if self._device:
            try:
                self._device.close()
            except Exception:  # noqa: BLE001
                pass
            self._device = None

        _LOGGER.debug(
            "Conti connecting: device=%s ip=%s version=%s key=%s...%s",
            self._device_id,
            self._ip,
            version,
            self._local_key[:2] if len(self._local_key) > 4 else "****",
            self._local_key[-2:] if len(self._local_key) > 4 else "",
        )

        d = tinytuya.Device(
            dev_id=self._device_id,
            address=self._ip,
            local_key=self._local_key,
            version=version,
            port=self._port,
        )
        d.set_socketPersistent(True)
        d.set_sendWait(None)  # remove 10ms post-send sleep for faster commands

        # Tell TinyTuya which DPs to request so multi-gang devices
        # report all channels, not just the default subset.
        if self._monitored_dp_ids:
            try:
                d.set_dpsUsed(self._monitored_dp_ids)
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "set_dpsUsed not supported for %s — continuing",
                    self._device_id,
                )

        try:
            result = d.status()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Conti v%.1f connect failed for %s: %s",
                version,
                self._device_id,
                exc,
            )
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
            return False

        if isinstance(result, dict) and "dps" in result:
            self._device = d
            self._protocol_version = str(version)
            self._cached_dps.update(
                {str(k): v for k, v in result["dps"].items()}
            )
            self._connected = True
            _LOGGER.debug(
                "Conti connected to %s with v%.1f — DPS keys: %s",
                self._device_id,
                version,
                sorted(result["dps"].keys()),
            )
            return True

        # Connection failed or returned an error payload
        err_msg = (
            result.get("Error", str(result))
            if isinstance(result, dict)
            else str(result)
        )
        _LOGGER.debug(
            "Conti v%.1f status for %s returned error: %s",
            version,
            self._device_id,
            err_msg,
        )
        try:
            d.close()
        except Exception:  # noqa: BLE001
            pass
        return False

    async def close(self) -> None:
        """Close the connection and release the socket."""
        dev = self._device
        if dev:
            try:
                await asyncio.to_thread(dev.close)
            except Exception:  # noqa: BLE001
                pass
        self._connected = False
        self._device = None

    # -- Status queries ------------------------------------------------------

    def _status_sync(self, timeout: float | None = None) -> dict[str, Any]:
        """Synchronous status query — returns DP dict or ``{}``.

        Parameters
        ----------
        timeout:
            If given, temporarily lowers ``connection_timeout`` on the
            underlying tinytuya device so the recv blocks for at most
            *timeout* seconds.  Restored in ``finally``.
        """
        if not self._device:
            return {}
        saved_timeout: float | None = None
        if timeout is not None:
            saved_timeout = getattr(self._device, "connection_timeout", None)
            self._device.connection_timeout = timeout
        try:
            result = self._device.status()
            if isinstance(result, dict) and "dps" in result:
                dps = {str(k): v for k, v in result["dps"].items()}
                self._cached_dps.update(dps)
                return dps
            if isinstance(result, dict) and "Error" in result:
                _LOGGER.debug(
                    "Conti status error for %s: %s",
                    self._device_id,
                    result.get("Error"),
                )
                # Socket may be dead after an error response
                try:
                    if self._device.socket is None:
                        self._connected = False
                except Exception:  # noqa: BLE001
                    pass
            return {}
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Conti status() raised for %s: %s", self._device_id, exc
            )
            self._connected = False
            return {}
        finally:
            if saved_timeout is not None and self._device:
                self._device.connection_timeout = saved_timeout

    async def status(self, timeout: float | None = None) -> dict[str, Any]:
        """Query the device for current DP values."""
        return await asyncio.to_thread(self._status_sync, timeout)

    async def status_with_fallback(
        self, timeout: float | None = None
    ) -> dict[str, Any]:
        """Query status; return cached DPS when the live query is empty."""
        dps = await self.status(timeout=timeout)
        if dps:
            return dps
        return dict(self._cached_dps)

    # -- DP commands ---------------------------------------------------------

    async def set_dp(self, dp_id: int, value: Any) -> bool:
        """Set a single DP on the device using fire-and-forget CONTROL."""
        if not self._device:
            return False

        def _set() -> bool:
            try:
                str_dps = {str(dp_id): value}
                payload = self._device.generate_payload(  # type: ignore[union-attr]
                    tinytuya.CONTROL, str_dps
                )
                # nowait: send without waiting for ACK to avoid multi-second stalls
                self._device._send_receive(payload, 0, getresponse=False)  # type: ignore[union-attr]  # noqa: SLF001
                self._cached_dps[str(dp_id)] = value
                return True
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "Conti set_dp(%s, %s) first attempt failed: %s",
                    self._device_id, dp_id, exc,
                )
                self._connected = False
                # Single reconnect-and-retry (with ACK to verify socket)
                try:
                    self._device.close()  # type: ignore[union-attr]
                    result = self._device.status()  # type: ignore[union-attr]
                    if not (isinstance(result, dict) and "dps" in result):
                        _LOGGER.warning(
                            "Conti set_dp(%s, %s) retry reconnect failed",
                            self._device_id, dp_id,
                        )
                        return False
                    self._connected = True
                    payload = self._device.generate_payload(  # type: ignore[union-attr]
                        tinytuya.CONTROL, {str(dp_id): value}
                    )
                    self._device._send_receive(payload, 0, getresponse=False)  # type: ignore[union-attr]  # noqa: SLF001
                    self._cached_dps[str(dp_id)] = value
                    return True
                except Exception as retry_exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "Conti set_dp(%s, %s) retry also failed: %s",
                        self._device_id, dp_id, retry_exc,
                    )
                    self._connected = False
                    return False

        return await asyncio.to_thread(_set)

    async def set_dps(self, dps: dict[int, Any]) -> bool:
        """Set multiple DP values in a single fire-and-forget CONTROL."""
        if not self._device:
            return False

        def _set() -> bool:
            try:
                str_dps = {str(k): v for k, v in dps.items()}
                payload = self._device.generate_payload(  # type: ignore[union-attr]
                    tinytuya.CONTROL, str_dps
                )
                # nowait: send without waiting for ACK to avoid multi-second stalls
                self._device._send_receive(payload, 0, getresponse=False)  # type: ignore[union-attr]  # noqa: SLF001
                self._cached_dps.update(str_dps)
                return True
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "Conti set_dps(%s) first attempt failed: %s",
                    self._device_id, exc,
                )
                self._connected = False
                # Single reconnect-and-retry (with ACK to verify socket)
                try:
                    self._device.close()  # type: ignore[union-attr]
                    result = self._device.status()  # type: ignore[union-attr]
                    if not (isinstance(result, dict) and "dps" in result):
                        _LOGGER.warning(
                            "Conti set_dps(%s) retry reconnect failed",
                            self._device_id,
                        )
                        return False
                    self._connected = True
                    str_dps = {str(k): v for k, v in dps.items()}
                    payload = self._device.generate_payload(  # type: ignore[union-attr]
                        tinytuya.CONTROL, str_dps
                    )
                    self._device._send_receive(payload, 0, getresponse=False)  # type: ignore[union-attr]  # noqa: SLF001
                    self._cached_dps.update(str_dps)
                    return True
                except Exception as retry_exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "Conti set_dps(%s) retry also failed: %s",
                        self._device_id, retry_exc,
                    )
                    self._connected = False
                    return False

        return await asyncio.to_thread(_set)

    # -- DP discovery --------------------------------------------------------

    async def detect_dps(self) -> dict[str, Any]:
        """Auto-detect available data-points on the device.

        Uses ``detect_available_dps()`` first (probes DPs 1-255).
        If the result is sparse (≤3 DPs), a secondary forced re-query
        with ``updatedps()`` + ``set_dpsUsed()`` recovers additional
        DPs that multi-gang devices may not report in the initial probe.
        """
        if not self._device:
            return {}

        def _detect() -> dict[str, Any]:
            dps: dict[str, Any] = {}

            # ── Phase 1: standard detection ──
            try:
                if hasattr(self._device, "detect_available_dps"):
                    result = self._device.detect_available_dps()  # type: ignore[union-attr]
                else:
                    raw = self._device.status()  # type: ignore[union-attr]
                    result = raw.get("dps", {}) if isinstance(raw, dict) else {}

                if result and isinstance(result, dict):
                    dps = {str(k): v for k, v in result.items()}
                    self._cached_dps.update(dps)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "Conti detect_dps() raised for %s: %s",
                    self._device_id,
                    exc,
                )

            # ── Phase 2: forced broad re-query for sparse results ──
            # Multi-gang switches and monitoring devices often report
            # only DP 1 initially.  Explicitly requesting a broader
            # DP range forces the firmware to report all channels.
            if len(dps) <= 3 and self._device:
                try:
                    broad_ids = {str(i): None for i in range(1, 51)}
                    try:
                        self._device.set_dpsUsed(broad_ids)  # type: ignore[union-attr]
                    except Exception:  # noqa: BLE001
                        pass
                    if hasattr(self._device, "updatedps"):
                        try:
                            self._device.updatedps(  # type: ignore[union-attr]
                                list(range(1, 51))
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    raw = self._device.status()  # type: ignore[union-attr]
                    if isinstance(raw, dict) and raw.get("dps"):
                        extra = {str(k): v for k, v in raw["dps"].items()}
                        new_count = len(set(extra) - set(dps))
                        dps.update(extra)
                        self._cached_dps.update(dps)
                        if new_count:
                            _LOGGER.info(
                                "detect_dps: forced re-query found %d new DPs "
                                "for %s (total: %d)",
                                new_count,
                                self._device_id,
                                len(dps),
                            )
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "detect_dps: forced re-query failed for %s",
                        self._device_id,
                    )

            # ── Phase 3: last-resort plain status ──
            if not dps and not self._cached_dps:
                try:
                    raw = self._device.status()  # type: ignore[union-attr]
                    if isinstance(raw, dict) and raw.get("dps"):
                        dps = {str(k): v for k, v in raw["dps"].items()}
                        self._cached_dps.update(dps)
                        _LOGGER.debug(
                            "detect_dps: recovered %d DPs via status() "
                            "fallback for %s",
                            len(dps),
                            self._device_id,
                        )
                except Exception:  # noqa: BLE001
                    pass

            return dps if dps else dict(self._cached_dps)

        return await asyncio.to_thread(_detect)

    # -- Unsolicited data listener -------------------------------------------

    def _receive_nowait_sync(self) -> dict[str, Any] | None:
        """Non-blocking check for unsolicited data on the persistent socket.

        Sets a short socket timeout (50 ms) so it never stalls other I/O.
        Returns a DP dict ``{"1": val, ...}`` if an update arrived, else
        ``None``.
        """
        if not self._device or not self._connected:
            return None
        sock = getattr(self._device, "socket", None)
        if sock is None:
            self._connected = False
            return None

        old_timeout = sock.gettimeout()
        try:
            sock.settimeout(0.05)
            data = self._device.receive()
        except Exception:  # noqa: BLE001
            # Socket error — caller should check ``connected``
            self._connected = False
            return None
        finally:
            try:
                if sock.fileno() != -1:
                    sock.settimeout(old_timeout)
            except Exception:  # noqa: BLE001
                pass

        if isinstance(data, dict) and "dps" in data:
            dps = {str(k): v for k, v in data["dps"].items()}
            self._cached_dps.update(dps)
            return dps
        return None

    async def receive_nowait(self) -> dict[str, Any] | None:
        """Async wrapper — check for pending unsolicited data."""
        return await asyncio.to_thread(self._receive_nowait_sync)
