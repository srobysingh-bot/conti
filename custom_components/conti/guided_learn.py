"""Guided learn mode for Conti onboarding.

Provides:
1. Device-family pre-classification from DPS shape + profile hints
2. Per-family learn step generation with clear, specific instructions
3. Minimum evidence rules per device family
4. Evidence validation and human-readable missing-item reporting
5. LearnSession for tracking DP changes across learn steps

The config_flow drives the UI; this module provides the engine.
All communication is local (TinyTuyaDevice), no cloud involved.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Device family constants
# ──────────────────────────────────────────────────────────────────────

FAMILY_SINGLE_SWITCH = "single_switch"
FAMILY_MULTI_GANG_SWITCH = "multi_gang_switch"
FAMILY_POWER_STRIP = "power_strip"
FAMILY_DIMMER = "dimmer"
FAMILY_WHITE_LIGHT = "white_light"
FAMILY_CCT_LIGHT = "cct_light"
FAMILY_RGB_LIGHT = "rgb_light"
FAMILY_FAN = "fan"
FAMILY_CLIMATE = "climate"
FAMILY_SENSOR = "sensor"
FAMILY_UNKNOWN = "unknown"

_FAMILY_DISPLAY: dict[str, str] = {
    FAMILY_SINGLE_SWITCH: "Single Switch",
    FAMILY_MULTI_GANG_SWITCH: "{gang_count}-Gang Switch",
    FAMILY_POWER_STRIP: "Power Strip ({gang_count} outlets)",
    FAMILY_DIMMER: "Dimmer",
    FAMILY_WHITE_LIGHT: "White Light",
    FAMILY_CCT_LIGHT: "White / CCT Light",
    FAMILY_RGB_LIGHT: "RGB + CCT Light",
    FAMILY_FAN: "Fan",
    FAMILY_CLIMATE: "Climate / Air Conditioner",
    FAMILY_SENSOR: "Sensor",
    FAMILY_UNKNOWN: "Unknown Device",
}


def family_display_name(family: str, gang_count: int = 0) -> str:
    """Human-readable family name with gang count substitution."""
    template = _FAMILY_DISPLAY.get(family, family)
    return template.format(gang_count=gang_count)


# ──────────────────────────────────────────────────────────────────────
# Pre-classification
# ──────────────────────────────────────────────────────────────────────


def classify_device_family(
    discovered_dps: dict[str, Any],
    device_type: str,
    profile: dict[str, Any] | None = None,
    tuya_category: str | None = None,
) -> tuple[str, int, str]:
    """Classify a device into a specific family.

    Uses all available evidence in priority order:
      1. Matched profile ID
      2. Tuya product category
      3. DPS shape heuristics

    Returns ``(family_id, gang_count, reason_string)``.
    ``gang_count`` is only meaningful for multi-gang / power-strip (0 otherwise).
    """
    if profile:
        family, gangs = _family_from_profile(profile)
        if family != FAMILY_UNKNOWN:
            return (
                family,
                gangs,
                f"Matched device profile: {profile.get('name', profile.get('id'))}",
            )

    if tuya_category:
        family, gangs = _family_from_category(tuya_category, discovered_dps)
        if family != FAMILY_UNKNOWN:
            return family, gangs, f"Tuya product category: {tuya_category}"

    return _family_from_dps_shape(discovered_dps, device_type)


def _family_from_profile(profile: dict[str, Any]) -> tuple[str, int]:
    """Derive family from a matched profile ID."""
    pid = profile.get("id", "").lower()
    template = profile.get("dp_template", {})

    if pid.startswith("switch_") and "gang" in pid:
        try:
            gangs = int(pid.split("_")[1].replace("gang", ""))
        except (ValueError, IndexError):
            gangs = 1
        if gangs == 1:
            return FAMILY_SINGLE_SWITCH, 1
        return FAMILY_MULTI_GANG_SWITCH, gangs

    if pid == "switch_powerstrip":
        gangs = sum(
            1 for v in template.values()
            if isinstance(v, dict) and v.get("type") == "bool"
        )
        return FAMILY_POWER_STRIP, max(gangs, 3)

    if "dimmer" in pid or "triac" in pid:
        return FAMILY_DIMMER, 0
    if "rgb" in pid:
        return FAMILY_RGB_LIGHT, 0
    if pid in ("light_cct", "light_white_strip"):
        return FAMILY_CCT_LIGHT, 0
    if pid.startswith("fan"):
        return FAMILY_FAN, 0
    if pid.startswith("climate"):
        return FAMILY_CLIMATE, 0
    if pid.startswith("sensor"):
        return FAMILY_SENSOR, 0

    dt = profile.get("device_type", "")
    if dt == "light":
        # Generic "light" profile metadata alone is too weak to force a
        # white-only classification; defer to category/DPS shape.
        return FAMILY_UNKNOWN, 0
    if dt == "switch":
        return FAMILY_SINGLE_SWITCH, 1
    return FAMILY_UNKNOWN, 0


def _family_from_category(
    category: str, discovered_dps: dict[str, Any]
) -> tuple[str, int]:
    """Derive family from Tuya product category."""
    cat = category.lower()
    if cat in ("dj", "dd"):
        return _classify_light_from_dps(discovered_dps)
    if cat in ("tgq",):
        return FAMILY_DIMMER, 0
    if cat in ("kg",):
        gangs = _count_switch_channel_bool_dps(discovered_dps)
        return (FAMILY_SINGLE_SWITCH, 1) if gangs <= 1 else (FAMILY_MULTI_GANG_SWITCH, gangs)
    if cat in ("cz", "pc"):
        gangs = _count_switch_channel_bool_dps(discovered_dps)
        return (FAMILY_SINGLE_SWITCH, 1) if gangs <= 1 else (FAMILY_POWER_STRIP, gangs)
    if cat in ("fs",):
        return FAMILY_FAN, 0
    if cat in ("kt", "wk"):
        return FAMILY_CLIMATE, 0
    if cat in ("wsdcg", "pir", "mcs"):
        return FAMILY_SENSOR, 0
    return FAMILY_UNKNOWN, 0


def _family_from_dps_shape(
    discovered_dps: dict[str, Any], device_type: str
) -> tuple[str, int, str]:
    """Classify by analysing the DPS values and structure."""
    if device_type == "light":
        family, _ = _classify_light_from_dps(discovered_dps)
        return family, 0, _light_reason(family, discovered_dps)
    if device_type == "switch":
        gangs = _count_switch_channel_bool_dps(discovered_dps)
        if gangs == 0:
            return FAMILY_UNKNOWN, 0, "No boolean data points found for switch"
        if gangs == 1:
            return FAMILY_SINGLE_SWITCH, 1, "1 boolean data point → single switch"
        return (
            FAMILY_MULTI_GANG_SWITCH,
            gangs,
            f"{gangs} boolean data points → likely {gangs}-gang switch",
        )
    if device_type == "fan":
        return FAMILY_FAN, 0, "Device type is fan"
    if device_type == "climate":
        return FAMILY_CLIMATE, 0, "Device type is climate"
    if device_type == "sensor":
        return FAMILY_SENSOR, 0, "Device type is sensor"

    # Fallback: infer from DPS shape alone
    bools = _count_bool_dps(discovered_dps)
    ints = sum(
        1 for v in discovered_dps.values()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    )
    if bools >= 2 and ints == 0:
        return (
            FAMILY_MULTI_GANG_SWITCH,
            bools,
            f"{bools} boolean DPs, no integers → likely multi-gang switch",
        )
    if bools == 1 and ints >= 2:
        return FAMILY_CCT_LIGHT, 0, "1 boolean + 2+ integer DPs → likely CCT light"
    if bools == 1 and ints == 1:
        return FAMILY_DIMMER, 0, "1 boolean + 1 integer DP → likely dimmer"
    if bools == 1:
        return FAMILY_SINGLE_SWITCH, 1, "1 boolean DP → likely single switch"
    return FAMILY_UNKNOWN, 0, "Could not determine device family"


def _classify_light_from_dps(dps: dict[str, Any]) -> tuple[str, int]:
    """Sub-classify a light device based on its DPS."""
    ints = sum(
        1 for v in dps.values()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    )
    has_long_str = any(isinstance(v, str) and len(v) > 10 for v in dps.values())
    # A short mode string like "white", "colour", "color" indicates
    # the device supports multiple modes → at least CCT.
    has_mode_str = any(
        isinstance(v, str)
        and v.lower() in ("white", "colour", "color", "scene", "music")
        for v in dps.values()
    )
    if has_long_str and (ints >= 2 or has_mode_str):
        return FAMILY_RGB_LIGHT, 0
    if ints >= 2:
        return FAMILY_CCT_LIGHT, 0
    if has_mode_str and ints >= 1:
        # Mode string + at least one int DP → likely CCT with an
        # undiscovered brightness or color_temp DP.
        return FAMILY_CCT_LIGHT, 0
    if ints == 1:
        return FAMILY_DIMMER, 0
    return FAMILY_WHITE_LIGHT, 0


def _light_reason(family: str, dps: dict[str, Any]) -> str:
    ints = sum(
        1 for v in dps.values()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    )
    has_mode = any(
        isinstance(v, str)
        and v.lower() in ("white", "colour", "color", "scene", "music")
        for v in dps.values()
    )
    if family == FAMILY_RGB_LIGHT:
        return f"{ints} integer DPs + colour data string → RGB light"
    if family == FAMILY_CCT_LIGHT:
        if has_mode and ints < 2:
            return f"mode string + {ints} integer DP(s) → warm/cool (CCT) light"
        return f"{ints} integer DPs → CCT light (brightness + colour temperature)"
    if family == FAMILY_DIMMER:
        return "1 integer DP → dimmer (brightness only)"
    return "Power DP only → basic white light"


def _count_bool_dps(discovered_dps: dict[str, Any]) -> int:
    """Count boolean DPs (typical for switch channels)."""
    return sum(1 for v in discovered_dps.values() if isinstance(v, bool))


def _count_switch_channel_bool_dps(discovered_dps: dict[str, Any]) -> int:
    """Count bool DPs that are likely relay channels for switch/plug devices.

    Some plugs expose extra bool telemetry/settings (for example lock/LED)
    that should not increase gang count. Prefer classic relay DP IDs first,
    and only fall back to all bool DPs when none of those IDs are present.
    """
    relay_like_ids = {
        "1", "2", "3", "4", "5", "6", "7", "8",
        "20", "21", "22", "23", "24", "25",
    }
    relay_bools = sum(
        1
        for dp_id, val in discovered_dps.items()
        if dp_id in relay_like_ids and isinstance(val, bool)
    )
    if relay_bools > 0:
        return relay_bools
    return _count_bool_dps(discovered_dps)


# ──────────────────────────────────────────────────────────────────────
# Learn session
# ──────────────────────────────────────────────────────────────────────

class LearnSession:
    """Tracks state across guided learn steps.

    Created once per learn session in the config flow, passed between
    steps via ``flow.context``.
    """

    def __init__(self, initial_dps: dict[str, Any]) -> None:
        self._baseline: dict[str, Any] = dict(initial_dps)
        self._learned: dict[str, dict[str, Any]] = {}  # dp_id → {key, type}
        self._pending_role: str | None = None
        self._pending_type: str | None = None

    @property
    def baseline(self) -> dict[str, Any]:
        """Current DP baseline snapshot."""
        return dict(self._baseline)

    @property
    def learned_map(self) -> dict[str, dict[str, Any]]:
        """DP map built so far from learned DPs."""
        return dict(self._learned)

    @property
    def learned_count(self) -> int:
        return len(self._learned)

    @property
    def learned_roles(self) -> set[str]:
        """Set of DP roles (keys) already learned."""
        return {info["key"] for info in self._learned.values()}

    def set_pending_role(self, role: str, dp_type: str) -> None:
        """Set which role we're waiting for the user to demonstrate."""
        self._pending_role = role
        self._pending_type = dp_type

    def apply_diff(
        self, new_dps: dict[str, Any]
    ) -> list[tuple[str, Any, Any]]:
        """Compare *new_dps* against baseline and return changed DPs.

        Returns list of ``(dp_id, old_value, new_value)`` for DPs that
        changed or appeared for the first time.  Updates baseline.
        """
        changes: list[tuple[str, Any, Any]] = []

        for dp_id, new_val in new_dps.items():
            if dp_id not in self._baseline:
                changes.append((dp_id, None, new_val))
            elif self._baseline[dp_id] != new_val:
                changes.append((dp_id, self._baseline[dp_id], new_val))

        self._baseline.update(new_dps)

        return changes

    def assign_change(
        self, dp_id: str, value: Any
    ) -> bool:
        """Assign the pending role to *dp_id* based on the observed change.

        Returns ``True`` if the assignment was made, ``False`` if no role
        was pending.
        """
        if not self._pending_role:
            return False

        dp_type = self._pending_type or _classify_value(value)
        entry: dict[str, Any] = {
            "key": self._pending_role,
            "type": dp_type,
        }

        # Add default ranges for known integer roles
        if dp_type == "int" and self._pending_role in ("brightness", "color_temp"):
            entry["min"] = 10
            entry["max"] = 1000

        self._learned[dp_id] = entry
        _LOGGER.info(
            "Guided learn: DP %s → role '%s' (type=%s, value=%r)",
            dp_id,
            self._pending_role,
            dp_type,
            value,
        )

        self._pending_role = None
        self._pending_type = None
        return True


