"""Remote events consume unsolicited reports, never cached/polled settings."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.conti.const import DOMAIN
from custom_components.conti.gateway import GatewayChild, GatewaySession
from tests.test_gateway import FakeFactory, config


@pytest.fixture
def factory():
    fake = FakeFactory()
    with patch("custom_components.conti.gateway.tinytuya.Device", fake):
        yield fake


@pytest.fixture
def event_platform():
    coordinator_module = sys.modules["homeassistant.helpers.update_coordinator"]

    class CoordinatorEntity(coordinator_module.CoordinatorEntity):
        def async_write_ha_state(self):
            self.writes += 1

    class EventEntity:
        def __init__(self):
            self.calls = []

        async def async_added_to_hass(self):
            self.events = []
            self.cleanup = []
            self.writes = 0

        def async_on_remove(self, callback):
            self.cleanup.append(callback)

        def _trigger_event(self, action, data):
            assert action in self._attr_event_types
            self.events.append((action, data))

        def async_write_ha_state(self):
            self.writes += 1

    spec = importlib.util.spec_from_file_location(
        "custom_components.conti._test_gateway_event",
        Path(__file__).parents[1] / "custom_components/conti/event.py",
    )
    module = importlib.util.module_from_spec(spec)
    with (
        patch.object(sys.modules["homeassistant.components.event"], "EventEntity", EventEntity),
        patch.object(coordinator_module, "CoordinatorEntity", CoordinatorEntity),
    ):
        spec.loader.exec_module(module)
    return module


async def make_event(platform, child, mapping=None, connection="gateway_child"):
    mapping = mapping or {
        "52": {"key": "remote_action", "type": "event", "events": {"switch_1": "s1"}},
    }
    entry = SimpleNamespace(
        entry_id="remote-entry", title="Remote", options={},
        data={
            "device_id": child._device_id,
            "dp_map": json.dumps(mapping),
            "connection_type": connection,
            "gateway_device_id": "gateway-1",
        },
    )
    coordinator = SimpleNamespace(
        device_manager=SimpleNamespace(get_client=lambda device_id: child),
        is_device_available=lambda: child.connected,
    )
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: {"coordinator": coordinator}}})
    add_entities = MagicMock()
    await platform.async_setup_entry(hass, entry, add_entities)
    entities = add_entities.call_args.args[0] if add_entities.called else []
    for entity in entities:
        await entity.async_added_to_hass()
    return entities


async def test_equal_live_reports_fire_twice_but_polls_and_reconnect_do_not(
    factory, event_platform,
):
    gateway = GatewaySession(config())
    child = GatewayChild(config(), gateway)
    sibling = GatewayChild(config("child-b", "node-b"), gateway)
    events = await make_event(event_platform, child)
    event = events[0]
    try:
        assert await gateway.connect()
        assert event.events == []
        factory.replies["node-a"] = {"cid": "node-a", "dps": {"52": "switch_1"}}
        await child.status()
        assert event.events == []
        assert child.cached_dps["52"] == "switch_1"
        for _ in range(2):
            factory.pushes.append({"cid": "node-a", "dps": {"52": "switch_1"}})
            await gateway.receive()
        assert [action for action, _ in event.events] == ["s1", "s1"]
        assert event.writes == 2
        factory.pushes.append({"cid": "node-b", "dps": {"52": "switch_1"}})
        await gateway.receive()
        assert sibling.cached_dps["52"] == "switch_1"
        assert event.writes == 2
        await gateway.close()
        assert not event.available
        assert await gateway.connect()
        assert event.available and event.writes == 2
        factory.pushes.append({"cid": "node-a", "dps": {"52": "unknown"}})
        await gateway.receive()
        assert event.writes == 2
        for cleanup in event.cleanup:
            cleanup()
        factory.pushes.append({"cid": "node-a", "dps": {"52": "switch_1"}})
        await gateway.receive()
        assert event.writes == 2
    finally:
        await gateway.close()


@pytest.mark.parametrize("mapping", [
    {"52": {"key": "dp_52", "type": "str"}},
    {"52": {"key": "action", "type": "event", "events": {}}},
    {"52": {"key": "action", "type": "event", "events": {"switch_1": None}}},
])
async def test_raw_and_invalid_maps_are_not_assumed_button_events(factory, event_platform, mapping):
    gateway = GatewaySession(config())
    child = GatewayChild(config(), gateway)
    assert await make_event(event_platform, child, mapping) == []


async def test_direct_device_entry_does_not_gain_event_entities(factory, event_platform):
    gateway = GatewaySession(config())
    child = GatewayChild(config(), gateway)
    assert await make_event(event_platform, child, connection="direct") == []


async def test_child_report_callbacks_survive_bad_listener(factory):
    gateway = GatewaySession(config())
    child = GatewayChild(config(), gateway)
    reports = []

    def fail(dps):
        raise RuntimeError("listener failed")

    child.subscribe_reports(fail)
    child.subscribe_reports(reports.append)
    child.publish_report({"52": "switch_1"})
    assert reports == [{"52": "switch_1"}]
