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
* **No push callbacks** — TinyTuya is pull-based; entities receive
  updates through the DataUpdateCoordinator polling loop.
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

    def _status_sync(self) -> dict[str, Any]:
        """Synchronous status query — returns DP dict or ``{}``."""
        if not self._device:
            return {}
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

    async def status(self) -> dict[str, Any]:
        """Query the device for current DP values."""
        return await asyncio.to_thread(self._status_sync)

    async def status_with_fallback(self) -> dict[str, Any]:
        """Query status; return cached DPS when the live query is empty."""
        dps = await self.status()
        if dps:
            return dps
        return dict(self._cached_dps)

    # -- DP commands ---------------------------------------------------------

    async def set_dp(self, dp_id: int, value: Any) -> bool:
        """Set a single DP on the device."""
        if not self._device:
            return False

        def _set() -> bool:
            try:
                self._device.set_value(dp_id, value)  # type: ignore[union-attr]
                return True
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "Conti set_dp(%s, %s, %s) failed: %s",
                    self._device_id,
                    dp_id,
                    value,
                    exc,
                )
                return False

        return await asyncio.to_thread(_set)

    async def set_dps(self, dps: dict[int, Any]) -> bool:
        """Set multiple DP values in a single CONTROL command."""
        if not self._device:
            return False

        def _set() -> bool:
            try:
                str_dps = {str(k): v for k, v in dps.items()}
                payload = self._device.generate_payload(  # type: ignore[union-attr]
                    tinytuya.CONTROL, str_dps
                )
                self._device._send_receive(payload)  # type: ignore[union-attr]  # noqa: SLF001
                return True
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "Conti set_dps(%s) failed: %s", self._device_id, exc
                )
                return False

        return await asyncio.to_thread(_set)

    # -- DP discovery --------------------------------------------------------

    async def detect_dps(self) -> dict[str, Any]:
        """Auto-detect available data-points on the device."""
        if not self._device:
            return {}

        def _detect() -> dict[str, Any]:
            try:
                if hasattr(self._device, "detect_available_dps"):
                    result = self._device.detect_available_dps()  # type: ignore[union-attr]
                else:
                    # Fallback for older tinytuya: use plain status
                    raw = self._device.status()  # type: ignore[union-attr]
                    result = raw.get("dps", {}) if isinstance(raw, dict) else {}

                if result and isinstance(result, dict):
                    dps = {str(k): v for k, v in result.items()}
                    self._cached_dps.update(dps)
                    return dps
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "Conti detect_dps() raised for %s: %s",
                    self._device_id,
                    exc,
                )
            return dict(self._cached_dps)

        return await asyncio.to_thread(_detect)
