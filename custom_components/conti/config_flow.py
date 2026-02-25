"""Config flow for the Conti integration.

Presents a single-step form that collects:
  * Device name, IP, Device ID, Local Key
  * Protocol version  (auto / 3.1 / 3.3 / 3.4 / 3.5)
  * Device type       (light / fan / climate / switch / sensor)
  * DP mapping JSON   (optional — auto-mapped when empty)

During submission the flow makes a **live connection** to the device
to validate credentials before persisting the config entry.

If protocol version is 'auto', versions 3.3 → 3.4 → 3.5 → 3.1 are tried;
the first that succeeds is persisted as ``detected_version``.

After a successful connection, DP discovery is attempted and heuristic
auto-mapping produces a merged dp_map stored in the config entry.

An options flow is provided for:
  * Toggling verbose/debug logging.
  * Editing the DP map.
  * Re-running DP discovery.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT

from .const import (
    AUTO_DETECT_ORDER,
    CONF_DETECTED_VERSION,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DISCOVERED_DPS,
    CONF_DP_MAP,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    CONF_VERBOSE_LOGGING,
    DEFAULT_PORT,
    DEFAULT_PROTOCOL_VERSION,
    DEVICE_TYPE_LIGHT,
    DOMAIN,
    SUPPORTED_DEVICE_TYPES,
    SUPPORTED_VERSIONS,
)

# NOTE: Do NOT import tinytuya_client at module level.
# HA loads config_flow.py very early; a top-level import of the protocol
# stack (which pulls in 'tinytuya') would cause "Invalid handler
# specified" if the dependency is missing or any import error occurs.
# Instead, import TinyTuyaDevice lazily inside _test_device().

_LOGGER = logging.getLogger(__name__)


def _mask_key(key: str) -> str:
    """Redact a local key for safe logging — first 2 + last 2 chars."""
    if len(key) <= 4:
        return "****"
    return key[:2] + "*" * (len(key) - 4) + key[-2:]


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): str,
            vol.Required(CONF_HOST, default=d.get(CONF_HOST, "")): str,
            vol.Required(CONF_DEVICE_ID, default=d.get(CONF_DEVICE_ID, "")): str,
            vol.Required(CONF_LOCAL_KEY, default=d.get(CONF_LOCAL_KEY, "")): str,
            vol.Optional(
                CONF_PORT, default=d.get(CONF_PORT, DEFAULT_PORT)
            ): int,
            vol.Required(
                CONF_PROTOCOL_VERSION,
                default=d.get(CONF_PROTOCOL_VERSION, DEFAULT_PROTOCOL_VERSION),
            ): vol.In(SUPPORTED_VERSIONS),
            vol.Required(
                CONF_DEVICE_TYPE,
                default=d.get(CONF_DEVICE_TYPE, DEVICE_TYPE_LIGHT),
            ): vol.In(SUPPORTED_DEVICE_TYPES),
            vol.Required(
                CONF_DP_MAP,
                default=d.get(CONF_DP_MAP, '{"1": {"key": "power", "type": "bool"}}'),
            ): str,
        }
    )


# ---------------------------------------------------------------------------
# Connection test helper with fine-grained error classification
# ---------------------------------------------------------------------------


async def _test_device(
    device_id: str,
    ip: str,
    local_key: str,
    version: str,
    port: int,
) -> tuple[bool, str | None, dict[str, Any], str]:
    """Attempt connection + DP discovery and classify any failure.

    Uses TinyTuya for connection validation — protocol version is
    converted to *float* before calling ``set_version()``.

    Returns ``(success, detected_version, discovered_dps, error_key)``.

    Error keys:
      * ``"cannot_connect"`` — network-level failure (timeout / refused).
      * ``"invalid_auth"``   — handshake or decrypt failure (bad local key).
      * ``"wrong_protocol"`` — protocol mismatch (all versions rejected).
      * ``""``               — no error.
    """
    from .tinytuya_client import TinyTuyaDevice  # noqa: PLC0415

    masked = _mask_key(local_key)

    # ------------------------------------------------------------------
    # Step 1: Verify raw TCP connectivity (fast, version-agnostic).
    # ------------------------------------------------------------------
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=5.0
        )
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError) as exc:
        _LOGGER.warning(
            "Config flow: TCP connect to %s:%d failed: %s (key=%s)",
            ip, port, exc, masked,
        )
        return False, None, {}, "cannot_connect"

    _LOGGER.debug(
        "Config flow: TCP reachable at %s:%d — testing protocol (key=%s)",
        ip, port, masked,
    )

    # ------------------------------------------------------------------
    # Step 2: Full protocol connect via TinyTuya.
    # ------------------------------------------------------------------
    client = TinyTuyaDevice(
        device_id=device_id,
        ip=ip,
        local_key=local_key,
        version=version,
        port=port,
    )

    try:
        ok = await client.connect()
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Config flow: unexpected error during connect for %s (key=%s)",
            device_id, masked,
        )
        await client.close()
        return False, None, {}, "cannot_connect"

    if not ok:
        await client.close()
        if version == "auto":
            _LOGGER.warning(
                "Config flow: all protocol versions failed for %s "
                "(%s:%d, tried %s, key=%s)",
                device_id, ip, port, AUTO_DETECT_ORDER, masked,
            )
            return False, None, {}, "wrong_protocol"
        # Explicit version — likely wrong key or wrong version.
        _LOGGER.warning(
            "Config flow: connect with v%s failed for %s (%s:%d, key=%s)",
            version, device_id, ip, port, masked,
        )
        return False, None, {}, "invalid_auth"

    detected = client.detected_version or client.protocol_version
    _LOGGER.debug(
        "Config flow: connected to %s with protocol v%s (key=%s)",
        device_id, detected, masked,
    )

    # ------------------------------------------------------------------
    # Step 3: DP discovery (best-effort — don't fail the flow if empty).
    # ------------------------------------------------------------------
    discovered_dps: dict[str, Any] = {}
    try:
        discovered_dps = await client.detect_dps()
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "Config flow: DP discovery raised for %s — continuing", device_id
        )

    await client.close()

    redacted = {k: ("***" if isinstance(v, str) and len(v) > 20 else v)
                for k, v in discovered_dps.items()}
    _LOGGER.debug(
        "Config flow: discovery complete for %s — v%s, DPs=%s",
        device_id, detected, redacted,
    )
    return True, detected, discovered_dps, ""


# =========================================================================
# Config flow
# =========================================================================


class ContiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Conti."""

    VERSION = 1

    # -- Options flow entry point -------------------------------------------

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ContiOptionsFlow:
        """Return the options flow handler."""
        return ContiOptionsFlow(config_entry)

    # -- User step ----------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step — device details form."""
        _LOGGER.debug("async_step_user called (user_input=%s)", user_input is not None)
        errors: dict[str, str] = {}

        if user_input is not None:
            # ---- Validate DP map JSON ------------------------------------
            try:
                user_dp_map = json.loads(user_input[CONF_DP_MAP])
                if not isinstance(user_dp_map, dict):
                    raise ValueError  # noqa: TRY301
            except (json.JSONDecodeError, ValueError):
                errors[CONF_DP_MAP] = "invalid_dp_map"

            if not errors:
                # ---- Ensure device isn't already configured --------------
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()

                selected_version: str = user_input[CONF_PROTOCOL_VERSION]
                _LOGGER.debug(
                    "Config flow: selected protocol '%s' for %s (key=%s)",
                    selected_version,
                    user_input[CONF_DEVICE_ID],
                    _mask_key(user_input[CONF_LOCAL_KEY]),
                )

                # ---- Connection test + DP discovery ----------------------
                ok, detected_version, discovered_dps, err = await _test_device(
                    device_id=user_input[CONF_DEVICE_ID],
                    ip=user_input[CONF_HOST],
                    local_key=user_input[CONF_LOCAL_KEY],
                    version=selected_version,
                    port=user_input.get(CONF_PORT, DEFAULT_PORT),
                )

                if not ok:
                    errors["base"] = err or "cannot_connect"

            if not errors:
                # ---- Auto DP mapping + merge -----------------------------
                from .dp_mapping import auto_map_dps, merge_dp_maps  # noqa: PLC0415

                device_type = user_input[CONF_DEVICE_TYPE]
                auto_dp_map: dict[str, Any] = {}
                if discovered_dps:
                    auto_dp_map = auto_map_dps(device_type, discovered_dps)
                    _LOGGER.info(
                        "Config flow: auto-mapped %d DP(s) for %s (%s)",
                        len(auto_dp_map),
                        user_input[CONF_DEVICE_ID],
                        device_type,
                    )

                final_dp_map = merge_dp_maps(user_dp_map, auto_dp_map)

                # ---- Build entry data ------------------------------------
                entry_data: dict[str, Any] = {
                    CONF_DEVICE_ID: user_input[CONF_DEVICE_ID],
                    CONF_HOST: user_input[CONF_HOST],
                    CONF_PORT: user_input.get(CONF_PORT, DEFAULT_PORT),
                    CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY],
                    CONF_PROTOCOL_VERSION: selected_version,
                    CONF_DEVICE_TYPE: device_type,
                    CONF_DP_MAP: json.dumps(final_dp_map),
                }

                # Persist auto-detected version so future connects skip
                # re-detection.
                if selected_version == "auto" and detected_version:
                    entry_data[CONF_DETECTED_VERSION] = detected_version

                # Persist raw discovered DPS for diagnostics / re-mapping.
                if discovered_dps:
                    entry_data[CONF_DISCOVERED_DPS] = json.dumps(discovered_dps)

                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=entry_data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
        )


