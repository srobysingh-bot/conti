"""Home Assistant diagnostics support for Conti devices."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_CONNECTION_FIELDS = (
    "device_id",
    "ha_host_ip",
    "ha_host_subnet",
    "device_ip",
    "dp_map_source",
    "monitored_dp_ids",
    "last_successful_local_update",
    "last_local_error",
    "control_path",
    "reconnect_attempts",
    "next_retry_time",
    "cloud_error",
    "cloud_error_code",
    "cloud_error_message",
    "online",
    "local_status",
    "protocol_version",
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return connection-focused diagnostics without config-entry secrets."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = (
        entry_data.get("coordinator") if isinstance(entry_data, dict) else None
    )
    if coordinator is None:
        device_id = (
            entry_data.get("device_id", "")
            if isinstance(entry_data, dict)
            else ""
        )
        return {"device_id": device_id}

    diagnostics = coordinator.get_diagnostics()
    return {
        field: diagnostics.get(field)
        for field in _CONNECTION_FIELDS
        if field in diagnostics
    }
