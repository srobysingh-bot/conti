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

import ipaddress
import json
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CLOUD_ACCESS_ID,
    CONF_CLOUD_ACCESS_SECRET,
    CONF_CLOUD_REGION,
    CONF_DEFERRED_LOCAL_CONNECT,
    CONF_DETECTED_VERSION,
    CONF_DEVICE_ID,
    CONF_DEVICE_PROFILE,
    CONF_DEVICE_TYPE,
    CONF_DP_MAP,
    CONF_ENABLE_AUTO_RECONNECT,
    CONF_IR_BRAND,
    CONF_IR_BRAND_ID,
    CONF_IR_CATEGORY_ID,
    CONF_IR_INFRARED_ID,
    CONF_IR_MODEL,
    CONF_IR_REMOTE_ID,
    CONF_IR_REMOTE_INDEX,
    CONF_LOCAL_KEY,
    CONF_LOW_POWER_DEVICE,
    CONF_MAPPING_SOURCE,
    CONF_PROTOCOL_VERSION,
    CONF_RUNTIME_CHANNEL,
    CONF_TUYA_CATEGORY,
    CONF_VERBOSE_LOGGING,
    DEFAULT_ENABLE_AUTO_RECONNECT,
    DEFAULT_PORT,
    DEFAULT_PROTOCOL_VERSION,
    DEVICE_TYPE_SENSOR,
    DOMAIN,
    PLATFORMS,
    RUNTIME_CHANNEL_CLOUD,
    RUNTIME_CHANNEL_CLOUD_SENSOR,
    RUNTIME_CHANNEL_IR,
    STORAGE_VERSION,
)
from .dp_mapping import mask_key

_LOGGER = logging.getLogger(__name__)

_MANAGER_KEY = "manager"
_REF_COUNT_KEY = "manager_ref_count"
_OAUTH_KEY = "oauth_manager"
_IR_MANAGER_KEY = "ir_manager"
_IR_SERVICES_REGISTERED = "ir_services_registered"
_RECONNECT_SERVICES_REGISTERED = "reconnect_services_registered"


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload runtime state when reconnect or other options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_network_diagnostics(
    hass: HomeAssistant, device_ip: str
) -> tuple[str, str]:
    """Return HA's routed source IP and configured subnet when available."""
    try:
        from homeassistant.components.network import (  # noqa: PLC0415
            async_get_adapters,
            async_get_source_ip,
        )

        source_ip = str(await async_get_source_ip(hass, target_ip=device_ip))
        adapters = await async_get_adapters(hass)
        if not isinstance(adapters, list):
            return source_ip, "unknown"
        for adapter in adapters:
            if not isinstance(adapter, dict):
                continue
            for address in adapter.get("ipv4", []):
                if not isinstance(address, dict) or address.get("address") != source_ip:
                    continue
                prefix = int(address.get("network_prefix", 0))
                if prefix:
                    subnet = ipaddress.ip_network(
                        f"{source_ip}/{prefix}", strict=False
                    )
                    return source_ip, str(subnet)
        return source_ip, "unknown"
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Unable to resolve HA network diagnostics: %s", exc)
        return "unknown", "unknown"


