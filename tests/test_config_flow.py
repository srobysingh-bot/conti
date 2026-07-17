"""Tests for config_flow — protocol selection, auto-detect, error classification."""

from __future__ import annotations

import asyncio
import errno
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from custom_components.conti.config_flow import (
    _device_type_choices,
    _device_type_from_cloud_category,
    _global_offline_alert_schema,
    _is_dali_cct_fallback,
    _test_device,
    _user_schema,
)
from custom_components.conti.const import (
    CONF_DEVELOPER_ALERT_EMAIL,
    CONF_DEVELOPER_ALERT_NOTIFY_SERVICE,
    CONF_DEVICE_TYPE,
    CONF_ENABLE_DEVELOPER_OFFLINE_ALERTS,
    CONF_OFFLINE_ALERT_DELAY_MINUTES,
    CONF_SEND_RECOVERY_EMAIL,
    DEFAULT_ENABLE_DEVELOPER_OFFLINE_ALERTS,
    DEFAULT_OFFLINE_ALERT_DELAY_MINUTES,
    DEVICE_TYPE_CURTAIN,
)


def test_global_onboarding_selection_has_no_device_fields() -> None:
    values = _user_schema()({"onboarding_mode": "global_settings"})

    assert values == {"onboarding_mode": "global_settings"}
    assert CONF_DEVICE_TYPE not in values
    for device_field in ("name", "host", "port", "local_key", "protocol_version", "dp_map"):
        assert device_field not in values


def test_global_alert_options_schema_has_safe_defaults() -> None:
    schema = _global_offline_alert_schema()
    values = schema({})

    assert values[CONF_ENABLE_DEVELOPER_OFFLINE_ALERTS] is False
    assert values[CONF_OFFLINE_ALERT_DELAY_MINUTES] == 5
    assert values[CONF_DEVELOPER_ALERT_EMAIL] == ""
    assert values[CONF_DEVELOPER_ALERT_NOTIFY_SERVICE] == "notify.email"
    assert values[CONF_SEND_RECOVERY_EMAIL] is False
    assert DEFAULT_ENABLE_DEVELOPER_OFFLINE_ALERTS is False
    assert DEFAULT_OFFLINE_ALERT_DELAY_MINUTES == 5


