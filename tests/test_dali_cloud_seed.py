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
