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
