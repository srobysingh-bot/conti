"""Production-safe developer notifications for offline Conti devices."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_DEVELOPER_ALERT_NOTIFY_SERVICE,
    DEFAULT_ENABLE_DEVELOPER_OFFLINE_ALERTS,
    DEFAULT_OFFLINE_ALERT_DELAY_MINUTES,
    DEFAULT_SEND_RECOVERY_EMAIL,
    ERROR_LOG_COOLDOWN,
)

_LOGGER = logging.getLogger(__name__)

_NETWORK_REASON = (
    "Home Assistant cannot reach this device at its saved IP address. The "
    "device IP may have changed, Wi-Fi may be weak, or the router/firewall "
    "may be blocking the local connection."
)
_NETWORK_ACTION = (
    "Check if the device has the same IP address in the router. If possible, "
    "add a DHCP reservation/static IP."
)
_SESSION_REASON = (
    "The device rejected the local connection. This can happen when the local "
    "session is stale, the protocol version is wrong, or the local key is no "
    "longer valid."
)
_SESSION_ACTION = (
    "Try reconnecting the device from Conti. If it still fails, verify the "
    "protocol version and local key."
)
_MALFORMED_REASON = "The device replied, but Conti could not read the local response correctly."
_MALFORMED_ACTION = "Check if this device needs cloud fallback or a different protocol."
_EMPTY_REASON = "Conti connected to the device, but the device did not send its current status."
_EMPTY_ACTION = "Try reconnecting. If it repeats, check Wi-Fi strength and protocol settings."
_PUSH_REASON = "The local connection was closed. Conti tried to reconnect automatically."
_PUSH_ACTION = (
    "If this happens often, check Wi-Fi stability, router/AP settings, and "
    "whether another integration is also controlling the same device."
)
_CLOUD_REASON = (
    "Tuya Cloud fallback is not available because the cloud project does not "
    "have the required data center or permission enabled."
)
_CLOUD_ACTION = "Check Tuya Developer Platform data center and cloud project permissions."
_UNKNOWN_REASON = (
    "Conti could not confirm the exact reason. The device is still unavailable "
    "after reconnect attempts."
)
_UNKNOWN_ACTION = "Check device Wi-Fi, saved IP, local key, protocol version, and Conti logs."


@dataclass(frozen=True, slots=True)
class OfflineReason:
    """Simple reason and suggested action safe for email."""

    code: str
    reason: str
    action: str


@dataclass(frozen=True, slots=True)
class OfflineAlertConfig:
    """Sanitized per-device alert configuration."""

    enabled: bool = DEFAULT_ENABLE_DEVELOPER_OFFLINE_ALERTS
    email: str = ""
    notify_service: str = DEFAULT_DEVELOPER_ALERT_NOTIFY_SERVICE
    delay_minutes: float = DEFAULT_OFFLINE_ALERT_DELAY_MINUTES
    send_recovery_email: bool = DEFAULT_SEND_RECOVERY_EMAIL
    friendly_name: str = ""
    area: str = ""
    device_type: str = "unknown"
    host: str = "unknown"
    protocol_version: str = "unknown"
    auto_reconnect: bool = False


@dataclass(slots=True)
class OfflineAlertState:
    """Mutable state for one device's current or most recent episode."""

    offline_since: datetime | None = None
    alert_task: asyncio.Task[None] | None = None
    alert_sent: bool = False
    last_alert_time: datetime | None = None
    last_reason: str = "unknown"
    last_recovery_time: datetime | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


