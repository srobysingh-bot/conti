"""Tests for DeviceManager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.conti.device_manager import DeviceManager


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self) -> None:
        mgr = DeviceManager()
        await mgr.start()
        assert mgr._running is True
        await mgr.stop()
        assert mgr._running is False
        assert mgr._devices == {}


# ---------------------------------------------------------------------------
# add_device / remove_device
# ---------------------------------------------------------------------------


class TestAddRemove:
    @pytest.mark.asyncio
    async def test_add_device_connects(self, device_config: dict) -> None:
        """When the TCP client connects successfully, the device is online."""
        mgr = DeviceManager()
        await mgr.start()

        with patch(
            "custom_components.conti.device_manager.TinyTuyaDevice"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock(return_value=True)
            instance.close = AsyncMock()
            instance.status_with_fallback = AsyncMock(return_value={"1": True})
            instance.set_dp_callback = MagicMock()
            instance.set_disconnect_callback = MagicMock()
            instance.cached_dps = {}
            instance.detected_version = None
            instance.protocol_version = "3.3"
            instance.last_tx_hex = ""
            instance.last_rx_hex = ""
            instance.ip = device_config["host"]
            instance.connected = True

            ok = await mgr.add_device(device_config)
            assert ok is True
            assert mgr.is_online(device_config["device_id"])
            assert device_config["device_id"] in mgr.device_ids()

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_add_device_fails(self, device_config: dict) -> None:
        mgr = DeviceManager()
        await mgr.start()

        with patch(
            "custom_components.conti.device_manager.TinyTuyaDevice"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock(return_value=False)
            instance.close = AsyncMock()
            instance.set_dp_callback = MagicMock()
            instance.set_disconnect_callback = MagicMock()
            instance.cached_dps = {}
            instance.ip = device_config["host"]

            ok = await mgr.add_device(device_config)
            assert ok is False
            assert not mgr.is_online(device_config["device_id"])

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_add_duplicate_skips(self, device_config: dict) -> None:
        mgr = DeviceManager()
        await mgr.start()

        with patch(
            "custom_components.conti.device_manager.TinyTuyaDevice"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock(return_value=True)
            instance.close = AsyncMock()
            instance.status_with_fallback = AsyncMock(return_value={"1": True})
            instance.set_dp_callback = MagicMock()
            instance.set_disconnect_callback = MagicMock()
            instance.cached_dps = {}
            instance.detected_version = None
            instance.protocol_version = "3.3"
            instance.last_tx_hex = ""
            instance.last_rx_hex = ""
            instance.ip = device_config["host"]
            instance.connected = True

            await mgr.add_device(device_config)
            # Adding same device_id again should skip
            result = await mgr.add_device(device_config)
            assert result is True  # returns existing online status

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_remove_device(self, device_config: dict) -> None:
        mgr = DeviceManager()
        await mgr.start()

        with patch(
            "custom_components.conti.device_manager.TinyTuyaDevice"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock(return_value=True)
            instance.close = AsyncMock()
            instance.status_with_fallback = AsyncMock(return_value={"1": True})
            instance.set_dp_callback = MagicMock()
            instance.set_disconnect_callback = MagicMock()
            instance.cached_dps = {}
            instance.detected_version = None
            instance.protocol_version = "3.3"
            instance.last_tx_hex = ""
            instance.last_rx_hex = ""
            instance.ip = device_config["host"]
            instance.connected = True

            await mgr.add_device(device_config)
            await mgr.remove_device(device_config["device_id"])
            assert device_config["device_id"] not in mgr.device_ids()

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self) -> None:
        mgr = DeviceManager()
        await mgr.start()
        await mgr.remove_device("no_such_device")  # should not raise
        await mgr.stop()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


class TestCommands:
    @pytest.mark.asyncio
    async def test_set_dp_offline(self, device_config: dict) -> None:
        mgr = DeviceManager()
        await mgr.start()
        # No device added → returns False
        result = await mgr.set_dp(device_config["device_id"], 1, True)
        assert result is False
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_set_dps_offline(self, device_config: dict) -> None:
        mgr = DeviceManager()
        await mgr.start()
        result = await mgr.set_dps(device_config["device_id"], {1: True})
        assert result is False
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_query_device_offline(self, device_config: dict) -> None:
        mgr = DeviceManager()
        await mgr.start()
        result = await mgr.query_device(device_config["device_id"])
        assert result == {}
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_get_cached_dps_unknown(self) -> None:
        mgr = DeviceManager()
        assert mgr.get_cached_dps("unknown") == {}

    @pytest.mark.asyncio
    async def test_is_online_unknown(self) -> None:
        mgr = DeviceManager()
        assert mgr.is_online("unknown") is False

    @pytest.mark.asyncio
    async def test_deferred_dali_cache_keeps_entity_available(
        self, device_config: dict
    ) -> None:
        config = dict(device_config)
        config["protocol_version"] = "3.4"
        config["deferred_local_connect"] = True
        config["device_type"] = "light"
        mgr = DeviceManager()
        await mgr.start()

        with patch(
            "custom_components.conti.device_manager.TinyTuyaDevice"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock(return_value=False)
            instance.close = AsyncMock()
            instance.set_dp_callback = MagicMock()
            instance.set_disconnect_callback = MagicMock()
            instance.cached_dps = {}
            instance._cached_dps = {}
            instance.last_failure_reason = "malformed_payload_904"
            instance.last_failure_detail = "Unexpected Payload from Device"
            instance.attempt_failures = []
            instance.ip = config["host"]

            assert await mgr.add_device(config) is False
            mgr.seed_cached_dps(
                config["device_id"],
                {"20": False, "21": "white", "22": 500, "23": 500},
            )
            instance.cached_dps = dict(instance._cached_dps)
            cloud = MagicMock()
            cloud.supports_dali_cct_fallback.return_value = True
            cloud.async_get_dps = AsyncMock(return_value=instance.cached_dps)
            assert mgr.configure_dali_cloud_fallback(
                config["device_id"], cloud
            )

            assert mgr.is_online(config["device_id"]) is True
            assert mgr.get_cached_dps(config["device_id"])["20"] is False
            diagnostics = mgr.get_device_diagnostics(config["device_id"])
            assert diagnostics["local_status_available"] is False
            assert (
                diagnostics["local_status_reason"]
                == "malformed_payload_904"
            )
            assert diagnostics["local_status"] == "failed_904"
            assert diagnostics["control_path"] == "cloud_fallback"
            assert (
                diagnostics["diagnostic_reason"]
                == "local_payload_904_cloud_fallback"
            )

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_deferred_dali_command_allowed_with_cloud_cache(
        self, device_config: dict
    ) -> None:
        config = dict(device_config)
        config["protocol_version"] = "3.4"
        config["deferred_local_connect"] = True
        config["device_type"] = "light"
        mgr = DeviceManager()
        await mgr.start()

        with patch(
            "custom_components.conti.device_manager.TinyTuyaDevice"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock(return_value=False)
            instance.close = AsyncMock()
            instance.set_dp_callback = MagicMock()
            instance.set_disconnect_callback = MagicMock()
            instance.cached_dps = {"20": False}
            instance._cached_dps = {"20": False}
            instance.last_failure_reason = "malformed_payload_904"
            instance.last_failure_detail = "Unexpected Payload from Device"
            instance.attempt_failures = []
            instance.protocol_version = "3.4"
            instance.ip = config["host"]

            assert await mgr.add_device(config) is False
            cloud = MagicMock()
            cloud.supports_dali_cct_fallback.return_value = True
            cloud.async_set_dp = AsyncMock(return_value=True)
            cloud.async_get_dps = AsyncMock(return_value={"20": True})
            assert mgr.configure_dali_cloud_fallback(
                config["device_id"], cloud
            )
            assert await mgr.set_dp(config["device_id"], 20, True) is True
            cloud.async_set_dp.assert_awaited_once_with(20, True)
            assert instance._cached_dps["20"] is True

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_non_cct_light_does_not_activate_cloud_fallback(
        self, device_config: dict
    ) -> None:
        config = dict(device_config)
        config.update(
            {
                "protocol_version": "3.4",
                "deferred_local_connect": True,
                "device_type": "light",
            }
        )
        mgr = DeviceManager()
        await mgr.start()

        with patch(
            "custom_components.conti.device_manager.TinyTuyaDevice"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock(return_value=False)
            instance.close = AsyncMock()
            instance.set_dp_callback = MagicMock()
            instance.set_disconnect_callback = MagicMock()
            instance.cached_dps = {"20": False}
            instance.last_failure_reason = "malformed_payload_904"
            instance.last_failure_detail = "Unexpected Payload"
            instance.attempt_failures = []
            instance.ip = config["host"]

            await mgr.add_device(config)
            cloud = MagicMock()
            cloud.supports_dali_cct_fallback.return_value = False
            assert not mgr.configure_dali_cloud_fallback(
                config["device_id"], cloud
            )
            assert (
                mgr.get_device_diagnostics(config["device_id"])[
                    "control_path"
                ]
                == "local"
            )

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_local_recovery_disables_cloud_fallback(
        self, device_config: dict
    ) -> None:
        config = dict(device_config)
        config.update(
            {
                "protocol_version": "3.4",
                "deferred_local_connect": True,
                "device_type": "light",
            }
        )
        mgr = DeviceManager()
        await mgr.start()

        with patch(
            "custom_components.conti.device_manager.TinyTuyaDevice"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock(return_value=False)
            instance.close = AsyncMock()
            instance.set_dp_callback = MagicMock()
            instance.set_disconnect_callback = MagicMock()
            instance.cached_dps = {"20": False}
            instance.last_failure_reason = "malformed_payload_904"
            instance.last_failure_detail = "Unexpected Payload"
            instance.attempt_failures = []
            instance.ip = config["host"]

            await mgr.add_device(config)
            cloud = MagicMock()
            cloud.supports_dali_cct_fallback.return_value = True
            assert mgr.configure_dali_cloud_fallback(
                config["device_id"], cloud
            )

            managed = mgr._devices[config["device_id"]]
            managed.reconnect_task.cancel()
            managed.reconnect_task = None
            instance.connect = AsyncMock(return_value=True)
            instance.status_with_fallback = AsyncMock(
                return_value={"20": True}
            )
            instance.connected = True
            instance.detected_version = None
            instance.protocol_version = "3.4"
            instance.last_tx_hex = ""
            instance.last_rx_hex = ""

            assert await mgr.query_device(config["device_id"]) == {"20": True}
            diagnostics = mgr.get_device_diagnostics(config["device_id"])
            assert diagnostics["control_path"] == "local"
            assert diagnostics["local_status"] == "healthy"
            assert diagnostics["diagnostic_reason"] == ""

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_grouped_light_command_uses_cloud_fallback(
        self, device_config: dict
    ) -> None:
        config = dict(device_config)
        config.update(
            {
                "protocol_version": "3.4",
                "deferred_local_connect": True,
                "device_type": "light",
            }
        )
        mgr = DeviceManager()
        await mgr.start()

        with patch(
            "custom_components.conti.device_manager.TinyTuyaDevice"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock(return_value=False)
            instance.close = AsyncMock()
            instance.set_dp_callback = MagicMock()
            instance.set_disconnect_callback = MagicMock()
            instance.cached_dps = {"20": False, "22": 500, "23": 500}
            instance._cached_dps = dict(instance.cached_dps)
            instance.last_failure_reason = "malformed_payload_904"
            instance.last_failure_detail = "Unexpected Payload"
            instance.attempt_failures = []
            instance.ip = config["host"]

            await mgr.add_device(config)
            cloud = MagicMock()
            cloud.supports_dali_cct_fallback.return_value = True
            cloud.async_set_dp = AsyncMock(return_value=True)
            cloud.async_get_dps = AsyncMock(return_value={})
            mgr.configure_dali_cloud_fallback(config["device_id"], cloud)

            assert await mgr.set_dps(
                config["device_id"], {20: True, 22: 700, 23: 300}
            )
            assert cloud.async_set_dp.await_count == 3

        await mgr.stop()


# ---------------------------------------------------------------------------
# State callback
# ---------------------------------------------------------------------------


class TestStateCallback:
    @pytest.mark.asyncio
    async def test_state_callback_wired(self, device_config: dict) -> None:
        """When device pushes DPs, the per-device state callback fires."""
        mgr = DeviceManager()
        await mgr.start()

        received: list[tuple[str, dict]] = []
        device_id = device_config["device_id"]

        with patch(
            "custom_components.conti.device_manager.TinyTuyaDevice"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock(return_value=True)
            instance.close = AsyncMock()
            instance.status_with_fallback = AsyncMock(return_value={"1": True})
            instance.set_dp_callback = MagicMock()
            instance.set_disconnect_callback = MagicMock()
            instance.cached_dps = {}
            instance.detected_version = None
            instance.protocol_version = "3.3"
            instance.last_tx_hex = ""
            instance.last_rx_hex = ""
            instance.ip = device_config["host"]
            instance.connected = True

            await mgr.add_device(device_config)

            # Register per-device callback (replaces old set_state_callback)
            mgr.register_state_callback(
                device_id, lambda did, dps: received.append((did, dps))
            )

            # Simulate DP push via the captured callback
            dp_callback = instance.set_dp_callback.call_args[0][0]
            dp_callback({"1": True, "3": 500})

            assert len(received) == 1
            assert received[0][0] == device_id
            assert received[0][1] == {"1": True, "3": 500}

        await mgr.stop()
