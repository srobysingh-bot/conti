"""Tests for Conti curtain cover support."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.conti.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DP_MAP,
    CONF_INVERT_POSITION,
    DEVICE_TYPE_CURTAIN,
    DOMAIN,
)
from custom_components.conti.cover import ContiCurtainCover, async_setup_entry
from custom_components.conti.dp_mapping import normalize_dp_map


def _entry(
    dp_map: dict[str, object],
    *,
    invert_position: bool = False,
    device_type: str = DEVICE_TYPE_CURTAIN,
) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.title = "Living Room Curtain"
    entry.data = {
        CONF_DEVICE_ID: "curtain-1",
        CONF_DEVICE_TYPE: device_type,
        CONF_DP_MAP: json.dumps(dp_map),
    }
    entry.options = {CONF_INVERT_POSITION: invert_position}
    return entry


def _coordinator(dps: dict[str, object] | None = None) -> MagicMock:
    manager = MagicMock()
    manager.set_dp = AsyncMock(return_value=True)
    coordinator = MagicMock()
    coordinator.hass = MagicMock()
    coordinator.data = {"curtain-1": dps or {}}
    coordinator.device_manager = manager
    coordinator.is_device_available.return_value = True
    coordinator.mark_dp_commanded = MagicMock()
    coordinator.apply_optimistic_update = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


def _cover(
    dp_map: dict[str, object],
    *,
    dps: dict[str, object] | None = None,
    invert_position: bool = False,
) -> tuple[ContiCurtainCover, MagicMock]:
    coordinator = _coordinator(dps)
    entity = ContiCurtainCover(
        coordinator,
        _entry(dp_map, invert_position=invert_position),
        "curtain-1",
        dp_map,
    )
    return entity, coordinator


@pytest.mark.asyncio
async def test_curtain_device_creates_cover_entity() -> None:
    dp_map = {"1": {"key": "control", "type": "str"}}
    entry = _entry(dp_map)
    coordinator = _coordinator({"1": "stop"})
    hass = MagicMock()
    hass.data = {DOMAIN: {entry.entry_id: {"coordinator": coordinator}}}
    add_entities = MagicMock()

    await async_setup_entry(hass, entry, add_entities)

    add_entities.assert_called_once()
    entities = add_entities.call_args.args[0]
    assert len(entities) == 1
    assert isinstance(entities[0], ContiCurtainCover)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "value"),
    [
        ("async_open_cover", "open"),
        ("async_close_cover", "close"),
        ("async_stop_cover", "stop"),
    ],
)
async def test_control_commands_send_correct_dp(method_name: str, value: str) -> None:
    entity, coordinator = _cover({"1": {"key": "control", "type": "str"}})

    await getattr(entity, method_name)()

    coordinator.device_manager.set_dp.assert_awaited_once_with("curtain-1", 1, value)
    coordinator.mark_dp_commanded.assert_called_with("1")


def test_set_position_feature_only_when_writable_position_exists() -> None:
    with_position, _ = _cover(
        {
            "1": {"key": "control", "type": "str"},
            "2": {"key": "position", "type": "int", "min": 0, "max": 100},
        }
    )
    readonly_position, _ = _cover(
        {
            "1": {"key": "control", "type": "str"},
            "3": {
                "key": "position",
                "type": "int",
                "min": 0,
                "max": 100,
                "read_only": True,
            },
        }
    )

    assert int(with_position._attr_supported_features) & 4
    assert not int(readonly_position._attr_supported_features) & 4


def test_percent_aliases_normalize_to_curtain_position_keys() -> None:
    assert normalize_dp_map(
        {
            "2": {"key": "percent_control", "type": "int"},
            "3": {"key": "percent_state", "type": "int"},
        }
    ) == {
        "2": {"key": "position", "type": "int"},
        "3": {"key": "current_position", "type": "int"},
    }


@pytest.mark.asyncio
async def test_invert_position_converts_reported_and_requested_positions() -> None:
    entity, coordinator = _cover(
        {
            "2": {"key": "position", "type": "int", "min": 0, "max": 100},
            "3": {"key": "current_position", "type": "int", "min": 0, "max": 100},
        },
        dps={"3": 20},
        invert_position=True,
    )

    assert entity.current_cover_position == 80

    await entity.async_set_cover_position(position=30)

    coordinator.device_manager.set_dp.assert_awaited_once_with("curtain-1", 2, 70)


def test_work_state_reports_opening_and_closing() -> None:
    opening, _ = _cover({"7": {"key": "work_state", "type": "str"}}, dps={"7": "opening"})
    closing, _ = _cover({"7": {"key": "work_state", "type": "str"}}, dps={"7": "closing"})

    assert opening.is_opening is True
    assert closing.is_closing is True


def test_position_state_uses_ha_closed_open_convention() -> None:
    closed, _ = _cover({"3": {"key": "current_position", "type": "int"}}, dps={"3": 0})
    open_, _ = _cover({"3": {"key": "current_position", "type": "int"}}, dps={"3": 100})
    partial, _ = _cover({"3": {"key": "current_position", "type": "int"}}, dps={"3": 45})

    assert closed.is_closed is True
    assert open_.is_closed is False
    assert partial.is_closed is False
