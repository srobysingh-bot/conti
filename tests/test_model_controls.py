"""User model command contracts, shared-child routing and honest UI values."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.conti import model_controls as model
from custom_components.conti.const import DOMAIN
from custom_components.conti.device_manager import DeviceManager
from tests.test_gateway import FakeFactory, config

EXAMPLE = Path(__file__).parents[1] / "docs/examples/yk-zigbee-control-model.json"


def fixture_model():
    return json.loads(EXAMPLE.read_text())


def test_correct_user_model_only_exposes_declared_writable_commands():
    controls = model.parse_control_model(EXAMPLE.read_text())
    assert {c.dp_id for c in controls} == {6, 7, 8, 9, 21, 22, 23, 24, 25, 26, 27, 28, 52}
    assert next(c for c in controls if c.dp_id == 52).options == tuple(
        f"switch_{i}" for i in range(1, 9)
    )
    assert {c.access for c in controls} == {"wr", "rw"}
    # No invented scene-controller writes, raw colour encoding or moon command.
    assert not {2, 10, 30} & {c.dp_id for c in controls}


def test_api_response_import_is_canonical_and_does_not_store_credentials():
    raw = json.dumps({"result": {"model": json.dumps(fixture_model())}, "access_token": "secret"})
    saved = model.canonical_control_model(raw)
    assert "secret" not in saved
    assert model.parse_control_model(saved) == model.parse_control_model(EXAMPLE.read_text())
    assert model.canonical_control_model("") == ""


@pytest.mark.parametrize("property_change", [
    {"abilityId": 0}, {"abilityId": True}, {"code": ""},
    {"typeSpec": {"type": "enum", "range": []}},
    {"typeSpec": {"type": "enum", "range": ["on", "on"]}},
    {"typeSpec": {"type": "enum", "range": [1]}},
    {"typeSpec": {"type": "value", "min": 5, "max": 1, "step": 1, "scale": 0}},
    {"typeSpec": {"type": "value", "min": 0, "max": 10, "step": 0, "scale": 0}},
    {"typeSpec": {"type": "value", "min": 0, "max": 10, "step": 1, "scale": -1}},
])
def test_malformed_writable_models_are_rejected(property_change):
    data = fixture_model()
    data["services"][0]["properties"][1].update(property_change)
    with pytest.raises(ValueError):
        model.parse_control_model(json.dumps(data))


def test_duplicate_dp_and_unwritable_models_are_rejected():
    data = fixture_model()
    props = data["services"][0]["properties"]
    props.append(props[1])
    with pytest.raises(ValueError):
        model.parse_control_model(json.dumps(data))
    with pytest.raises(ValueError):
        model.parse_control_model('{"services":[{"properties":[]}]}')


@pytest.fixture
def platforms(monkeypatch):
    class Entity:
        pass

    monkeypatch.setattr(model, "HomeAssistantError", RuntimeError)
    loaded = {}
    for name in ("button", "select", "number"):
        spec = importlib.util.spec_from_file_location(
            f"custom_components.conti._test_model_{name}",
            Path(__file__).parents[1] / f"custom_components/conti/{name}.py",
        )
        module = importlib.util.module_from_spec(spec)
        with patch.object(
            sys.modules[f"homeassistant.components.{name}"], f"{name.capitalize()}Entity", Entity,
        ):
            spec.loader.exec_module(module)
        if hasattr(module, "HomeAssistantError"):
            monkeypatch.setattr(module, "HomeAssistantError", RuntimeError)
        loaded[name] = module
    return loaded


async def setup_controls(platforms, *, gateway=True, raw=None):
    entry = SimpleNamespace(
        entry_id="entry", title="YK remote",
        data={
            "device_id": "child", "connection_type": "gateway_child" if gateway else "direct",
            "gateway_device_id": "parent", "device_metadata": {"model": "YK-ZIGBEE"},
        },
        options={} if raw is None else {"control_model": raw},
    )
    coordinator = SimpleNamespace(
        data={"child": {"23": "wireless_switch"}}, hass=MagicMock(),
        device_manager=SimpleNamespace(set_dp=AsyncMock(return_value=True)),
        is_device_available=lambda: True,
    )
    hass = SimpleNamespace(data={DOMAIN: {"entry": {"coordinator": coordinator}}})
    entities = {}
    for name, platform in platforms.items():
        add = MagicMock()
        await platform.async_setup_entry(hass, entry, add)
        entities[name] = add.call_args.args[0]
    return entities, coordinator


async def test_opt_in_and_direct_device_isolation(platforms):
    for gateway, raw in [(True, None), (True, "invalid"), (False, EXAMPLE.read_text())]:
        entities, coordinator = await setup_controls(platforms, gateway=gateway, raw=raw)
        assert not any(entities.values())
        coordinator.device_manager.set_dp.assert_not_called()


async def test_user_model_builds_thirteen_buttons_eight_selects_two_numbers(platforms):
    entities, coordinator = await setup_controls(platforms, raw=EXAMPLE.read_text())
    assert {k: len(v) for k, v in entities.items()} == {"button": 13, "select": 8, "number": 2}
    all_entities = [e for group in entities.values() for e in group]
    assert len({e._attr_unique_id for e in all_entities}) == 23
    assert {e._attr_name for e in entities["button"]} == {
        "Power ON", "Power OFF", "White mode", "Colour mode", "Scene mode",
        *(f"Select S{i}" for i in range(1, 9)),
    }
    for entity in all_entities:
        assert entity._attr_device_info["identifiers"] == {(DOMAIN, "child")}
        assert entity._attr_device_info["via_device"] == (DOMAIN, "parent")
    coordinator.device_manager.set_dp.assert_not_called()


async def test_repeated_button_presses_send_exact_child_and_values(platforms):
    entities, coordinator = await setup_controls(platforms, raw=EXAMPLE.read_text())
    for button in entities["button"]:
        await button.async_press()
        await button.async_press()
        assert coordinator.device_manager.set_dp.await_args.args == (
            "child", button.control.dp_id, button._value,
        )
    assert coordinator.device_manager.set_dp.await_count == 26


async def test_offline_and_failed_commands_raise_without_false_values(platforms):
    entities, coordinator = await setup_controls(platforms, raw=EXAMPLE.read_text())
    button = entities["button"][0]
    coordinator.is_device_available = lambda: False
    with pytest.raises(RuntimeError):
        await button.async_press()
    coordinator.device_manager.set_dp.assert_not_called()
    coordinator.is_device_available = lambda: True
    coordinator.device_manager.set_dp.return_value = False
    with pytest.raises(RuntimeError):
        await button.async_press()
    assert button._last_command is None


async def test_enum_validation_and_write_only_values_are_last_commands(platforms):
    entities, coordinator = await setup_controls(platforms, raw=EXAMPLE.read_text())
    lighting = next(e for e in entities["button"] if e._attr_name == "Colour mode")
    # Do not treat a write-only property from the cached/query data as reported state.
    coordinator.data["child"]["7"] = "scene"
    assert lighting.current_value() is None
    await lighting.async_press()
    assert lighting.current_value() == "colour"
    assert lighting.extra_state_attributes == {"command_only": True}
    assert coordinator.device_manager.set_dp.await_count == 1
    button_mode = next(e for e in entities["select"] if e.control.dp_id == 23)
    assert button_mode.current_option == "wireless_switch"
    with pytest.raises(RuntimeError):
        await button_mode.async_select_option("invented")


async def test_numeric_values_preserve_model_range_and_reject_invalid_commands(platforms):
    entities, coordinator = await setup_controls(platforms, raw=EXAMPLE.read_text())
    brightness = next(e for e in entities["number"] if e.control.dp_id == 8)
    assert brightness.native_value is None
    for invalid in (9, 1001, float("nan"), float("inf"), 12.5, True):
        with pytest.raises(RuntimeError):
            await brightness.async_set_native_value(invalid)
    coordinator.device_manager.set_dp.assert_not_called()
    await brightness.async_set_native_value(500)
    assert brightness.native_value == 500
    coordinator.device_manager.set_dp.assert_awaited_once_with("child", 8, 500)


async def test_numeric_scale_and_step_are_applied_before_send(platforms):
    data = fixture_model()
    data["services"][0]["properties"][3]["typeSpec"].update(
        {"min": 10, "max": 1000, "scale": 1, "step": 5}
    )
    entities, coordinator = await setup_controls(platforms, raw=json.dumps(data))
    brightness = next(e for e in entities["number"] if e.control.dp_id == 8)
    with pytest.raises(RuntimeError):
        await brightness.async_set_native_value(10.1)
    await brightness.async_set_native_value(10.5)
    coordinator.device_manager.set_dp.assert_awaited_once_with("child", 8, 105)
    assert brightness.native_value == 10.5


async def test_dashboard_commands_reuse_real_manager_and_one_parent(platforms):
    factory = FakeFactory()
    manager = DeviceManager()
    with patch("custom_components.conti.gateway.tinytuya.Device", factory):
        try:
            await manager.add_device(config())
            await manager.add_device(config("child-b", "node-b"))
            entities, coordinator = await setup_controls(platforms, raw=EXAMPLE.read_text())
            coordinator.device_manager = manager
            for button in entities["button"]:
                button._device_id = "child-b"
            power = next(e for e in entities["button"] if e._attr_name == "Power ON")
            selection = next(e for e in entities["button"] if e._attr_name == "Select S1")
            await power.async_press()
            await selection.async_press()
            await selection.async_press()
            assert factory.commands == [
                ("child-b", "node-b", {"6": True}),
                ("child-b", "node-b", {"52": "switch_1"}),
                ("child-b", "node-b", {"52": "switch_1"}),
            ]
            assert len(factory.parents) == 1
            assert "52" not in manager.get_cached_dps("child-a")
        finally:
            await manager.stop()