def map_offline_reason(diagnostics: Mapping[str, Any] | str | None) -> OfflineReason:
    """Map internal error details to simple, non-secret user-facing text."""
    if isinstance(diagnostics, Mapping):
        raw = " ".join(
            str(diagnostics.get(key, ""))
            for key in (
                "diagnostic_reason",
                "last_error_class",
                "last_probe_failure_reason",
                "last_local_error",
                "last_error",
            )
        )
    else:
        raw = str(diagnostics or "")
    value = raw.casefold().replace("-", "_")

    if "cloud_permission_error" in value or ("cloud" in value and "permission" in value):
        return OfflineReason("cloud_permission_error", _CLOUD_REASON, _CLOUD_ACTION)
    if "914" in value or "local_key_or_version" in value:
        return OfflineReason("local_key_or_version_914", _SESSION_REASON, _SESSION_ACTION)
    if "904" in value or "malformed" in value or "unreadable" in value:
        return OfflineReason("malformed_payload_904", _MALFORMED_REASON, _MALFORMED_ACTION)
    if "empty_status" in value or "empty status" in value:
        return OfflineReason("empty_status", _EMPTY_REASON, _EMPTY_ACTION)
    if "push listener" in value or "push listen" in value or "disconnected" in value:
        return OfflineReason("push_listener_disconnected", _PUSH_REASON, _PUSH_ACTION)
    if any(
        marker in value
        for marker in (
            "901",
            "unable to connect",
            "cannot connect",
            "timeout",
            "timed out",
            "connection refused",
            "port_closed",
            "subnet_unreachable",
            "no route",
            "unreachable",
        )
    ):
        return OfflineReason("network_unreachable_901", _NETWORK_REASON, _NETWORK_ACTION)
    return OfflineReason("unknown", _UNKNOWN_REASON, _UNKNOWN_ACTION)


