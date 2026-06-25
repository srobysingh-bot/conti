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
import errno
import logging
import socket
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
        self._initial_status_dps: dict[str, Any] = {}
        self._last_failure_reason: str = ""
        self._last_failure_detail: str = ""
        self._confirmed_protocol_mismatch: bool = False
        self._attempt_failures: list[dict[str, Any]] = []

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
    def initial_status_dps(self) -> dict[str, Any]:
        return dict(self._initial_status_dps)

    @property
    def last_failure_reason(self) -> str:
        return self._last_failure_reason

    @property
    def last_failure_detail(self) -> str:
        return self._last_failure_detail

    @property
    def confirmed_protocol_mismatch(self) -> bool:
        return self._confirmed_protocol_mismatch

    @property
    def attempt_failures(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._attempt_failures]

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
        self._attempt_failures = []
        self._last_failure_reason = ""
        self._last_failure_detail = ""
        self._confirmed_protocol_mismatch = False
        self._initial_status_dps = {}

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
                "Conti auto-detect failed device=%s ip=%s tried=%s "
                "attempt_failures=%s",
                self._device_id,
                self._ip,
                AUTO_DETECT_ORDER,
                self._attempt_failures,
            )
            self._connected = False
            return False

        ver = float(self._version_str)
        return await asyncio.to_thread(self._try_connect_version, ver)

    def _try_connect_version(self, version: float) -> bool:
        """Run one synchronous TinyTuya status probe."""
        query_command = getattr(tinytuya, "DP_QUERY", 10)
        if not isinstance(query_command, int):
            query_command = 10
        command = f"status/DP_QUERY({query_command})"
        if self._device:
            try:
                self._device.close()
            except Exception:  # noqa: BLE001
                pass
            self._device = None

        _LOGGER.info(
            "Conti status probe starting device=%s ip=%s protocol=%.1f "
            "command=%s",
            self._device_id,
            self._ip,
            version,
            command,
        )

        d = tinytuya.Device(
            dev_id=self._device_id,
            address=self._ip,
            local_key=self._local_key,
            version=version,
            port=self._port,
        )
        d.set_socketPersistent(True)
        d.set_sendWait(None)

        if self._monitored_dp_ids:
            try:
                d.set_dpsUsed(self._monitored_dp_ids)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "set_dpsUsed failed device=%s ip=%s protocol=%.1f "
                    "exception=%r",
                    self._device_id,
                    self._ip,
                    version,
                    exc,
                )

        try:
            result = d.status()
        except Exception as exc:  # noqa: BLE001
            reason = self._classify_status_failure(exc=exc)
            self._record_attempt_failure(
                version, command, reason, repr(exc), confirmed=False
            )
            _LOGGER.warning(
                "Conti status probe failed device=%s ip=%s protocol=%.1f "
                "command=%s reason=%s exception=%r",
                self._device_id,
                self._ip,
                version,
                command,
                reason,
                exc,
            )
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
            return False

        if version == 3.4 and self._is_err_904(result):
            result = self._try_v34_dali_status_fallbacks(d, version, result)

        if isinstance(result, dict) and "dps" in result:
            raw_dps = result.get("dps")
            dps = (
                {str(k): v for k, v in raw_dps.items()}
                if isinstance(raw_dps, dict)
                else {}
            )
            self._device = d
            self._protocol_version = str(version)
            self._initial_status_dps = dps
            self._cached_dps.update(dps)
            self._connected = True
            if dps:
                self._last_failure_reason = ""
                self._last_failure_detail = ""
                _LOGGER.info(
                    "Conti status probe succeeded device=%s ip=%s "
                    "protocol=%.1f command=%s dps=%s",
                    self._device_id,
                    self._ip,
                    version,
                    command,
                    dps,
                )
            else:
                self._last_failure_reason = "empty_status"
                self._last_failure_detail = repr(result)
                _LOGGER.warning(
                    "Conti status probe returned empty DPS device=%s ip=%s "
                    "protocol=%.1f command=%s payload=%r",
                    self._device_id,
                    self._ip,
                    version,
                    command,
                    result,
                )
            return True

        reason = self._classify_status_failure(result=result)
        self._record_attempt_failure(
            version,
            command,
            reason,
            repr(result),
            confirmed=reason == "protocol_mismatch",
        )
        _LOGGER.warning(
            "Conti status probe rejected device=%s ip=%s protocol=%.1f "
            "command=%s reason=%s payload=%r",
            self._device_id,
            self._ip,
            version,
            command,
            reason,
            result,
        )
        if reason in {"malformed_payload_904", "local_key_or_version_914"}:
            # Keep the configured v3.4 device object so deferred-local DALI
            # entries can attempt CONTROL commands without a status handshake.
            self._device = d
            self._protocol_version = str(version)
            self._connected = False
        else:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
        return False

    @staticmethod
    def _is_err_904(result: Any) -> bool:
        return isinstance(result, dict) and str(result.get("Err", "")) == "904"

    def _try_v34_dali_status_fallbacks(
        self,
        device: Any,
        version: float,
        initial_result: Any,
    ) -> Any:
        """Try TinyTuya-supported v3.4 status alternatives after Err 904."""
        _LOGGER.warning(
            "Conti DALI probe strategy=normal_status device=%s ip=%s "
            "protocol=%.1f raw_result=%r",
            self._device_id,
            self._ip,
            version,
            initial_result,
        )

        strategies: list[tuple[str, Callable[[], Any]]] = []
        query_new = getattr(tinytuya, "DP_QUERY_NEW", 0x10)
        if not isinstance(query_new, int):
            query_new = 0x10
        strategies.append(
            (
                "dp_query_new_0x10",
                lambda: device._send_receive(  # noqa: SLF001
                    device.generate_payload(query_new),
                    0,
                ),
            )
        )

        if hasattr(device, "updatedps"):
            dp_ids = (
                [int(dp_id) for dp_id in self._monitored_dp_ids]
                if self._monitored_dp_ids
                else [20, 21, 22, 23]
            )
            strategies.append(
                ("updatedps", lambda: device.updatedps(dp_ids))
            )

        if hasattr(device, "heartbeat"):
            def _heartbeat_status() -> Any:
                heartbeat_result = device.heartbeat(nowait=False)
                _LOGGER.info(
                    "Conti DALI probe strategy=heartbeat device=%s ip=%s "
                    "protocol=%.1f raw_result=%r",
                    self._device_id,
                    self._ip,
                    version,
                    heartbeat_result,
                )
                return device.status()

            strategies.append(("heartbeat_then_status", _heartbeat_status))

        if hasattr(device, "receive"):
            def _open_status() -> Any:
                send_result = device.status(nowait=True)
                _LOGGER.info(
                    "Conti DALI probe strategy=open_status_send device=%s "
                    "ip=%s protocol=%.1f raw_result=%r",
                    self._device_id,
                    self._ip,
                    version,
                    send_result,
                )
                return device.receive()

            strategies.append(("open_status_receive", _open_status))

        last_result = initial_result
        for strategy, probe in strategies:
            try:
                last_result = probe()
                _LOGGER.info(
                    "Conti DALI probe strategy=%s device=%s ip=%s "
                    "protocol=%.1f raw_result=%r",
                    strategy,
                    self._device_id,
                    self._ip,
                    version,
                    last_result,
                )
            except Exception as exc:  # noqa: BLE001
                last_result = exc
                _LOGGER.warning(
                    "Conti DALI probe strategy=%s device=%s ip=%s "
                    "protocol=%.1f exception=%r",
                    strategy,
                    self._device_id,
                    self._ip,
                    version,
                    exc,
                )
                continue

            if isinstance(last_result, dict) and "dps" in last_result:
                return last_result

        return initial_result

    def _record_attempt_failure(
        self,
        version: float,
        command: str,
        reason: str,
        detail: str,
        *,
        confirmed: bool,
    ) -> None:
        """Store one protocol attempt failure for diagnostics."""
        self._last_failure_reason = reason
        self._last_failure_detail = detail
        if confirmed:
            self._confirmed_protocol_mismatch = True
        self._attempt_failures.append(
            {
                "ip": self._ip,
                "protocol": f"{version:.1f}",
                "command": command,
                "reason": reason,
                "detail": detail,
                "confirmed_protocol_mismatch": confirmed,
            }
        )

    @staticmethod
    def _classify_status_failure(
        *,
        exc: Exception | None = None,
        result: Any = None,
    ) -> str:
        """Classify a TinyTuya status exception or error payload."""
        if exc is not None:
            text = f"{type(exc).__name__}: {exc}".lower()
            if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in text:
                return "timeout"
            if isinstance(exc, ConnectionRefusedError) or (
                isinstance(exc, OSError)
                and exc.errno in {errno.ECONNREFUSED, 10061}
            ):
                return "connection_refused"
            if "decrypt" in text or "decode" in text or "padding" in text:
                return "decrypt_error"
            if "914" in text:
                return "local_key_or_version_914"
            if "key" in text:
                return "invalid_key"
            if "protocol" in text and (
                "mismatch" in text or "unsupported" in text
            ):
                return "protocol_mismatch"
            if "empty" in text and "payload" in text:
                return "empty_payload"
            return "no_response"

        if result is None:
            return "no_response"
        if result in ("", b""):
            return "empty_payload"

        text = repr(result).lower()
        if isinstance(result, dict):
            error = str(result.get("Error", ""))
            err_code = str(result.get("Err", ""))
            payload = result.get("Payload")
            text = f"{error} {err_code} {payload!r}".lower()
            if err_code == "904":
                return "malformed_payload_904"
            if err_code == "914":
                return "local_key_or_version_914"
            if "Payload" in result and payload in (None, "", b"") and error:
                return "empty_payload"
        if "timeout" in text or "timed out" in text:
            return "timeout"
        if "refused" in text:
            return "connection_refused"
        if "decrypt" in text or "decode" in text or "padding" in text:
            return "decrypt_error"
        if "914" in text:
            return "local_key_or_version_914"
        if "key" in text:
            return "invalid_key"
        if "protocol" in text and ("mismatch" in text or "unsupported" in text):
            return "protocol_mismatch"
        if "empty" in text and "payload" in text:
            return "empty_payload"
        return "no_response"

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
                reason = self._classify_status_failure(result=result)
                self._last_failure_reason = reason
                self._last_failure_detail = repr(result)
                _LOGGER.debug(
                    "Conti status error for %s reason=%s raw_result=%r",
                    self._device_id,
                    reason,
                    result,
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

    def _control_failed(self, result: Any) -> bool:
        """Record a TinyTuya control error payload for fallback routing."""
        if not (isinstance(result, dict) and "Error" in result):
            return False
        self._last_failure_reason = self._classify_status_failure(result=result)
        self._last_failure_detail = repr(result)
        _LOGGER.warning(
            "Conti local control failed device=%s ip=%s protocol=%s "
            "reason=%s raw_result=%r",
            self._device_id,
            self._ip,
            self._protocol_version,
            self._last_failure_reason,
            result,
        )
        return True

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
                result = self._device._send_receive(  # type: ignore[union-attr]  # noqa: SLF001
                    payload, 0, getresponse=False
                )
                if self._control_failed(result):
                    return False
                self._cached_dps[str(dp_id)] = value
                return True
            except Exception as exc:  # noqa: BLE001
                self._last_failure_reason = self._classify_status_failure(exc=exc)
                self._last_failure_detail = repr(exc)
                _LOGGER.debug(
                    "Conti set_dp(%s, %s) first attempt failed: %s",
                    self._device_id, dp_id, exc,
                )
                self._connected = False
                if self._cached_dps:
                    try:
                        payload = self._device.generate_payload(  # type: ignore[union-attr]
                            tinytuya.CONTROL, {str(dp_id): value}
                        )
                        result = self._device._send_receive(  # type: ignore[union-attr]  # noqa: SLF001
                            payload, 0, getresponse=False
                        )
                        if self._control_failed(result):
                            return False
                        self._cached_dps[str(dp_id)] = value
                        _LOGGER.info(
                            "Conti set_dp direct cached fallback device=%s "
                            "protocol=%s dp=%s value=%r success=True",
                            self._device_id,
                            self._protocol_version,
                            dp_id,
                            value,
                        )
                        return True
                    except Exception as direct_exc:  # noqa: BLE001
                        self._last_failure_reason = self._classify_status_failure(
                            exc=direct_exc
                        )
                        self._last_failure_detail = repr(direct_exc)
                        _LOGGER.warning(
                            "Conti set_dp direct cached fallback device=%s "
                            "protocol=%s dp=%s success=False exception=%r",
                            self._device_id,
                            self._protocol_version,
                            dp_id,
                            direct_exc,
                        )
                        return False

                # Single reconnect-and-retry for devices without cloud cache.
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
                result = self._device._send_receive(  # type: ignore[union-attr]  # noqa: SLF001
                    payload, 0, getresponse=False
                )
                if self._control_failed(result):
                    return False
                self._cached_dps.update(str_dps)
                return True
            except Exception as exc:  # noqa: BLE001
                self._last_failure_reason = self._classify_status_failure(exc=exc)
                self._last_failure_detail = repr(exc)
                _LOGGER.debug(
                    "Conti set_dps(%s) first attempt failed: %s",
                    self._device_id, exc,
                )
                self._connected = False
                if self._cached_dps:
                    try:
                        str_dps = {str(k): v for k, v in dps.items()}
                        payload = self._device.generate_payload(  # type: ignore[union-attr]
                            tinytuya.CONTROL, str_dps
                        )
                        result = self._device._send_receive(  # type: ignore[union-attr]  # noqa: SLF001
                            payload, 0, getresponse=False
                        )
                        if self._control_failed(result):
                            return False
                        self._cached_dps.update(str_dps)
                        _LOGGER.info(
                            "Conti set_dps direct cached fallback device=%s "
                            "protocol=%s dps=%s success=True",
                            self._device_id,
                            self._protocol_version,
                            str_dps,
                        )
                        return True
                    except Exception as direct_exc:  # noqa: BLE001
                        self._last_failure_reason = self._classify_status_failure(
                            exc=direct_exc
                        )
                        self._last_failure_detail = repr(direct_exc)
                        _LOGGER.warning(
                            "Conti set_dps direct cached fallback device=%s "
                            "protocol=%s success=False exception=%r",
                            self._device_id,
                            self._protocol_version,
                            direct_exc,
                        )
                        return False

                # Single reconnect-and-retry for devices without cloud cache.
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
