"""Tests for tuya_protocol.client — TuyaDeviceClient."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.conti.tuya_protocol.base import TuyaCommand
from custom_components.conti.tuya_protocol.client import TuyaDeviceClient
from custom_components.conti.tuya_protocol.crypto import pack_frame


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response_frame(
    cmd: int,
    payload: bytes,
    local_key: bytes,
    seqno: int = 1,
    version: str = "3.3",
) -> bytes:
    """Build a Tuya response frame to feed into the reader."""
    return pack_frame(
        cmd=cmd,
        payload=payload,
        seqno=seqno,
        local_key=local_key,
        version=version,
    )


# ---------------------------------------------------------------------------
# Constructor / properties
# ---------------------------------------------------------------------------


class TestClientInit:
    def test_defaults(self) -> None:
        c = TuyaDeviceClient(
            device_id="d1", ip="10.0.0.1", local_key="abcdef1234567890"
        )
        assert c.device_id == "d1"
        assert c.ip == "10.0.0.1"
        assert not c.connected
        assert c.cached_dps == {}

    def test_ip_setter(self) -> None:
        c = TuyaDeviceClient(
            device_id="d1", ip="10.0.0.1", local_key="abcdef1234567890"
        )
        c.ip = "10.0.0.2"
        assert c.ip == "10.0.0.2"


# ---------------------------------------------------------------------------
# connect() — success & failure
# ---------------------------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_success(self, local_key: str) -> None:
        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        with patch(
            "custom_components.conti.tuya_protocol.client.asyncio.open_connection",
            return_value=(reader, writer),
        ):
            c = TuyaDeviceClient(
                device_id="d1", ip="10.0.0.1", local_key=local_key
            )
            ok = await c.connect()
            assert ok is True
            assert c.connected is True
            await c.close()

    @pytest.mark.asyncio
    async def test_connect_failure_timeout(self, local_key: str) -> None:
        with patch(
            "custom_components.conti.tuya_protocol.client.asyncio.open_connection",
            side_effect=asyncio.TimeoutError,
        ):
            c = TuyaDeviceClient(
                device_id="d1", ip="10.0.0.1", local_key=local_key
            )
            ok = await c.connect()
            assert ok is False
            assert c.connected is False

    @pytest.mark.asyncio
    async def test_connect_failure_os_error(self, local_key: str) -> None:
        with patch(
            "custom_components.conti.tuya_protocol.client.asyncio.open_connection",
            side_effect=OSError("Connection refused"),
        ):
            c = TuyaDeviceClient(
                device_id="d1", ip="10.0.0.1", local_key=local_key
            )
            ok = await c.connect()
            assert ok is False


# ---------------------------------------------------------------------------
# set_dp / set_dps
# ---------------------------------------------------------------------------


class TestSetDP:
    @pytest.mark.asyncio
    async def test_set_dp_while_disconnected(self, local_key: str) -> None:
        c = TuyaDeviceClient(
            device_id="d1", ip="10.0.0.1", local_key=local_key
        )
        result = await c.set_dp(1, True)
        assert result is False

    @pytest.mark.asyncio
    async def test_set_dps_while_disconnected(self, local_key: str) -> None:
        c = TuyaDeviceClient(
            device_id="d1", ip="10.0.0.1", local_key=local_key
        )
        result = await c.set_dps({1: True, 2: 500})
        assert result is False


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_disconnected(self, local_key: str) -> None:
        c = TuyaDeviceClient(
            device_id="d1", ip="10.0.0.1", local_key=local_key
        )
        assert await c.heartbeat() is False


# ---------------------------------------------------------------------------
# _process_data
# ---------------------------------------------------------------------------


class TestProcessData:
    def test_heartbeat_response_no_dps(self, local_key: str, local_key_bytes: bytes) -> None:
        c = TuyaDeviceClient(
            device_id="d1", ip="10.0.0.1", local_key=local_key
        )
        frame = _make_response_frame(
            cmd=TuyaCommand.HEARTBEAT,
            payload=b"",
            local_key=local_key_bytes,
        )
        c._process_data(frame)
        assert c.cached_dps == {}

    def test_status_update_caches_dps(self, local_key: str, local_key_bytes: bytes) -> None:
        c = TuyaDeviceClient(
            device_id="d1", ip="10.0.0.1", local_key=local_key
        )
        dps_payload = json.dumps({"dps": {"1": True, "3": 500}}).encode()
        frame = _make_response_frame(
            cmd=TuyaCommand.STATUS,
            payload=dps_payload,
            local_key=local_key_bytes,
        )
        c._process_data(frame)
        assert c.cached_dps == {"1": True, "3": 500}

    def test_dp_callback_invoked(self, local_key: str, local_key_bytes: bytes) -> None:
        c = TuyaDeviceClient(
            device_id="d1", ip="10.0.0.1", local_key=local_key
        )
        received: list[dict] = []
        c.set_dp_callback(lambda dps: received.append(dps))

        dps_payload = json.dumps({"dps": {"1": False}}).encode()
        frame = _make_response_frame(
            cmd=TuyaCommand.STATUS,
            payload=dps_payload,
            local_key=local_key_bytes,
        )
        c._process_data(frame)
        assert len(received) == 1
        assert received[0] == {"1": False}

    def test_invalid_frame_ignored(self, local_key: str) -> None:
        c = TuyaDeviceClient(
            device_id="d1", ip="10.0.0.1", local_key=local_key
        )
        c._process_data(b"\x00" * 10)
        assert c.cached_dps == {}


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


class TestCallbacks:
    @pytest.mark.asyncio
    async def test_disconnect_callback(self, local_key: str) -> None:
        c = TuyaDeviceClient(
            device_id="d1", ip="10.0.0.1", local_key=local_key
        )
        called = []
        c.set_disconnect_callback(lambda: called.append(True))
        # Simulate disconnect
        c._connected = True
        await c._handle_disconnect()
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_close_idempotent(self, local_key: str) -> None:
        c = TuyaDeviceClient(
            device_id="d1", ip="10.0.0.1", local_key=local_key
        )
        # Should not raise even when never connected
        await c.close()
        await c.close()
