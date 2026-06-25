"""Tests for DALI fallback cloud DPS seeding."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.conti.cloud_device_runtime import CloudDeviceRuntime


@pytest.mark.asyncio
async def test_cloud_properties_seed_dali_cct_dp_ids() -> None:
    oauth = MagicMock()
    oauth.async_get_device_status = AsyncMock(
        return_value=[
            {"code": "switch_led", "value": True},
            {"code": "work_mode", "value": "white"},
            {"code": "bright_value", "value": 600},
            {"code": "temp_value", "value": 450},
        ]
    )
    runtime = CloudDeviceRuntime(
        device_id="dali1",
        oauth_manager=oauth,
        dp_map={
            "20": {"key": "power", "code": "switch_led"},
            "21": {"key": "mode", "code": "work_mode"},
            "22": {"key": "brightness", "code": "bright_value"},
            "23": {"key": "color_temp", "code": "temp_value"},
        },
    )

    assert await runtime.async_get_dps() == {
        "20": True,
        "21": "white",
        "22": 600,
        "23": 450,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dp_id", "value", "code"),
    [
        (20, True, "switch_led"),
        (22, 700, "bright_value"),
        (23, 300, "temp_value"),
    ],
)
async def test_dali_cloud_commands_supported(
    dp_id: int, value: object, code: str
) -> None:
    oauth = MagicMock()
    oauth.async_send_device_commands = AsyncMock(return_value=True)
    runtime = CloudDeviceRuntime(
        device_id="dali1",
        oauth_manager=oauth,
        dp_map={
            "20": {"key": "power", "code": "switch_led"},
            "21": {"key": "mode", "code": "work_mode"},
            "22": {"key": "brightness", "code": "bright_value"},
            "23": {"key": "color_temp", "code": "temp_value"},
        },
    )

    assert await runtime.async_set_dp(dp_id, value) is True
    oauth.async_send_device_commands.assert_awaited_once_with(
        "dali1", [{"code": code, "value": value}]
    )


def test_non_cct_cloud_map_is_not_eligible() -> None:
    runtime = CloudDeviceRuntime(
        device_id="rgb1",
        oauth_manager=MagicMock(),
        dp_map={
            "20": {"key": "power", "code": "switch_led"},
            "22": {"key": "brightness", "code": "bright_value"},
            "24": {"key": "color_rgb", "code": "colour_data"},
        },
    )

    assert runtime.supports_dali_cct_fallback() is False
