"""Gateway transport and HA routing regression tests; no real device or network."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.conti.cloud_schema import TuyaCloudSchemaHelper
from custom_components.conti.const import (
    CONF_CHILD_CID,
    CONF_CONNECTION_TYPE,
    CONF_GATEWAY_DEVICE_ID,
    CONNECTION_TYPE_GATEWAY_CHILD,
    DOMAIN,
)
from custom_components.conti.device_manager import DeviceManager
from custom_components.conti.gateway import (
    GatewayChild,
    GatewaySession,
    child_cid,
    device_metadata,
    gateway_error,
    parent_id,
    probe_child,
)


def config(device_id: str = "child-a", cid: str = "node-a", version: str = "3.3") -> dict:
    return {
        "device_id": device_id,
        CONF_CHILD_CID: cid,
        CONF_CONNECTION_TYPE: CONNECTION_TYPE_GATEWAY_CHILD,
        CONF_GATEWAY_DEVICE_ID: "gateway-1",
        "host": "10.10.10.10",
        "local_key": "0123456789abcdef",
        "protocol_version": version,
        "port": 6668,
        "device_type": "switch",
        "dp_map": {},
    }


class FakeDevice:
    """TinyTuya surface with independent child IDs/CIDs and a parent socket."""

    def __init__(self, factory, dev_id, *, parent=None, cid=None, version=3.3, **kwargs):
        self.factory = factory
        self.id = dev_id
        self.parent = parent
        self.cid = cid
        self.version = parent.version if parent else version
        self.socket = None if parent else object()
        self.children = {}
        self.connection_timeout = 3.0
        self.closed = False
        if parent:
            parent.children[dev_id] = self

    def set_socketPersistent(self, value):  # noqa: N802 - TinyTuya API
        pass

    def set_socketRetryLimit(self, value):  # noqa: N802 - TinyTuya API
        assert value >= 1, "zero disables TinyTuya's initial socket connection"

    def set_socketTimeout(self, value):  # noqa: N802 - TinyTuya API
        self.connection_timeout = value

    def set_sendWait(self, value):  # noqa: N802 - TinyTuya API
        pass

    def set_dpsUsed(self, value):  # noqa: N802 - TinyTuya API
        self.dps_used = value

    def subdev_query(self):
        result = self.factory.gateway_reply
        if isinstance(result, Exception):
            raise result
        return result

    def generate_payload(self, command, data=None):
        return command, self.cid, data

    def _send_receive(self, payload, *args, **kwargs):
        return self.status()

    def status(self):
        assert self.parent is not None, "gateway shell must never be polled for child DPS"
        if self.factory.status_delay:
            time.sleep(self.factory.status_delay)
        value = self.factory.replies.get(self.cid, {"cid": self.cid, "dps": {"11": False}})
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return value

    def set_multiple_values(self, values, nowait=False):
        assert self.parent is not None
        assert nowait is True
        self.factory.commands.append((self.id, self.cid, values))
        return self.factory.command_reply

    def heartbeat(self, nowait=False):
        assert nowait is True

    def receive(self):
        if self.factory.pushes:
            return self.factory.pushes.popleft()
        return None

    def close(self):
        self.closed = True
        self.socket = None


class FakeFactory:
    def __init__(self):
        self.parents = []
        self.replies = {}
        self.pushes = deque()
        self.commands = []
        self.gateway_reply = {"dps": {"250": "gateway-only"}}
        self.command_reply = None
        self.status_delay = 0.0

    def __call__(self, dev_id, **kwargs):
        device = FakeDevice(self, dev_id, **kwargs)
        if device.parent is None:
            self.parents.append(device)
        return device


@pytest.fixture
def factory():
    fake = FakeFactory()
    with (
        patch("custom_components.conti.gateway.tinytuya.Device", fake),
        patch(
            "custom_components.conti.gateway.select.select",
            side_effect=lambda readers, *args: (readers if fake.pushes else [], [], []),
        ),
    ):
        yield fake


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["3.1", "3.3", "3.4", "3.5", "auto"])
async def test_child_discovery_uses_supported_parent_cid_api(factory, version):
    factory.replies["node-a"] = {"cid": "node-a", "dps": {"11": True, "18": False}}
    ok, detected, dps, error = await probe_child(config(version=version))
    assert (ok, error) == (True, "")
    assert detected == ("3.3" if version == "auto" else version)
    assert dps == {"11": True, "18": False}
    assert "250" not in dps
    assert factory.parents[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "packet",
    [
        {"dps": {"11": True}},
        {"cid": "someone-else", "dps": {"11": True}},
        {"data": {"cid": "someone-else", "dps": {"11": True}}},
    ],
)
async def test_gateway_or_sibling_dps_cannot_be_discovered_as_child(factory, packet):
    factory.replies["node-a"] = packet
    ok, _, dps, error = await probe_child(config())
    assert not ok and not dps
    assert error == "child_status_query_failed"


@pytest.mark.asyncio
async def test_missing_cid_never_connects(factory):
    result = await probe_child(config(cid=""))
    assert result == (False, None, {}, "child_identifier_missing")
    assert not factory.parents


@pytest.mark.parametrize(
    ("result", "exc", "expected"),
    [
        ({"Error": "Decrypt failed"}, None, "gateway_authentication_failed"),
        ({"Err": "914"}, None, "gateway_key_or_version"),
        ({"Err": "901"}, None, "gateway_unreachable"),
        ({"Err": "902"}, None, "gateway_response_timeout"),
        ({"Err": "905"}, None, "gateway_unreachable"),
        ({"Err": "904"}, None, "child_protocol_error"),
        (None, ConnectionRefusedError(), "gateway_unreachable"),
        (None, ValueError("protocol unsupported"), "gateway_protocol_unsupported"),
    ],
)
def test_error_classification(result, exc, expected):
    assert gateway_error(result, exc) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            {"Error": "Decrypt failed", "Payload": "0123456789abcdef"},
            "gateway_authentication_failed",
        ),
        ({"Err": "901", "Error": "Network Error"}, "gateway_unreachable"),
        ({"Err": "902", "Error": "Timeout", "Payload": "0123456789abcdef"},
         "gateway_response_timeout"),
        ({"Err": "914", "Error": "key or version"}, "gateway_key_or_version"),
    ],
)
async def test_failed_gateway_probe_has_no_secret_detail(factory, caplog, reply, expected):
    factory.gateway_reply = reply
    factory.replies["node-a"] = reply
    ok, _, dps, error = await probe_child(config())
    assert not ok and not dps and error == expected
    assert "0123456789abcdef" not in caplog.text
    assert "Conti gateway setup failed: stage=child_status" in caplog.text
    if reply.get("Err"):
        assert f"tinytuya_code={reply['Err']}" in caplog.text
    assert factory.parents[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("online", "offline", "expected"),
    [
        (["other-node"], [], "gateway_connected_but_child_not_found"),
        ([], ["node-a"], "child_offline"),
    ],
)
async def test_gateway_presence_report_yields_child_specific_error(
    factory, online, offline, expected
):
    factory.gateway_reply = {
        "reqType": "subdev_online_stat_report",
        "data": {"online": online, "offline": offline},
    }
    factory.replies["node-a"] = None
    ok, _, dps, error = await probe_child(config())
    assert not ok and not dps and error == expected


@pytest.mark.asyncio
async def test_empty_verified_child_is_not_mapped(factory):
    factory.replies["node-a"] = {"cid": "node-a", "dps": {}}
    assert (await probe_child(config()))[-1] == "child_dp_discovery_empty"


@pytest.mark.asyncio
async def test_discovery_keeps_verified_child_dps_across_partial_status_packets(factory):
    packets = deque([
        {"cid": "node-a", "dps": {"11": True, "18": False, "31": False, "52": True}},
        {"cid": "node-a", "dps": {"11": True}},
    ])
    factory.replies["node-a"] = packets.popleft
    ok, _, dps, _ = await probe_child(config())
    assert ok and set(dps) == {"11", "18", "31", "52"}


@pytest.mark.asyncio
async def test_multiple_children_share_socket_commands_and_cache_are_isolated(factory):
    manager = DeviceManager()
    # Do not run background listeners; explicitly exercise receive below.
    assert await manager.add_device(config())
    assert await manager.add_device(config("child-b", "node-b"))
    a = manager.get_client("child-a")
    b = manager.get_client("child-b")
    assert a is not b and a.gateway is b.gateway
    assert a._device.parent is b._device.parent
    assert len(factory.parents) == 1
    assert await manager.set_dp("child-a", 11, True)
    assert await manager.set_dps("child-b", {19: True, 22: False})
    assert factory.commands == [
        ("child-a", "node-a", {"11": True}),
        ("child-b", "node-b", {"19": True, "22": False}),
    ]
    assert "19" not in manager.get_cached_dps("child-a")
    assert manager.get_cached_dps("child-b")["11"] is False
    await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", ["flat", "nested", "queued"])
async def test_push_only_updates_addressed_child(factory, wrapped):
    manager = DeviceManager()
    await manager.add_device(config())
    await manager.add_device(config("child-b", "node-b"))
    a_callback, b_callback = MagicMock(), MagicMock()
    manager.register_state_callback("child-a", a_callback)
    manager.register_state_callback("child-b", b_callback)
    packet = {"cid": "node-b", "dps": {"11": True}}
    if wrapped == "nested":
        packet = {"data": packet}
    elif wrapped == "queued":
        packet = (manager.get_client("child-b")._device, packet)
    factory.pushes.append(packet)
    await manager.get_gateway("gateway-1").receive()
    a_callback.assert_not_called()
    b_callback.assert_called_once_with("child-b", {"11": True})
    assert manager.get_cached_dps("child-a") == {"11": False}
    assert manager.get_cached_dps("child-b") == {"11": True}
    await manager.stop()


@pytest.mark.asyncio
async def test_gateway_push_and_unknown_cid_ignored(factory):
    manager = DeviceManager()
    await manager.add_device(config())
    callback = MagicMock()
    manager.register_state_callback("child-a", callback)
    for packet in [{"dps": {"11": True}}, {"cid": "other", "dps": {"11": True}}]:
        factory.pushes.append(packet)
        await manager.get_gateway("gateway-1").receive()
    callback.assert_not_called()
    await manager.stop()


@pytest.mark.asyncio
async def test_remove_child_keeps_parent_until_last_child(factory):
    manager = DeviceManager()
    await manager.add_device(config())
    await manager.add_device(config("child-b", "node-b"))
    await manager.remove_device("child-a")
    assert not factory.parents[0].closed
    assert await manager.set_dp("child-b", 11, True)
    await manager.remove_device("child-b")
    assert factory.parents[0].closed
    assert manager.get_gateway("gateway-1") is None


@pytest.mark.asyncio
async def test_gateway_disconnect_marks_all_children_unavailable_and_restores(factory):
    manager = DeviceManager()
    await manager.add_device(config())
    await manager.add_device(config("child-b", "node-b"))
    gateway = manager.get_gateway("gateway-1")
    gateway.connected = False
    manager._gateway_disconnected(gateway)
    assert not manager.is_online("child-a")
    assert not manager.is_online("child-b")
    factory.replies["node-a"] = {"cid": "node-a", "dps": {"11": True}}
    factory.replies["node-b"] = {"cid": "node-b", "dps": {"18": True}}
    assert await manager.reconnect_device("child-b")
    assert len(factory.parents) == 2
    assert manager.is_online("child-a") and manager.is_online("child-b")
    assert manager.get_cached_dps("child-a")["11"] is True
    assert manager.get_cached_dps("child-b")["18"] is True
    assert gateway.children["child-a"]._device.parent is factory.parents[-1]
    await manager.stop()


@pytest.mark.asyncio
async def test_reconnect_never_uses_old_child_cache_as_proof_of_recovery(factory):
    manager = DeviceManager()
    await manager.add_device(config())
    factory.replies["node-a"] = None
    assert await manager.reconnect_device("child-a")  # gateway is reachable
    assert not manager.is_online("child-a")
    assert manager.get_cached_dps("child-a")  # state retained, availability false
    await manager.stop()


@pytest.mark.asyncio
async def test_one_reconnect_task_per_gateway(factory):
    manager = DeviceManager()
    await manager.start()
    a_config, b_config = config(), config("child-b", "node-b")
    a_config["enable_auto_reconnect"] = True
    b_config["enable_auto_reconnect"] = True
    await manager.add_device(a_config)
    await manager.add_device(b_config)
    gateway = manager.get_gateway("gateway-1")
    assert gateway.listener_task is not None
    assert all(d.listener_task is None for d in manager._devices.values())
    gateway.connected = False
    manager._schedule_reconnect("child-a")
    first = gateway.reconnect_task
    manager._schedule_reconnect("child-b")
    assert gateway.reconnect_task is first
    await manager.stop()
    assert first.done()


@pytest.mark.asyncio
async def test_cancelled_executor_keeps_shared_lock_until_io_finishes(factory):
    gateway = GatewaySession(config())
    child = GatewayChild(config(), gateway)
    await child.connect()
    started = threading.Event()
    release = threading.Event()

    def blocked_status():
        started.set()
        release.wait(timeout=3)
        return {"cid": "node-a", "dps": {"11": True}}

    child._device.status = blocked_status
    query = asyncio.create_task(child.status())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        query.cancel()
        await asyncio.sleep(0)
        assert gateway.lock.locked()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await query
        await gateway.close()


@pytest.mark.asyncio
async def test_duplicate_cid_and_conflicting_gateway_settings_are_rejected(factory):
    manager = DeviceManager()
    await manager.add_device(config())
    with pytest.raises(ValueError, match="child_identifier_duplicate"):
        await manager.add_device(config("child-b", "node-a"))
    conflict = config("child-b", "node-b")
    conflict["local_key"] = "another-key"
    with pytest.raises(ValueError, match="gateway_configuration_conflict"):
        await manager.add_device(conflict)
    assert len(factory.parents) == 1
    await manager.stop()


@pytest.mark.asyncio
async def test_onboarding_probe_reuses_runtime_gateway(factory):
    manager = DeviceManager()
    await manager.add_device(config())
    gateway = manager.get_gateway("gateway-1")
    assert (await probe_child(config("child-b", "node-b"), gateway))[0]
    assert len(factory.parents) == 1
    assert set(gateway.children) == {"child-a"}
    assert not factory.parents[0].closed
    await manager.stop()


def test_cloud_metadata_preserves_relationship_without_guessing_cid():
    raw = {
        "id": "cloud-id",
        "gateway_id": "parent-id",
        "node_id": "zigbee-node",
        "mesh_id": "mesh",
        "product_id": "prod",
        "online": True,
        "local_key": "secret",
    }
    normalized = TuyaCloudSchemaHelper._extract_device_list({"list": [raw]})[0]
    assert parent_id(normalized) == "parent-id"
    assert child_cid(normalized) == "zigbee-node"
    assert child_cid({"id": "cloud-id"}) == ""
    assert parent_id({"mesh_id": "mesh"}) == ""
    assert "local_key" not in device_metadata(raw)


@pytest.fixture
def flow_module(monkeypatch):
    """Load real flow methods against a minimal HA ConfigFlow test base.

    Existing tests mock the whole ConfigFlow class, which cannot exercise
    onboarding methods. Keep this module isolated from those legacy fixtures.
    """
    entries = sys.modules["homeassistant"].config_entries

    class FlowBase:
        def __init__(self):
            self.flow_id = "current-flow"
            self.context = {"source": "user"}
            self.in_progress = []
            self.configured_ids = set()

        def __init_subclass__(cls, **kwargs):
            pass

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

        def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

        def _async_in_progress(self, include_uninitialized=False, match_context=None):
            return [
                flow for flow in self.in_progress
                if flow["flow_id"] != self.flow_id
                and all(flow["context"].get(k) == v for k, v in (match_context or {}).items())
            ]

        async def async_set_unique_id(self, value, *, raise_on_progress=True):
            if value is not None and raise_on_progress and any(
                flow["context"].get("source") != "reauth"
                for flow in self._async_in_progress(match_context={"unique_id": value})
            ):
                raise RuntimeError("already_in_progress")
            self.unique_id = value
            self.context["unique_id"] = value

        def _abort_if_unique_id_configured(self):
            if self.unique_id in self.configured_ids:
                raise RuntimeError("already_configured")

    name = "custom_components.conti._gateway_test_flow"
    path = Path(__file__).parents[1] / "custom_components/conti/config_flow.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with (
        patch.object(entries, "ConfigFlow", FlowBase),
        patch.object(entries, "OptionsFlow", FlowBase),
    ):
        spec.loader.exec_module(module)
    module.CONF_HOST, module.CONF_PORT, module.CONF_NAME = "host", "port", "name"
    monkeypatch.setattr(entries, "SOURCE_REAUTH", "reauth")
    monkeypatch.setattr(entries, "FlowType", SimpleNamespace(CONFIG_FLOW="config_flow"))
    return module


def make_flow(module):
    flow = module.ContiConfigFlow()
    flow.hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(async_entries=lambda domain: []),
    )
    flow._flow_data = {
        module.CONF_NAME: "Test child",
        module.CONF_DEVICE_TYPE: "switch",
        module.CONF_HOST: "10.10.10.10",
        module.CONF_PORT: 6668,
        module.CONF_CONNECTION_TYPE: CONNECTION_TYPE_GATEWAY_CHILD,
    }
    flow._async_try_silent_cloud_schema_fetch = AsyncMock()
    return flow


@pytest.mark.asyncio
async def test_child_flow_persists_cid_and_only_verified_dp_map(factory, flow_module):
    f = flow_module
    flow = make_flow(f)
    factory.replies["node-a"] = {
        "cid": "node-a",
        "dps": {
            "11": True,
            "18": False,
            "31": False,
            "52": True,
        },
    }
    flow._cloud_dp_map = {"250": {"key": "power", "type": "bool"}}
    result = await flow.async_step_gateway_child(
        {
            f.CONF_GATEWAY_DEVICE_ID: "gateway-1",
            f.CONF_HOST: "10.10.10.10",
            f.CONF_LOCAL_KEY: "0123456789abcdef",
            f.CONF_DEVICE_ID: "child-a",
            f.CONF_CHILD_CID: "node-a",
            f.CONF_PORT: 6668,
            f.CONF_PROTOCOL_VERSION: "3.3",
        }
    )
    assert result["step_id"] == "review"
    result = await flow.async_step_review({"action": "accept"})
    assert result["type"] == "create_entry"
    data = result["data"]
    assert data[f.CONF_CHILD_CID] == "node-a"
    assert data[f.CONF_DEVICE_ID] == "child-a"
    assert data[f.CONF_RUNTIME_CHANNEL] == "local"
    assert set(json.loads(data[f.CONF_DP_MAP])) == {"11", "18", "31", "52"}
    assert data[f.CONF_GATEWAY_DEVICE_ID] == "gateway-1"


@pytest.mark.asyncio
async def test_child_flow_rejects_unverified_manual_mapping(factory, flow_module):
    flow = make_flow(flow_module)
    flow._discovered_dps = {"11": True}
    flow._final_dp_map = {"11": {"key": "power", "type": "bool"}}
    result = await flow.async_step_review(
        {
            "action": "edit",
            flow_module.CONF_DP_MAP: '{"250":{"key":"power","type":"bool"}}',
        }
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "child_mapping_failed"


@pytest.mark.asyncio
async def test_child_flow_missing_identifier_shows_specific_error(factory, flow_module):
    flow = make_flow(flow_module)
    result = await flow.async_step_gateway_child({flow_module.CONF_CHILD_CID: ""})
    assert result["errors"]["base"] == "child_identifier_missing"
    assert not factory.parents


@pytest.mark.asyncio
async def test_retry_resumes_unfinished_child_flow_without_opening_socket(factory, flow_module):
    flow = make_flow(flow_module)
    original = {
        "flow_id": "previous-setup",
        "context": {"source": "user", "unique_id": "child-a"},
    }
    flow.in_progress = [original]
    result = await flow.async_step_gateway_child(config())
    assert result == {
        "type": "abort",
        "reason": "gateway_setup_in_progress",
        "next_flow": ("config_flow", "previous-setup"),
    }
    assert flow.in_progress == [original]
    assert not factory.parents


@pytest.mark.asyncio
async def test_cloud_selection_resumes_existing_child_setup(flow_module):
    flow = make_flow(flow_module)
    flow.in_progress = [{
        "flow_id": "previous-setup",
        "context": {"source": "user", "unique_id": "child-a"},
    }]
    flow._get_oauth_manager = AsyncMock()
    result = await flow._prepare_gateway_child(
        "child-a", {"gateway_id": "gateway-1", "node_id": "node-a"},
    )
    assert result["next_flow"] == ("config_flow", "previous-setup")
    flow._get_oauth_manager.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_child_probe_releases_id_and_allows_retry(factory, flow_module):
    flow = make_flow(flow_module)
    factory.replies["node-a"] = {"cid": "node-a", "dps": {}}
    result = await flow.async_step_gateway_child(config())
    assert result["type"] == "form"
    assert result["errors"]["base"] == "child_dp_discovery_empty"
    assert flow.unique_id is None
    factory.replies["node-a"] = {"cid": "node-a", "dps": {"11": True}}
    result = await flow.async_step_gateway_child(config())
    assert result["step_id"] == "review"
    assert flow.unique_id == "child-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["901", "902"])
@pytest.mark.parametrize("reachable", [True, False])
async def test_failed_setup_reports_tcp_evidence_after_closing_probe(
    factory, flow_module, caplog, code, reachable,
):
    flow = make_flow(flow_module)
    reply = {"Err": code, "Error": "failure", "Payload": "0123456789abcdef"}
    factory.gateway_reply = reply
    factory.replies["node-a"] = reply

    async def tcp_probe(host, port):
        assert host == "10.10.10.10" and port == 6668
        assert factory.parents and all(parent.closed for parent in factory.parents)
        return reachable, None if reachable else "cannot_connect", (
            "connected" if reachable else "timeout"
        )

    with patch.object(flow_module, "_async_probe_tcp", AsyncMock(side_effect=tcp_probe)) as tcp:
        result = await flow.async_step_gateway_child(config())
    expected = (
        "gateway_tcp_reachable_no_session" if reachable
        else "gateway_unreachable" if code == "901"
        else "gateway_response_timeout"
    )
    assert result["errors"]["base"] == expected
    assert flow.unique_id is None
    tcp.assert_awaited_once()
    assert "Conti gateway setup diagnostic:" in caplog.text
    assert f"tcp_result={'connected' if reachable else 'timeout'}" in caplog.text
    assert "0123456789abcdef" not in caplog.text


@pytest.mark.asyncio
async def test_configured_child_still_blocks_duplicate_setup(factory, flow_module):
    flow = make_flow(flow_module)
    flow.configured_ids.add("child-a")
    flow.in_progress = [{
        "flow_id": "previous-setup",
        "context": {"source": "user", "unique_id": "child-a"},
    }]
    with pytest.raises(RuntimeError, match="already_configured"):
        await flow.async_step_gateway_child(config())
    assert not factory.parents


@pytest.mark.asyncio
async def test_gateway_retry_ignores_self_other_devices_and_reauth(factory, flow_module):
    flow = make_flow(flow_module)
    flow.in_progress = [
        {"flow_id": flow.flow_id, "context": {"source": "user", "unique_id": "child-a"}},
        {"flow_id": "other", "context": {"source": "user", "unique_id": "child-b"}},
        {"flow_id": "reauth", "context": {"source": "reauth", "unique_id": "child-a"}},
    ]
    result = await flow.async_step_gateway_child(config())
    assert result["step_id"] == "review"
    assert flow.unique_id == "child-a"


@pytest.mark.asyncio
async def test_oauth_child_with_no_key_routes_to_parent_credentials(flow_module):
    f = flow_module
    flow = make_flow(f)
    oauth = SimpleNamespace(
        async_get_device_info=AsyncMock(
            return_value={
                "local_key": "parent-local-key",
                "ip": "10.10.10.10",
            }
        )
    )
    flow._get_oauth_manager = AsyncMock(return_value=oauth)
    result = await flow._prepare_gateway_child(
        "child-cloud-id",
        {
            "gateway_id": "parent-cloud-id",
            "node_id": "child-node",
            "local_key": "",
        },
    )
    assert result["step_id"] == "gateway_child"
    assert flow._flow_data[f.CONF_GATEWAY_DEVICE_ID] == "parent-cloud-id"
    assert flow._flow_data[f.CONF_CHILD_CID] == "child-node"
    assert flow._flow_data[f.CONF_LOCAL_KEY] == "parent-local-key"
    oauth.async_get_device_info.assert_awaited_once_with("parent-cloud-id")


@pytest.mark.asyncio
async def test_arbitrary_multi_gang_dps_make_entities_for_one_child(factory):
    switch_module = sys.modules["homeassistant.components.switch"]

    # The legacy HA fixture mocks SwitchEntity as an instance, so load the
    # actual platform class against a real empty Entity base for this test.
    class SwitchEntity:
        pass

    spec = importlib.util.spec_from_file_location(
        "custom_components.conti._gateway_test_switch",
        Path(__file__).parents[1] / "custom_components/conti/switch.py",
    )
    platform = importlib.util.module_from_spec(spec)
    with patch.object(switch_module, "SwitchEntity", SwitchEntity):
        spec.loader.exec_module(platform)
    mapping = {dp: {"key": f"switch_{dp}", "type": "bool"} for dp in ["11", "18", "31", "52"]}
    entry = SimpleNamespace(
        entry_id="child-entry",
        title="Quad",
        options={},
        data={
            "device_id": "child-a",
            "device_type": "switch",
            "dp_map": json.dumps(mapping),
            "connection_type": "gateway_child",
            "gateway_device_id": "gateway-1",
        },
    )
    coordinator = SimpleNamespace(hass=MagicMock())
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: {"coordinator": coordinator}}})
    add_entities = MagicMock()
    await platform.async_setup_entry(hass, entry, add_entities)
    entities = add_entities.call_args.args[0]
    assert len(entities) == 4
    assert {entity._dp_id for entity in entities} == set(mapping)
    assert all(
        entity._attr_device_info["identifiers"] == {(DOMAIN, "child-a")} for entity in entities
    )
    assert all(
        entity._attr_device_info["via_device"] == (DOMAIN, "gateway-1") for entity in entities
    )


@pytest.mark.asyncio
async def test_offline_child_does_not_disconnect_siblings_and_recovers(factory):
    factory.gateway_reply = {
        "reqType": "subdev_online_stat_report",
        "data": {"online": ["node-b"], "offline": ["node-a"]},
    }
    manager = DeviceManager()
    assert not await manager.add_device(config())
    assert await manager.add_device(config("child-b", "node-b"))
    gateway = manager.get_gateway("gateway-1")
    assert gateway.connected
    assert await manager.query_device("child-a") == {}
    assert not manager.is_online("child-a") and manager.is_online("child-b")
    factory.pushes.append(
        {
            "reqType": "subdev_online_stat_report",
            "data": {"online": ["node-a", "node-b"], "offline": []},
        }
    )
    await gateway.receive()
    assert await manager.query_device("child-a")
    assert manager.is_online("child-a")
    assert len(factory.parents) == 1
    await manager.stop()


@pytest.mark.asyncio
async def test_parent_receive_socket_loss_notifies_all_children(factory):
    manager = DeviceManager()
    await manager.add_device(config())
    await manager.add_device(config("child-b", "node-b"))
    factory.parents[0].socket = None
    await manager.get_gateway("gateway-1").receive()
    assert not manager.is_online("child-a") and not manager.is_online("child-b")
    await manager.stop()


@pytest.mark.asyncio
async def test_reconnect_all_rebuilds_parent_only_once(factory):
    manager = DeviceManager()
    await manager.add_device(config())
    await manager.add_device(config("child-b", "node-b"))
    manager.get_gateway("gateway-1").connected = False
    assert await manager.reconnect_all() == {"child-a": True, "child-b": True}
    assert len(factory.parents) == 2
    await manager.stop()


@pytest.mark.asyncio
async def test_child_query_and_sibling_command_cannot_overlap(factory):
    manager = DeviceManager()
    await manager.add_device(config())
    await manager.add_device(config("child-b", "node-b"))
    started, release = threading.Event(), threading.Event()

    def status():
        started.set()
        release.wait(timeout=3)
        return {"cid": "node-a", "dps": {"11": True}}

    manager.get_client("child-a")._device.status = status
    query = asyncio.create_task(manager.query_device("child-a"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        command = asyncio.create_task(manager.set_dp("child-b", 11, True))
        await asyncio.sleep(0)
        assert not factory.commands
    finally:
        release.set()
        await query
    assert await command
    await manager.stop()


@pytest.mark.asyncio
async def test_subdevice_endpoint_preserves_node_id_and_parent():
    helper = TuyaCloudSchemaHelper("access", "secret", "eu")
    helper._api_get = AsyncMock(
        return_value=[
            {
                "id": "cloud-child",
                "node_id": "zigbee-node",
                "product_id": "product",
            }
        ]
    )
    children = await helper.get_gateway_children("parent-id")
    assert children[0]["gateway_id"] == "parent-id"
    assert children[0]["node_id"] == "zigbee-node"
    helper._api_get.assert_awaited_once_with("/v1.0/devices/parent-id/sub-devices")


@pytest.mark.asyncio
async def test_oauth_picker_never_routes_keyless_child_to_cloud_runtime(flow_module):
    f = flow_module
    flow = make_flow(f)
    flow._onboarding_mode = "smart_life"
    oauth = SimpleNamespace(
        async_get_device_info=AsyncMock(
            side_effect=[
                {"id": "cloud-child", "node_id": "zigbee-node", "gateway_id": "parent-id"},
                {"local_key": "parent-key", "ip": "10.10.10.10"},
            ]
        )
    )
    flow._get_oauth_manager = AsyncMock(return_value=oauth)
    result = await flow.async_step_oauth_pick_device({"cloud_device_id": "cloud-child"})
    assert result["step_id"] == "gateway_child"
    assert flow._flow_data[f.CONF_LOCAL_KEY] == "parent-key"
    assert flow._flow_data[f.CONF_DEVICE_ID] == "cloud-child"


@pytest.mark.asyncio
async def test_options_rediscovery_uses_managed_child_and_preserves_errors(flow_module):
    f = flow_module
    manager = SimpleNamespace(
        get_cached_dps=lambda device_id: {"11": True},
        detect_dps=AsyncMock(return_value={}),
    )
    entry = SimpleNamespace(
        data={
            "device_id": "child-a",
            "connection_type": "gateway_child",
            "dp_map": '{"11":{"key":"power","type":"bool"}}',
            "discovered_dps": '{"11":true}',
        },
        options={},
    )
    flow = f.ContiOptionsFlow(entry)
    flow.hass = SimpleNamespace(data={DOMAIN: {"manager": manager}})
    result = await flow.async_step_init(
        {f.CONF_DP_MAP: entry.data["dp_map"], "rediscover_dps": True}
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "child_dp_discovery_empty"
    manager.detect_dps.assert_awaited_once_with("child-a")


@pytest.mark.parametrize("raw", ["invalid", "", "example"])
async def test_optional_model_import_keeps_sensor_mapping_and_validates(flow_module, raw):
    f = flow_module
    model = Path(__file__).parents[1] / "docs/examples/yk-zigbee-control-model.json"
    raw = model.read_text() if raw == "example" else raw
    entry = SimpleNamespace(
        data={
            "device_id": "child-a", "connection_type": "gateway_child",
            "dp_map": '{"52":{"key":"dp_52","type":"str"}}',
            "discovered_dps": '{"52":"switch_8"}',
        }, options={},
    )
    manager = SimpleNamespace(get_cached_dps=lambda _: {"52": "switch_8"})
    flow = f.ContiOptionsFlow(entry)
    flow.hass = SimpleNamespace(data={DOMAIN: {"manager": manager}})
    result = await flow.async_step_init({"dp_map": entry.data["dp_map"], "control_model": raw})
    if raw == "invalid":
        assert result["type"] == "form"
        assert result["errors"] == {"control_model": "invalid_control_model"}
    else:
        assert result["type"] == "create_entry"
        assert json.loads(result["data"]["dp_map"]) == json.loads(entry.data["dp_map"])
        assert bool(result["data"]["control_model"]) == bool(raw)


async def test_direct_options_do_not_offer_control_model(flow_module):
    f = flow_module
    entry = SimpleNamespace(data={"device_id": "direct", "dp_map": "{}"}, options={})
    flow = f.ContiOptionsFlow(entry)
    flow.hass = SimpleNamespace(data={DOMAIN: {}})
    result = await flow.async_step_init()
    assert "control_model" not in {str(marker.schema) for marker in result["data_schema"].schema}
