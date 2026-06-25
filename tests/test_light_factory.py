"""Tests for Conti light entity factory selection."""

from __future__ import annotations

import sys
from enum import Enum
from unittest.mock import MagicMock


class _CoordinatorEntity:
    def __class_getitem__(cls, _item: object) -> type[_CoordinatorEntity]:
        return cls

    def __init__(self, coordinator: object) -> None:
        self.coordinator = coordinator


class _LightEntity:
    pass


class _ColorMode(Enum):
    ONOFF = "onoff"
    BRIGHTNESS = "brightness"
    COLOR_TEMP = "color_temp"
    RGB = "rgb"


_light_module = sys.modules["homeassistant.components.light"]


def test_create_conti_light_selects_white_strip_for_cct_map(monkeypatch) -> None:
    monkeypatch.setattr(_light_module, "ATTR_BRIGHTNESS", "brightness")
    monkeypatch.setattr(
        _light_module, "ATTR_COLOR_TEMP_KELVIN", "color_temp_kelvin"
    )
    monkeypatch.setattr(_light_module, "ATTR_RGB_COLOR", "rgb_color")
    monkeypatch.setattr(_light_module, "ColorMode", _ColorMode)
    monkeypatch.setattr(_light_module, "LightEntity", _LightEntity)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.update_coordinator"],
        "CoordinatorEntity",
        _CoordinatorEntity,
    )

    from custom_components.conti.lights import (  # noqa: PLC0415
        ContiWhiteStripLight,
        create_conti_light,
    )

    dp_map = {
        "20": {"key": "power", "type": "bool"},
        "21": {"key": "mode", "type": "str"},
        "22": {"key": "brightness", "type": "int", "min": 10, "max": 1000},
        "23": {"key": "color_temp", "type": "int", "min": 10, "max": 1000},
    }
    entry = MagicMock()
    entry.title = "CCT Bar"

    entity = create_conti_light(MagicMock(), entry, "cct-bar", dp_map)

    assert isinstance(entity, ContiWhiteStripLight)
