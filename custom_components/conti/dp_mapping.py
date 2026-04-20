"""Auto DP discovery heuristics and DP map utilities for Conti.

Provides:
* ``auto_map_dps()`` — heuristically map discovered DPs to entity roles
  based on device type, DP id conventions, and value types.
* ``merge_dp_maps()`` — merge a user-supplied dp_map with an auto-generated
  one (user entries always win).
* ``mask_key()`` — redact a local key for safe logging.
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
    DP_KEY_CURRENT,
    DP_KEY_DOOR_STATE,
    DP_KEY_CURRENT_TEMP,
    DP_KEY_ENERGY_TOTAL,
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
    DP_KEY_VOLTAGE,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type alias for a single heuristic rule.
#   (candidate_dp_ids, role_key, expected_value_type, multi_entity)
# ---------------------------------------------------------------------------
_HeuristicRule = tuple[list[str], str, str, bool]

# ---------------------------------------------------------------------------
# Heuristic tables — one per device type.
#
# ``multi_entity=True`` means every matching DP creates its own entity
# (e.g., multi-gang switches).  ``False`` means the first match wins.
# ---------------------------------------------------------------------------

LIGHT_HEURISTICS: Final[list[_HeuristicRule]] = [
    (["1", "20"],            DP_KEY_POWER,      "bool", False),
    (["21"],                 "mode",            "str",  False),
    (["2", "22"],            DP_KEY_BRIGHTNESS,  "int",  False),
    (["3", "23"],            DP_KEY_COLOR_TEMP,  "int",  False),
    (["5", "24"],            DP_KEY_COLOR_RGB,   "str",  False),
]

SWITCH_HEURISTICS: Final[list[_HeuristicRule]] = [
    # Every bool DP on a switch device is assumed to be a relay.
    (
        ["1", "2", "3", "4", "5", "6", "7",
         "20", "21", "22", "23", "24", "25"],
        DP_KEY_POWER, "bool", True,
    ),
    # Power-monitoring plugs commonly expose active-power/energy style DPs
    # on 19/17. Map one strong match as sensor-capable telemetry.
    (["19", "17"], DP_KEY_POWER_USAGE, "int", False),
    # Energy monitoring DPs — present on plugs and some multi-gang switches.
    (["17"],  DP_KEY_ENERGY_TOTAL, "int", False),
    (["18"],  DP_KEY_CURRENT,      "int", False),
    (["20"],  DP_KEY_VOLTAGE,      "int", False),
]

FAN_HEURISTICS: Final[list[_HeuristicRule]] = [
    (["1", "20"],  DP_KEY_POWER,          "bool", False),
    (["3", "4"],   DP_KEY_FAN_SPEED,      "int",  False),
    (["4", "8"],   DP_KEY_FAN_DIRECTION,  "str",  False),
    (["8", "104"], DP_KEY_FAN_OSCILLATION, "bool", False),
]

CLIMATE_HEURISTICS: Final[list[_HeuristicRule]] = [
    (["1", "20"],  DP_KEY_POWER,        "bool", False),
    (["2"],        DP_KEY_TARGET_TEMP,   "int",  False),
    (["3"],        DP_KEY_CURRENT_TEMP,  "int",  False),
    (["4"],        DP_KEY_HVAC_MODE,     "str",  False),
    (["5"],        DP_KEY_FAN_MODE,      "str",  False),
]

SENSOR_HEURISTICS: Final[list[_HeuristicRule]] = [
    (["1", "18"],         DP_KEY_TEMPERATURE,  "int",  False),
    (["2", "19"],         DP_KEY_HUMIDITY,      "int",  False),
    (["3"],               DP_KEY_BATTERY,       "int",  False),
    (["9", "10"],         DP_KEY_POWER_USAGE,   "int",  False),
    (["101", "102", "103"], DP_KEY_MOTION,      "bool", False),
    (["101", "102"],      DP_KEY_CONTACT,       "bool", False),
]

_HEURISTIC_MAP: Final[dict[str, list[_HeuristicRule]]] = {
    DEVICE_TYPE_LIGHT:   LIGHT_HEURISTICS,
    DEVICE_TYPE_SWITCH:  SWITCH_HEURISTICS,
    DEVICE_TYPE_FAN:     FAN_HEURISTICS,
    DEVICE_TYPE_CLIMATE: CLIMATE_HEURISTICS,
    DEVICE_TYPE_SENSOR:  SENSOR_HEURISTICS,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify_value(value: Any) -> str:
    """Classify a runtime DP value into ``bool`` / ``int`` / ``str``."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "int"
    return "str"


