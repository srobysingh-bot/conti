"""Focused tests for delayed, non-spamming Conti offline alerts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.conti.device_manager import DeviceManager
from custom_components.conti.offline_alerts import (
    OfflineAlertManager,
    map_offline_reason,
)


class _Services:
    def __init__(self, *, fail_notify: bool = False) -> None:
        self.calls: list[tuple[str, str, dict, bool]] = []
        self.fail_notify = fail_notify

    async def async_call(self, domain: str, service: str, data: dict, *, blocking: bool) -> None:
        self.calls.append((domain, service, data, blocking))
        if self.fail_notify and domain == "notify":
            raise RuntimeError("service unavailable")


class _Hass:
    def __init__(self, *, fail_notify: bool = False) -> None:
        self.services = _Services(fail_notify=fail_notify)

    def async_create_task(self, coro, *, name: str):
        return asyncio.create_task(coro, name=name)


async def _immediate_sleep(_: float) -> None:
    await asyncio.sleep(0)


def _config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "enable_developer_offline_alerts": True,
        "developer_alert_email": "developer@example.com",
        "developer_alert_notify_service": "notify.email",
        "offline_alert_delay_minutes": 0,
        "send_recovery_email": False,
        "friendly_name": "Kitchen Plug",
        "area": "Kitchen",
        "device_type": "switch",
        "host": "192.168.1.20",
        "protocol_version": "3.4",
        "enable_auto_reconnect": True,
    }
    config.update(overrides)
    return config


def _diagnostics(**overrides: object) -> dict[str, object]:
    diagnostics: dict[str, object] = {
        "last_error_class": "timeout",
        "last_successful_local_update": "2026-07-15T10:00:00+00:00",
        "reconnect_attempts": 3,
    }
    diagnostics.update(overrides)
    return diagnostics


async def _wait_for_alert(manager: OfflineAlertManager, device_id: str) -> None:
    task = manager._states[device_id].alert_task
    assert task is not None
    await task


async def _drain_device_tasks(manager: OfflineAlertManager, device_id: str) -> None:
    tasks = list(manager._tasks_by_device.get(device_id, set()))
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_offline_then_online_before_delay_sends_no_email() -> None:
    gate = asyncio.Event()

    async def _blocked_sleep(_: float) -> None:
        await gate.wait()

    hass = _Hass()
    manager = OfflineAlertManager(hass, sleep_func=_blocked_sleep)
    manager.configure_device("device-1", _config())
    manager.device_offline("device-1", _diagnostics())
    manager.device_online("device-1")
    await asyncio.sleep(0)

    assert hass.services.calls == []
    assert manager.get_diagnostics("device-1")["offline_since"] is None
    await manager.async_shutdown()


@pytest.mark.asyncio
async def test_offline_past_delay_sends_one_email() -> None:
    hass = _Hass()
    manager = OfflineAlertManager(hass, sleep_func=_immediate_sleep)
    manager.configure_device("device-1", _config())
    manager.device_offline("device-1", _diagnostics())
    await _wait_for_alert(manager, "device-1")

    assert len(hass.services.calls) == 1
    domain, service, data, blocking = hass.services.calls[0]
    assert (domain, service, blocking) == ("notify", "email", True)
    assert data["target"] == ["developer@example.com"]
    assert data["title"] == "Conti Alert: Device offline - Kitchen Plug"
    assert "Reconnect attempts: 3" in data["message"]


@pytest.mark.asyncio
async def test_device_stays_offline_without_repeated_spam() -> None:
    hass = _Hass()
    manager = OfflineAlertManager(hass, sleep_func=_immediate_sleep)
    manager.configure_device("device-1", _config())
    manager.device_offline("device-1", _diagnostics())
    await _wait_for_alert(manager, "device-1")

    for attempts in range(4, 10):
        manager.device_offline("device-1", _diagnostics(reconnect_attempts=attempts))
    await asyncio.sleep(0)

    assert len(hass.services.calls) == 1
    assert manager.get_diagnostics("device-1")["offline_alert_sent"] is True


@pytest.mark.asyncio
async def test_recovery_resets_episode_and_next_offline_can_alert() -> None:
    hass = _Hass()
    manager = OfflineAlertManager(hass, sleep_func=_immediate_sleep)
    manager.configure_device("device-1", _config())
    manager.device_offline("device-1", _diagnostics())
    await _wait_for_alert(manager, "device-1")
    manager.device_online("device-1")

    manager.device_offline("device-1", _diagnostics())
    await _wait_for_alert(manager, "device-1")

    assert len(hass.services.calls) == 2


@pytest.mark.asyncio
async def test_recovery_email_disabled_by_default() -> None:
    hass = _Hass()
    manager = OfflineAlertManager(hass, sleep_func=_immediate_sleep)
    manager.configure_device("device-1", _config())
    manager.device_offline("device-1", _diagnostics())
    await _wait_for_alert(manager, "device-1")
    manager.device_online("device-1")
    await asyncio.sleep(0)

    assert len(hass.services.calls) == 1


@pytest.mark.asyncio
async def test_recovery_email_sent_only_when_enabled() -> None:
    hass = _Hass()
    manager = OfflineAlertManager(hass, sleep_func=_immediate_sleep)
    manager.configure_device("device-1", _config(send_recovery_email=True))
    manager.device_offline("device-1", _diagnostics())
    await _wait_for_alert(manager, "device-1")
    manager.device_online("device-1")
    await _drain_device_tasks(manager, "device-1")

    assert len(hass.services.calls) == 2
    assert hass.services.calls[1][2]["title"].startswith("Conti Recovery:")


def test_err_901_maps_to_simple_network_reason() -> None:
    reason = map_offline_reason({"last_error": "Unable to connect: Err 901"})
    assert reason.code == "network_unreachable_901"
    assert "saved IP address" in reason.reason


def test_err_914_maps_to_simple_session_protocol_key_reason() -> None:
    reason = map_offline_reason({"last_error": "local_key_or_version_914"})
    assert reason.code == "local_key_or_version_914"
    assert "local session is stale" in reason.reason


def test_err_904_maps_to_unreadable_response_reason() -> None:
    reason = map_offline_reason({"last_error": "malformed_payload_904"})
    assert reason.code == "malformed_payload_904"
    assert "could not read" in reason.reason


def test_cloud_permission_error_maps_to_simple_cloud_reason() -> None:
    reason = map_offline_reason({"diagnostic_reason": "cloud_permission_error"})
    assert reason.code == "cloud_permission_error"
    assert "data center or permission" in reason.reason


@pytest.mark.asyncio
async def test_notify_failure_falls_back_without_crashing() -> None:
    hass = _Hass(fail_notify=True)
    manager = OfflineAlertManager(hass, sleep_func=_immediate_sleep)
    manager.configure_device("device-1", _config())
    manager.device_offline("device-1", _diagnostics())
    await _wait_for_alert(manager, "device-1")

    assert [call[:2] for call in hass.services.calls] == [
        ("notify", "email"),
        ("persistent_notification", "create"),
    ]
    assert manager.get_diagnostics("device-1")["offline_alert_sent"] is True


@pytest.mark.asyncio
async def test_manager_stop_cancels_pending_alert_tasks(device_config: dict) -> None:
    gate = asyncio.Event()

    async def _blocked_sleep(_: float) -> None:
        await gate.wait()

    hass = _Hass()
    alerts = OfflineAlertManager(hass, sleep_func=_blocked_sleep)
    manager = DeviceManager(alerts)
    await manager.start()
    client = _client(connected=False)
    config = {**device_config, **_config(offline_alert_delay_minutes=5)}
    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        return_value=client,
    ):
        assert not await manager.add_device(config)
        task = alerts._states[config["device_id"]].alert_task
        assert task is not None
        await manager.stop()

    assert task.done()
    assert alerts._states[config["device_id"]].alert_task is None
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_email_body_does_not_include_secrets_or_stack_traces() -> None:
    hass = _Hass()
    manager = OfflineAlertManager(hass, sleep_func=_immediate_sleep)
    manager.configure_device(
        "device-1",
        {
            **_config(),
            "local_key": "secret-local-key",
            "token": "secret-token",
            "access_id": "secret-access-id",
            "access_secret": "secret-access-secret",
        },
    )
    manager.device_offline(
        "device-1",
        _diagnostics(
            local_key="secret-local-key",
            token="secret-token",
            access_id="secret-access-id",
            access_secret="secret-access-secret",
            stack_trace="Traceback: secret stack",
        ),
    )
    await _wait_for_alert(manager, "device-1")
    message = hass.services.calls[0][2]["message"]

    for forbidden in (
        "secret-local-key",
        "secret-token",
        "secret-access-id",
        "secret-access-secret",
        "Traceback",
    ):
        assert forbidden not in message


@pytest.mark.asyncio
async def test_auto_reconnect_success_before_delay_cancels_email(
    device_config: dict,
) -> None:
    gate = asyncio.Event()

    async def _blocked_sleep(_: float) -> None:
        await gate.wait()

    hass = _Hass()
    alerts = OfflineAlertManager(hass, sleep_func=_blocked_sleep)
    manager = DeviceManager(alerts)
    await manager.start()
    stale = _client(connected=False)
    fresh = _client(connected=True, dps={"1": True})
    config = {**device_config, **_config(offline_alert_delay_minutes=5)}

    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        side_effect=[stale, fresh],
    ):
        assert not await manager.add_device(config)
        assert await manager.reconnect_device(config["device_id"])
        await asyncio.sleep(0)

    assert hass.services.calls == []
    assert alerts.get_diagnostics(config["device_id"])["offline_since"] is None
    await manager.stop()


def _client(*, connected: bool, dps: dict[str, object] | None = None) -> MagicMock:
    client = MagicMock()
    client.connect = AsyncMock(return_value=connected)
    client.close = AsyncMock()
    client.status_with_fallback = AsyncMock(return_value=dps or {})
    client.receive_nowait = AsyncMock(return_value=None)
    client.set_dp_callback = MagicMock()
    client.set_disconnect_callback = MagicMock()
    client.set_monitored_dp_ids = MagicMock()
    client._cached_dps = {}
    client.cached_dps = client._cached_dps
    client.connected = connected
    client.detected_version = None
    client.protocol_version = "3.3"
    client.last_tx_hex = ""
    client.last_rx_hex = ""
    client.last_failure_reason = "" if connected else "timeout"
    client.last_failure_detail = "" if connected else "timed out"
    client.attempt_failures = []
    client.ip = "192.168.1.20"
    return client
