"""Data-driven device family profiles for Conti.

Each profile describes a *family* of devices (e.g. "CCT light", "4-gang
switch") with its expected DP layout.  Profiles are matched during
onboarding — either from a Tuya cloud product category or from local
heuristics — to produce a ready-to-use ``dp_map`` without manual JSON.

The profile system is **onboarding-only**.  At runtime, the saved
``dp_map`` in ``entry.data`` is the single source of truth.

Adding support for a new device family means adding ONE dict to
``DEVICE_PROFILES`` — no code changes anywhere else.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from .const import (
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_FAN,
    DEVICE_TYPE_LIGHT,
    DEVICE_TYPE_SENSOR,
    DEVICE_TYPE_SWITCH,
    DP_KEY_BATTERY,
    DP_KEY_BRIGHTNESS,
    DP_KEY_COLOR_RGB,
    DP_KEY_COLOR_TEMP,
    DP_KEY_CONTACT,
    DP_KEY_DOOR_STATE,
    DP_KEY_CURRENT_TEMP,
    DP_KEY_FAN_DIRECTION,
    DP_KEY_FAN_MODE,
    DP_KEY_FAN_OSCILLATION,
    DP_KEY_FAN_SPEED,
    DP_KEY_HUMIDITY,
    DP_KEY_HVAC_MODE,
    DP_KEY_MOTION,
    DP_KEY_POWER,
    DP_KEY_POWER_USAGE,
    DP_KEY_TARGET_TEMP,
    DP_KEY_TEMPERATURE,
)

_LOGGER = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Profile definition
# ──────────────────────────────────────────────────────────────────────
# Each profile is a dict with:
#   id          — Unique stable identifier (never changes)
#   name        — Human-readable label for the UI
#   device_type — Maps to entity platform ("light", "switch", …)
#   tuya_categories — Tuya product category codes that auto-select this
#                     profile (e.g. "dj" for lights).  Optional.
#   dp_template — Dict of {dp_id: {key, type, …}} used as the dp_map
#                 when this profile is selected.  DP ids may be strings
#                 or placeholders resolved from discovered DPS.
#   dp_hints    — Maps Tuya DP *code names* (e.g. "switch_led") to
#                 Conti DP keys.  Used by cloud-schema mapping.

# ──────────────────────────────────────────────────────────────────────
# Tuya code → Conti key translation table
# ──────────────────────────────────────────────────────────────────────
# Used by cloud_schema.py to convert Tuya DP code names into Conti keys.
TUYA_CODE_TO_CONTI_KEY: Final[dict[str, str]] = {
    # ── Lights ──
    "switch_led": DP_KEY_POWER,
    "switch_led_1": DP_KEY_POWER,
    "led_switch": DP_KEY_POWER,
    "bright_value": DP_KEY_BRIGHTNESS,
    "bright_value_1": DP_KEY_BRIGHTNESS,
    "bright_value_v2": DP_KEY_BRIGHTNESS,
    "temp_value": DP_KEY_COLOR_TEMP,
    "temp_value_v2": DP_KEY_COLOR_TEMP,
    "colour_data": DP_KEY_COLOR_RGB,
    "colour_data_v2": DP_KEY_COLOR_RGB,
    "work_mode": "mode",
    # ── Switches / sockets ──
    "switch_1": DP_KEY_POWER,
    "switch_2": "switch_2",
    "switch_3": "switch_3",
    "switch_4": "switch_4",
    "switch_5": "switch_5",
    "switch_6": "switch_6",
    "switch": DP_KEY_POWER,
    "switch_usb1": "switch_usb1",
    "switch_usb2": "switch_usb2",
    "countdown_1": "countdown",
    "countdown_2": "countdown_2",
    "countdown_3": "countdown_3",
    "countdown_4": "countdown_4",
    "add_ele": "energy_total",
    "fault": "fault",
    "relay_status": "relay_status",
    "overcharge_switch": "overcharge_switch",
    "light_mode": "light_mode",
    "child_lock": "child_lock",
    "cycle_time": "cycle_time",
    "random_time": "random_time",
    "switch_inching": "switch_inching",
    # ── Fans ──
    "fan_switch": DP_KEY_POWER,
    "switch_fan": DP_KEY_POWER,
    "fan_speed": DP_KEY_FAN_SPEED,
    "fan_speed_percent": DP_KEY_FAN_SPEED,
    "fan_direction": DP_KEY_FAN_DIRECTION,
    "switch_horizontal": DP_KEY_FAN_OSCILLATION,
    "switch_vertical": "fan_oscillation_v",
    "fan_speed_enum": DP_KEY_FAN_SPEED,
    # ── Climate ──
    "switch_climate": DP_KEY_POWER,
    "temp_set": DP_KEY_TARGET_TEMP,
    "temp_current": DP_KEY_CURRENT_TEMP,
    "mode": DP_KEY_HVAC_MODE,
    "windspeed": DP_KEY_FAN_MODE,
    # ── Sensors ──
    "temp_current_f": DP_KEY_TEMPERATURE,
    "va_temperature": DP_KEY_TEMPERATURE,
    "va_humidity": DP_KEY_HUMIDITY,
    "battery_percentage": DP_KEY_BATTERY,
    "battery_state": DP_KEY_BATTERY,
    "cur_power": DP_KEY_POWER_USAGE,
    "cur_current": "current",
    "cur_voltage": "voltage",
    "pir": DP_KEY_MOTION,
    "doorcontact_state": DP_KEY_DOOR_STATE,
    "alarm_switch": "alarm_switch",
    "arming_switch": "arming_switch",
    "delay_alarm": "delay_alarm",
    "time_alarm": "time_alarm",
    "alarm_volume_value": "alarm_volume",
}

# Type that maps Tuya code → expected Python type string
TUYA_TYPE_MAP: Final[dict[str, str]] = {
    "Boolean": "bool",
    "Integer": "int",
    "Enum": "str",
    "String": "str",
    "Json": "str",
    "Raw": "str",
}

# ──────────────────────────────────────────────────────────────────────
# Device family profiles
# ──────────────────────────────────────────────────────────────────────

DEVICE_PROFILES: Final[list[dict[str, Any]]] = [
    # ── White / CCT Light ──
    {
        "id": "light_cct",
        "name": "White / CCT Light",
        "device_type": DEVICE_TYPE_LIGHT,
        "tuya_categories": ["dj"],
        "dp_template": {
            "20": {"key": DP_KEY_POWER, "type": "bool"},
            "22": {"key": DP_KEY_BRIGHTNESS, "type": "int", "min": 10, "max": 1000},
            "23": {"key": DP_KEY_COLOR_TEMP, "type": "int", "min": 0, "max": 1000},
        },
    },
    # ── RGB + CCT Light ──
    {
        "id": "light_rgb_cct",
        "name": "RGB + CCT Light",
        "device_type": DEVICE_TYPE_LIGHT,
        "tuya_categories": ["dj"],
        "dp_template": {
            "20": {"key": DP_KEY_POWER, "type": "bool"},
            "21": {"key": "mode", "type": "str"},
            "22": {"key": DP_KEY_BRIGHTNESS, "type": "int", "min": 10, "max": 1000},
            "23": {"key": DP_KEY_COLOR_TEMP, "type": "int", "min": 0, "max": 1000},
            "24": {"key": DP_KEY_COLOR_RGB, "type": "str"},
        },
    },
    # ── RGB Strip ──
    {
        "id": "light_rgb_strip",
        "name": "RGB Strip Light",
        "device_type": DEVICE_TYPE_LIGHT,
        "tuya_categories": ["dd"],
        "dp_template": {
            "20": {"key": DP_KEY_POWER, "type": "bool"},
            "21": {"key": "mode", "type": "str"},
            "22": {"key": DP_KEY_BRIGHTNESS, "type": "int", "min": 10, "max": 1000},
            "24": {"key": DP_KEY_COLOR_RGB, "type": "str"},
        },
    },
    # ── White Strip ──
    {
        "id": "light_white_strip",
        "name": "White Strip Light",
        "device_type": DEVICE_TYPE_LIGHT,
        "tuya_categories": ["dd"],
        "dp_template": {
            "20": {"key": DP_KEY_POWER, "type": "bool"},
            "21": {"key": "mode", "type": "str"},
            "22": {"key": DP_KEY_BRIGHTNESS, "type": "int", "min": 10, "max": 1000},
            "23": {"key": DP_KEY_COLOR_TEMP, "type": "int", "min": 0, "max": 1000},
        },
    },
    # ── Dimmer (standard) ──
    {
        "id": "light_dimmer",
        "name": "Dimmer Light",
        "device_type": DEVICE_TYPE_LIGHT,
        "tuya_categories": ["dj", "tgq"],
        "dp_template": {
            "1": {"key": DP_KEY_POWER, "type": "bool"},
            "2": {"key": DP_KEY_BRIGHTNESS, "type": "int", "min": 10, "max": 1000},
        },
    },
    # ── Dimmer (TRIAC / leading-edge) ──
    {
        "id": "light_triac_dimmer",
        "name": "TRIAC Dimmer",
        "device_type": DEVICE_TYPE_LIGHT,
        "tuya_categories": ["tgq"],
        "dp_template": {
            "20": {"key": DP_KEY_POWER, "type": "bool"},
            "22": {"key": DP_KEY_BRIGHTNESS, "type": "int", "min": 10, "max": 1000},
        },
    },
    # ── Single-gang Switch ──
    {
        "id": "switch_1gang",
        "name": "Single Switch",
        "device_type": DEVICE_TYPE_SWITCH,
        "tuya_categories": ["kg", "cz", "pc"],
        "dp_template": {
            "1": {"key": DP_KEY_POWER, "type": "bool"},
        },
    },
    # ── 2-gang Switch ──
    {
        "id": "switch_2gang",
        "name": "2-Gang Switch",
        "device_type": DEVICE_TYPE_SWITCH,
        "tuya_categories": ["kg"],
        "dp_template": {
            "1": {"key": DP_KEY_POWER, "type": "bool"},
            "2": {"key": DP_KEY_POWER, "type": "bool"},
        },
    },
    # ── 3-gang Switch ──
    {
        "id": "switch_3gang",
        "name": "3-Gang Switch",
        "device_type": DEVICE_TYPE_SWITCH,
        "tuya_categories": ["kg"],
        "dp_template": {
            "1": {"key": DP_KEY_POWER, "type": "bool"},
            "2": {"key": DP_KEY_POWER, "type": "bool"},
            "3": {"key": DP_KEY_POWER, "type": "bool"},
        },
    },
    # ── 4-gang Switch ──
    {
        "id": "switch_4gang",
        "name": "4-Gang Switch",
        "device_type": DEVICE_TYPE_SWITCH,
        "tuya_categories": ["kg"],
        "dp_template": {
            "1": {"key": "switch_1", "type": "bool"},
            "2": {"key": "switch_2", "type": "bool"},
            "3": {"key": "switch_3", "type": "bool"},
            "4": {"key": "switch_4", "type": "bool"},
        },
    },
    # ── Power Strip / Extension Board ──
    {
        "id": "switch_powerstrip",
        "name": "Power Strip / Extension Board",
        "device_type": DEVICE_TYPE_SWITCH,
        "tuya_categories": ["cz", "pc"],
        "dp_template": {
            "1": {"key": DP_KEY_POWER, "type": "bool"},
            "2": {"key": DP_KEY_POWER, "type": "bool"},
            "3": {"key": DP_KEY_POWER, "type": "bool"},
            "4": {"key": DP_KEY_POWER, "type": "bool"},
            "5": {"key": DP_KEY_POWER, "type": "bool"},
        },
    },
    # ── Fan ──
    {
        "id": "fan_basic",
        "name": "Fan",
        "device_type": DEVICE_TYPE_FAN,
        "tuya_categories": ["fs"],
        "dp_template": {
            "1": {"key": DP_KEY_POWER, "type": "bool"},
            "3": {"key": DP_KEY_FAN_SPEED, "type": "int", "min": 1, "max": 6},
            "4": {"key": DP_KEY_FAN_DIRECTION, "type": "str"},
            "8": {"key": DP_KEY_FAN_OSCILLATION, "type": "bool"},
        },
    },
    # ── Climate / AC ──
    {
        "id": "climate_ac",
        "name": "Climate / Air Conditioner",
        "device_type": DEVICE_TYPE_CLIMATE,
        "tuya_categories": ["kt", "wk"],
        "dp_template": {
            "1": {"key": DP_KEY_POWER, "type": "bool"},
            "2": {"key": DP_KEY_TARGET_TEMP, "type": "int", "min": 16, "max": 30, "scale": 1},
            "3": {"key": DP_KEY_CURRENT_TEMP, "type": "int", "min": 0, "max": 50, "scale": 1},
            "4": {"key": DP_KEY_HVAC_MODE, "type": "str"},
            "5": {"key": DP_KEY_FAN_MODE, "type": "str"},
        },
    },
    # ── Temperature + Humidity Sensor ──
    {
        "id": "sensor_temp_humidity",
        "name": "Temperature & Humidity Sensor",
        "device_type": DEVICE_TYPE_SENSOR,
        "tuya_categories": ["wsdcg"],
        "dp_template": {
            "1": {"key": DP_KEY_TEMPERATURE, "type": "int", "scale": 10},
            "2": {"key": DP_KEY_HUMIDITY, "type": "int"},
            "3": {"key": DP_KEY_BATTERY, "type": "int"},
        },
    },
    # ── Power Monitoring Plug ──
    {
        "id": "sensor_power_plug",
        "name": "Power Monitoring Plug",
        "device_type": DEVICE_TYPE_SWITCH,
        "tuya_categories": ["cz"],
        "dp_template": {
            "1": {"key": DP_KEY_POWER, "type": "bool"},
            "9": {"key": "countdown", "type": "int"},
            "17": {"key": "energy_total", "type": "int"},
            "18": {"key": "current", "type": "int"},
            "19": {"key": DP_KEY_POWER_USAGE, "type": "int"},
            "20": {"key": "voltage", "type": "int"},
            "26": {"key": "fault", "type": "int"},
            "38": {"key": "relay_status", "type": "str"},
            "39": {"key": "overcharge_switch", "type": "bool"},
            "40": {"key": "light_mode", "type": "str"},
            "41": {"key": "child_lock", "type": "bool"},
            "42": {"key": "cycle_time", "type": "str"},
            "43": {"key": "random_time", "type": "str"},
            "44": {"key": "switch_inching", "type": "str"},
        },
    },
    # ── Motion Sensor ──
    {
        "id": "sensor_motion",
        "name": "Motion Sensor",
        "device_type": DEVICE_TYPE_SENSOR,
        "tuya_categories": ["pir"],
        "dp_template": {
            "101": {"key": DP_KEY_MOTION, "type": "bool"},
            "3": {"key": DP_KEY_BATTERY, "type": "int"},
        },
    },
    # ── Contact Sensor ──
    {
        "id": "sensor_contact",
        "name": "Contact / Door Sensor",
        "device_type": DEVICE_TYPE_SENSOR,
        "tuya_categories": ["mcs"],
        "dp_template": {
            "1": {"key": DP_KEY_DOOR_STATE, "type": "bool"},
            "2": {"key": DP_KEY_BATTERY, "type": "int"},
        },
    },
    # ── Contact Sensor (alarm-capable family) ──
    {
        "id": "sensor_contact_alarm",
        "name": "Contact / Door Sensor (Alarm-capable)",
        "device_type": DEVICE_TYPE_SENSOR,
        "tuya_categories": ["mcs"],
        "dp_template": {
            "1": {"key": DP_KEY_DOOR_STATE, "type": "bool"},
            "2": {"key": DP_KEY_BATTERY, "type": "int"},
            "101": {"key": "alarm_switch", "type": "bool"},
            "102": {"key": "arming_switch", "type": "bool"},
            "103": {"key": "delay_alarm", "type": "int"},
            "104": {"key": "time_alarm", "type": "int"},
            "105": {"key": "alarm_volume", "type": "int"},
        },
    },
]

# Fast lookup by profile ID
_PROFILE_BY_ID: Final[dict[str, dict[str, Any]]] = {
    p["id"]: p for p in DEVICE_PROFILES
}


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


def get_profile(profile_id: str) -> dict[str, Any] | None:
    """Return a profile by its unique ID, or ``None``."""
    return _PROFILE_BY_ID.get(profile_id)


def list_profiles() -> list[dict[str, Any]]:
    """Return all available profiles (for UI selection)."""
    return list(DEVICE_PROFILES)


def profiles_for_device_type(device_type: str) -> list[dict[str, Any]]:
    """Return profiles that match a given device type."""
    return [p for p in DEVICE_PROFILES if p["device_type"] == device_type]


def profile_choices() -> dict[str, str]:
    """Return ``{profile_id: display_name}`` for use in a selector."""
    result: dict[str, str] = {"auto": "Auto-detect (recommended)"}
    for p in DEVICE_PROFILES:
        result[p["id"]] = p["name"]
    return result


def match_profile_by_category(category: str) -> list[dict[str, Any]]:
    """Return profiles whose ``tuya_categories`` include *category*."""
    cat_lower = category.lower()
    return [
        p for p in DEVICE_PROFILES
        if cat_lower in [c.lower() for c in p.get("tuya_categories", [])]
    ]


def score_profile_against_dps(
    profile: dict[str, Any],
    discovered_dps: dict[str, Any],
) -> float:
    """Score how well a profile's dp_template matches discovered DPS.

    Returns a float 0.0–1.0+.  Uses a weighted combination of:
    - Template completeness: what fraction of the profile's DPs were found
    - Device coverage: what fraction of the device's DPs are explained
    - Tiebreaker: absolute match count to prefer more specific profiles

    This ensures a CCT profile (3 matches, 3/3 template, 3/4 coverage)
    beats a dimmer profile (2 matches, 2/2 template, 2/4 coverage) for
    a device that has brightness + color_temp DPs.
    """
    template = profile.get("dp_template", {})
    if not template:
        return 0.0

    matches = 0
    for dp_id, spec in template.items():
        if dp_id not in discovered_dps:
            continue
        val = discovered_dps[dp_id]
        expected = spec.get("type", "")
        actual = _classify_value(val)
        if actual == expected:
            matches += 1

    if matches == 0:
        return 0.0

    template_score = matches / len(template)

    device_dp_count = len(discovered_dps)
    coverage = matches / device_dp_count if device_dp_count > 0 else 0.0

    base_score = template_score * 0.5 + coverage * 0.5 + matches * 0.001

    # If the discovered DPS strongly indicates warm/cool capability,
    # prefer light profiles that include a color_temp role.
    if profile.get("device_type") == DEVICE_TYPE_LIGHT:
        discovered_ints = {
            dp_id for dp_id, val in discovered_dps.items()
            if isinstance(val, (int, float)) and not isinstance(val, bool)
        }
        cct_signal = "23" in discovered_ints or (
            len(discovered_ints) >= 2
            and ("1" in discovered_dps or "20" in discovered_dps)
        )
        has_color_temp = any(
            isinstance(spec, dict) and spec.get("key") == DP_KEY_COLOR_TEMP
            for spec in template.values()
        )
        if cct_signal and has_color_temp:
            base_score += 0.08
        elif cct_signal and not has_color_temp:
            base_score -= 0.08

    # Power-monitoring smart plug signature (switch category family):
    # DP 1 bool relay + telemetry-style integer DPs (commonly 17/19/20).
    # This helps prefer the dedicated power-plug profile over a generic
    # single-switch profile when strong evidence exists.
    if profile.get("id") == "sensor_power_plug":
        has_relay = isinstance(discovered_dps.get("1"), bool)
        has_monitor = any(
            isinstance(discovered_dps.get(dp), (int, float))
            and not isinstance(discovered_dps.get(dp), bool)
            for dp in ("17", "19", "20")
        )
        if has_relay and has_monitor:
            base_score += 0.12

    # 4-gang wall-switch family signature:
    # relays on 1..4 + countdowns on 7..10 + advanced control DPs.
    if profile.get("id") == "switch_4gang":
        has_relays_1_4 = all(
            isinstance(discovered_dps.get(dp), bool)
            for dp in ("1", "2", "3", "4")
        )
        has_countdown_7_10 = all(
            isinstance(discovered_dps.get(dp), (int, float))
            and not isinstance(discovered_dps.get(dp), bool)
            for dp in ("7", "8", "9", "10")
        )
        advanced_hits = sum(
            1
            for dp in ("14", "17", "18", "19", "47")
            if isinstance(discovered_dps.get(dp), str)
        )
        if has_relays_1_4 and has_countdown_7_10 and advanced_hits >= 2:
            base_score += 0.12

    # Contact/alarm sensor signature:
    # DP 1 bool door state + DP 2 integer battery + optional alarm DPs.
    if profile.get("id") == "sensor_contact_alarm":
        has_main = isinstance(discovered_dps.get("1"), bool)
        has_battery = (
            isinstance(discovered_dps.get("2"), (int, float))
            and not isinstance(discovered_dps.get("2"), bool)
        )
        alarm_hits = 0
        for dp, expected in {
            "101": "bool",
            "102": "bool",
            "103": "int",
            "104": "int",
            "105": "int",
        }.items():
            val = discovered_dps.get(dp)
            if val is None:
                continue
            if _classify_value(val) == expected:
                alarm_hits += 1

        if has_main and has_battery and alarm_hits >= 2:
            base_score += 0.14

    return max(base_score, 0.0)


def best_profile_for_dps(
    discovered_dps: dict[str, Any],
    device_type: str | None = None,
    tuya_category: str | None = None,
) -> tuple[dict[str, Any] | None, float]:
    """Find the best-matching profile for discovered DPS.

    Narrows candidates by *device_type* and/or *tuya_category* if given.
    Returns ``(best_profile, confidence_score)`` or ``(None, 0.0)``.
    """
    candidates = list(DEVICE_PROFILES)

    # Narrow by category first (strongest signal)
    if tuya_category:
        cat_matches = match_profile_by_category(tuya_category)
        if cat_matches:
            candidates = cat_matches

    # Then narrow by device type
    if device_type:
        type_matches = [p for p in candidates if p["device_type"] == device_type]
        if type_matches:
            candidates = type_matches

    best: dict[str, Any] | None = None
    best_score: float = 0.0

    for profile in candidates:
        score = score_profile_against_dps(profile, discovered_dps)
        if score > best_score:
            best_score = score
            best = profile

    if best:
        _LOGGER.debug(
            "Best profile match: '%s' (score=%.2f, %d candidates)",
            best["id"],
            best_score,
            len(candidates),
        )

    return best, best_score


def dp_map_from_profile(
    profile: dict[str, Any],
    discovered_dps: dict[str, Any] | None = None,
    confidence: float = 0.0,
) -> dict[str, dict[str, Any]]:
    """Generate a dp_map from a profile template.

    If *confidence* >= 0.5, include ALL template DPs regardless of
    whether they were discovered.  The profile defines what the device
    SHOULD support; missing DPs during discovery is common when the
    device was off or in a partial-report state.

    If confidence is low or no *discovered_dps* is provided, only
    include DPs confirmed by discovery.
    """
    template = profile.get("dp_template", {})
    if not discovered_dps:
        return {dp_id: dict(spec) for dp_id, spec in template.items()}

    # High confidence: trust the profile, include all template DPs
    if confidence >= 0.5:
        return {dp_id: dict(spec) for dp_id, spec in template.items()}

    # Low confidence: only include DPs confirmed by discovery
    result: dict[str, dict[str, Any]] = {}
    for dp_id, spec in template.items():
        if dp_id in discovered_dps:
            result[dp_id] = dict(spec)
    return result


# ──────────────────────────────────────────────────────────────────────
# Internal helper
# ──────────────────────────────────────────────────────────────────────


def _classify_value(value: Any) -> str:
    """Classify a runtime DP value into ``bool`` / ``int`` / ``str``."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "int"
    return "str"