# ──────────────────────────────────────────────────────────────────────
# Learn step generation (per-family)
# ──────────────────────────────────────────────────────────────────────


def generate_learn_steps(
    family: str,
    gang_count: int = 0,
) -> list[dict[str, Any]]:
    """Generate learn steps tailored to the device family.

    Each step dict has:
        instruction:  str  — human-readable action for the user
        role:         str  — DP key to assign on success
        type:         str  — expected value type (bool / int / str)
        required:     bool — must complete for minimum evidence
    """
    gen = _STEP_GENERATORS.get(family)
    if gen:
        return gen(gang_count)
    return _steps_unknown(gang_count)


def _steps_single_switch(_gc: int) -> list[dict[str, Any]]:
    return [
        {
            "instruction": (
                "Toggle the switch once "
                "(press the button or flip it on/off)."
            ),
            "role": "power",
            "type": "bool",
            "required": True,
        },
    ]


def _steps_multi_gang(gang_count: int) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for i in range(1, gang_count + 1):
        steps.append(
            {
                "instruction": (
                    f"Press ONLY switch/button {i}. "
                    f"Do not touch any other button. "
                    f"Wait 2 seconds, then click 'Check'."
                ),
                "role": "power" if i == 1 else f"switch_{i}",
                "type": "bool",
                "required": True,
            }
        )
    return steps


