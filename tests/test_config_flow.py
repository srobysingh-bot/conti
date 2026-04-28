"""Tests for config_flow — protocol selection, auto-detect, error classification."""

from __future__ import annotations

import asyncio
import errno
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.conti.config_flow import ContiConfigFlow, _test_device


# ---------------------------------------------------------------------------
# _test_device — TCP unreachable
# ---------------------------------------------------------------------------


class TestDeviceTCPFail:
    @pytest.mark.asyncio
    async def test_tcp_timeout_returns_device_not_responding(self) -> None:
        with patch(
            "custom_components.conti.config_flow.asyncio.open_connection",
            side_effect=asyncio.TimeoutError,
        ):
            ok, ver, dps, err = await _test_device(
                "dev1", "10.0.0.1", "0123456789abcdef", "3.3", 6668
            )
        assert ok is False
        from custom_components.conti.config_flow import ERR_DEVICE_NOT_RESPONDING

        assert err == ERR_DEVICE_NOT_RESPONDING

    @pytest.mark.asyncio
    async def test_tcp_refused_returns_port_blocked(self) -> None:
        with patch(
            "custom_components.conti.config_flow.asyncio.open_connection",
            side_effect=OSError(errno.ECONNREFUSED, "Connection refused"),
        ):
            ok, ver, dps, err = await _test_device(
                "dev1", "10.0.0.1", "0123456789abcdef", "3.4", 6668
            )
        assert ok is False
        from custom_components.conti.config_flow import ERR_PORT_BLOCKED_LOCAL

        assert err == ERR_PORT_BLOCKED_LOCAL

    @pytest.mark.asyncio
    async def test_tcp_host_unreachable_classified(self) -> None:
        with patch(
            "custom_components.conti.config_flow.asyncio.open_connection",
            side_effect=OSError(errno.EHOSTUNREACH, "No route to host"),
        ):
            ok, ver, dps, err = await _test_device(
                "dev1", "10.0.0.1", "0123456789abcdef", "3.4", 6668
            )
        assert ok is False
        from custom_components.conti.config_flow import ERR_DEVICE_UNREACHABLE_NETWORK

        assert err == ERR_DEVICE_UNREACHABLE_NETWORK


# ---------------------------------------------------------------------------
# _test_device — TCP ok, protocol fail
# ---------------------------------------------------------------------------


class TestDeviceProtocolFail:
    @pytest.mark.asyncio
    async def test_auto_all_versions_fail_returns_wrong_protocol(self) -> None:
        """If TCP works but all protocol versions fail → wrong_protocol."""
        mock_writer = MagicMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "custom_components.conti.config_flow.asyncio.open_connection",
            return_value=(AsyncMock(), mock_writer),
        ):
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(return_value=False)
            mock_client.close = AsyncMock()
            mock_client.detected_version = None
            mock_client.protocol_version = "3.3"

            with patch(
                "custom_components.conti.tinytuya_client.TinyTuyaDevice",
                return_value=mock_client,
            ):
                ok, ver, dps, err = await _test_device(
                    "dev1", "10.0.0.1", "0123456789abcdef", "auto", 6668
                )

        assert ok is False
        assert err == "wrong_protocol"

    @pytest.mark.asyncio
    async def test_explicit_version_fail_returns_invalid_auth(self) -> None:
        """If user chose explicit version and connect fails → invalid_auth."""
        mock_writer = MagicMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "custom_components.conti.config_flow.asyncio.open_connection",
            return_value=(AsyncMock(), mock_writer),
        ):
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(return_value=False)
            mock_client.close = AsyncMock()

            with patch(
                "custom_components.conti.tinytuya_client.TinyTuyaDevice",
                return_value=mock_client,
            ):
                ok, ver, dps, err = await _test_device(
                    "dev1", "10.0.0.1", "0123456789abcdef", "3.4", 6668
                )

        assert ok is False
        assert err == "invalid_auth"


# ---------------------------------------------------------------------------
# _test_device — success
# ---------------------------------------------------------------------------


class TestDeviceSuccess:
    @pytest.mark.asyncio
    async def test_connect_success_returns_detected_version(self) -> None:
        mock_writer = MagicMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "custom_components.conti.config_flow.asyncio.open_connection",
            return_value=(AsyncMock(), mock_writer),
        ):
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(return_value=True)
            mock_client.close = AsyncMock()
            mock_client.detected_version = "3.4"
            mock_client.protocol_version = "3.4"
            mock_client.detect_dps = AsyncMock(return_value={"1": True, "2": 500})

            with patch(
                "custom_components.conti.tinytuya_client.TinyTuyaDevice",
                return_value=mock_client,
            ):
                ok, ver, dps, err = await _test_device(
                    "dev1", "10.0.0.1", "0123456789abcdef", "auto", 6668
                )

        assert ok is True
        assert ver == "3.4"
        assert dps == {"1": True, "2": 500}
        assert err == ""


# ---------------------------------------------------------------------------
# Auto-detect order
# ---------------------------------------------------------------------------


class TestAutoDetectOrder:
    def test_auto_detect_order_constant(self) -> None:
        """Auto mode should try 3.3 → 3.4 → 3.5 → 3.1."""
        from custom_components.conti.const import AUTO_DETECT_ORDER

        assert AUTO_DETECT_ORDER == ["3.3", "3.4", "3.5", "3.1"]

    def test_versions_are_valid_floats(self) -> None:
        """All auto-detect versions must be convertible to float for tinytuya."""
        from custom_components.conti.const import AUTO_DETECT_ORDER

        for v in AUTO_DETECT_ORDER:
            fv = float(v)
            assert fv > 3.0
