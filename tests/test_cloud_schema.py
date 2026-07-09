"""Tests for Tuya cloud schema DP mapping."""

from __future__ import annotations

from custom_components.conti.cloud_schema import TuyaCloudSchemaHelper


def test_optional_light_dps_are_ignored_without_protocol_error() -> None:
    schema = {
        "category": "dd",
        "functions": [
            {"dp_id": 20, "code": "switch_led", "type": "Boolean"},
            {"dp_id": 21, "code": "work_mode", "type": "Enum"},
            {
                "dp_id": 22,
                "code": "bright_value",
                "type": "Integer",
                "values": '{"min": 10, "max": 1000}',
            },
            {
                "dp_id": 23,
                "code": "temp_value",
                "type": "Integer",
                "values": '{"min": 10, "max": 1000}',
            },
            {"dp_id": 25, "code": "scene_data", "type": "String"},
            {"dp_id": 26, "code": "countdown", "type": "Integer"},
            {"dp_id": 27, "code": "music_data", "type": "String"},
            {"dp_id": 28, "code": "control_data", "type": "String"},
            {"dp_id": 29, "code": "debug_data", "type": "String"},
        ],
        "status": [],
    }

    dp_map, category, device_type = TuyaCloudSchemaHelper.schema_to_dp_map(schema)

    assert category == "dd"
    assert device_type == "light"
    assert {spec["key"] for spec in dp_map.values()} == {
        "power",
        "mode",
        "brightness",
        "color_temp",
    }
    assert set(dp_map) == {"20", "21", "22", "23"}


def test_curtain_schema_maps_category_and_standard_codes() -> None:
    schema = {
        "category": "cl",
        "functions": [
            {
                "dp_id": 15,
                "code": "control",
                "type": "Enum",
                "values": '{"range": ["open", "stop", "close"]}',
            },
            {
                "dp_id": 44,
                "code": "percent_control",
                "type": "Integer",
                "values": '{"min": 0, "max": 100}',
            },
        ],
        "status": [
            {
                "dp_id": 45,
                "code": "percent_state",
                "type": "Integer",
                "values": '{"min": 0, "max": 100}',
            },
            {"dp_id": 46, "code": "work_state", "type": "Enum"},
        ],
    }

    dp_map, category, device_type = TuyaCloudSchemaHelper.schema_to_dp_map(schema)

    assert category == "cl"
    assert device_type == "curtain"
    assert dp_map == {
        "15": {
            "key": "control",
            "type": "str",
            "code": "control",
            "values": ["open", "stop", "close"],
        },
        "44": {
            "key": "position",
            "type": "int",
            "code": "percent_control",
            "min": 0,
            "max": 100,
        },
        "45": {
            "key": "current_position",
            "type": "int",
            "code": "percent_state",
            "read_only": True,
            "min": 0,
            "max": 100,
        },
        "46": {
            "key": "work_state",
            "type": "str",
            "code": "work_state",
            "read_only": True,
        },
    }


def test_curtain_switch_category_maps_to_curtain() -> None:
    _, category, device_type = TuyaCloudSchemaHelper.schema_to_dp_map(
        {"category": "clkg", "functions": [], "status": []}
    )

    assert category == "clkg"
    assert device_type == "curtain"