# =========================================================================
# Options flow — verbose logging, DP map editing, re-discovery
# =========================================================================


class ContiOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Conti — edit DP map, toggle debug, re-discover."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Main options step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate DP map
            dp_map_raw = user_input.get(CONF_DP_MAP, "{}")
            try:
                dp_map = json.loads(dp_map_raw)
                if not isinstance(dp_map, dict):
                    raise ValueError  # noqa: TRY301
            except (json.JSONDecodeError, ValueError):
                errors[CONF_DP_MAP] = "invalid_dp_map"

            if not errors:
                new_options = dict(self._entry.options)
                new_options[CONF_VERBOSE_LOGGING] = user_input.get(
                    CONF_VERBOSE_LOGGING, False
                )
                new_options[CONF_DP_MAP] = json.dumps(dp_map)

                # If re-discover is requested, run DP detection now.
                if user_input.get("rediscover_dps", False):
                    ok, _, discovered, _ = await _test_device(
                        device_id=self._entry.data[CONF_DEVICE_ID],
                        ip=self._entry.data[CONF_HOST],
                        local_key=self._entry.data[CONF_LOCAL_KEY],
                        version=(
                            self._entry.data.get(CONF_DETECTED_VERSION)
                            or self._entry.data.get(
                                CONF_PROTOCOL_VERSION, DEFAULT_PROTOCOL_VERSION
                            )
                        ),
                        port=self._entry.data.get(CONF_PORT, DEFAULT_PORT),
                    )
                    if ok and discovered:
                        from .dp_mapping import auto_map_dps, merge_dp_maps  # noqa: PLC0415

                        auto = auto_map_dps(
                            self._entry.data.get(CONF_DEVICE_TYPE, "switch"),
                            discovered,
                        )
                        new_options[CONF_DP_MAP] = json.dumps(
                            merge_dp_maps(dp_map, auto)
                        )
                        new_options[CONF_DISCOVERED_DPS] = json.dumps(discovered)

                return self.async_create_entry(title="", data=new_options)

        # Build defaults from current options / data.
        current_dp_map = (
            self._entry.options.get(CONF_DP_MAP)
            or self._entry.data.get(CONF_DP_MAP, "{}")
        )
        current_verbose = self._entry.options.get(CONF_VERBOSE_LOGGING, False)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DP_MAP, default=current_dp_map): str,
                    vol.Optional(
                        CONF_VERBOSE_LOGGING, default=current_verbose
                    ): bool,
                    vol.Optional("rediscover_dps", default=False): bool,
                }
            ),
            errors=errors,
        )
