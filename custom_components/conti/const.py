"""Constants for the Conti integration."""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Integration identifiers
# ---------------------------------------------------------------------------
DOMAIN: Final = "conti"
MANUFACTURER: Final = "Conti"

# ---------------------------------------------------------------------------
# Config-entry data keys
# ---------------------------------------------------------------------------
CONF_DEVICE_ID: Final = "device_id"
CONF_LOCAL_KEY: Final = "local_key"
CONF_PROTOCOL_VERSION: Final = "protocol_version"
CONF_DEVICE_TYPE: Final = "device_type"
CONF_DP_MAP: Final = "dp_map"
CONF_DETECTED_VERSION: Final = "detected_version"
CONF_DISCOVERED_DPS: Final = "discovered_dps"
CONF_VERBOSE_LOGGING: Final = "verbose_logging"

# ---------------------------------------------------------------------------
# Supported device types
# ---------------------------------------------------------------------------
DEVICE_TYPE_LIGHT: Final = "light"
DEVICE_TYPE_FAN: Final = "fan"
DEVICE_TYPE_CLIMATE: Final = "climate"
DEVICE_TYPE_SWITCH: Final = "switch"
DEVICE_TYPE_SENSOR: Final = "sensor"

SUPPORTED_DEVICE_TYPES: Final = [
    DEVICE_TYPE_LIGHT,
    DEVICE_TYPE_FAN,
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_SWITCH,
    DEVICE_TYPE_SENSOR,
]

# ---------------------------------------------------------------------------
# Protocol defaults
# ---------------------------------------------------------------------------
DEFAULT_PORT: Final = 6668
DEFAULT_PROTOCOL_VERSION: Final = "auto"
SUPPORTED_VERSIONS: Final = ["auto", "3.1", "3.3", "3.4", "3.5"]
AUTO_DETECT_ORDER: Final = ["3.3", "3.4", "3.5", "3.1"]

# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------
DEFAULT_SCAN_INTERVAL: Final = 10  # seconds

# ---------------------------------------------------------------------------
# Platforms to forward
# ---------------------------------------------------------------------------
PLATFORMS: Final = ["light", "switch", "sensor", "fan", "climate"]

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
STORAGE_KEY: Final = f"{DOMAIN}_device_cache"
STORAGE_VERSION: Final = 1

# ---------------------------------------------------------------------------
# DP-map well-known keys
# ---------------------------------------------------------------------------
# Light
DP_KEY_POWER: Final = "power"
DP_KEY_BRIGHTNESS: Final = "brightness"
DP_KEY_COLOR_TEMP: Final = "color_temp"
DP_KEY_COLOR_RGB: Final = "color_rgb"
# Fan
DP_KEY_FAN_POWER: Final = "fan_power"  # separate power DP for combo (fan+light)
DP_KEY_FAN_SPEED: Final = "fan_speed"
DP_KEY_FAN_DIRECTION: Final = "fan_direction"
DP_KEY_FAN_OSCILLATION: Final = "fan_oscillation"
# Climate
DP_KEY_TARGET_TEMP: Final = "target_temp"
DP_KEY_CURRENT_TEMP: Final = "current_temp"
DP_KEY_HVAC_MODE: Final = "hvac_mode"
DP_KEY_FAN_MODE: Final = "fan_mode"
# Sensor
DP_KEY_TEMPERATURE: Final = "temperature"
DP_KEY_HUMIDITY: Final = "humidity"
DP_KEY_POWER_USAGE: Final = "power_usage"
DP_KEY_BATTERY: Final = "battery"
DP_KEY_MOTION: Final = "motion"
DP_KEY_CONTACT: Final = "contact"

# ---------------------------------------------------------------------------
# Reconnect
# ---------------------------------------------------------------------------
RECONNECT_BASE_DELAY: Final = 2.0
RECONNECT_MAX_DELAY: Final = 60.0

# ---------------------------------------------------------------------------
# Stability
# ---------------------------------------------------------------------------
MAX_CONSECUTIVE_FAILURES: Final = 5  # mark UpdateFailed after this many

# ---------------------------------------------------------------------------
# Activity tracking
# ---------------------------------------------------------------------------
COMMAND_TRACK_WINDOW: Final = 6.0  # seconds — match entity stale-protect

# ---------------------------------------------------------------------------
# Example DP maps  (used in config-flow description only)
# ---------------------------------------------------------------------------
EXAMPLE_DP_MAP_LIGHT: Final = {
    "1": {"key": DP_KEY_POWER, "type": "bool"},
    "3": {"key": DP_KEY_BRIGHTNESS, "type": "int", "min": 10, "max": 1000},
    "4": {"key": DP_KEY_COLOR_TEMP, "type": "int", "min": 0, "max": 1000},
}

EXAMPLE_DP_MAP_SWITCH: Final = {
    "1": {"key": DP_KEY_POWER, "type": "bool"},
}

EXAMPLE_DP_MAP_SENSOR: Final = {
    "1": {"key": DP_KEY_TEMPERATURE, "type": "int", "scale": 10},
    "2": {"key": DP_KEY_HUMIDITY, "type": "int"},
    "3": {"key": DP_KEY_BATTERY, "type": "int"},
}
