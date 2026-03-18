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
