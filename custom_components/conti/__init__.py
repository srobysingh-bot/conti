"""Conti — Local LAN Control for Tuya-based IoT Devices.

A local-first Home Assistant custom integration for Tuya devices.

Standard devices (lights, switches, plugs, fans, climate) use local TCP
runtime only. Low-power sleepy sensors can use an isolated cloud status
runtime path when explicitly flagged during onboarding.

Architecture
~~~~~~~~~~~~
* **One** :class:`DeviceManager` singleton lives at
  ``hass.data[DOMAIN]["manager"]`` and is shared across ALL config entries.
* **One** :class:`ContiCoordinator` per config entry (= per device).
* Entity platforms read state from the coordinator and send commands
  through ``coordinator.device_manager``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CLOUD_ACCESS_ID,
    CONF_CLOUD_ACCESS_SECRET,
    CONF_CLOUD_REGION,
    CONF_DETECTED_VERSION,
    CONF_DEVICE_ID,
    CONF_LOW_POWER_DEVICE,
    CONF_DEVICE_PROFILE,
    CONF_DEVICE_TYPE,
    CONF_DISCOVERED_DPS,
    CONF_DP_MAP,
    CONF_LOCAL_KEY,
    CONF_MAPPING_SOURCE,
    CONF_PROTOCOL_VERSION,
    CONF_RUNTIME_CHANNEL,
    CONF_TUYA_CATEGORY,
    CONF_VERBOSE_LOGGING,
    DEFAULT_PORT,
    DEFAULT_PROTOCOL_VERSION,
    DEVICE_TYPE_SENSOR,
    DOMAIN,
    PLATFORMS,
    RUNTIME_CHANNEL_CLOUD,
    RUNTIME_CHANNEL_CLOUD_SENSOR,
    STORAGE_VERSION,
)
from .dp_mapping import mask_key

_LOGGER = logging.getLogger(__name__)

_MANAGER_KEY = "manager"
_REF_COUNT_KEY = "manager_ref_count"
_OAUTH_KEY = "oauth_manager"


def _parse_dp_map(entry: ConfigEntry) -> dict[str, Any]:
    """Parse the dp_map from entry data or options (JSON string or dict)."""
    raw = entry.options.get(CONF_DP_MAP) or entry.data.get(CONF_DP_MAP) or "{}"
    if isinstance(raw, str):
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            result = {}
    else:
        result = raw
    return result if isinstance(result, dict) else {}


def _effective_version(entry: ConfigEntry) -> str:
    """Return the protocol version to use at runtime.

    Prefers a previously auto-detected version, then falls back to what
    the user selected (which may be ``"auto"`` for first-time detection).
    """
    return (
        entry.data.get(CONF_DETECTED_VERSION)
        or entry.data.get(CONF_PROTOCOL_VERSION, DEFAULT_PROTOCOL_VERSION)
    )


async def _load_dps_cache(hass: HomeAssistant, device_id: str) -> dict[str, Any]:
    """Load cached DPS from persistent storage."""
    store: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, f"conti_{device_id}"
    )
    data = await store.async_load()
    if data and isinstance(data, dict):
        return data.get("dps", {})
    return {}


async def _save_dps_cache(
    hass: HomeAssistant, device_id: str, dps: dict[str, Any]
) -> None:
    """Persist discovered DPS to ``.storage/conti_<device_id>.json``."""
    store: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, f"conti_{device_id}"
    )
    await store.async_save({"dps": dps})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a single Conti device from a config entry."""
    # Lazy imports to avoid cascading import errors during config-flow loading
    from .coordinator import ContiCoordinator  # noqa: PLC0415
    from .device_manager import DeviceManager  # noqa: PLC0415

    hass.data.setdefault(DOMAIN, {})

    # --- Apply verbose logging from options --------------------------------
    if entry.options.get(CONF_VERBOSE_LOGGING, False):
        logging.getLogger("custom_components.conti").setLevel(logging.DEBUG)
        _LOGGER.debug("Verbose logging enabled for Conti via options flow")

    # ---- Singleton DeviceManager ------------------------------------------
    if _MANAGER_KEY not in hass.data[DOMAIN]:
        manager = DeviceManager()
        await manager.start()
        hass.data[DOMAIN][_MANAGER_KEY] = manager
        hass.data[DOMAIN][_REF_COUNT_KEY] = 0
        _LOGGER.info("Conti DeviceManager created (singleton)")

    manager: DeviceManager = hass.data[DOMAIN][_MANAGER_KEY]
    hass.data[DOMAIN][_REF_COUNT_KEY] = (
        hass.data[DOMAIN].get(_REF_COUNT_KEY, 0) + 1
    )

    # ---- Build device config for the manager ------------------------------
    device_id: str = entry.data[CONF_DEVICE_ID]
    dp_map = _parse_dp_map(entry)
    version = _effective_version(entry)

    device_config: dict[str, Any] = {
        "device_id": device_id,
        "host": entry.data.get(CONF_HOST, ""),
        "port": entry.data.get(CONF_PORT, DEFAULT_PORT),
        "local_key": entry.data.get(CONF_LOCAL_KEY, ""),
        "protocol_version": version,
        "dp_map": dp_map,
    }

    low_power_sensor = bool(
        entry.data.get(CONF_LOW_POWER_DEVICE, False)
        and entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_SENSOR
        and entry.data.get(CONF_RUNTIME_CHANNEL) == RUNTIME_CHANNEL_CLOUD_SENSOR
    )

    cloud_only_device = bool(
        entry.data.get(CONF_RUNTIME_CHANNEL) == RUNTIME_CHANNEL_CLOUD
        and not low_power_sensor
    )

    local_key_str = str(entry.data.get(CONF_LOCAL_KEY, "")).strip()

    _LOGGER.info(
        "Setting up Conti device %s at %s:%d (v%s, key=%s, dp_map keys=%s, "
        "profile=%s, mapping_source=%s, runtime=%s)",
        device_id,
        device_config["host"],
        device_config["port"],
        version,
        mask_key(local_key_str) if local_key_str else "none",
        list(dp_map.keys()) if dp_map else "none",
        entry.data.get(CONF_DEVICE_PROFILE, "none"),
        entry.data.get(CONF_MAPPING_SOURCE, "legacy"),
        entry.data.get(CONF_RUNTIME_CHANNEL, "local"),
    )

    low_power_runtime = None
    cloud_fallback_runtime = None

    if low_power_sensor:
        access_id = str(entry.data.get(CONF_CLOUD_ACCESS_ID, "")).strip()
        access_secret = str(entry.data.get(CONF_CLOUD_ACCESS_SECRET, "")).strip()
        region = str(entry.data.get(CONF_CLOUD_REGION, "eu")).strip() or "eu"

        if access_id and access_secret:
            from .low_power_runtime import LowPowerSensorCloudRuntime  # noqa: PLC0415

            low_power_runtime = LowPowerSensorCloudRuntime(
                device_id=device_id,
                access_id=access_id,
                access_secret=access_secret,
                region=region,
                dp_map=dp_map,
            )
            _LOGGER.info(
                "Setting up low-power cloud runtime for sensor %s (category=%s)",
                device_id,
                entry.data.get(CONF_TUYA_CATEGORY, ""),
            )
        else:
            _LOGGER.warning(
                "Low-power sensor %s has no cloud credentials; falling back to local runtime",
                device_id,
            )
            low_power_sensor = False

    elif cloud_only_device:
        # Cloud-only device (no local_key) — use global OAuth manager.
        from .tuya_oauth import TuyaOAuthManager  # noqa: PLC0415

        if _OAUTH_KEY not in hass.data[DOMAIN]:
            oauth = TuyaOAuthManager(hass)
            await oauth.async_load()
            hass.data[DOMAIN][_OAUTH_KEY] = oauth

        oauth_mgr = hass.data[DOMAIN][_OAUTH_KEY]

        if oauth_mgr.is_configured:
            from .cloud_device_runtime import CloudDeviceRuntime  # noqa: PLC0415

            cloud_fallback_runtime = CloudDeviceRuntime(
                device_id=device_id,
                oauth_manager=oauth_mgr,
                dp_map=dp_map,
            )
            _LOGGER.info(
                "Setting up cloud-only runtime for device %s via OAuth",
                device_id,
            )
        else:
            # Try per-entry credentials as fallback.
            access_id = str(entry.data.get(CONF_CLOUD_ACCESS_ID, "")).strip()
            access_secret = str(entry.data.get(CONF_CLOUD_ACCESS_SECRET, "")).strip()
            region = str(entry.data.get(CONF_CLOUD_REGION, "eu")).strip() or "eu"
            if access_id and access_secret:
                from .low_power_runtime import LowPowerSensorCloudRuntime  # noqa: PLC0415

                cloud_fallback_runtime = LowPowerSensorCloudRuntime(
                    device_id=device_id,
                    access_id=access_id,
                    access_secret=access_secret,
                    region=region,
                    dp_map=dp_map,
                )
                _LOGGER.info(
                    "Setting up cloud-only runtime for device %s via per-entry credentials",
                    device_id,
                )
            else:
                _LOGGER.warning(
                    "Cloud-only device %s has no OAuth or per-entry credentials; "
                    "device will not be functional until credentials are configured",
                    device_id,
                )

    if not low_power_sensor and not cloud_only_device:
        await manager.add_device(device_config)

    # ---- Persist auto-detected version back to entry data -----------------
    if (
        entry.data.get(CONF_PROTOCOL_VERSION) == "auto"
        and not entry.data.get(CONF_DETECTED_VERSION)
    ):
        client = manager.get_client(device_id)
        if client and client.detected_version:
            new_data = dict(entry.data)
            new_data[CONF_DETECTED_VERSION] = client.detected_version
            hass.config_entries.async_update_entry(entry, data=new_data)
            _LOGGER.info(
                "Persisted auto-detected protocol v%s for %s",
                client.detected_version, device_id,
            )

    # ---- Load DPS cache from storage & feed into manager ------------------
    cached_dps = await _load_dps_cache(hass, device_id)
    if cached_dps:
        _LOGGER.debug(
            "Loaded %d cached DPs for %s from storage", len(cached_dps), device_id,
        )
        manager.seed_cached_dps(device_id, cached_dps)

    # ---- Per-device coordinator -------------------------------------------
    coordinator = ContiCoordinator(
        hass,
        manager,
        device_id,
        low_power_cloud=low_power_runtime,
        cloud_fallback=cloud_fallback_runtime,
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "device_id": device_id,
    }

    # Initial coordinator refresh (populates entity data)
    await coordinator.async_config_entry_first_refresh()

    # ---- Save discovered DPS to persistent cache --------------------------
    current_dps = (
        manager.get_cached_dps(device_id)
        if not low_power_sensor and not cloud_only_device
        else {}
    )
    if current_dps:
        await _save_dps_cache(hass, device_id, current_dps)

    # Forward to entity platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Conti config entry — disconnect device and clean up."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})
        device_id = entry_data.get("device_id") or entry.data.get(CONF_DEVICE_ID)

        manager = hass.data[DOMAIN].get(_MANAGER_KEY)
        runtime_channel = entry.data.get(CONF_RUNTIME_CHANNEL, "local")
        is_local_device = runtime_channel not in (
            RUNTIME_CHANNEL_CLOUD, RUNTIME_CHANNEL_CLOUD_SENSOR
        )

        if manager and device_id and is_local_device:
            # Persist final DPS snapshot before cleanup
            final_dps = manager.get_cached_dps(device_id)
            if final_dps:
                await _save_dps_cache(hass, device_id, final_dps)

            # Callback cleanup + disconnect handled inside
            # remove_device (coordinator.async_shutdown already
            # unregistered its specific callback).
            await manager.remove_device(device_id)

        # Decrement ref count — stop manager when last entry unloads
        ref_count = hass.data[DOMAIN].get(_REF_COUNT_KEY, 1) - 1
        hass.data[DOMAIN][_REF_COUNT_KEY] = ref_count

        if ref_count <= 0 and manager:
            await manager.stop()
            hass.data[DOMAIN].pop(_MANAGER_KEY, None)
            hass.data[DOMAIN].pop(_REF_COUNT_KEY, None)
            _LOGGER.info("Conti DeviceManager stopped (last entry unloaded)")

        _LOGGER.info("Unloaded Conti device %s", device_id)

    return unload_ok
