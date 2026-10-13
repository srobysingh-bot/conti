"""Capture reports on the managed socket without changing device operation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.conti.const import DOMAIN
from custom_components.conti.diagnostics import async_get_config_entry_diagnostics
from custom_components.conti.gateway import GatewayChild, GatewaySession
from custom_components.conti.gateway_capture import (
    CAPTURES_KEY,
    MAX_REPORT_BYTES,
    MAX_REPORTS,
    REPORT_EVENT,
    GatewayReportCapture,
    capture_diagnostics,
    register_capture_services,
    stop_capture,
)
from tests.test_gateway import FakeFactory, config


@pytest.fixture
def capture_runtime():
    factory = FakeFactory()
    with patch("custom_components.conti.gateway.tinytuya.Device", factory):
        gateway = GatewaySession(config())
        child = GatewayChild(config(), gateway)
        sibling = GatewayChild(config("child-b", "node-b"), gateway)
        hass = SimpleNamespace(
            data={DOMAIN: {"manager": SimpleNamespace(get_client=lambda device_id: {
                "child-a": child, "child-b": sibling,
            }.get(device_id))}},
            services=MagicMock(), bus=MagicMock(),
        )
        register_capture_services(hass)
        services = {
            call.args[1]: (call.args[2], call.kwargs["schema"])
            for call in hass.services.async_register.call_args_list
        }
        yield SimpleNamespace(
            factory=factory, gateway=gateway, child=child, sibling=sibling,
            hass=hass, services=services,
        )


async def invoke(runtime, action, data):
    handler, schema = runtime.services[action]
    await handler(SimpleNamespace(data=schema(data)))


async def test_capture_uses_one_socket_and_keeps_normal_child_updates(capture_runtime):
    r = capture_runtime
    ordinary_updates = []
    r.child.set_dp_callback(ordinary_updates.append)
    assert await r.gateway.connect()
    try:
        before_parents = list(r.factory.parents)
        await invoke(r, "start_gateway_report_capture", {"device_id": "child-a"})
        assert r.factory.parents == before_parents
        r.factory.replies["node-a"] = {"cid": "node-a", "dps": {"52": "switch_1"}}
        await r.child.status()
        assert capture_diagnostics(r.hass, "child-a")["listener_report_count"] == 0
        assert capture_diagnostics(r.hass, "child-a")["query_report_count"] == 1
        for _ in range(2):
            r.factory.pushes.append({"cid": "node-a", "dps": {"52": "switch_1"}})
            await r.gateway.receive()
        r.factory.pushes.append({"cid": "node-b", "dps": {"52": "switch_2"}})
        await r.gateway.receive()
        capture = capture_diagnostics(r.hass, "child-a")
        assert capture["report_count"] == 3
        assert capture["listener_report_count"] == 2
        assert capture["query_report_count"] == 1
        assert [report["dps"] for report in capture["reports"]] == [
            {"52": "switch_1"}, {"52": "switch_1"}, {"52": "switch_1"},
        ]
        assert ordinary_updates[-2:] == [{"52": "switch_1"}, {"52": "switch_1"}]
        assert r.sibling.cached_dps["52"] == "switch_2"
        assert r.factory.commands == []
        assert len(r.factory.parents) == 1
        assert r.hass.bus.async_fire.call_count == 3
        assert r.hass.bus.async_fire.call_args.args[0] == REPORT_EVENT
        await invoke(r, "stop_gateway_report_capture", {"device_id": "child-a"})
        assert not capture_diagnostics(r.hass, "child-a")["active"]
        r.child.publish_report({"52": "switch_2"})
        assert r.hass.bus.async_fire.call_count == 3
        assert r.child.connected
    finally:
        stop_capture(r.hass, "child-a")
        await r.gateway.close()


async def test_expiry_unsubscribes_without_closing_device(capture_runtime):
    r = capture_runtime
    capture = GatewayReportCapture(r.child, 0, lambda report: None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not capture.active and capture.reason == "completed"
    assert not r.child._observation_callbacks
    assert capture._timer is None and capture._unsubscribe is None


async def test_restart_capture_replaces_previous_subscription(capture_runtime):
    r = capture_runtime
    assert await r.gateway.connect()
    try:
        await invoke(r, "start_gateway_report_capture", {"device_id": "child-a"})
        old = r.hass.data[DOMAIN][CAPTURES_KEY]["child-a"]
        await invoke(r, "start_gateway_report_capture", {"device_id": "child-a", "duration": 60})
        assert old.reason == "restarted" and not old.active
        assert len(r.child._observation_callbacks) == 1
        stop_capture(r.hass, "child-a", "entry_unloaded")
        assert capture_diagnostics(r.hass, "child-a")["status"] == "entry_unloaded"
        assert not r.child._observation_callbacks
    finally:
        stop_capture(r.hass, "child-a")
        await r.gateway.close()


async def test_capture_size_count_and_mutation_are_bounded(capture_runtime):
    r = capture_runtime
    capture = GatewayReportCapture(r.child, 120, lambda report: report["dps"].clear())
    try:
        original = {"52": {"nested": ["value"]}, "local_key": "ignored-packet-field"}
        r.child.publish_report(original)
        original["52"]["nested"].clear()
        snapshot = capture.snapshot()
        assert snapshot["reports"][0]["dps"] == {"52": {"nested": ["value"]}}
        snapshot["reports"].clear()
        assert capture.snapshot()["report_count"] == 1
        r.child.publish_report({"52": "x" * (MAX_REPORT_BYTES + 1)})
        r.child.publish_report({"52": float("nan")})
        assert capture.ignored_reports == 2
        for _ in range(MAX_REPORTS):
            r.child.publish_report({"52": "switch_1"})
        assert len(capture.reports) == MAX_REPORTS
        assert not capture.active and capture.reason == "report_limit"
        assert not r.child._observation_callbacks
    finally:
        capture.stop()


@pytest.mark.parametrize("device_id", ["unknown", "child-a"])
async def test_missing_or_offline_child_does_not_open_connection(capture_runtime, device_id):
    r = capture_runtime
    with (
        patch("custom_components.conti.gateway_capture.HomeAssistantError", RuntimeError),
        pytest.raises(RuntimeError),
    ):
        await invoke(r, "start_gateway_report_capture", {"device_id": device_id})
    assert not r.factory.parents
    assert CAPTURES_KEY not in r.hass.data[DOMAIN]


async def test_diagnostics_include_capture_without_config_credentials(capture_runtime):
    r = capture_runtime
    assert await r.gateway.connect()
    try:
        await invoke(r, "start_gateway_report_capture", {"device_id": "child-a"})
        r.child.publish_report({"52": "switch_1"})
        entry = SimpleNamespace(
            entry_id="child-entry", options={},
            data={"device_id": "child-a", "local_key": "do-not-export", "access_token": "private"},
        )
        coordinator = SimpleNamespace(get_diagnostics=lambda: {"device_id": "child-a"})
        r.hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}
        diagnostics = await async_get_config_entry_diagnostics(r.hass, entry)
        assert diagnostics["gateway_report_capture"]["report_count"] == 1
        assert "do-not-export" not in str(diagnostics)
        assert "private" not in str(diagnostics)
    finally:
        stop_capture(r.hass, "child-a")
        await r.gateway.close()


async def test_capture_services_registered_without_loaded_entries():
    from custom_components.conti import async_setup

    hass = SimpleNamespace(data={}, services=MagicMock(), bus=MagicMock())
    assert await async_setup(hass, {})
    registered = {
        call.args[1]: call.args[2] for call in hass.services.async_register.call_args_list
    }
    assert set(registered) == {"start_gateway_report_capture", "stop_gateway_report_capture"}
    with (
        patch("custom_components.conti.gateway_capture.HomeAssistantError", RuntimeError),
        pytest.raises(RuntimeError, match="loaded Conti gateway child"),
    ):
        await registered["start_gateway_report_capture"](
            SimpleNamespace(data={"device_id": "none"}),
        )
