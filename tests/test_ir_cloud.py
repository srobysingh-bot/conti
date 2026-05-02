"""Tests for the OAuth-backed IR cloud wrapper."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.conti.ir_cloud import TuyaIRCloud


class FakeOAuth:
    """Minimal OAuth manager stand-in for IR cloud tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def async_get_ir_categories(self, device_id: str) -> Any:
        self.calls.append(("categories", device_id))
        return [{"category_id": "5", "category_name": "AC"}]

    async def async_get_ir_brands(self, category_id: str, *, device_id: str = "") -> Any:
        self.calls.append(("brands", device_id, category_id))
        return [{"brand_id": "lg", "brand_name": "LG"}]

    async def async_get_ir_remotes(
        self,
        category_id: str,
        brand_id: str,
        *,
        device_id: str = "",
    ) -> Any:
        self.calls.append(("remotes", device_id, category_id, brand_id))
        return [{"remote_index": "1", "remote_name": "LG AC"}]

    async def async_get_ir_remote_commands(
        self,
        device_id: str,
        category_id: str,
        brand_id: str,
        remote_index: str,
    ) -> Any:
        self.calls.append(("commands", device_id, category_id, brand_id, remote_index))
        return [{"code": "power", "key_id": "1"}]

    async def async_send_ir_command(self, device_id: str, command: dict[str, Any]) -> bool:
        self.calls.append(("send", device_id, command))
        return True


@pytest.mark.asyncio
async def test_ir_cloud_uses_oauth_for_library_fetches() -> None:
    oauth = FakeOAuth()
    cloud = TuyaIRCloud(oauth)  # type: ignore[arg-type]

    categories = await cloud.list_categories("irhub1")
    brands = await cloud.list_brands("irhub1", "5")
    models = await cloud.list_models("irhub1", "5", "lg")
    commands = await cloud.fetch_commands("irhub1", models[0])

    assert categories == [{"id": "5", "name": "AC", "raw": {"category_id": "5", "category_name": "AC"}}]
    assert brands[0]["id"] == "lg"
    assert models[0]["remote_index"] == "1"
    assert commands["power"]["source"] == "cloud"
    assert oauth.calls == [
        ("categories", "irhub1"),
        ("brands", "irhub1", "5"),
        ("remotes", "irhub1", "5", "lg"),
        ("commands", "irhub1", "5", "lg", "1"),
    ]


@pytest.mark.asyncio
async def test_ir_cloud_send_uses_oauth_session() -> None:
    oauth = FakeOAuth()
    cloud = TuyaIRCloud(oauth)  # type: ignore[arg-type]

    command = {"payload": {"key": "power"}}

    assert await cloud.send_command("irhub1", command) is True
    assert oauth.calls == [("send", "irhub1", command)]
