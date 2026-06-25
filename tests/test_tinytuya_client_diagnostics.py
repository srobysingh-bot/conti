"""Tests for TinyTuya connection diagnostics."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.conti.tinytuya_client import TinyTuyaDevice


@pytest.mark.asyncio
async def test_timeout_records_protocol_attempt_details() -> None:
    device = MagicMock()
    device.status.side_effect = TimeoutError("timed out waiting for payload")

    with patch(
        "custom_components.conti.tinytuya_client.tinytuya.Device",
        return_value=device,
    ):
        client = TinyTuyaDevice(
            "dali1", "192.168.1.20", "0123456789abcdef", "3.4"
        )
        assert await client.connect() is False

    assert client.last_failure_reason == "timeout"
    assert client.attempt_failures == [
        {
            "ip": "192.168.1.20",
            "protocol": "3.4",
            "command": "status/DP_QUERY(10)",
            "reason": "timeout",
            "detail": "TimeoutError('timed out waiting for payload')",
            "confirmed_protocol_mismatch": False,
        }
    ]


@pytest.mark.asyncio
async def test_empty_dps_is_connected_empty_status() -> None:
    device = MagicMock()
    device.status.return_value = {"dps": {}}

    with patch(
        "custom_components.conti.tinytuya_client.tinytuya.Device",
        return_value=device,
    ):
        client = TinyTuyaDevice(
            "dali1", "192.168.1.20", "0123456789abcdef", "3.4"
        )
        assert await client.connect() is True

    assert client.connected is True
    assert client.initial_status_dps == {}
    assert client.last_failure_reason == "empty_status"


def test_protocol_mismatch_requires_explicit_mismatch_text() -> None:
    assert (
        TinyTuyaDevice._classify_status_failure(
            result={"Error": "unsupported protocol version"}
        )
        == "protocol_mismatch"
    )
    assert (
        TinyTuyaDevice._classify_status_failure(
            result={"Error": "no response from device"}
        )
        == "no_response"
    )


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError("timed out"), "timeout"),
        (ConnectionRefusedError("refused"), "connection_refused"),
        (ValueError("decrypt failed"), "decrypt_error"),
        (ValueError("invalid key"), "invalid_key"),
        (EOFError("no response"), "no_response"),
    ],
)
def test_exception_failure_classification(
    failure: Exception, expected: str
) -> None:
    assert TinyTuyaDevice._classify_status_failure(exc=failure) == expected


def test_empty_payload_classification() -> None:
    assert (
        TinyTuyaDevice._classify_status_failure(
            result={"Error": "network error", "Payload": None}
        )
        == "empty_payload"
    )


@pytest.mark.asyncio
async def test_protocol_exception_is_not_confirmed_device_response() -> None:
    device = MagicMock()
    device.status.side_effect = RuntimeError("protocol mismatch")

    with patch(
        "custom_components.conti.tinytuya_client.tinytuya.Device",
        return_value=device,
    ):
        client = TinyTuyaDevice(
            "dali1", "192.168.1.20", "0123456789abcdef", "3.4"
        )
        assert await client.connect() is False

    assert client.last_failure_reason == "protocol_mismatch"
    assert client.confirmed_protocol_mismatch is False
