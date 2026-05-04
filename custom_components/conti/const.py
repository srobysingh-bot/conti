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
CONF_DEVICE_PROFILE: Final = "device_profile"  # profile id from device_profiles.py
CONF_MAPPING_SOURCE: Final = "mapping_source"  # "auto" | "cloud" | "learn" | "manual"
CONF_MAPPING_CONFIDENCE: Final = "mapping_confidence"  # 0.0–1.0
CONF_TUYA_CATEGORY: Final = "tuya_category"  # Tuya product category (e.g. "dj")
CONF_LOW_POWER_DEVICE: Final = "low_power_device"
CONF_RUNTIME_CHANNEL: Final = "runtime_channel"
CONF_CLOUD_ACCESS_ID: Final = "cloud_access_id"
CONF_CLOUD_ACCESS_SECRET: Final = "cloud_access_secret"
CONF_CLOUD_REGION: Final = "cloud_region"
CONF_EXTERNAL_ON_PROFILE: Final = "external_on_profile"  # legacy JSON blob
CONF_IR_CATEGORY: Final = "ir_category"
CONF_IR_BRAND: Final = "ir_brand"
CONF_IR_MODEL: Final = "ir_model"

# OAuth / Smart Life global cloud account
CONF_OAUTH_CONFIGURED: Final = "oauth_configured"
CONF_OAUTH_UID: Final = "oauth_uid"

# Environment variable names for app-level Tuya IoT credentials.
# The Smart Life flow reads these at runtime — nothing is hardcoded.
# Set TUYA_APP_ACCESS_ID and TUYA_APP_ACCESS_SECRET in your HA environment.
TUYA_APP_ACCESS_ID_ENV: Final = "TUYA_APP_ACCESS_ID"
TUYA_APP_ACCESS_SECRET_ENV: Final = "TUYA_APP_ACCESS_SECRET"

# Runtime channel for full cloud devices (no local_key)
RUNTIME_CHANNEL_CLOUD: Final = "cloud"

# External-ON correction — individual UI fields
CONF_EXTERNAL_ON_ENABLED: Final = "external_on_enabled"
CONF_EXTERNAL_ON_APPLY: Final = "external_on_apply"
CONF_MORNING_START: Final = "morning_start_time"
CONF_MORNING_END: Final = "morning_end_time"
CONF_MORNING_BRIGHTNESS: Final = "morning_brightness_pct"
CONF_MORNING_KELVIN: Final = "morning_kelvin"
CONF_DAY_START: Final = "day_start_time"
CONF_DAY_END: Final = "day_end_time"
CONF_DAY_BRIGHTNESS: Final = "day_brightness_pct"
CONF_DAY_KELVIN: Final = "day_kelvin"
CONF_NIGHT_START: Final = "night_start_time"
CONF_NIGHT_END: Final = "night_end_time"
CONF_NIGHT_BRIGHTNESS: Final = "night_brightness_pct"
CONF_NIGHT_KELVIN: Final = "night_kelvin"

# ---------------------------------------------------------------------------
# Supported device types
# ---------------------------------------------------------------------------
DEVICE_TYPE_LIGHT: Final = "light"
DEVICE_TYPE_FAN: Final = "fan"
DEVICE_TYPE_CLIMATE: Final = "climate"
DEVICE_TYPE_SWITCH: Final = "switch"
DEVICE_TYPE_SENSOR: Final = "sensor"
DEVICE_TYPE_IR: Final = "ir"

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
RUNTIME_CHANNEL_LOCAL: Final = "local"
RUNTIME_CHANNEL_CLOUD_SENSOR: Final = "cloud_sensor"
RUNTIME_CHANNEL_IR: Final = "ir"

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
PLATFORMS: Final = ["light", "switch", "sensor", "fan", "climate", "remote"]

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
DP_KEY_DOOR_STATE: Final = "door_state"
# Energy monitoring (switches / plugs)
DP_KEY_ENERGY_TOTAL: Final = "energy_total"
DP_KEY_CURRENT: Final = "current"
DP_KEY_VOLTAGE: Final = "voltage"

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