class OfflineAlertManager:
    """Own delayed, one-shot notifications for per-device offline episodes."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._hass = hass
        self._sleep = sleep_func
        self._configs: dict[str, OfflineAlertConfig] = {}
        self._states: dict[str, OfflineAlertState] = {}
        self._warning_times: dict[str, float] = {}
        self._tasks_by_device: dict[str, set[asyncio.Task[None]]] = {}

    def configure_device(self, device_id: str, config: Mapping[str, Any]) -> None:
        """Store only values that are safe and necessary for notifications."""
        try:
            delay_minutes = float(
                config.get(
                    "offline_alert_delay_minutes",
                    DEFAULT_OFFLINE_ALERT_DELAY_MINUTES,
                )
            )
        except (TypeError, ValueError):
            delay_minutes = float(DEFAULT_OFFLINE_ALERT_DELAY_MINUTES)
        # Config flow enforces this range; clamping also protects hand-edited
        # or migrated config entries from creating unsafe timers.
        delay_minutes = min(60.0, max(1.0, delay_minutes))
        self._configs[device_id] = OfflineAlertConfig(
            enabled=bool(config.get("enable_developer_offline_alerts", False)),
            email=str(config.get("developer_alert_email", "") or "").strip(),
            notify_service=str(
                config.get(
                    "developer_alert_notify_service",
                    DEFAULT_DEVELOPER_ALERT_NOTIFY_SERVICE,
                )
                or ""
            ).strip(),
            delay_minutes=delay_minutes,
            send_recovery_email=bool(config.get("send_recovery_email", False)),
            friendly_name=str(config.get("friendly_name", "") or "").strip(),
            area=str(config.get("area", "") or "").strip(),
            device_type=str(config.get("device_type", "unknown") or "unknown"),
            host=str(config.get("host", "unknown") or "unknown"),
            protocol_version=str(config.get("protocol_version", "unknown") or "unknown"),
            auto_reconnect=bool(config.get("enable_auto_reconnect", False)),
        )
        self._states.setdefault(device_id, OfflineAlertState())

    def device_offline(self, device_id: str, diagnostics: Mapping[str, Any] | None = None) -> None:
        """Start an alert delay once and refresh diagnostics on repeats."""
        config = self._configs.get(device_id)
        if config is None or not config.enabled:
            return
        state = self._states.setdefault(device_id, OfflineAlertState())
        state.diagnostics = dict(diagnostics or {})
        state.last_reason = map_offline_reason(state.diagnostics).code
        if state.offline_since is not None:
            return

        state.offline_since = datetime.now(UTC)
        state.alert_sent = False
        state.alert_task = self._create_task(
            device_id,
            self._delayed_alert(device_id),
            f"conti-offline-alert-{device_id}",
        )

    def device_online(self, device_id: str, diagnostics: Mapping[str, Any] | None = None) -> None:
        """Cancel a pending alert and optionally send one recovery message."""
        state = self._states.get(device_id)
        config = self._configs.get(device_id)
        if state is None or state.offline_since is None:
            return

        task = state.alert_task
        if task is not None and not task.done():
            task.cancel()
        should_recover = bool(
            state.alert_sent and config is not None and config.send_recovery_email
        )
        state.offline_since = None
        state.alert_task = None
        state.alert_sent = False
        state.diagnostics = dict(diagnostics or {})
        if should_recover:
            state.last_recovery_time = datetime.now(UTC)
            self._create_task(
                device_id,
                self._send_recovery(device_id),
                f"conti-recovery-alert-{device_id}",
            )

    async def remove_device(self, device_id: str) -> None:
        """Cancel one device's pending alert without treating unload as recovery."""
        self._states.pop(device_id, None)
        self._configs.pop(device_id, None)
        tasks = list(self._tasks_by_device.pop(device_id, set()))
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def async_shutdown(self) -> None:
        """Cancel all pending alert timers cleanly."""
        tasks = [
            task
            for device_tasks in self._tasks_by_device.values()
            for task in device_tasks
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for state in self._states.values():
            state.alert_task = None
        self._tasks_by_device.clear()

    def get_diagnostics(self, device_id: str) -> dict[str, Any]:
        """Return alert status without exposing the developer email address."""
        config = self._configs.get(device_id, OfflineAlertConfig())
        state = self._states.get(device_id, OfflineAlertState())
        reason = map_offline_reason(state.diagnostics)
        return {
            "offline_alert_enabled": config.enabled,
            "offline_alert_sent": state.alert_sent,
            "offline_since": self._iso(state.offline_since),
            "offline_alert_reason": reason.code if state.offline_since else "",
            "offline_alert_last_sent": self._iso(state.last_alert_time),
            "developer_alert_notify_service": config.notify_service,
        }

    async def _delayed_alert(self, device_id: str) -> None:
        current = asyncio.current_task()
        try:
            config = self._configs.get(device_id)
            if config is None:
                return
            await self._sleep(max(0.0, config.delay_minutes * 60.0))
            state = self._states.get(device_id)
            config = self._configs.get(device_id)
            if (
                state is None
                or config is None
                or not config.enabled
                or state.offline_since is None
                or state.alert_sent
            ):
                return
            # Mark first so notification-service failures cannot cause spam.
            state.alert_sent = True
            state.last_alert_time = datetime.now(UTC)
            await self._send_offline(device_id)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # defensive isolation from the reconnect lifecycle
            self._warn_rate_limited(
                "alert_worker",
                "Conti offline alert worker failed for device %s: %s",
                device_id,
                exc,
            )
        finally:
            state = self._states.get(device_id)
            if state is not None and state.alert_task is current:
                state.alert_task = None

    async def _send_offline(self, device_id: str) -> None:
        config = self._configs[device_id]
        state = self._states[device_id]
        diagnostics = state.diagnostics
        reason = map_offline_reason(diagnostics)
        name = config.friendly_name or device_id
        delay = self._format_minutes(config.delay_minutes)
        title = f"Conti Alert: Device offline - {name}"
        message = "\n".join(
            (
                f"Device: {name}",
                f"Device ID: {device_id}",
                f"Area/Room: {config.area or 'Not available'}",
                f"Device type: {config.device_type}",
                f"Saved IP: {config.host}",
                f"Protocol: {config.protocol_version}",
                f"Status: Offline for more than {delay}",
                "Last seen: " + str(diagnostics.get("last_successful_local_update") or "Unknown"),
                "Auto reconnect: " + ("enabled" if config.auto_reconnect else "disabled"),
                "Reconnect attempts: " + str(int(diagnostics.get("reconnect_attempts", 0) or 0)),
                "",
                f"Simple reason: {reason.reason}",
                "",
                "What Conti tried:",
                "- Tried reconnecting automatically",
                "- Created a fresh local session",
                "- Checked the saved IP",
                "- Checked the device status",
                "- Device is still not responding locally",
                "",
                f"Suggested action: {reason.action}",
            )
        )
        await self._notify(device_id, title, message)

    async def _send_recovery(self, device_id: str) -> None:
        config = self._configs.get(device_id)
        if config is None:
            return
        name = config.friendly_name or device_id
        await self._notify(
            device_id,
            f"Conti Recovery: Device online - {name}",
            f"Device: {name}\nDevice ID: {device_id}\nStatus: Online again",
        )

    async def _notify(self, device_id: str, title: str, message: str) -> None:
        config = self._configs.get(device_id)
        if config is None:
            return
        try:
            domain, service = self._split_service(config.notify_service)
            data: dict[str, Any] = {"title": title, "message": message}
            if config.email:
                data["target"] = [config.email]
            await self._hass.services.async_call(domain, service, data, blocking=True)
        except Exception as exc:  # noqa: BLE001
            self._warn_rate_limited(
                "notify_failure",
                "Conti developer alert notification failed for device %s: %s",
                device_id,
                exc,
            )
            fallback = (
                "Conti offline alert could not be emailed because developer "
                "email is not configured."
                if not config.email
                else f"Conti could not send the developer alert for {device_id}."
            )
            await self._persistent_notification(device_id, fallback)

    async def _persistent_notification(self, device_id: str, message: str) -> None:
        notification_id = "conti_offline_alert_" + re.sub(r"[^a-zA-Z0-9_-]", "_", device_id)
        try:
            await self._hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Conti offline alert",
                    "message": message,
                    "notification_id": notification_id,
                },
                blocking=False,
            )
        except Exception as exc:  # noqa: BLE001
            self._warn_rate_limited(
                "persistent_notification_failure",
                "Conti persistent notification failed: %s",
                exc,
            )

    def _create_task(self, device_id: str, coro: Awaitable[None], name: str) -> asyncio.Task[None]:
        creator = getattr(self._hass, "async_create_task", None)
        if callable(creator):
            task = creator(coro, name=name)
        else:
            task = asyncio.create_task(coro, name=name)
        tasks = self._tasks_by_device.setdefault(device_id, set())
        tasks.add(task)
        task.add_done_callback(lambda done, _id=device_id: self._discard_task(_id, done))
        return task

    def _discard_task(self, device_id: str, task: asyncio.Task[None]) -> None:
        tasks = self._tasks_by_device.get(device_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._tasks_by_device.pop(device_id, None)

    def _warn_rate_limited(self, key: str, message: str, *args: Any) -> None:
        now = time.monotonic()
        last_warning = self._warning_times.get(key)
        if last_warning is not None and now - last_warning < ERROR_LOG_COOLDOWN:
            _LOGGER.debug(message + " (suppressed by cooldown)", *args)
            return
        self._warning_times[key] = now
        _LOGGER.warning(message, *args)

    @staticmethod
    def _split_service(value: str) -> tuple[str, str]:
        normalized = value.strip()
        if not normalized:
            raise ValueError("notify service is not configured")
        if "." in normalized:
            domain, service = normalized.split(".", 1)
        else:
            domain, service = "notify", normalized
        if not domain or not service:
            raise ValueError("invalid notify service")
        return domain, service

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _format_minutes(value: float) -> str:
        minutes = int(value) if value.is_integer() else value
        return f"{minutes} minute" + ("" if value == 1 else "s")
