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

    # Signature-based enrichment for switches.
    # Multi-gang detection runs FIRST — if it triggers (2+ relays),
    # power-monitoring plug enrichment is skipped to avoid conflicts.
    if device_type == DEVICE_TYPE_SWITCH:
        is_multi_gang = _augment_multi_gang_switch_family_map(
            result, discovered_dps
        )
        if not is_multi_gang:
            _augment_power_monitoring_plug_map(result, discovered_dps)
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


def _is_int_dp(discovered_dps: dict[str, Any], dp_id: str) -> bool:
    """Return True if *dp_id* is a non-bool numeric DP."""
    val = discovered_dps.get(dp_id)
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _augment_multi_gang_switch_family_map(
    result: dict[str, dict[str, Any]],
    discovered_dps: dict[str, Any],
) -> bool:
    """Give distinct ``switch_N`` key names to multi-gang relay DPs.

    Applied when **two or more** relay-class bool DPs (1–7) are present.
    Also enriches countdown, energy-monitoring, and advanced DPs when
    the DP signature matches.

    Returns ``True`` if multi-gang detection triggered, ``False`` otherwise.
    """
    relay_ids = ["1", "2", "3", "4", "5", "6", "7"]
    found_relays = [
        dp_id for dp_id in relay_ids
        if isinstance(discovered_dps.get(dp_id), bool)
    ]

    if len(found_relays) < 2:
        return False  # Not multi-gang

    added_or_updated: list[str] = []

    # ── Rename relay DPs to switch_N ──
    for idx, dp_id in enumerate(found_relays, start=1):
        spec = {"key": f"switch_{idx}", "type": "bool"}
        if result.get(dp_id) != spec:
            result[dp_id] = spec
            added_or_updated.append(dp_id)

    # ── Auto-detect countdown pattern ──
    # Two common Tuya layouts:
    #   Wall switches:      relay 1-N → countdown at offset +6 (DPs 7-13)
    #   Power strip / smart: relay 1-N → countdown at offset +8 (DPs 9-12+)
    offset_8_hits = sum(
        1 for dp in found_relays
        if _is_int_dp(discovered_dps, str(int(dp) + 8))
    )
    offset_6_hits = sum(
        1 for dp in found_relays
        if _is_int_dp(discovered_dps, str(int(dp) + 6))
    )
    countdown_offset = (
        8 if offset_8_hits > 0 and offset_8_hits >= offset_6_hits else 6
    )

    for idx, dp_id in enumerate(found_relays, start=1):
        cd_dp = str(int(dp_id) + countdown_offset)
        if cd_dp in discovered_dps and _is_int_dp(discovered_dps, cd_dp):
            cd_spec = {"key": f"countdown_{idx}", "type": "int"}
            if result.get(cd_dp) != cd_spec:
                result[cd_dp] = cd_spec
                added_or_updated.append(cd_dp)

    # ── Energy-monitoring DPs (common on multi-gang switches with metering) ──
    energy_specs: dict[str, dict[str, Any]] = {
        "17": {"key": DP_KEY_ENERGY_TOTAL, "type": "int", "scale": 100},
        "18": {"key": DP_KEY_CURRENT, "type": "int"},
        "19": {"key": DP_KEY_POWER_USAGE, "type": "int", "scale": 10},
        "20": {"key": DP_KEY_VOLTAGE, "type": "int", "scale": 10},
    }
    for dp_id, spec in energy_specs.items():
        if dp_id not in discovered_dps:
            continue
        if not _is_int_dp(discovered_dps, dp_id):
            continue
        if dp_id not in result:
            result[dp_id] = dict(spec)
            added_or_updated.append(dp_id)

    # ── Advanced / control DPs ──
    advanced_specs: dict[str, dict[str, Any]] = {
        "14": {"key": "relay_status", "type": "str"},
        "26": {"key": "fault", "type": "int"},
        "38": {"key": "relay_status", "type": "str"},
        "40": {"key": "light_mode", "type": "str"},
        "41": {"key": "child_lock", "type": "bool"},
        "42": {"key": "cycle_time", "type": "str"},
        "43": {"key": "random_time", "type": "str"},
        "44": {"key": "switch_inching", "type": "str"},
        "47": {"key": "switch_type", "type": "str"},
    }
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

    return True


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


def merge_cloud_priority_dp_maps(
    heuristic_map: dict[str, Any],
    profile_map: dict[str, Any],
    cloud_map: dict[str, Any],
) -> dict[str, Any]:
    """Merge dp_maps with strict cloud-first priority.

    Priority: cloud_schema > profile_map > heuristic_map

    When a cloud schema is present:
    - Cloud entries are authoritative and are never overridden.
    - Profile entries fill DPs not covered by cloud.
    - Heuristic entries only fill DPs not covered by cloud or profile.

    This prevents heuristic guesses from corrupting authoritative
    cloud DP assignments (e.g. guessing ``power`` for a DP that the
    cloud has correctly identified as ``brightness``).
    """
    result: dict[str, Any] = {}

    # Lowest priority first — each layer overwrites previous gaps only
    # (we build in reverse then flip, but since cloud is last it wins).
    # Actually: start with lowest and let higher-priority overwrite.
    if heuristic_map:
        result.update(heuristic_map)
    if profile_map:
        result.update(profile_map)

    if cloud_map:
        # Cloud overrides everything
        result.update(cloud_map)

        # Additionally, remove any heuristic/profile assignments for DPs
        # whose dp_id is already handled by cloud, to avoid stale role
        # collisions from the lower-priority layers.
        cloud_keys: set[str] = {v["key"] for v in cloud_map.values() if "key" in v}
        to_remove = [
            dp_id for dp_id, entry in result.items()
            if dp_id not in cloud_map and entry.get("key") in cloud_keys
        ]
        for dp_id in to_remove:
            del result[dp_id]
            _LOGGER.debug(
                "Cloud-priority merge: removed heuristic assignment "
                "dp_id=%s (role conflict with cloud map)",
                dp_id,
            )

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