def _steps_power_strip(gang_count: int) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for i in range(1, gang_count + 1):
        steps.append(
            {
                "instruction": (
                    f"Press ONLY outlet/socket {i}. "
                    f"Do not touch any other outlet. "
                    f"Wait 2 seconds, then click 'Check'."
                ),
                "role": "power" if i == 1 else f"switch_{i}",
                "type": "bool",
                "required": True,
            }
        )
    return steps


def _steps_dimmer(_gc: int) -> list[dict[str, Any]]:
    return [
        {
            "instruction": "Turn the dimmer ON or OFF (toggle power once).",
            "role": "power",
            "type": "bool",
            "required": True,
        },
        {
            "instruction": (
                "Change the brightness level "
                "(dim or brighten the light)."
            ),
            "role": "brightness",
            "type": "int",
            "required": True,
        },
    ]


def _steps_white_light(_gc: int) -> list[dict[str, Any]]:
    return [
        {
            "instruction": "Turn the light ON or OFF once.",
            "role": "power",
            "type": "bool",
            "required": True,
        },
        {
            "instruction": (
                "Change the brightness level, if supported. "
                "Skip if this light has no dimming capability."
            ),
            "role": "brightness",
            "type": "int",
            "required": False,
        },
    ]


def _steps_cct_light(_gc: int) -> list[dict[str, Any]]:
    return [
        {
            "instruction": "Turn the light ON or OFF once.",
            "role": "power",
            "type": "bool",
            "required": True,
        },
        {
            "instruction": (
                "Change the brightness level "
                "(dim or brighten)."
            ),
            "role": "brightness",
            "type": "int",
            "required": True,
        },
        {
            "instruction": (
                "Change the colour temperature "
                "(switch between warm white and cool white)."
            ),
            "role": "color_temp",
            "type": "int",
            "required": True,
        },
    ]