def _register_reconnect_services(hass: HomeAssistant) -> None:
    """Register manager-owned reconnect services exactly once."""
    if hass.data[DOMAIN].get(_RECONNECT_SERVICES_REGISTERED):
        return

    async def _refresh(device_ids: set[str]) -> None:
        for entry_data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(entry_data, dict):
                continue
            if entry_data.get("device_id") not in device_ids:
                continue
            coordinator = entry_data.get("coordinator")
            if coordinator is not None:
                try:
                    await coordinator.async_request_refresh()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug(
                        "Post-reconnect coordinator refresh failed device=%s: %s",
                        entry_data.get("device_id"),
                        exc,
                        exc_info=exc,
                    )

    async def _handle_reconnect_device(call: Any) -> None:
        device_id = str(call.data["device_id"]).strip()
        manager = hass.data.get(DOMAIN, {}).get(_MANAGER_KEY)
        if manager is None or device_id not in manager.device_ids():
            raise HomeAssistantError("conti_device_not_found")
        success = await manager.reconnect_device(device_id)
        if success:
            await _refresh({device_id})

    async def _handle_reconnect_all(call: Any) -> None:
        manager = hass.data.get(DOMAIN, {}).get(_MANAGER_KEY)
        if manager is None:
            return
        results = await manager.reconnect_all()
        await _refresh({device_id for device_id, ok in results.items() if ok})

    hass.services.async_register(
        DOMAIN,
        "reconnect_device",
        _handle_reconnect_device,
        schema=vol.Schema({vol.Required("device_id"): str}),
    )
    hass.services.async_register(
        DOMAIN,
        "reconnect_all",
        _handle_reconnect_all,
        schema=vol.Schema({}),
    )
    hass.data[DOMAIN][_RECONNECT_SERVICES_REGISTERED] = True


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


def _dp_map_source(entry: ConfigEntry) -> str:
    """Return the normalized source of the effective saved DP map."""
    source = str(
        entry.options.get(CONF_MAPPING_SOURCE)
        or entry.data.get(CONF_MAPPING_SOURCE, "discovered")
    )
    if source == "cloud":
        return "cloud"
    if source in {"manual", "learn"} or (
        CONF_DP_MAP in entry.options and CONF_MAPPING_SOURCE not in entry.options
    ):
        return "manual"
    return "discovered"


def _effective_version(entry: ConfigEntry) -> str:
    """Return the protocol version to use at runtime.

    Prefers a previously auto-detected version, then falls back to what
    the user selected (which may be ``"auto"`` for first-time detection).
    """
    detected = entry.data.get(CONF_DETECTED_VERSION)
    configured = entry.data.get(
        CONF_PROTOCOL_VERSION, DEFAULT_PROTOCOL_VERSION
    )
    if (
        entry.data.get(CONF_DEFERRED_LOCAL_CONNECT, False)
        and not detected
        and configured == "auto"
    ):
        return "3.4"
    return detected or configured


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


