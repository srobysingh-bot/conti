"""Home Assistant diagnostics support for Conti devices."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_GLOBAL_SETTINGS
from .offline_alerts import offline_alert_config_from_mapping

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
    "offline_alert_enabled",
    "using_global_alert_settings",
    "alerts_disabled_for_this_device",
    "offline_alert_sent",
    "offline_since",
    "offline_alert_reason",
    "offline_alert_last_sent",
    "developer_alert_notify_service",
)

_GLOBAL_ALERT_FIELDS = (
    "global_offline_alerts_enabled",
    "global_offline_alert_delay_minutes",
    "global_notify_service",
    "global_recovery_email_enabled",
    "developer_alert_email",
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return connection-focused diagnostics without config-entry secrets."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GLOBAL_SETTINGS:
        values = dict(entry.data)
        values.update(entry.options)
        config = offline_alert_config_from_mapping(values)
        diagnostics = {
            "global_offline_alerts_enabled": config.enabled,
            "global_offline_alert_delay_minutes": config.delay_minutes,
            "global_notify_service": config.notify_service,
            "global_recovery_email_enabled": config.send_recovery_email,
            "developer_alert_email": "**REDACTED**" if config.email else "",
        }
        return {field: diagnostics[field] for field in _GLOBAL_ALERT_FIELDS}

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
