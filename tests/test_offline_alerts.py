"""Focused tests for delayed, non-spamming Conti offline alerts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.conti import (
    _GLOBAL_ALERT_CONFIG_KEY,
    _MANAGER_KEY,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.conti.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_GLOBAL_SETTINGS,
)
from custom_components.conti.device_manager import DeviceManager
from custom_components.conti.diagnostics import async_get_config_entry_diagnostics
from custom_components.conti.offline_alerts import (
    OfflineAlertConfig,
    OfflineAlertManager,
    map_offline_reason,
    offline_alert_config_from_mapping,
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
async def test_one_global_config_applies_to_multiple_devices() -> None:
    hass = _Hass()
    manager = OfflineAlertManager(
        hass,
        config=_config(
            developer_alert_email="global@example.com",
            offline_alert_delay_minutes=5,
        ),
        sleep_func=_immediate_sleep,
    )
    manager.configure_device(
        "device-1",
        _config(
            friendly_name="Kitchen Plug",
            developer_alert_email="legacy-one@example.com",
        ),
    )
    manager.configure_device(
        "device-2",
        _config(
            friendly_name="Office Plug",
            developer_alert_email="legacy-two@example.com",
        ),
    )

    manager.device_offline("device-1", _diagnostics())
    manager.device_offline("device-2", _diagnostics())
    await asyncio.gather(
        manager._states["device-1"].alert_task,
        manager._states["device-2"].alert_task,
    )

    assert len(hass.services.calls) == 2
    assert {call[2]["target"][0] for call in hass.services.calls} == {
        "global@example.com"
    }
    assert {call[2]["title"] for call in hass.services.calls} == {
        "Conti Alert: Device offline - Kitchen Plug",
        "Conti Alert: Device offline - Office Plug",
    }


@pytest.mark.asyncio
async def test_device_alert_override_does_not_affect_other_devices() -> None:
    hass = _Hass()
    manager = OfflineAlertManager(
        hass,
        config=_config(offline_alert_delay_minutes=5),
        sleep_func=_immediate_sleep,
    )
    manager.configure_device(
        "disabled",
        _config(disable_offline_alerts_for_this_device=True),
    )
    manager.configure_device("enabled", _config(friendly_name="Enabled Device"))

    manager.device_offline("disabled", _diagnostics())
    manager.device_offline("enabled", _diagnostics())
    await _wait_for_alert(manager, "enabled")

    assert len(hass.services.calls) == 1
    assert hass.services.calls[0][2]["title"].endswith("Enabled Device")
    disabled = manager.get_diagnostics("disabled")
    assert disabled["alerts_disabled_for_this_device"] is True
    assert disabled["offline_since"] is None


@pytest.mark.asyncio
async def test_disabled_global_config_takes_precedence_over_legacy_device_options() -> None:
    hass = _Hass()
    manager = OfflineAlertManager(
        hass,
        config=OfflineAlertConfig(enabled=False),
        sleep_func=_immediate_sleep,
    )
    manager.configure_device("device-1", _config())
    manager.device_offline("device-1", _diagnostics())
    await asyncio.sleep(0)

    assert hass.services.calls == []
    diagnostics = manager.get_diagnostics("device-1")
    assert diagnostics["using_global_alert_settings"] is True
    assert diagnostics["offline_alert_enabled"] is False


@pytest.mark.asyncio
async def test_legacy_per_device_alert_options_work_without_global_config() -> None:
    hass = _Hass()
    manager = OfflineAlertManager(hass, sleep_func=_immediate_sleep)
    manager.configure_device("device-1", _config())
    manager.device_offline("device-1", _diagnostics())
    await _wait_for_alert(manager, "device-1")

    assert hass.services.calls[0][2]["target"] == ["developer@example.com"]
    assert manager.get_diagnostics("device-1")["using_global_alert_settings"] is False


def test_global_alert_defaults_are_safe_and_redacted() -> None:
    config = offline_alert_config_from_mapping({})
    assert config.enabled is False
    assert config.delay_minutes == 5

    manager = OfflineAlertManager(
        _Hass(),
        config=_config(developer_alert_email="private@example.com"),
    )
    diagnostics = manager.get_global_diagnostics()
    assert diagnostics == {
        "global_offline_alerts_enabled": True,
        "global_offline_alert_delay_minutes": 1.0,
        "global_notify_service": "notify.email",
        "global_recovery_email_enabled": False,
        "developer_alert_email": "**REDACTED**",
    }
    assert "private@example.com" not in str(diagnostics)


@pytest.mark.asyncio
async def test_global_entry_diagnostics_redact_developer_email() -> None:
    entry = MagicMock()
    entry.data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_GLOBAL_SETTINGS,
        **_config(developer_alert_email="private@example.com"),
    }
    entry.options = {}

    diagnostics = await async_get_config_entry_diagnostics(MagicMock(), entry)

    assert diagnostics["global_offline_alerts_enabled"] is True
    assert diagnostics["global_offline_alert_delay_minutes"] == 1
    assert diagnostics["global_notify_service"] == "notify.email"
    assert diagnostics["global_recovery_email_enabled"] is False
    assert diagnostics["developer_alert_email"] == "**REDACTED**"
    assert "private@example.com" not in str(diagnostics)


@pytest.mark.asyncio
async def test_global_entry_restart_setup_never_enters_device_runtime() -> None:
    entry = MagicMock()
    entry.entry_id = "conti-global"
    entry.data = {CONF_ENTRY_TYPE: ENTRY_TYPE_GLOBAL_SETTINGS}
    entry.options = {
        "enable_developer_offline_alerts": True,
        "developer_alert_email": "developer@example.com",
        "developer_alert_notify_service": "notify.email",
        "offline_alert_delay_minutes": 5,
        "send_recovery_email": False,
    }

    with (
        patch(
            "custom_components.conti.device_manager.DeviceManager.add_device",
            new_callable=AsyncMock,
        ) as add_device,
        patch("custom_components.conti.device_manager.TinyTuyaDevice") as tiny_client,
    ):
        # Two fresh hass objects model initial setup followed by a restart.
        for _ in range(2):
            hass = MagicMock()
            hass.data = {}
            hass.config_entries.async_forward_entry_setups = AsyncMock()
            hass.config_entries.async_unload_platforms = AsyncMock()

            assert await async_setup_entry(hass, entry)
            assert isinstance(
                hass.data[DOMAIN][_GLOBAL_ALERT_CONFIG_KEY],
                OfflineAlertConfig,
            )
            assert _MANAGER_KEY not in hass.data[DOMAIN]
            hass.config_entries.async_forward_entry_setups.assert_not_awaited()

            assert await async_unload_entry(hass, entry)
            hass.config_entries.async_unload_platforms.assert_not_awaited()

        add_device.assert_not_awaited()
        tiny_client.assert_not_called()


@pytest.mark.asyncio
async def test_global_entry_updates_existing_manager_shared_config_only() -> None:
    shared_manager = MagicMock()
    entry = MagicMock()
    entry.entry_id = "conti-global"
    entry.data = {CONF_ENTRY_TYPE: ENTRY_TYPE_GLOBAL_SETTINGS}
    entry.options = {
        "enable_developer_offline_alerts": True,
        "developer_alert_email": "developer@example.com",
    }
    hass = MagicMock()
    hass.data = {DOMAIN: {_MANAGER_KEY: shared_manager}}

    assert await async_setup_entry(hass, entry)

    config = shared_manager.set_offline_alert_config.call_args.args[0]
    assert isinstance(config, OfflineAlertConfig)
    assert config.enabled is True
    assert config.email == "developer@example.com"
    assert not hasattr(config, "host")
    assert not hasattr(config, "local_key")


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