def _steps_rgb_light(_gc: int) -> list[dict[str, Any]]:
    return [
        {
            "instruction": "Turn the light ON or OFF once.",
            "role": "power",
            "type": "bool",
            "required": True,
        },
        {
            "instruction": "Change the brightness level.",
            "role": "brightness",
            "type": "int",
            "required": True,
        },
        {
            "instruction": (
                "Change the colour temperature "
                "(warm \u2194 cool white)."
            ),
            "role": "color_temp",
            "type": "int",
            "required": True,
        },
        {
            "instruction": (
                "Change the colour or switch colour mode "
                "(e.g. pick a new RGB colour, or switch to colour mode)."
            ),
            "role": "color_rgb",
            "type": "str",
            "required": True,
        },
    ]


def _steps_fan(_gc: int) -> list[dict[str, Any]]:
    return [
        {
            "instruction": "Turn the fan ON or OFF once.",
            "role": "fan_power",
            "type": "bool",
            "required": True,
        },
        {
            "instruction": (
                "Change the fan speed "
                "(increase or decrease)."
            ),
            "role": "fan_speed",
            "type": "int",
            "required": True,
        },
        {
            "instruction": (
                "Toggle oscillation or change direction, if supported. "
                "Skip if not available on this fan."
            ),
            "role": "fan_oscillation",
            "type": "bool",
            "required": False,
        },
    ]


