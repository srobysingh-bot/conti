"""External-on correction profile engine for Conti lights.

When a light is turned on by an external source (RF remote, physical
button), the device powers up with its firmware's last-remembered state.
This module provides a generic, data-driven engine that resolves
time-based rules to determine a target brightness and optional colour
temperature so that Conti can apply a post-on correction immediately.

One engine, many devices — per-device rules are stored as plain data in
the config entry ``options["external_on_profile"]``.  No per-device
Python code, no per-device automations.

Data model (stored as JSON in entry.options)::

    {
        "enabled": true,
        "rules": [
            {"start": "06:00", "end": "12:00", "brightness_pct": 30},
            {"start": "12:00", "end": "18:00", "brightness_pct": 70, "kelvin": 4000},
            {"start": "22:00", "end": "06:00", "brightness_pct": 15, "kelvin": 2200}
        ]
    }

Rules are evaluated in order; the **first** match wins.
Overnight ranges (start > end, e.g. 22:00–06:00) are supported.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def resolve_active_rule(
    profile: dict[str, Any],
    now_hour: int,
    now_minute: int,
) -> dict[str, Any] | None:
    """Return the first rule that matches the current local time.

    Parameters
    ----------
    profile:
        The external-on profile dict.  Must contain ``"enabled": True``
        and a ``"rules"`` list, otherwise ``None`` is returned.
    now_hour, now_minute:
        Current local time (0-23, 0-59).

    Returns
    -------
    dict | None
        The matching rule dict (with at least ``brightness_pct``), or
        ``None`` if the profile is disabled / no rule matches.
    """
    if not isinstance(profile, dict) or not profile.get("enabled", False):
        return None

    rules = profile.get("rules")
    if not rules or not isinstance(rules, list):
        return None

    now_minutes = now_hour * 60 + now_minute

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        start_str = rule.get("start", "")
        end_str = rule.get("end", "")
        if not start_str or not end_str:
            continue
        try:
            s_parts = str(start_str).split(":")
            sh, sm = int(s_parts[0]), int(s_parts[1])
            e_parts = str(end_str).split(":")
            eh, em = int(e_parts[0]), int(e_parts[1])
        except (ValueError, TypeError, IndexError):
            _LOGGER.debug("Invalid time in external-on rule: %s", rule)
            continue

        start = sh * 60 + sm
        end = eh * 60 + em

        if start <= end:
            # Same-day range, e.g. 06:00–12:00
            if start <= now_minutes < end:
                return rule
        else:
            # Overnight range, e.g. 22:00–06:00
            if now_minutes >= start or now_minutes < end:
                return rule

    return None
