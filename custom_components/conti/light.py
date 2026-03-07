"""Light platform for Conti.

This module is the Home Assistant platform entry-point.  It inspects the
device's DP map and delegates to the correct light subclass via the
factory in :mod:`.lights`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DP_MAP,
    DEVICE_TYPE_LIGHT,
    DOMAIN,
)
from .coordinator import ContiCoordinator
from .lights import create_conti_light

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Conti light entities from a config entry."""
    if entry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_LIGHT:
        return

    coordinator: ContiCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    dp_map: dict[str, Any] = json.loads(
        entry.options.get(CONF_DP_MAP) or entry.data.get(CONF_DP_MAP, "{}")
    )
    device_id: str = entry.data[CONF_DEVICE_ID]

    entity = create_conti_light(coordinator, entry, device_id, dp_map)
    _LOGGER.debug(
        "Created %s for device %s (dp_map keys: %s)",
        type(entity).__name__,
        device_id,
        list(dp_map.keys()),
    )

    async_add_entities([entity], update_before_add=True)

