"""Tests for dp_mapping — heuristic auto-mapping and key masking."""

from __future__ import annotations

import pytest

from custom_components.conti.dp_mapping import (
    auto_map_dps,
    mask_key,
    merge_dp_maps,
)


# ---------------------------------------------------------------------------
# mask_key
# ---------------------------------------------------------------------------


class TestMaskKey:
    def test_normal_key(self) -> None:
        assert mask_key("0123456789abcdef") == "01************ef"

    def test_short_key(self) -> None:
        assert mask_key("ab") == "****"

    def test_four_char_key(self) -> None:
        assert mask_key("abcd") == "****"

    def test_five_char_key(self) -> None:
        assert mask_key("abcde") == "ab*de"

    def test_empty_key(self) -> None:
        assert mask_key("") == "****"


# ---------------------------------------------------------------------------
# auto_map_dps — switch
# ---------------------------------------------------------------------------


class TestAutoMapSwitch:
    def test_single_bool_dp(self) -> None:
        dps = {"1": True}
        result = auto_map_dps("switch", dps)
        assert "1" in result
        assert result["1"]["key"] == "power"
        assert result["1"]["type"] == "bool"

    def test_multi_gang(self) -> None:
        dps = {"1": True, "2": False, "3": True}
        result = auto_map_dps("switch", dps)
        assert len(result) == 3
        for dp_id in ("1", "2", "3"):
            assert result[dp_id]["key"] == "power"

    def test_non_bool_skipped(self) -> None:
        """Int DPs should not be auto-mapped as switch power DPs."""
        dps = {"1": True, "9": 120}
        result = auto_map_dps("switch", dps)
        assert "1" in result
        assert "9" not in result

    def test_empty_dps(self) -> None:
        assert auto_map_dps("switch", {}) == {}


# ---------------------------------------------------------------------------
# auto_map_dps — light
# ---------------------------------------------------------------------------


class TestAutoMapLight:
    def test_full_light(self) -> None:
        dps = {"1": True, "2": 500, "3": 300, "5": "ff0000"}
        result = auto_map_dps("light", dps)
        assert result["1"]["key"] == "power"
        assert result["2"]["key"] == "brightness"
        assert result["3"]["key"] == "color_temp"
        assert result["5"]["key"] == "color_rgb"

    def test_power_only(self) -> None:
        dps = {"1": True}
        result = auto_map_dps("light", dps)
        assert "1" in result
        assert result["1"]["key"] == "power"
        assert len(result) == 1

    def test_alt_dp_ids(self) -> None:
        """Devices using DP 20/22/23/24 patterns."""
        dps = {"20": True, "22": 300, "23": 200, "24": "aabb00"}
        result = auto_map_dps("light", dps)
        assert result["20"]["key"] == "power"
        assert result["22"]["key"] == "brightness"
        assert result["23"]["key"] == "color_temp"
        assert result["24"]["key"] == "color_rgb"

    def test_brightness_range_defaults(self) -> None:
        dps = {"1": True, "2": 500}
        result = auto_map_dps("light", dps)
        assert result["2"]["min"] == 10
        assert result["2"]["max"] == 1000


# ---------------------------------------------------------------------------
# auto_map_dps — sensor
# ---------------------------------------------------------------------------


class TestAutoMapSensor:
    def test_temp_humidity_battery(self) -> None:
        dps = {"1": 225, "2": 65, "3": 100}
        result = auto_map_dps("sensor", dps)
        assert result["1"]["key"] == "temperature"
        assert result["2"]["key"] == "humidity"
        assert result["3"]["key"] == "battery"


# ---------------------------------------------------------------------------
# auto_map_dps — climate
# ---------------------------------------------------------------------------


class TestAutoMapClimate:
    def test_basic_climate(self) -> None:
        dps = {"1": True, "2": 24, "3": 22, "4": "cool", "5": "auto"}
        result = auto_map_dps("climate", dps)
        assert result["1"]["key"] == "power"
        assert result["2"]["key"] == "target_temp"
        assert result["3"]["key"] == "current_temp"
        assert result["4"]["key"] == "hvac_mode"
        assert result["5"]["key"] == "fan_mode"


# ---------------------------------------------------------------------------
# auto_map_dps — fan
# ---------------------------------------------------------------------------


class TestAutoMapFan:
    def test_basic_fan(self) -> None:
        dps = {"1": True, "3": 2}
        result = auto_map_dps("fan", dps)
        assert result["1"]["key"] == "power"
        assert result["3"]["key"] == "fan_speed"


# ---------------------------------------------------------------------------
# auto_map_dps — unknown device type
# ---------------------------------------------------------------------------


class TestAutoMapUnknown:
    def test_unknown_type_returns_empty(self) -> None:
        assert auto_map_dps("fridge", {"1": True}) == {}


# ---------------------------------------------------------------------------
# merge_dp_maps
# ---------------------------------------------------------------------------


class TestMergeDpMaps:
    def test_user_overrides_auto(self) -> None:
        user = {"1": {"key": "power", "type": "bool"}}
        auto = {"1": {"key": "power", "type": "bool"}, "2": {"key": "brightness", "type": "int"}}
        merged = merge_dp_maps(user, auto)
        # User's DP 1 definition should win
        assert merged["1"] == user["1"]
        # Auto's DP 2 should be added
        assert "2" in merged

    def test_empty_user_map(self) -> None:
        auto = {"1": {"key": "power", "type": "bool"}}
        assert merge_dp_maps({}, auto) == auto

    def test_empty_auto_map(self) -> None:
        user = {"1": {"key": "power", "type": "bool"}}
        assert merge_dp_maps(user, {}) == user

    def test_both_empty(self) -> None:
        assert merge_dp_maps({}, {}) == {}

    def test_no_mutation(self) -> None:
        user = {"1": {"key": "power"}}
        auto = {"2": {"key": "brightness"}}
        user_copy = dict(user)
        auto_copy = dict(auto)
        merge_dp_maps(user, auto)
        assert user == user_copy
        assert auto == auto_copy