def _steps_climate(_gc: int) -> list[dict[str, Any]]:
    return [
        {
            "instruction": (
                "Turn the climate device ON or OFF once."
            ),
            "role": "power",
            "type": "bool",
            "required": True,
        },
        {
            "instruction": (
                "Change the target temperature "
                "(increase or decrease by at least 1\u00b0)."
            ),
            "role": "target_temp",
            "type": "int",
            "required": True,
        },
        {
            "instruction": (
                "Change the HVAC mode "
                "(e.g. cooling \u2192 heating, or heating \u2192 fan only). "
                "Skip if not applicable."
            ),
            "role": "hvac_mode",
            "type": "str",
            "required": False,
        },
        {
            "instruction": (
                "Change the fan speed / fan mode, if supported. "
                "Skip if not available."
            ),
            "role": "fan_mode",
            "type": "str",
            "required": False,
        },
    ]


def _steps_sensor(_gc: int) -> list[dict[str, Any]]:
    return [
        {
            "instruction": (
                "Sensors are typically read-only. "
                "Wait 10 seconds for a sensor reading update, "
                "then click 'Check'."
            ),
            "role": "sensor_value",
            "type": "int",
            "required": False,
        },
    ]


def _steps_unknown(_gc: int) -> list[dict[str, Any]]:
    return [
        {
            "instruction": (
                "Perform an action on the device "
                "(toggle power, change a setting), "
                "then click 'Check'."
            ),
            "role": "power",
            "type": "bool",
            "required": False,
        },
    ]


