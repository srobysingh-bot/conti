"""Raw gateway reports remain visible without inventing remote button roles."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.conti.const import DOMAIN
from custom_components.conti.gateway import map_child_dps

REMOTE_MAPPING = {
    dp: {"key": f"dp_{dp}", "type": "str"} for dp in ("22", "23", "27", "52")
}


@pytest.fixture
def sensor_platform():
    """Load the real sensor platform with a usable HA Entity base."""
    class SensorEntity:
        pass

    spec = importlib.util.spec_from_file_location(
        "custom_components.conti._test_gateway_raw_sensor",
        Path(__file__).parents[1] / "custom_components/conti/sensor.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys.modules["homeassistant.components.sensor"], "SensorEntity", SensorEntity):
        spec.loader.exec_module(module)
    return module


async def setup_sensors(platform, mapping, *, gateway=True, category="switch", options=None):
    entry = SimpleNamespace(
        entry_id="remote-entry",
        title="YK remote",
        data={
            "device_id": "child-remote",
            "device_type": category,
            "dp_map": json.dumps(mapping),
            "connection_type": "gateway_child" if gateway else "direct",
            "gateway_device_id": "gateway-test",
            "device_metadata": {"model": "Remote model", "manufacturer": "Test brand"},
        },
        options=options or {},
    )
    coordinator = SimpleNamespace(
        data={"child-remote": {"22": "single", "23": "hold", "27": "release", "52": "s1"}},
        hass=MagicMock(),
        is_device_available=lambda: True,
    )
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: {"coordinator": coordinator}}})
    add_entities = MagicMock()
    await platform.async_setup_entry(hass, entry, add_entities)
    return add_entities.call_args.args[0] if add_entities.called else [], coordinator


@pytest.mark.parametrize("category", ["switch", "sensor", "light"])
async def test_existing_remote_mapping_creates_four_read_only_sensors(sensor_platform, category):
    entities, coordinator = await setup_sensors(sensor_platform, REMOTE_MAPPING, category=category)
    assert len(entities) == 4
    assert {entity._dp_id for entity in entities} == set(REMOTE_MAPPING)
    assert {entity._attr_unique_id for entity in entities} == {
        f"conti_child-remote_dp_{dp}" for dp in REMOTE_MAPPING
    }
    for entity in entities:
        assert entity.native_value == coordinator.data["child-remote"][entity._dp_id]
        assert entity.available
        assert entity.extra_state_attributes == {"dp_id": entity._dp_id, "read_only": True}
        assert entity._attr_device_info["identifiers"] == {(DOMAIN, "child-remote")}
        assert entity._attr_device_info["via_device"] == (DOMAIN, "gateway-test")
        assert entity._attr_device_info["model"] == "Remote model"
    coordinator.data["child-remote"]["22"] = "double"
    assert next(entity for entity in entities if entity._dp_id == "22").native_value == "double"
    coordinator.data["sibling"] = {"22": "unrelated"}
    assert next(entity for entity in entities if entity._dp_id == "22").native_value == "double"


async def test_direct_raw_mapping_keeps_existing_behavior(sensor_platform):
    entities, _ = await setup_sensors(sensor_platform, REMOTE_MAPPING, gateway=False)
    assert entities == []


async def test_normal_direct_sensor_keeps_scale_and_metadata(sensor_platform):
    entities, coordinator = await setup_sensors(
        sensor_platform, {"22": {"key": "temperature", "type": "int", "scale": 10}},
        gateway=False, category="sensor",
    )
    coordinator.data["child-remote"]["22"] = 235
    assert len(entities) == 1
    assert entities[0].native_value == 23.5
    assert entities[0]._attr_unique_id == "conti_child-remote_temperature"
    assert "via_device" not in entities[0]._attr_device_info
    assert entities[0].extra_state_attributes is None


async def test_options_mapping_takes_priority(sensor_platform):
    entities, _ = await setup_sensors(
        sensor_platform, REMOTE_MAPPING,
        options={"dp_map": {"52": REMOTE_MAPPING["52"]}},
    )
    assert [entity._dp_id for entity in entities] == ["52"]


async def test_raw_values_fit_state_limit_without_losing_payload(sensor_platform):
    entities, coordinator = await setup_sensors(sensor_platform, REMOTE_MAPPING)
    sensor = next(entity for entity in entities if entity._dp_id == "22")
    for raw in ("x" * 300, {"payload": "x" * 300}, ["x" * 300]):
        coordinator.data["child-remote"]["22"] = raw
        assert len(sensor.native_value) <= 255
        assert sensor.extra_state_attributes["raw_value"] == raw
    coordinator.data["child-remote"].pop("22")
    assert sensor.native_value is None


def test_child_mapping_preserves_unmapped_dps_alongside_known_roles():
    dps = {"1": True, "22": "single", "23": "hold", "27": "release", "52": "s1"}
    mapping = map_child_dps("switch", dps)
    assert set(mapping) == set(dps)
    for dp in REMOTE_MAPPING:
        assert mapping[dp] == {**REMOTE_MAPPING[dp], "read_only": True}
    assert mapping["1"]["type"] == "bool"
    assert not mapping["1"].get("read_only")


def test_child_mapping_cloud_roles_only_apply_to_observed_dps():
    mapping = map_child_dps(
        "sensor", {"22": "single", "52": "s1"},
        {"52": {"key": "scene", "type": "str"}, "999": {"key": "battery", "type": "int"}},
    )
    assert set(mapping) == {"22", "52"}
    assert mapping["22"]["read_only"]
    assert mapping["52"]["key"] == "scene"
