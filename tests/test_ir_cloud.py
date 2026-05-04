"""Tests for the OAuth-backed IR cloud wrapper."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.conti.ir_cloud import TuyaIRCloud
from custom_components.conti.tuya_oauth import TuyaOAuthManager


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

    async def async_get_ir_device_remotes(self, device_id: str) -> Any:
        self.calls.append(("device_remotes", device_id))
        return [
            {
                "category_id": "5",
                "brand_id": "lg",
                "remote_id": "remote-a",
                "remote_index": "1",
                "remote_name": "Living Room AC",
            }
        ]

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


@pytest.mark.asyncio
async def test_ir_cloud_lists_device_remotes_first() -> None:
    oauth = FakeOAuth()
    cloud = TuyaIRCloud(oauth)  # type: ignore[arg-type]

    remotes = await cloud.list_device_remotes("irhub1")

    assert remotes == [
        {
            "id": "1",
            "name": "Living Room AC",
            "category_id": "5",
            "brand_id": "lg",
            "remote_id": "remote-a",
            "remote_index": "1",
            "raw": {
                "category_id": "5",
                "brand_id": "lg",
                "remote_id": "remote-a",
                "remote_index": "1",
                "remote_name": "Living Room AC",
            },
        }
    ]
    assert oauth.calls == [("device_remotes", "irhub1")]


@pytest.mark.asyncio
async def test_oauth_resolves_infrared_id_from_ir_list() -> None:
    oauth = TuyaOAuthManager.__new__(TuyaOAuthManager)
    oauth._infrared_id_cache = {}  # type: ignore[attr-defined]
    calls: list[tuple[str, str]] = []

    async def fake_get(device_id: str, path: str) -> Any:
        calls.append((device_id, path))
        if path == "/v2.0/infrareds":
            return [{"device_id": "irhub1", "infrared_id": "infrared-123"}]
        return [{"category_id": "5", "category_name": "AC"}]

    oauth._sharing_api_get = fake_get  # type: ignore[method-assign]

    result = await oauth.async_get_ir_categories("irhub1")

    assert result == [{"category_id": "5", "category_name": "AC"}]
    assert calls == [
        ("", "/v2.0/infrareds"),
        ("irhub1", "/v2.0/infrareds/infrared-123/categories"),
    ]


@pytest.mark.asyncio
async def test_oauth_resolver_validates_ir_capability_before_device_id_fallback() -> None:
    oauth = TuyaOAuthManager.__new__(TuyaOAuthManager)
    oauth._infrared_id_cache = {}  # type: ignore[attr-defined]

    async def fake_get(device_id: str, path: str) -> Any:
        return [] if path == "/v2.0/infrareds" else None

    async def fake_devices() -> list[dict[str, Any]]:
        return [{"id": "irhub1", "category": "wnykq"}]

    oauth._sharing_api_get = fake_get  # type: ignore[method-assign]
    oauth.async_list_devices_sharing = fake_devices  # type: ignore[method-assign]

    assert await oauth.async_get_infrared_id("irhub1") == "irhub1"