_STEP_GENERATORS: dict[str, Any] = {
    FAMILY_SINGLE_SWITCH: _steps_single_switch,
    FAMILY_MULTI_GANG_SWITCH: _steps_multi_gang,
    FAMILY_POWER_STRIP: _steps_power_strip,
    FAMILY_DIMMER: _steps_dimmer,
    FAMILY_WHITE_LIGHT: _steps_white_light,
    FAMILY_CCT_LIGHT: _steps_cct_light,
    FAMILY_RGB_LIGHT: _steps_rgb_light,
    FAMILY_FAN: _steps_fan,
    FAMILY_CLIMATE: _steps_climate,
    FAMILY_SENSOR: _steps_sensor,
    FAMILY_UNKNOWN: _steps_unknown,
}


# ──────────────────────────────────────────────────────────────────────
# Minimum evidence validation
# ──────────────────────────────────────────────────────────────────────

_EVIDENCE_RULES: dict[str, dict[str, bool]] = {
    FAMILY_SINGLE_SWITCH: {"power": True},
    FAMILY_DIMMER: {"power": True, "brightness": True},
    FAMILY_WHITE_LIGHT: {"power": True},
    FAMILY_CCT_LIGHT: {
        "power": True,
        "brightness": True,
        "color_temp": True,
    },
    FAMILY_RGB_LIGHT: {
        "power": True,
        "brightness": True,
        "color_temp": True,
        "color_rgb": True,
    },
    FAMILY_FAN: {"fan_power": True, "fan_speed": True},
    FAMILY_CLIMATE: {"power": True, "target_temp": True},
    FAMILY_SENSOR: {},
    FAMILY_UNKNOWN: {},
}


def get_required_roles(family: str, gang_count: int = 0) -> list[str]:
    """Return the list of DP roles required for minimum evidence."""
    if family in (FAMILY_MULTI_GANG_SWITCH, FAMILY_POWER_STRIP):
        roles = ["power"]
        for i in range(2, gang_count + 1):
            roles.append(f"switch_{i}")
        return roles
    rules = _EVIDENCE_RULES.get(family, {})
    return [role for role, required in rules.items() if required]


def validate_evidence(
    family: str,
    learned_roles: set[str],
    gang_count: int = 0,
) -> tuple[bool, list[str]]:
    """Check if minimum evidence has been collected.

    Returns ``(is_complete, missing_roles_list)``.
    """
    required = get_required_roles(family, gang_count)
    if not required:
        return True, []
    missing = [r for r in required if r not in learned_roles]
    return len(missing) == 0, missing


def describe_missing(missing_roles: list[str], family: str) -> str:
    """Human-readable description of what evidence is still needed."""
    if not missing_roles:
        return ""
    labels: list[str] = []
    for role in missing_roles:
        if role == "power":
            if family in (FAMILY_MULTI_GANG_SWITCH, FAMILY_POWER_STRIP):
                labels.append("Switch/outlet 1")
            else:
                labels.append("Power on/off")
        elif role.startswith("switch_"):
            num = role.replace("switch_", "")
            labels.append(f"Switch/outlet {num}")
        elif role == "brightness":
            labels.append("Brightness")
        elif role == "color_temp":
            labels.append("Colour temperature")
        elif role == "color_rgb":
            labels.append("Colour / colour mode")
        elif role == "fan_power":
            labels.append("Fan power")
        elif role == "fan_speed":
            labels.append("Fan speed")
        elif role == "target_temp":
            labels.append("Target temperature")
        else:
            labels.append(role.replace("_", " ").title())
    return "Still needed: " + ", ".join(labels)


def build_action_plan(steps: list[dict[str, Any]]) -> str:
    """Build a numbered summary of all learn steps for display."""
    lines: list[str] = []
    for i, step in enumerate(steps, 1):
        req = " (required)" if step.get("required") else " (optional)"
        lines.append(f"{i}. {step['instruction']}{req}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Helper
# ──────────────────────────────────────────────────────────────────────


def _classify_value(value: Any) -> str:
    """Classify a runtime DP value into ``bool`` / ``int`` / ``str``."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "int"
    return "str"