@pytest.mark.parametrize("delay", [0, 61])
def test_global_alert_delay_schema_rejects_out_of_range(delay: int) -> None:
    with pytest.raises(vol.Invalid):
        _global_offline_alert_schema()(
            {CONF_OFFLINE_ALERT_DELAY_MINUTES: delay}
        )


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
    async def test_auto_no_response_does_not_return_wrong_protocol(self) -> None:
        """An open TCP port without a response is not a protocol mismatch."""
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
            mock_client.last_failure_reason = "no_response"
            mock_client.last_failure_detail = "status returned None"
            mock_client.confirmed_protocol_mismatch = False
            mock_client.attempt_failures = [
                {"protocol": "3.3", "reason": "no_response"}
            ]

            with patch(
                "custom_components.conti.tinytuya_client.TinyTuyaDevice",
                return_value=mock_client,
            ):
                ok, ver, dps, err = await _test_device(
                    "dev1", "10.0.0.1", "0123456789abcdef", "auto", 6668
                )

        assert ok is False
        assert err == "no_response"

    @pytest.mark.asyncio
    async def test_confirmed_mismatch_returns_wrong_protocol(self) -> None:
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
            mock_client.last_failure_reason = "protocol_mismatch"
            mock_client.last_failure_detail = "unsupported protocol version"
            mock_client.confirmed_protocol_mismatch = True
            mock_client.attempt_failures = [
                {"protocol": "3.4", "reason": "protocol_mismatch"}
            ]

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
    async def test_err_904_is_not_wrong_protocol(self) -> None:
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
            mock_client.last_failure_reason = "malformed_payload_904"
            mock_client.last_failure_detail = (
                "{'Error': 'Unexpected Payload from Device', 'Err': '904'}"
            )
            mock_client.confirmed_protocol_mismatch = False
            mock_client.attempt_failures = [
                {"protocol": "3.4", "reason": "malformed_payload_904"}
            ]

            with patch(
                "custom_components.conti.tinytuya_client.TinyTuyaDevice",
                return_value=mock_client,
            ):
                ok, ver, dps, err = await _test_device(
                    "dev1", "10.0.0.1", "0123456789abcdef", "3.4", 6668
                )

        assert ok is False
        assert err == "malformed_payload_904"

    @pytest.mark.asyncio
    async def test_err_914_is_not_wrong_protocol(self) -> None:
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
            mock_client.last_failure_reason = "local_key_or_version_914"
            mock_client.last_failure_detail = (
                "{'Error': 'Check device key or version', 'Err': '914'}"
            )
            mock_client.confirmed_protocol_mismatch = False
            mock_client.attempt_failures = [
                {"protocol": "3.4", "reason": "local_key_or_version_914"}
            ]

            with patch(
                "custom_components.conti.tinytuya_client.TinyTuyaDevice",
                return_value=mock_client,
            ):
                ok, ver, dps, err = await _test_device(
                    "dev1", "10.0.0.1", "0123456789abcdef", "3.4", 6668
                )

        assert ok is False
        assert err == "local_key_or_version_914"

    @pytest.mark.asyncio
    async def test_earlier_904_attempt_is_not_erased_by_later_no_response(
        self,
    ) -> None:
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
            mock_client.last_failure_reason = "no_response"
            mock_client.last_failure_detail = "status returned None"
            mock_client.confirmed_protocol_mismatch = False
            mock_client.attempt_failures = [
                {
                    "protocol": "3.4",
                    "reason": "malformed_payload_904",
                    "detail": "Err 904",
                },
                {"protocol": "3.1", "reason": "no_response"},
            ]

            with patch(
                "custom_components.conti.tinytuya_client.TinyTuyaDevice",
                return_value=mock_client,
            ):
                ok, ver, dps, err = await _test_device(
                    "dev1", "10.0.0.1", "0123456789abcdef", "auto", 6668
                )

        assert ok is False
        assert err == "malformed_payload_904"

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
            mock_client.last_failure_reason = "invalid_key"
            mock_client.last_failure_detail = "check device key"
            mock_client.confirmed_protocol_mismatch = False
            mock_client.attempt_failures = []

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
            mock_client.initial_status_dps = {"1": True, "2": 500}
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

    @pytest.mark.asyncio
    async def test_v34_empty_status_is_classified(self) -> None:
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
            mock_client.detected_version = None
            mock_client.protocol_version = "3.4"
            mock_client.initial_status_dps = {}

            with patch(
                "custom_components.conti.tinytuya_client.TinyTuyaDevice",
                return_value=mock_client,
            ):
                ok, ver, dps, err = await _test_device(
                    "dev1", "10.0.0.1", "0123456789abcdef", "3.4", 6668
                )

        assert ok is False
        assert ver == "3.4"
        assert dps == {}
        assert err == "empty_status"

    @pytest.mark.asyncio
    async def test_v34_uses_direct_status_without_dp_discovery(self) -> None:
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
            mock_client.detected_version = None
            mock_client.protocol_version = "3.4"
            mock_client.initial_status_dps = {"20": True, "22": 500}
            mock_client.detect_dps = AsyncMock()

            with patch(
                "custom_components.conti.tinytuya_client.TinyTuyaDevice",
                return_value=mock_client,
            ):
                ok, ver, dps, err = await _test_device(
                    "dev1", "10.0.0.1", "0123456789abcdef", "3.4", 6668
                )

        assert ok is True
        assert ver == "3.4"
        assert dps == {"20": True, "22": 500}
        assert err == ""
        mock_client.detect_dps.assert_not_awaited()


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


def test_curtain_appears_in_config_flow_device_type_choices() -> None:
    assert _device_type_choices()["curtain"] == "Curtain"


class TestDaliFallback:
    @staticmethod
    def _cloud_map() -> dict:
        return {
            "20": {"key": "power", "code": "switch_led"},
            "21": {"key": "mode", "code": "work_mode"},
            "22": {"key": "brightness", "code": "bright_value"},
            "23": {"key": "color_temp", "code": "temp_value"},
        }

    def test_cloud_cct_map_with_904_is_candidate(self) -> None:
        assert (
            _is_dali_cct_fallback(
                "light", self._cloud_map(), "malformed_payload_904"
            )
            is True
        )

    def test_cloud_cct_map_with_914_is_candidate(self) -> None:
        assert (
            _is_dali_cct_fallback(
                "light", self._cloud_map(), "local_key_or_version_914"
            )
            is True
        )

    def test_unrelated_local_session_error_is_not_candidate(self) -> None:
        cloud_map = {
            "20": {"key": "power", "code": "switch_led"},
            "21": {"key": "mode", "code": "work_mode"},
            "22": {"key": "brightness", "code": "bright_value"},
            "23": {"key": "color_temp", "code": "temp_value"},
        }

        assert _is_dali_cct_fallback("light", cloud_map, "no_response") is False

    def test_tcp_reachability_error_is_not_candidate(self) -> None:
        assert (
            _is_dali_cct_fallback(
                "light", self._cloud_map(), "device_not_responding"
            )
            is False
        )


class TestCurtainCloudCategory:
    @pytest.mark.parametrize("category", ["cl", "clkg"])
    def test_curtain_categories_select_curtain_device_type(
        self, category: str
    ) -> None:
        assert _device_type_from_cloud_category(category) == DEVICE_TYPE_CURTAIN
