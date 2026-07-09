"""Tests for production reconnect lifecycle and diagnostics."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import yaml

from custom_components.conti import (
    _async_reload_entry,
    _dp_map_source,
    _parse_dp_map,
    _register_reconnect_services,
)
from custom_components.conti.cloud_schema import TuyaCloudSchemaHelper
from custom_components.conti.config_flow import _options_with_manual_dp_map
from custom_components.conti.const import (
    CONF_DP_MAP,
    CONF_MAPPING_SOURCE,
    DEFAULT_ENABLE_AUTO_RECONNECT,
    DOMAIN,
)
from custom_components.conti.device_manager import DeviceManager
from custom_components.conti.diagnostics import async_get_config_entry_diagnostics
from custom_components.conti.tinytuya_client import TinyTuyaDevice


def _client(*, connected: bool, dps: dict[str, object] | None = None) -> MagicMock:
    client = MagicMock()
    client.connect = AsyncMock(return_value=connected)
    client.close = AsyncMock()
    client.status_with_fallback = AsyncMock(return_value=dps or {})
    client.receive_nowait = AsyncMock(return_value=None)
    client.detect_dps = AsyncMock(return_value={})
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
    client.ip = "192.168.20.10"
    return client


@pytest.mark.parametrize("version", ["3.4", "3.5"])
def test_modern_protocol_status_uses_dp_query_new_first(version: str) -> None:
    client = TinyTuyaDevice(
        "device-1", "192.168.20.10", "0123456789abcdef", version
    )
    device = MagicMock()
    device.generate_payload.side_effect = lambda command: command
    device._send_receive.return_value = {"dps": {"1": True}}

    assert client._send_status_query(device, float(version)) == {
        "dps": {"1": True}
    }
    assert device.generate_payload.call_args_list[0] == call(0x10)
    assert call(0x0A) not in device.generate_payload.call_args_list


def test_rejected_legacy_fallback_does_not_override_correct_query_error() -> None:
    client = TinyTuyaDevice(
        "device-1", "192.168.20.10", "0123456789abcdef", "3.4"
    )
    device = MagicMock()
    device.generate_payload.side_effect = lambda command: command
    correct_error = {"Error": "Unexpected Payload", "Err": "904"}
    device._send_receive.side_effect = [
        correct_error,
        {"Error": "Check device key or version", "Err": "914"},
    ]

    assert client._send_status_query(device, 3.4) == correct_error
    assert device.generate_payload.call_args_list == [call(0x10), call(0x0A)]


def test_working_session_914_is_initially_query_rejected() -> None:
    client = TinyTuyaDevice(
        "device-1", "192.168.20.10", "0123456789abcdef", "3.4"
    )
    device = MagicMock()
    device.generate_payload.side_effect = lambda command: command
    device._send_receive.return_value = {
        "Error": "Check device key or version",
        "Err": "914",
    }
    client._device = device
    client._connected = True
    client._protocol_version = "3.4"

    assert client._status_sync() == {}
    assert client.last_failure_reason == "session_or_query_rejected"


@pytest.mark.asyncio
async def test_manual_reconnect_replaces_stale_client(device_config: dict) -> None:
    stale = _client(connected=False)
    fresh = _client(connected=True, dps={"1": True})
    manager = DeviceManager()
    await manager.start()

    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        side_effect=[stale, fresh],
    ) as client_factory:
        assert not await manager.add_device(device_config)
        assert manager._devices[device_config["device_id"]].reconnect_task is None

        assert await manager.reconnect_device(device_config["device_id"])

    assert client_factory.call_args_list[-1] == call(
        device_id=device_config["device_id"],
        ip=device_config["host"],
        local_key=device_config["local_key"],
        version=device_config["protocol_version"],
        port=device_config["port"],
    )
    assert manager.get_client(device_config["device_id"]) is fresh
    stale.close.assert_awaited()
    assert manager.get_cached_dps(device_config["device_id"]) == {"1": True}
    diagnostics = manager.get_device_diagnostics(device_config["device_id"])
    assert diagnostics["control_path"] == "local"
    assert diagnostics["reconnect_attempts"] == 1
    assert diagnostics["last_successful_local_update"] is not None
    await manager.stop()


@pytest.mark.asyncio
async def test_push_disconnect_closes_stale_client_without_immediate_unavailable(
    device_config: dict,
) -> None:
    stale = _client(connected=True, dps={"1": True})
    dropped = asyncio.Event()

    async def _drop_socket() -> None:
        stale.connected = False
        dropped.set()
        return None

    stale.receive_nowait.side_effect = _drop_socket
    manager = DeviceManager()
    await manager.start()
    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        return_value=stale,
    ):
        assert await manager.add_device(device_config)
        await asyncio.wait_for(dropped.wait(), timeout=1)
        for _ in range(10):
            if stale.close.await_count:
                break
            await asyncio.sleep(0)

    stale.close.assert_awaited()
    assert manager.is_online(device_config["device_id"])
    diagnostics = manager.get_device_diagnostics(device_config["device_id"])
    assert diagnostics["local_status"] == "reconnecting"
    assert diagnostics["control_path"] == "local"
    await manager.stop()


@pytest.mark.asyncio
async def test_listener_and_status_query_never_read_concurrently(
    device_config: dict,
) -> None:
    client = _client(connected=True, dps={"1": True})
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    listener_reading = False

    async def _receive() -> None:
        nonlocal listener_reading
        listener_reading = True
        read_started.set()
        await release_read.wait()
        listener_reading = False
        return None

    client.receive_nowait.side_effect = _receive
    manager = DeviceManager()
    await manager.start()
    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        return_value=client,
    ):
        assert await manager.add_device(device_config)
        await asyncio.wait_for(read_started.wait(), timeout=1)

        async def _status(*, timeout: float | None = None) -> dict[str, bool]:
            assert timeout == 2.0
            assert listener_reading is False
            return {"1": True}

        client.status_with_fallback = AsyncMock(side_effect=_status)
        query = asyncio.create_task(manager.query_device(device_config["device_id"]))
        await asyncio.sleep(0)
        client.status_with_fallback.assert_not_awaited()
        release_read.set()
        assert await asyncio.wait_for(query, timeout=1) == {"1": True}

    await manager.stop()


@pytest.mark.asyncio
async def test_valid_dps_after_push_reconnect_restores_availability(
    device_config: dict,
) -> None:
    stale = _client(connected=True, dps={"1": False})
    fresh = _client(connected=True, dps={"1": True})
    manager = DeviceManager()
    await manager.start()
    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        side_effect=[stale, fresh],
    ):
        assert await manager.add_device(device_config)
        manager._on_disconnect(device_config["device_id"], source_client=stale)
        assert manager.is_online(device_config["device_id"])
        assert await manager.reconnect_device(device_config["device_id"])

    assert manager.is_online(device_config["device_id"])
    diagnostics = manager.get_device_diagnostics(device_config["device_id"])
    assert diagnostics["local_status"] == "healthy"
    assert diagnostics["control_path"] == "local"
    assert diagnostics["consecutive_failures"] == 0
    assert manager.get_cached_dps(device_config["device_id"])["1"] is True
    await manager.stop()


@pytest.mark.asyncio
async def test_reconnect_grace_expires_after_three_verified_failures(
    device_config: dict,
) -> None:
    stale = _client(connected=True, dps={"1": True})
    failures = [_client(connected=False) for _ in range(3)]
    manager = DeviceManager()
    await manager.start()
    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        side_effect=[stale, *failures],
    ):
        assert await manager.add_device(device_config)
        manager._on_disconnect(device_config["device_id"], source_client=stale)
        for attempt in range(2):
            assert not await manager.reconnect_device(device_config["device_id"])
            assert manager.is_online(device_config["device_id"]), attempt
        assert not await manager.reconnect_device(device_config["device_id"])

    assert not manager.is_online(device_config["device_id"])
    assert manager.get_device_diagnostics(device_config["device_id"])[
        "consecutive_failures"
    ] == 3
    await manager.stop()


@pytest.mark.asyncio
async def test_reconnect_preserves_saved_manual_four_channel_map(
    device_config: dict,
) -> None:
    manual_map = {
        str(dp_id): {"key": f"switch_{dp_id}", "type": "bool"}
        for dp_id in range(1, 5)
    }
    config = {
        **device_config,
        "dp_map": manual_map,
        "dp_map_source": "manual",
        "discovered_dps": {"1": False},
    }
    original_config = deepcopy(config)
    stale = _client(connected=False)
    fresh = _client(
        connected=True,
        dps={str(dp_id): False for dp_id in range(1, 5)},
    )
    manager = DeviceManager()
    await manager.start()
    cloud = MagicMock()
    cloud.async_get_dps = AsyncMock()

    with (
        patch(
            "custom_components.conti.device_manager.TinyTuyaDevice",
            side_effect=[stale, fresh],
        ),
        patch.object(manager, "detect_dps", AsyncMock()) as detect_dps,
    ):
        await manager.add_device(config)
        manager._devices[config["device_id"]].cloud_fallback = cloud
        assert await manager.reconnect_device(config["device_id"])

    assert manager._devices[config["device_id"]].config == original_config
    assert manager._devices[config["device_id"]].config["dp_map"] == manual_map
    fresh.set_monitored_dp_ids.assert_called_once_with(["1", "2", "3", "4"])
    stale.detect_dps.assert_not_awaited()
    fresh.detect_dps.assert_not_awaited()
    detect_dps.assert_not_awaited()
    cloud.async_get_dps.assert_not_awaited()
    diagnostics = manager.get_device_diagnostics(config["device_id"])
    assert diagnostics["dp_map_source"] == "manual"
    assert diagnostics["monitored_dp_ids"] == ["1", "2", "3", "4"]
    await manager.stop()


@pytest.mark.asyncio
async def test_reconnect_preserves_manual_curtain_map_without_discovery(
    device_config: dict,
) -> None:
    manual_map = {
        "15": {"key": "control", "type": "str", "values": ["open", "stop", "close"]},
        "44": {"key": "position", "type": "int", "min": 0, "max": 100},
        "45": {
            "key": "current_position",
            "type": "int",
            "min": 0,
            "max": 100,
            "read_only": True,
        },
    }
    config = {
        **device_config,
        "device_type": "curtain",
        "dp_map": manual_map,
        "dp_map_source": "manual",
        "discovered_dps": {"15": "stop"},
    }
    stale = _client(connected=False)
    fresh = _client(connected=True, dps={"15": "stop", "45": 50})
    manager = DeviceManager()
    await manager.start()

    with (
        patch(
            "custom_components.conti.device_manager.TinyTuyaDevice",
            side_effect=[stale, fresh],
        ),
        patch.object(manager, "detect_dps", AsyncMock()) as detect_dps,
    ):
        await manager.add_device(config)
        assert await manager.reconnect_device(config["device_id"])

    assert manager._devices[config["device_id"]].config["dp_map"] == manual_map
    fresh.set_monitored_dp_ids.assert_called_once_with(["15", "44", "45"])
    stale.detect_dps.assert_not_awaited()
    fresh.detect_dps.assert_not_awaited()
    detect_dps.assert_not_awaited()
    assert (
        manager.get_device_diagnostics(config["device_id"])["dp_map_source"]
        == "manual"
    )
    await manager.stop()


@pytest.mark.asyncio
async def test_manual_dp_option_persists_reloads_and_becomes_effective() -> None:
    manual_map = {
        str(dp_id): {"key": f"switch_{dp_id}", "type": "bool"}
        for dp_id in range(1, 5)
    }
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.options = {}
    entry.data = {
        "device_id": "device-1",
        "host": "192.168.20.10",
        "local_key": "0123456789abcdef",
        "protocol_version": "3.3",
        "device_type": "switch",
    }
    saved_options = _options_with_manual_dp_map(
        entry.options,
        manual_map,
        verbose_logging=False,
        enable_auto_reconnect=False,
    )
    assert json.loads(saved_options[CONF_DP_MAP]) == manual_map
    assert saved_options[CONF_MAPPING_SOURCE] == "manual"

    entry.options = saved_options
    assert _parse_dp_map(entry) == manual_map
    assert _dp_map_source(entry) == "manual"

    hass = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    await _async_reload_entry(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with("entry-1")

    runtime_config = {
        **entry.data,
        "dp_map": _parse_dp_map(entry),
        "dp_map_source": _dp_map_source(entry),
    }
    stale = _client(connected=False)
    fresh = _client(
        connected=True,
        dps={str(dp_id): False for dp_id in range(1, 5)},
    )
    manager = DeviceManager()
    await manager.start()
    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        side_effect=[stale, fresh],
    ):
        await manager.add_device(runtime_config)
        assert await manager.reconnect_device("device-1")
    fresh.set_monitored_dp_ids.assert_called_once_with(["1", "2", "3", "4"])
    await manager.stop()


@pytest.mark.asyncio
async def test_auto_reconnect_has_one_delayed_task(device_config: dict) -> None:
    config = {**device_config, "enable_auto_reconnect": True}
    manager = DeviceManager()
    await manager.start()

    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        return_value=_client(connected=False),
    ):
        assert not await manager.add_device(config)
        first_task = manager._devices[config["device_id"]].reconnect_task
        manager._schedule_reconnect(config["device_id"])
        assert manager._devices[config["device_id"]].reconnect_task is first_task
        await asyncio.sleep(0)
        diagnostics = manager.get_device_diagnostics(config["device_id"])
        assert diagnostics["reconnect_delay"] == 15.0
        assert diagnostics["next_retry_time"] is not None

    await manager.stop()


@pytest.mark.asyncio
async def test_auto_reconnect_backoff_caps_at_five_minutes(
    device_config: dict,
) -> None:
    manager = DeviceManager()
    await manager.start()
    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        return_value=_client(connected=False),
    ):
        await manager.add_device(device_config)

    managed = manager._devices[device_config["device_id"]]
    managed.config["enable_auto_reconnect"] = True
    delays: list[float] = []

    async def _record_sleep(delay: float) -> None:
        delays.append(delay)

    attempts = AsyncMock(side_effect=[False, False, False, False, False, False, True])
    with (
        patch("custom_components.conti.device_manager.asyncio.sleep", _record_sleep),
        patch.object(manager, "_attempt_reconnect", attempts),
    ):
        await manager._reconnect(device_config["device_id"])

    assert delays == [15.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0]
    await manager.stop()


@pytest.mark.asyncio
async def test_reconnect_all_skips_healthy_and_isolates_errors(
    device_config: dict,
) -> None:
    healthy = _client(connected=True, dps={"1": True})
    offline_one = _client(connected=False)
    offline_two = _client(connected=False)
    manager = DeviceManager()
    await manager.start()
    configs = [
        {**device_config, "device_id": "healthy"},
        {**device_config, "device_id": "offline_one"},
        {**device_config, "device_id": "offline_two"},
    ]

    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        side_effect=[healthy, offline_one, offline_two],
    ):
        for config in configs:
            await manager.add_device(config)

    reconnect = AsyncMock(side_effect=[RuntimeError("boom"), True])
    with patch.object(manager, "reconnect_device", reconnect):
        results = await manager.reconnect_all()

    assert reconnect.await_args_list == [call("offline_one"), call("offline_two")]
    assert results == {"offline_one": False, "offline_two": True}
    await manager.stop()


@pytest.mark.asyncio
async def test_empty_status_does_not_clear_working_cloud_fallback(
    device_config: dict,
) -> None:
    config = {
        **device_config,
        "device_type": "light",
        "dp_map": {
            "20": {"code": "switch_led"},
            "21": {"code": "work_mode"},
            "22": {"code": "bright_value"},
            "23": {"code": "temp_value"},
        },
    }
    stale = _client(connected=False)
    stale.last_failure_reason = "malformed_payload_904"
    stale.last_failure_detail = "Unexpected Payload Err 904"
    fresh = _client(connected=True, dps={})
    cloud = MagicMock()
    cloud.supports_dali_cct_fallback.return_value = True
    cloud.last_online_state = True
    manager = DeviceManager()
    await manager.start()

    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        side_effect=[stale, fresh],
    ):
        await manager.add_device(config)
        manager.seed_cached_dps(config["device_id"], {"20": False})
        assert manager.configure_dali_cloud_fallback(config["device_id"], cloud)
        with patch.object(manager, "_clear_cloud_fallback") as clear_fallback:
            assert not await manager.reconnect_device(config["device_id"])
            clear_fallback.assert_not_called()

    diagnostics = manager.get_device_diagnostics(config["device_id"])
    assert diagnostics["control_path"] == "cloud_fallback"
    assert diagnostics["last_error_class"] == "empty_status"
    assert manager.get_cached_dps(config["device_id"]) == {"20": False}
    await manager.stop()


@pytest.mark.asyncio
async def test_reconnect_all_skips_healthy_cloud_fallback(
    device_config: dict,
) -> None:
    config = {**device_config, "device_type": "light"}
    stale = _client(connected=False)
    stale.last_failure_reason = "malformed_payload_904"
    stale.last_failure_detail = "Unexpected Payload Err 904"
    cloud = MagicMock()
    cloud.supports_dali_cct_fallback.return_value = True
    cloud.last_online_state = True
    manager = DeviceManager()
    await manager.start()

    with patch(
        "custom_components.conti.device_manager.TinyTuyaDevice",
        return_value=stale,
    ):
        await manager.add_device(config)
    manager.seed_cached_dps(config["device_id"], {"20": False})
    assert manager.configure_dali_cloud_fallback(config["device_id"], cloud)

    reconnect = AsyncMock(return_value=True)
    with patch.object(manager, "reconnect_device", reconnect):
        assert await manager.reconnect_all() == {}
    reconnect.assert_not_awaited()
    await manager.stop()


def test_cloud_permission_error_is_preserved() -> None:
    helper = TuyaCloudSchemaHelper("id", "secret", "in")
    helper._record_cloud_error(
        "/v1.0/devices/test/status",
        "28841002",
        "Data center suspended: no permission",
    )
    helper._record_cloud_error(
        "/v1.0/devices/test/status",
        "28841002",
        "Data center suspended: no permission",
    )

    assert helper.get_connection_diagnostics()["cloud_error"] == "cloud_permission_error"


def test_manager_marks_failed_cloud_fallback_unavailable(
    device_config: dict, caplog: pytest.LogCaptureFixture
) -> None:
    manager = DeviceManager()
    managed = MagicMock()
    managed.config = device_config
    managed.cloud_error = ""
    managed.cloud_error_code = ""
    managed.cloud_error_message = ""
    managed.control_path = "cloud_fallback"
    managed.diagnostic_reason = ""
    managed.error_log_times = {}
    manager._devices[device_config["device_id"]] = managed
    runtime = MagicMock()
    runtime.get_connection_diagnostics.return_value = {
        "cloud_error": "cloud_permission_error",
        "cloud_error_code": "28841002",
        "cloud_error_message": "data center suspended",
    }

    assert manager.record_cloud_fallback_diagnostics(
        device_config["device_id"], runtime
    )
    assert manager.record_cloud_fallback_diagnostics(
        device_config["device_id"], runtime
    )
    assert managed.control_path == "unavailable"
    assert managed.diagnostic_reason == "cloud_permission_error"
    messages = [record.message for record in caplog.records]
    assert sum("cloud_permission_error" in message for message in messages) == 1


@pytest.mark.asyncio
async def test_reconnect_services_are_registered_and_refresh_state() -> None:
    manager = MagicMock()
    manager.device_ids.return_value = ["device-1"]
    manager.reconnect_device = AsyncMock(return_value=True)
    manager.reconnect_all = AsyncMock(return_value={"device-1": True})
    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "manager": manager,
            "entry-1": {"device_id": "device-1", "coordinator": coordinator},
        }
    }

    _register_reconnect_services(hass)

    registrations = {
        registration.args[1]: registration
        for registration in hass.services.async_register.call_args_list
    }
    assert {"reconnect_device", "reconnect_all"} <= registrations.keys()

    reconnect_device_handler = registrations["reconnect_device"].args[2]
    await reconnect_device_handler(MagicMock(data={"device_id": "device-1"}))
    manager.reconnect_device.assert_awaited_once_with("device-1")
    coordinator.async_request_refresh.assert_awaited_once()


def test_services_yaml_has_safe_reconnect_schema() -> None:
    services_path = (
        Path(__file__).parents[1] / "custom_components" / "conti" / "services.yaml"
    )
    services = yaml.safe_load(services_path.read_text(encoding="utf-8"))

    assert "reconnect_device" in services
    assert "reconnect_all" in services
    assert set(services["reconnect_device"].get("fields", {})) == {"device_id"}
    assert not services["reconnect_all"].get("fields")


def test_auto_reconnect_default_remains_disabled() -> None:
    assert DEFAULT_ENABLE_AUTO_RECONNECT is False


@pytest.mark.asyncio
async def test_ha_diagnostics_exposes_connection_fields_only() -> None:
    coordinator = MagicMock()
    coordinator.get_diagnostics.return_value = {
        "device_id": "device-1",
        "ha_host_ip": "192.168.4.2",
        "ha_host_subnet": "192.168.4.0/24",
        "device_ip": "192.168.20.10",
        "control_path": "unavailable",
        "last_local_error": "timeout",
        "local_key": "must-not-leak",
    }
    entry = MagicMock(entry_id="entry-1")
    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "entry-1": {"device_id": "device-1", "coordinator": coordinator}
        }
    }

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["ha_host_subnet"] == "192.168.4.0/24"
    assert diagnostics["device_ip"] == "192.168.20.10"
    assert "local_key" not in diagnostics