def mask_key(key: str) -> str:
    """Redact a local key for safe logging — show first 2 + last 2 chars."""
    if len(key) <= 4:
        return "****"
    return key[:2] + "*" * (len(key) - 4) + key[-2:]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def auto_map_dps(
    device_type: str,
    discovered_dps: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Heuristically map *discovered_dps* to entity roles.

    Parameters
    ----------
    device_type:
        One of the ``DEVICE_TYPE_*`` constants (``"light"``, ``"switch"``, …).
    discovered_dps:
        Dict of ``{"dp_id": value, …}`` as returned by
        ``TuyaDeviceClient.detect_dps()``.

    Returns
    -------
    dict[str, dict[str, Any]]
        A dp_map compatible with the entity platforms, e.g.
        ``{"1": {"key": "power", "type": "bool"}, …}``.
    """
    rules = _HEURISTIC_MAP.get(device_type, [])
    if not rules:
        _LOGGER.debug("No DP heuristics defined for device type '%s'", device_type)
        return {}

    result: dict[str, dict[str, Any]] = {}
    assigned_roles: set[str] = set()

    for candidates, role, expected_type, multi in rules:
        for dp_id in candidates:
            if dp_id not in discovered_dps:
                continue

            actual_type = _classify_value(discovered_dps[dp_id])
            if actual_type != expected_type:
                _LOGGER.debug(
                    "DP %s skipped for role '%s': expected %s, got %s (val=%r)",
                    dp_id, role, expected_type, actual_type, discovered_dps[dp_id],
                )
                continue

            if not multi and role in assigned_roles:
                continue

            entry: dict[str, Any] = {"key": role, "type": actual_type}

            # Provide sensible default ranges for integer DPs.
            if actual_type == "int":
                if role in (DP_KEY_BRIGHTNESS, DP_KEY_COLOR_TEMP):
                    entry["min"] = 10
                    entry["max"] = 1000
                elif role in (DP_KEY_TARGET_TEMP, DP_KEY_CURRENT_TEMP):
                    entry["min"] = 0
                    entry["max"] = 50
                    entry["scale"] = 1

            result[dp_id] = entry
            if not multi:
                assigned_roles.add(role)

            _LOGGER.debug(
                "Auto-mapped DP %s → role '%s' (type=%s, value=%r)",
                dp_id, role, actual_type, discovered_dps[dp_id],
            )

    # Signature-based enrichment for power-monitoring smart plugs.
    # This keeps generic switch mapping conservative, and only adds
    # richer known DPs when the observed DP shape strongly matches.
    if device_type == DEVICE_TYPE_SWITCH:
        _augment_power_monitoring_plug_map(result, discovered_dps)
        _augment_multi_gang_switch_family_map(result, discovered_dps)
    elif device_type == DEVICE_TYPE_SENSOR:
        _augment_contact_alarm_sensor_family_map(result, discovered_dps)

    # Report unmapped DPs so the user knows what was ignored.
    unmapped = set(discovered_dps.keys()) - set(result.keys())
    if unmapped:
        _LOGGER.info(
            "Auto-mapping (%s): %d DP(s) unmapped: %s  values=%s",
            device_type,
            len(unmapped),
            sorted(unmapped),
            {k: discovered_dps[k] for k in sorted(unmapped)},
        )

    return result


def _augment_power_monitoring_plug_map(
    result: dict[str, dict[str, Any]],
    discovered_dps: dict[str, Any],
) -> None:
    """Add richer DP mappings for known power-monitoring smart-plug layouts.

    Applied only when there is strong evidence this is a monitoring plug,
    not a generic switch:
      * DP 1 is a bool relay
      * at least two telemetry DPs among 17/18/19/20 are numeric
      * plus at least one advanced-control DP among 26/38/39/40/41/42/43/44
    """
    if not isinstance(discovered_dps.get("1"), bool):
        return

    telemetry_ids = ("17", "18", "19", "20")
    telemetry_hits = sum(
        1
        for dp_id in telemetry_ids
        if isinstance(discovered_dps.get(dp_id), (int, float))
        and not isinstance(discovered_dps.get(dp_id), bool)
    )
    if telemetry_hits < 2:
        return

    advanced_hits = 0
    if isinstance(discovered_dps.get("26"), (int, float)) and not isinstance(discovered_dps.get("26"), bool):
        advanced_hits += 1
    if isinstance(discovered_dps.get("38"), str):
        advanced_hits += 1
    if isinstance(discovered_dps.get("39"), bool):
        advanced_hits += 1
    if isinstance(discovered_dps.get("40"), str):
        advanced_hits += 1
    if isinstance(discovered_dps.get("41"), bool):
        advanced_hits += 1
    if isinstance(discovered_dps.get("42"), str):
        advanced_hits += 1
    if isinstance(discovered_dps.get("43"), str):
        advanced_hits += 1
    if isinstance(discovered_dps.get("44"), str):
        advanced_hits += 1
    if advanced_hits < 1:
        return

    rich_map: dict[str, dict[str, Any]] = {
        "1": {"key": DP_KEY_POWER, "type": "bool"},
        "9": {"key": "countdown", "type": "int"},
        "17": {"key": DP_KEY_ENERGY_TOTAL, "type": "int", "scale": 100},
        "18": {"key": DP_KEY_CURRENT, "type": "int"},
        "19": {"key": DP_KEY_POWER_USAGE, "type": "int", "scale": 10},
        "20": {"key": DP_KEY_VOLTAGE, "type": "int", "scale": 10},
        "26": {"key": "fault", "type": "int"},
        "38": {"key": "relay_status", "type": "str"},
        "39": {"key": "overcharge_switch", "type": "bool"},
        "40": {"key": "light_mode", "type": "str"},
        "41": {"key": "child_lock", "type": "bool"},
        "42": {"key": "cycle_time", "type": "str"},
        "43": {"key": "random_time", "type": "str"},
        "44": {"key": "switch_inching", "type": "str"},
    }

    added: list[str] = []
    for dp_id, spec in rich_map.items():
        if dp_id not in discovered_dps:
            continue
        actual_type = _classify_value(discovered_dps[dp_id])
        if actual_type != spec["type"]:
            continue
        if dp_id not in result:
            result[dp_id] = dict(spec)
            added.append(dp_id)

    if added:
        _LOGGER.info(
            "Auto-mapping (%s): enriched power-monitoring plug DPs: %s",
            DEVICE_TYPE_SWITCH,
            sorted(added),
        )


def _augment_multi_gang_switch_family_map(
    result: dict[str, dict[str, Any]],
    discovered_dps: dict[str, Any],
) -> None:
    """Give distinct ``switch_N`` key names to multi-gang relay DPs.

    Applied when **two or more** relay-class bool DPs (1–7) are present.
    Also enriches countdown and advanced DPs when the signature matches.

    This replaces the generic ``"power"`` key with ``"switch_1"``,
    ``"switch_2"``, etc. so that Home Assistant shows distinct entity names.
    """
    relay_ids = ["1", "2", "3", "4", "5", "6", "7"]
    found_relays = [
        dp_id for dp_id in relay_ids
        if isinstance(discovered_dps.get(dp_id), bool)
    ]

    if len(found_relays) < 2:
        return  # Not multi-gang

    # Countdown DPs paired with relay DPs (DP 7-13 map to relay 1-7).
    countdown_map = {"1": "7", "2": "8", "3": "9", "4": "10",
                     "5": "11", "6": "12", "7": "13"}

    # Known advanced-settings DPs for multi-gang wall switches.
    advanced_specs: dict[str, dict[str, Any]] = {
        "14": {"key": "relay_status", "type": "str"},
        "17": {"key": "cycle_time", "type": "str"},
        "18": {"key": "random_time", "type": "str"},
        "19": {"key": "switch_inching", "type": "str"},
        "38": {"key": "relay_status", "type": "str"},
        "40": {"key": "light_mode", "type": "str"},
        "41": {"key": "child_lock", "type": "bool"},
        "44": {"key": "switch_inching", "type": "str"},
        "47": {"key": "switch_type", "type": "str"},
    }

    added_or_updated: list[str] = []

    # Rename relay DPs to switch_N.
    for idx, dp_id in enumerate(found_relays, start=1):
        spec = {"key": f"switch_{idx}", "type": "bool"}
        if result.get(dp_id) != spec:
            result[dp_id] = spec
            added_or_updated.append(dp_id)

        # Pair countdown DP if present.
        cd_dp = countdown_map.get(dp_id)
        if cd_dp and cd_dp in discovered_dps:
            cd_type = _classify_value(discovered_dps[cd_dp])
            if cd_type == "int":
                cd_spec = {"key": f"countdown_{idx}", "type": "int"}
                if result.get(cd_dp) != cd_spec:
                    result[cd_dp] = cd_spec
                    added_or_updated.append(cd_dp)

    # Enrich with advanced DPs if present.
    for dp_id, spec in advanced_specs.items():
        if dp_id not in discovered_dps:
            continue
        actual_type = _classify_value(discovered_dps[dp_id])
        if actual_type != spec["type"]:
            continue
        if dp_id not in result:
            result[dp_id] = dict(spec)
            added_or_updated.append(dp_id)

    if added_or_updated:
        _LOGGER.info(
            "Auto-mapping (%s): enriched multi-gang switch DPs (%d relays): %s",
            DEVICE_TYPE_SWITCH,
            len(found_relays),
            sorted(added_or_updated),
        )


def _augment_contact_alarm_sensor_family_map(
    result: dict[str, dict[str, Any]],
    discovered_dps: dict[str, Any],
) -> None:
    """Enrich contact/alarm sensor family when the DP signature is strong.

    Expected shape (common Tuya Wi-Fi contact sensors):
      * DP 1: door/contact state (bool)
      * DP 2: battery percentage (int)
      * optional alarm controls on 101..105 with fixed types
    """
    if not isinstance(discovered_dps.get("1"), bool):
        return

    if not (
        isinstance(discovered_dps.get("2"), (int, float))
        and not isinstance(discovered_dps.get("2"), bool)
    ):
        return

    expected_optional_types: dict[str, str] = {
        "101": "bool",
        "102": "bool",
        "103": "int",
        "104": "int",
        "105": "int",
    }
    optional_hits = 0
    for dp_id, expected in expected_optional_types.items():
        if dp_id not in discovered_dps:
            continue
        if _classify_value(discovered_dps[dp_id]) == expected:
            optional_hits += 1

    # Require strong evidence so we don't over-map unrelated sensors.
    if optional_hits < 2:
        return

    family_map: dict[str, dict[str, Any]] = {
        "1": {"key": DP_KEY_DOOR_STATE, "type": "bool"},
        "2": {"key": DP_KEY_BATTERY, "type": "int"},
        "101": {"key": "alarm_switch", "type": "bool"},
        "102": {"key": "arming_switch", "type": "bool"},
        "103": {"key": "delay_alarm", "type": "int"},
        "104": {"key": "time_alarm", "type": "int"},
        "105": {"key": "alarm_volume", "type": "int"},
    }

    added_or_updated: list[str] = []
    for dp_id, spec in family_map.items():
        if dp_id not in discovered_dps:
            continue
        actual_type = _classify_value(discovered_dps[dp_id])
        if actual_type != spec["type"]:
            continue
        if result.get(dp_id) != spec:
            result[dp_id] = dict(spec)
            added_or_updated.append(dp_id)

    if added_or_updated:
        _LOGGER.info(
            "Auto-mapping (%s): enriched contact/alarm sensor DPs: %s",
            DEVICE_TYPE_SENSOR,
            sorted(added_or_updated),
        )


def merge_dp_maps(
    user_map: dict[str, Any],
    auto_map: dict[str, Any],
) -> dict[str, Any]:
    """Merge *user_map* with *auto_map*.  User entries always take priority.

    Returns a new dict — neither input is mutated.
    """
    merged = dict(auto_map)
    merged.update(user_map)
    return merged


def merge_all_dp_maps(
    *maps: dict[str, Any],
) -> dict[str, Any]:
    """Merge multiple dp_maps.  Later maps override earlier ones.

    Typical priority: auto-heuristic < profile < cloud-schema < user-manual.
    """
    result: dict[str, Any] = {}
    for m in maps:
        if m:
            result.update(m)
    return result


def build_raw_dp_map(
    discovered_dps: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build a minimal dp_map directly from discovered DPs.

    Used as a last-resort fallback when all heuristic, profile, and
    cloud mapping pipelines produce nothing.  Entity keys are named
    ``dp_<id>`` so they are at least visible in the review step.
    """
    result: dict[str, dict[str, Any]] = {}
    for dp_id, value in discovered_dps.items():
        dp_str = str(dp_id)
        if isinstance(value, bool):
            vtype = "bool"
        elif isinstance(value, (int, float)):
            vtype = "int"
        else:
            vtype = "str"
        result[dp_str] = {"key": f"dp_{dp_str}", "type": vtype}
    return result