def _register_ir_services(hass: HomeAssistant) -> None:
    """Register isolated IR services once."""
    if hass.data[DOMAIN].get(_IR_SERVICES_REGISTERED):
        return

    async def _handle_send_ir(call: Any) -> None:
        from .ir_manager import (  # noqa: PLC0415
            IRCommandNotConfigured,
            IRSendError,
        )

        device_id = str(call.data["device_id"]).strip()
        action = str(call.data.get("command") or call.data.get("action") or "").strip()
        if not action:
            raise ValueError("command required")
        for entry_data in hass.data.get(DOMAIN, {}).values():
            if (
                isinstance(entry_data, dict)
                and entry_data.get("device_id") == device_id
                and entry_data.get(_IR_MANAGER_KEY) is not None
            ):
                storage = entry_data.get("ir_storage")
                if (
                    storage is not None
                    and not _is_raw_ir_action(action)
                    and await storage.async_get_command(action) is None
                ):
                    raise HomeAssistantError("ir_command_not_found")
                try:
                    await entry_data[_IR_MANAGER_KEY].send_ir_command(
                        device_id, action
                    )
                    return
                except IRCommandNotConfigured as exc:
                    raise HomeAssistantError("ir_command_not_found") from exc
                except IRSendError as exc:
                    raise HomeAssistantError("ir_send_failed") from exc
        _LOGGER.warning(
            "IR command requested for unknown/unloaded IR device %s action=%s",
            device_id,
            action,
        )
        raise HomeAssistantError("ir_command_not_found")

    async def _handle_import_ir_pack(call: Any) -> None:
        device_id = str(call.data["device_id"]).strip()
        path = _resolve_ir_pack_path(hass, str(call.data["path"]).strip())
        overwrite = bool(call.data.get("overwrite", False))
        storage = _find_ir_storage(hass, device_id)
        if storage is None:
            raise HomeAssistantError("ir_device_not_found")
        imported = await storage.async_import_code_pack_file(path, overwrite=overwrite)
        _LOGGER.info(
            "Imported IR code pack device=%s path=%s commands=%d",
            device_id,
            path,
            imported,
        )

    async def _handle_export_ir_pack(call: Any) -> None:
        device_id = str(call.data["device_id"]).strip()
        path = _resolve_ir_pack_path(hass, str(call.data["path"]).strip())
        storage = _find_ir_storage(hass, device_id)
        if storage is None:
            raise HomeAssistantError("ir_device_not_found")
        await storage.async_export_code_pack_file(path)
        _LOGGER.info("Exported IR code pack device=%s path=%s", device_id, path)

    async def _handle_test_ir_raw_emit(call: Any) -> None:
        device_id = str(call.data["device_id"]).strip()
        transport_mode = str(call.data.get("transport_mode", "cloud")).strip()
        remote_id = str(call.data.get("remote_id", "")).strip()
        raw_payload = _raw_payload_from_service(call.data)
        if raw_payload in (None, "", {}, []):
            raise HomeAssistantError("ir_raw_payload_required")
        manager = _find_ir_manager(hass, device_id)
        if manager is None:
            raise HomeAssistantError("ir_device_not_found")
        try:
            await manager.test_raw_emit(
                device_id,
                raw_payload,
                transport_mode=transport_mode,
                remote_id=remote_id,
            )
        except HomeAssistantError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HomeAssistantError("ir_send_failed") from exc

    async def _handle_resend_last_ir(call: Any) -> None:
        device_id = str(call.data["device_id"]).strip()
        manager = _find_ir_manager(hass, device_id)
        if manager is None:
            raise HomeAssistantError("ir_device_not_found")
        try:
            await manager.resend_last(device_id)
        except HomeAssistantError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HomeAssistantError("ir_send_failed") from exc

    async def _handle_debug_list_ir_remotes(call: Any) -> None:
        device_id = str(call.data["device_id"]).strip()
        entry_data = _find_ir_entry_data(hass, device_id)
        if entry_data is None:
            raise HomeAssistantError("ir_device_not_found")
        cloud = getattr(entry_data.get(_IR_MANAGER_KEY), "_cloud", None)
        storage = entry_data.get("ir_storage")
        if cloud is None or storage is None:
            raise HomeAssistantError("ir_cloud_not_available")
        infrared_id = await storage.async_infrared_id()
        if not infrared_id:
            infrared_id = await cloud.resolve_infrared_id(device_id)
        remotes = await cloud.list_device_remotes(device_id)
        _LOGGER.warning(
            "IR DEBUG remotes: infrared_id=%s available_remotes=%s",
            infrared_id,
            [
                {
                    "remote_id": remote.get("remote_id") or remote.get("id"),
                    "remote_name": remote.get("remote_name") or remote.get("name"),
                    "category": remote.get("category_id") or remote.get("category"),
                    "remote_index": remote.get("remote_index"),
                }
                for remote in remotes
            ],
        )

    hass.services.async_register(
        DOMAIN,
        "send_ir_command",
        _handle_send_ir,
        schema=vol.Schema(
            {
                vol.Required("device_id"): str,
                vol.Optional("command"): str,
                vol.Optional("action"): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "import_ir_code_pack",
        _handle_import_ir_pack,
        schema=vol.Schema(
            {
                vol.Required("device_id"): str,
                vol.Required("path"): str,
                vol.Optional("overwrite", default=False): bool,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "export_ir_code_pack",
        _handle_export_ir_pack,
        schema=vol.Schema(
            {
                vol.Required("device_id"): str,
                vol.Required("path"): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "test_ir_raw_emit",
        _handle_test_ir_raw_emit,
        schema=vol.Schema(
            {
                vol.Required("device_id"): str,
                vol.Optional("raw"): object,
                vol.Optional("code"): object,
                vol.Optional("payload"): object,
                vol.Optional("transport_mode", default="cloud"): vol.In(
                    ["cloud", "raw_runtime", "tuya", "local"]
                ),
                vol.Optional("remote_id"): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "resend_last_ir",
        _handle_resend_last_ir,
        schema=vol.Schema({vol.Required("device_id"): str}),
    )
    hass.services.async_register(
        DOMAIN,
        "debug_list_ir_remotes",
        _handle_debug_list_ir_remotes,
        schema=vol.Schema({vol.Required("device_id"): str}),
    )
    hass.data[DOMAIN][_IR_SERVICES_REGISTERED] = True


def _find_ir_storage(hass: HomeAssistant, device_id: str) -> Any | None:
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if (
            isinstance(entry_data, dict)
            and entry_data.get("device_id") == device_id
            and entry_data.get("ir_storage") is not None
        ):
            return entry_data["ir_storage"]
    return None


def _find_ir_manager(hass: HomeAssistant, device_id: str) -> Any | None:
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if (
            isinstance(entry_data, dict)
            and entry_data.get("device_id") == device_id
            and entry_data.get(_IR_MANAGER_KEY) is not None
        ):
            return entry_data[_IR_MANAGER_KEY]
    return None


def _find_ir_entry_data(hass: HomeAssistant, device_id: str) -> dict[str, Any] | None:
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and entry_data.get("device_id") == device_id:
            return entry_data
    return None


def _raw_payload_from_service(data: dict[str, Any]) -> Any:
    for key in ("raw", "code", "payload"):
        value = data.get(key)
        if value not in (None, "", {}, []):
            if isinstance(value, str) and value.startswith(("raw:", "base64:")):
                return {"code": value.split(":", 1)[1].strip()}
            return value
    return None


def _select_ir_runtime_remote(
    remotes: list[dict[str, Any]],
    *,
    brand: str = "",
    model: str = "",
) -> dict[str, Any] | None:
    """Pick the most likely Tuya runtime remote for an IR entry."""
    best: tuple[int, dict[str, Any]] | None = None
    brand = brand.lower()
    model = model.lower().replace("_", " ")
    for remote in remotes:
        remote_id = str(remote.get("remote_id") or remote.get("id") or "").strip()
        if not remote_id:
            continue
        score = 1
        haystack = " ".join(
            str(remote.get(key) or "").lower().replace("_", " ")
            for key in ("name", "brand_id", "category_id", "remote_index")
        )
        if brand and brand in haystack:
            score += 4
        if model and model in haystack:
            score += 4
        if any(token in haystack for token in ("ac", "air", "condition", "kt")):
            score += 2
        if best is None or score > best[0]:
            best = (score, remote)
    return best[1] if best else None


def _resolve_ir_pack_path(hass: HomeAssistant, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(hass.config.path(raw_path))
    return path


def _is_raw_ir_action(action: str) -> bool:
    return str(action).strip().startswith(("raw:", "base64:"))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a single Conti device from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    if entry.data.get(CONF_RUNTIME_CHANNEL) == RUNTIME_CHANNEL_IR:
        from .ir_cloud import TuyaIRCloud  # noqa: PLC0415
        from .ir_manager import IRManager  # noqa: PLC0415
        from .ir_storage import IRStorage  # noqa: PLC0415
        from .tuya_oauth import TuyaOAuthManager  # noqa: PLC0415

        device_id = entry.data[CONF_DEVICE_ID]
        storage = IRStorage(hass, device_id)
        await storage.async_load()

        oauth = TuyaOAuthManager(hass, entry_id=entry.entry_id)
        await oauth.async_load()
        if not oauth.is_configured:
            oauth_global = TuyaOAuthManager(hass)
            await oauth_global.async_load()
            oauth = oauth_global

        cloud = TuyaIRCloud(oauth) if oauth.is_configured else None
        infrared_id = str(entry.data.get(CONF_IR_INFRARED_ID, "")).strip()
        remote_id = str(entry.data.get(CONF_IR_REMOTE_ID, "")).strip()
        if cloud is not None and (not infrared_id or not remote_id):
            try:
                infrared_id = infrared_id or await cloud.resolve_infrared_id(device_id)
                if not remote_id:
                    remotes = await cloud.list_device_remotes(device_id)
                    selected_remote = _select_ir_runtime_remote(
                        remotes,
                        brand=str(entry.data.get(CONF_IR_BRAND) or ""),
                        model=str(entry.data.get(CONF_IR_MODEL) or ""),
                    )
                    if selected_remote:
                        remote_id = str(
                            selected_remote.get("remote_id")
                            or selected_remote.get("id")
                            or ""
                        ).strip()
                        _LOGGER.info(
                            "Resolved Tuya AC remote: infrared_id=%s remote_id=%s "
                            "remote_name=%s category=%s",
                            infrared_id,
                            remote_id,
                            selected_remote.get("remote_name")
                            or selected_remote.get("name")
                            or remote_id,
                            selected_remote.get("category_id")
                            or selected_remote.get("category")
                            or "air_conditioner",
                        )
                _LOGGER.info(
                    "IR runtime metadata setup device=%s infrared_id=%s remote_id=%s",
                    device_id,
                    infrared_id,
                    remote_id,
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "IR runtime metadata setup failed device=%s error=%s",
                    device_id,
                    exc,
                )
        await storage.async_update_runtime_metadata(
            infrared_id=infrared_id,
            remote_id=remote_id,
            remote_index=str(entry.data.get(CONF_IR_REMOTE_INDEX, "")).strip(),
            category_id=str(entry.data.get(CONF_IR_CATEGORY_ID, "")).strip(),
            brand_id=str(entry.data.get(CONF_IR_BRAND_ID, "")).strip(),
        )
        if (
            infrared_id != str(entry.data.get(CONF_IR_INFRARED_ID, "")).strip()
            or remote_id != str(entry.data.get(CONF_IR_REMOTE_ID, "")).strip()
        ):
            new_data = dict(entry.data)
            if infrared_id:
                new_data[CONF_IR_INFRARED_ID] = infrared_id
            if remote_id:
                new_data[CONF_IR_REMOTE_ID] = remote_id
            hass.config_entries.async_update_entry(entry, data=new_data)
        ir_manager = IRManager(
            storage,
            cloud,
            host=str(entry.data.get(CONF_HOST, "")).strip(),
            port=int(entry.data.get(CONF_PORT, DEFAULT_PORT) or DEFAULT_PORT),
        )
        hass.data[DOMAIN][entry.entry_id] = {
            "device_id": device_id,
            _IR_MANAGER_KEY: ir_manager,
            "ir_storage": storage,
        }
        _register_ir_services(hass)
        ir_platforms = [Platform.REMOTE, Platform.CLIMATE]
        _LOGGER.info("Forwarding Conti IR entry %s to platforms %s", entry.entry_id, ir_platforms)
        await hass.config_entries.async_forward_entry_setups(entry, ir_platforms)
        _LOGGER.info(
            "Set up Conti IR device %s (category=%s)",
            device_id,
            entry.data.get(CONF_TUYA_CATEGORY, "infrared"),
        )
        return True

    # Lazy imports to avoid cascading import errors during config-flow loading.
    from .coordinator import ContiCoordinator  # noqa: PLC0415
    from .device_manager import DeviceManager  # noqa: PLC0415

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

    _register_reconnect_services(hass)

    manager: DeviceManager = hass.data[DOMAIN][_MANAGER_KEY]
    hass.data[DOMAIN][_REF_COUNT_KEY] = (
        hass.data[DOMAIN].get(_REF_COUNT_KEY, 0) + 1
    )

    # ---- Build device config for the manager ------------------------------
    device_id: str = entry.data[CONF_DEVICE_ID]
    dp_map = _parse_dp_map(entry)
    version = _effective_version(entry)
    ha_host_ip, ha_host_subnet = await _async_network_diagnostics(
        hass, str(entry.data.get(CONF_HOST, ""))
    )

    device_config: dict[str, Any] = {
        "device_id": device_id,
        "host": entry.data.get(CONF_HOST, ""),
        "port": entry.data.get(CONF_PORT, DEFAULT_PORT),
        "local_key": entry.data.get(CONF_LOCAL_KEY, ""),
        "device_type": entry.data.get(CONF_DEVICE_TYPE, ""),
        "protocol_version": version,
        "dp_map": dp_map,
        "dp_map_source": _dp_map_source(entry),
        "deferred_local_connect": bool(
            entry.data.get(CONF_DEFERRED_LOCAL_CONNECT, False)
        ),
        "enable_auto_reconnect": bool(
            entry.options.get(
                CONF_ENABLE_AUTO_RECONNECT, DEFAULT_ENABLE_AUTO_RECONNECT
            )
        ),
        "ha_host_ip": ha_host_ip,
        "ha_host_subnet": ha_host_subnet,
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
    cloud_availability_runtime = None
    cloud_seed_dps: dict[str, Any] = {}

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
        # Cloud-only device (no local_key) — use per-entry OAuth manager.
        # Each entry gets its own isolated storage key to prevent cross-account
        # token leakage when multiple Smart Life accounts are used.
        from .tuya_oauth import TuyaOAuthManager  # noqa: PLC0415

        entry_oauth_key = f"{_OAUTH_KEY}_{entry.entry_id}"
        if entry_oauth_key not in hass.data[DOMAIN]:
            oauth = TuyaOAuthManager(hass, entry_id=entry.entry_id)
            await oauth.async_load()
            # Fall back to the global onboarding store if the per-entry store
            # is empty (first load after upgrading from a version without
            # per-entry keys, or entry created before isolation was added).
            if not oauth.is_configured:
                oauth_global = TuyaOAuthManager(hass)
                await oauth_global.async_load()
                if oauth_global.is_configured:
                    _LOGGER.debug(
                        "Migrating global OAuth store to per-entry key for %s",
                        entry.entry_id,
                    )
                    oauth = oauth_global
                    # Re-create manager bound to this entry_id so future saves
                    # go to the per-entry store key.
                    oauth_bound = TuyaOAuthManager(hass, entry_id=entry.entry_id)
                    oauth_bound._access_id = oauth._access_id  # noqa: SLF001
                    oauth_bound._access_secret = oauth._access_secret  # noqa: SLF001
                    oauth_bound._region = oauth._region  # noqa: SLF001
                    oauth_bound._user_code = oauth._user_code  # noqa: SLF001
                    oauth_bound._access_token = oauth._access_token  # noqa: SLF001
                    oauth_bound._refresh_token = oauth._refresh_token  # noqa: SLF001
                    oauth_bound._token_expiry = oauth._token_expiry  # noqa: SLF001
                    oauth_bound._uid = oauth._uid  # noqa: SLF001
                    oauth_bound._terminal_id = oauth._terminal_id  # noqa: SLF001
                    oauth_bound._endpoint_url = oauth._endpoint_url  # noqa: SLF001
                    oauth_bound._loaded = True
                    await oauth_bound.async_save()
                    oauth = oauth_bound
            hass.data[DOMAIN][entry_oauth_key] = oauth

        oauth_mgr = hass.data[DOMAIN][entry_oauth_key]

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
        local_connected = await manager.add_device(device_config)
        if device_config["deferred_local_connect"] and not local_connected:
            _LOGGER.warning(
                "Deferred local mode active for %s: initial TinyTuya status "
                "failed; setup will continue and DeviceManager will retry "
                "in the background",
                device_id,
            )
        try:
            from .cloud_device_runtime import CloudDeviceRuntime  # noqa: PLC0415
            from .tuya_oauth import TuyaOAuthManager  # noqa: PLC0415

            availability_oauth_key = f"{_OAUTH_KEY}_availability"
            oauth = hass.data[DOMAIN].get(availability_oauth_key)
            if oauth is None:
                oauth = TuyaOAuthManager(hass, entry_id=entry.entry_id)
                await oauth.async_load()
                if not oauth.is_configured:
                    oauth_global = TuyaOAuthManager(hass)
                    await oauth_global.async_load()
                    oauth = oauth_global
                if oauth.is_configured:
                    hass.data[DOMAIN][availability_oauth_key] = oauth

            if oauth.is_configured:
                cloud_availability_runtime = CloudDeviceRuntime(
                    device_id=device_id,
                    oauth_manager=oauth,
                    dp_map=dp_map,
                )
                _LOGGER.info(
                    "Cloud availability monitor enabled for local device %s",
                    device_id,
                )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Cloud availability monitor unavailable for %s: %s",
                device_id,
                exc,
            )

        if cloud_availability_runtime is None:
            access_id = str(entry.data.get(CONF_CLOUD_ACCESS_ID, "")).strip()
            access_secret = str(
                entry.data.get(CONF_CLOUD_ACCESS_SECRET, "")
            ).strip()
            region = str(
                entry.data.get(CONF_CLOUD_REGION, "eu")
            ).strip() or "eu"
            if access_id and access_secret:
                try:
                    from .low_power_runtime import (  # noqa: PLC0415
                        LowPowerSensorCloudRuntime,
                    )

                    seed_runtime = LowPowerSensorCloudRuntime(
                        device_id=device_id,
                        access_id=access_id,
                        access_secret=access_secret,
                        region=region,
                        dp_map=dp_map,
                    )
                    if cloud_availability_runtime is None:
                        cloud_availability_runtime = seed_runtime
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "DALI cloud DPS seed via entry credentials failed "
                        "for %s: %s",
                        device_id,
                        exc,
                    )

        if cloud_availability_runtime is not None:
            fallback_active = manager.configure_dali_cloud_fallback(
                device_id, cloud_availability_runtime
            )
            if fallback_active:
                cloud_fallback_runtime = cloud_availability_runtime
                device_config["deferred_local_connect"] = True
                try:
                    cloud_seed_dps = (
                        await cloud_fallback_runtime.async_get_dps()
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "DALI cloud DPS seed failed for %s: %s",
                        device_id,
                        exc,
                    )
                if cloud_seed_dps:
                    manager.seed_cached_dps(
                        device_id, cloud_seed_dps, overwrite=True
                    )
                    _LOGGER.warning(
                        "DALI local status unavailable for %s reason=%s; "
                        "seeded cloud DPS cache=%s and continuing local probes",
                        device_id,
                        manager.get_device_diagnostics(device_id).get(
                            "last_probe_failure_reason"
                        ),
                        cloud_seed_dps,
                    )

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
        cloud_availability=cloud_availability_runtime,
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
    platforms = (
        [Platform.REMOTE, Platform.CLIMATE]
        if entry.data.get(CONF_RUNTIME_CHANNEL) == RUNTIME_CHANNEL_IR
        else PLATFORMS
    )
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)

    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})
        device_id = entry_data.get("device_id") or entry.data.get(CONF_DEVICE_ID)
        if entry.data.get(CONF_RUNTIME_CHANNEL) == RUNTIME_CHANNEL_IR:
            _LOGGER.info("Unloaded Conti IR device %s", device_id)
            return True

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
