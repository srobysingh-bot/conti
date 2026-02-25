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
