"""IR action name normalization for Conti."""

from __future__ import annotations

STANDARD_IR_ACTIONS: dict[str, list[str]] = {
    "power": ["power", "power_on", "turn_on", "on_off"],
    "swing_vertical": [
        "swing_vertical",
        "vertical_swing",
        "swing_v",
        "swing_vertical_on",
        "v_swing",
    ],
    "swing_horizontal": [
        "swing_horizontal",
        "horizontal_swing",
        "swing_h",
        "swing_horizontal_on",
        "h_swing",
    ],
}

_ALIAS_TO_STANDARD = {
    alias: standard
    for standard, aliases in STANDARD_IR_ACTIONS.items()
    for alias in aliases
}


def normalize_ir_action(action: str) -> str:
    """Return the canonical Conti IR action name."""
    normalized = str(action).strip().lower()
    for old, new in ((" ", "_"), ("-", "_"), ("/", "_")):
        normalized = normalized.replace(old, new)
    normalized = "_".join(part for part in normalized.split("_") if part)
    return _ALIAS_TO_STANDARD.get(normalized, normalized)


def ir_action_aliases(action: str) -> set[str]:
    """Return all known aliases for an action, including the canonical name."""
    canonical = normalize_ir_action(action)
    return {canonical, *STANDARD_IR_ACTIONS.get(canonical, [])}
