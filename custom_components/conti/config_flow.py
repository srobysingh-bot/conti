"""Config flow for the Conti integration.

Three onboarding paths are provided:

Smart Life OAuth path (default — recommended)
    Step 1  (``user``)               — choose onboarding mode.
    Step 2a (``oauth_login``)        — select data-centre region; QR code
                                       is generated (no credentials needed).
    Step 2b (``oauth_qr_scan``)      — scan QR code with Smart Life app.
    Step 2c (``oauth_pick_device``)  — select device from auto-discovered
                                       cloud list.
    Step 3  (``confirm_host``)       — confirm / enter local IP (if needed).
    Step 4  (``detect``)             — auto-detect protocol + DP discovery.
    Step 5  (``cloud_assist``)       — (optional) refine DP mapping via cloud.
    Step 6  (``review``)             — review mapping, accept/edit/learn.
    Step 7  (``learn``)              — (optional) guided interactive learning.

Cloud-assisted path (legacy)
    Step 1  (``user``)               — device name, IP, type, choose mode.
    Step 2a (``cloud_credentials``)  — Tuya API credentials; Conti fetches
                                       all linked devices and auto-fills
                                       Device ID and Local Key.
    Step 2b (``cloud_pick_device``)  — select device when multiple are found.
    Step 3  (``detect``)             — auto-detect protocol + DP discovery.
    Step 4  (``cloud_assist``)       — (optional) refine DP mapping via cloud.
    Step 5  (``review``)             — review mapping, accept/edit/learn.
    Step 6  (``learn``)              — (optional) guided interactive learning.

Manual path (advanced fallback)
    Step 1  (``user``)               — device name, IP, type, choose mode.
    Step 2  (``manual_credentials``) — Device ID and Local Key entered by user.
    Step 3  (``detect``)             — auto-detect protocol + DP discovery.
    Step 4  (``cloud_assist``)       — (optional) refine DP mapping via cloud.
    Step 5  (``review``)             — review mapping, accept/edit/learn.
    Step 6  (``learn``)              — (optional) guided interactive learning.

Runtime
~~~~~~~
Cloud is used during onboarding for mapping and metadata.
Runtime remains local-only for standard always-on devices.
Low-power sleepy sensors can be flagged to use a separate cloud-backed
status path. Existing local config entries continue to work unchanged.

An options flow is provided for:
  * Toggling verbose/debug logging.
  * Editing the DP map.
  * Re-running DP discovery.
  * External-ON correction profiles.
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT

from .const import (
    AUTO_DETECT_ORDER,
    CONF_DAY_BRIGHTNESS,
    CONF_DAY_END,
    CONF_DAY_KELVIN,
    CONF_DAY_START,
    CONF_CLOUD_ACCESS_ID,
    CONF_CLOUD_ACCESS_SECRET,
    CONF_CLOUD_REGION,
    CONF_DETECTED_VERSION,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DEVICE_PROFILE,
    CONF_DISCOVERED_DPS,
    CONF_DP_MAP,
    CONF_EXTERNAL_ON_APPLY,
    CONF_EXTERNAL_ON_ENABLED,
    CONF_LOCAL_KEY,
    CONF_IR_BRAND,
    CONF_IR_CATEGORY,
    CONF_IR_MODEL,
    CONF_MAPPING_CONFIDENCE,
    CONF_MAPPING_SOURCE,
    CONF_LOW_POWER_DEVICE,
    CONF_MORNING_BRIGHTNESS,
    CONF_MORNING_END,
    CONF_MORNING_KELVIN,
    CONF_MORNING_START,
    CONF_NIGHT_BRIGHTNESS,
    CONF_NIGHT_END,
    CONF_NIGHT_KELVIN,
    CONF_NIGHT_START,
    CONF_PROTOCOL_VERSION,
    CONF_RUNTIME_CHANNEL,
    CONF_TUYA_CATEGORY,
    CONF_VERBOSE_LOGGING,
    DEFAULT_PORT,
    DEFAULT_PROTOCOL_VERSION,
    DEVICE_TYPE_LIGHT,
    DEVICE_TYPE_IR,
    DEVICE_TYPE_SENSOR,
    DOMAIN,
    RUNTIME_CHANNEL_CLOUD,
    RUNTIME_CHANNEL_CLOUD_SENSOR,
    RUNTIME_CHANNEL_IR,
    RUNTIME_CHANNEL_LOCAL,
    SUPPORTED_DEVICE_TYPES,
    SUPPORTED_VERSIONS,
)

# NOTE: Do NOT import tinytuya_client at module level.
# HA loads config_flow.py very early; a top-level import of the protocol
# stack (which pulls in 'tinytuya') would cause "Invalid handler
# specified" if the dependency is missing or any import error occurs.
# Instead, import TinyTuyaDevice lazily inside _test_device().

_LOGGER = logging.getLogger(__name__)

# Minimum confidence score to skip guided learn prompt
_CONFIDENCE_THRESHOLD = 0.6

# LAN discovery timeout during onboarding (seconds)
_LAN_DISCOVERY_TIMEOUT = 8.0

# TCP probe timeout during config-flow validation (seconds)
_CONFIG_FLOW_TCP_PROBE_TIMEOUT = 5.0

# User-facing config-flow keys for LAN TCP failures (replacing generic cannot_connect)
ERR_DEVICE_NOT_RESPONDING = "device_not_responding"
ERR_DEVICE_UNREACHABLE_NETWORK = "device_unreachable_network"
ERR_PORT_BLOCKED_LOCAL = "port_blocked_local_unsupported"

_LAN_TCP_REACHABILITY_ERRORS = frozenset(
    {
        ERR_DEVICE_NOT_RESPONDING,
        ERR_DEVICE_UNREACHABLE_NETWORK,
        ERR_PORT_BLOCKED_LOCAL,
    }
)


def _classify_tcp_connect_error(exc: BaseException) -> tuple[str, str]:
    """Map ``open_connection`` / connect failures to a user-facing error key.

    Returns ``(config_flow_error_key, resolved_type)`` where *resolved_type* is
    one of: ``timeout``, ``host_unreachable``, ``connection_refused``,
    ``os_error``, or a short diagnostic tag for logs.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return ERR_DEVICE_NOT_RESPONDING, "timeout"
    if isinstance(exc, OSError):
        err = exc.errno
        winerr = getattr(exc, "winerror", None)
        # Refused / no listener
        if err == errno.ECONNREFUSED or winerr == 10061:
            return ERR_PORT_BLOCKED_LOCAL, "connection_refused"
        # No route / network down / unreachable
        _unreach_errno = {
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
            errno.ENETDOWN,
        }
        # Windows: 10065 WSAEHOSTUNREACH, 10051 WSAENETUNREACH, 10050 WSAENETDOWN
        if err in _unreach_errno or winerr in {10065, 10051, 10050}:
            return ERR_DEVICE_UNREACHABLE_NETWORK, "host_unreachable"
        # Platform connect timeout
        if err == errno.ETIMEDOUT or winerr == 10060:
            return ERR_DEVICE_NOT_RESPONDING, "timeout"
        return ERR_DEVICE_NOT_RESPONDING, f"os_error errno={err} winerror={winerr}"
    return ERR_DEVICE_NOT_RESPONDING, f"other ({type(exc).__name__})"


def _is_lan_tcp_reachability_error(error_key: str) -> bool:
    """Return True if *error_key* is a LAN TCP reachability class failure."""
    return error_key in _LAN_TCP_REACHABILITY_ERRORS


async def _async_probe_tcp(
    host: str,
    port: int,
    timeout: float | None = None,
) -> tuple[bool, str | None, str]:
    """Try opening a TCP connection to *host*:*port* (config-flow diagnostic).

    Returns ``(success, error_key_if_failed, resolved_type)``.
    """
    t = timeout if timeout is not None else _CONFIG_FLOW_TCP_PROBE_TIMEOUT
    _LOGGER.debug(
        "Config flow: TCP probe starting host=%s port=%s timeout=%.1fs",
        host,
        port,
        t,
    )
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=t,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        err_key, kind = _classify_tcp_connect_error(exc)
        _LOGGER.warning(
            "Config flow: TCP probe FAILED host=%s port=%s connection_result=fail "
            "resolved_error_type=%s exc=%s",
            host,
            port,
            kind,
            exc,
        )
        return False, err_key, kind

    writer.close()
    try:
        await writer.wait_closed()
    except OSError as exc:
        _LOGGER.debug("Config flow: TCP probe peer close note: %s", exc)

    _LOGGER.debug(
        "Config flow: TCP probe OK host=%s port=%s connection_result=success",
        host,
        port,
    )
    return True, None, "connected"

_LOW_POWER_SENSOR_CATEGORIES = {
    "mcs",     # contact/door
    "pir",     # motion
    "wsdcg",   # temp/humidity
    "sj",      # leak
    "ywbj",    # smoke
    "rqbj",    # gas
}

_LOW_POWER_SENSOR_PROFILE_IDS = {
    "sensor_contact",
    "sensor_motion",
    "sensor_temp_humidity",
}


def _mask_key(key: str) -> str:
    """Redact a local key for safe logging — first 2 + last 2 chars."""
    if len(key) <= 4:
        return "****"
    return key[:2] + "*" * (len(key) - 4) + key[-2:]


def _is_private_lan_ip(ip: str) -> bool:
    """Return True only for valid private IPv4 addresses."""
    try:
        parsed = ipaddress.ip_address(ip)
        return parsed.version == 4 and parsed.is_private
    except ValueError:
        return False


def _scan_lan_for_device_id_sync(device_id: str) -> list[str]:
    """Run a local TinyTuya scan and return private LAN IPs matching device_id."""
    try:
        import tinytuya  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("LAN discovery unavailable (tinytuya import failed): %s", exc)
        return []

    try:
        raw = tinytuya.deviceScan(
            verbose=False,
            maxretry=1,
            color=False,
            poll=False,
            forcescan=False,
            byID=False,
        )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("LAN discovery scan failed for %s: %s", device_id, exc)
        return []

    if not isinstance(raw, dict):
        return []

    matches: list[str] = []
    for fallback_ip, payload in raw.items():
        if not isinstance(payload, dict):
            continue

        found_id = str(payload.get("gwId") or payload.get("id") or "").strip()
        if found_id != device_id:
            continue

        ip = str(payload.get("ip") or fallback_ip or "").strip()
        if _is_private_lan_ip(ip):
            matches.append(ip)

    # Preserve order, drop duplicates.
    return list(dict.fromkeys(matches))


async def _discover_confident_lan_host(device_id: str) -> tuple[str | None, list[str]]:
    """Discover local host by device_id; return (single_confident_match, candidates)."""
    try:
        candidates = await asyncio.wait_for(
            asyncio.to_thread(_scan_lan_for_device_id_sync, device_id),
            timeout=_LAN_DISCOVERY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        _LOGGER.info(
            "LAN discovery timed out for %s after %.1fs; using manual fallback",
            device_id,
            _LAN_DISCOVERY_TIMEOUT,
        )
        return None, []

    if len(candidates) == 1:
        return candidates[0], candidates

    return None, candidates


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Step 1: basic info + choose onboarding path (no device_id/local_key)."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): str,
            vol.Optional(CONF_HOST, default=d.get(CONF_HOST, "")): str,
            vol.Optional(
                CONF_PORT, default=d.get(CONF_PORT, DEFAULT_PORT)
            ): int,
            vol.Required(
                CONF_DEVICE_TYPE,
                default=d.get(CONF_DEVICE_TYPE, DEVICE_TYPE_LIGHT),
            ): vol.In(SUPPORTED_DEVICE_TYPES),
            vol.Required(
                "onboarding_mode",
                default=d.get("onboarding_mode", "smart_life"),
            ): vol.In(
                {
                    "smart_life": "Login with Smart Life (recommended)",
                    "cloud_assisted": "Cloud-assisted (legacy)",
                    "manual": "Manual / Advanced",
                }
            ),
        }
    )


def _cloud_credentials_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Cloud path step 2: Tuya API credentials."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                "tuya_access_id", default=d.get("tuya_access_id", "")
            ): str,
            vol.Required(
                "tuya_access_secret", default=d.get("tuya_access_secret", "")
            ): str,
            vol.Required(
                "tuya_region", default=d.get("tuya_region", "eu")
            ): vol.In(
                {"us": "Americas", "eu": "Europe", "cn": "China", "in": "India"}
            ),
        }
    )


def _manual_credentials_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Manual path step 2: Device ID and Local Key."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_DEVICE_ID, default=d.get(CONF_DEVICE_ID, "")
            ): str,
            vol.Required(
                CONF_LOCAL_KEY, default=d.get(CONF_LOCAL_KEY, "")
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
      * ``device_not_responding`` / ``device_unreachable_network`` /
        ``port_blocked_local_unsupported`` — TCP reachability (classified).
      * ``"invalid_auth"``   — handshake or decrypt failure (bad local key).
      * ``"wrong_protocol"`` — protocol mismatch (all versions rejected).
      * ``""``               — no error.
    """
    from .tinytuya_client import TinyTuyaDevice  # noqa: PLC0415

    masked = _mask_key(local_key)

    # ------------------------------------------------------------------
    # Step 1: Verify raw TCP connectivity (fast, version-agnostic).
    # ------------------------------------------------------------------
    ok_tcp, tcp_err, tcp_kind = await _async_probe_tcp(ip, port)
    if not ok_tcp:
        _LOGGER.warning(
            "Config flow: TCP pre-check failed device=%s host=%s port=%s "
            "resolved_error_type=%s key=%s",
            device_id,
            ip,
            port,
            tcp_kind,
            masked,
        )
        return False, None, {}, tcp_err or ERR_DEVICE_NOT_RESPONDING

    _LOGGER.debug(
        "Config flow: TCP reachable at %s:%d — testing protocol (key=%s)",
        ip,
        port,
        masked,
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
    except OSError as exc:
        err_key, kind = _classify_tcp_connect_error(exc)
        _LOGGER.warning(
            "Config flow: connect() failed device=%s host=%s port=%s "
            "connection_result=fail resolved_error_type=%s key=%s exc=%s",
            device_id,
            ip,
            port,
            kind,
            masked,
            exc,
        )
        await client.close()
        return False, None, {}, err_key
    except asyncio.TimeoutError as exc:
        _LOGGER.warning(
            "Config flow: connect() timed out device=%s host=%s port=%s key=%s %s",
            device_id,
            ip,
            port,
            masked,
            exc,
        )
        await client.close()
        return False, None, {}, ERR_DEVICE_NOT_RESPONDING
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Config flow: unexpected error during connect for %s host=%s port=%s (key=%s)",
            device_id,
            ip,
            port,
            masked,
        )
        await client.close()
        return False, None, {}, ERR_DEVICE_NOT_RESPONDING

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
# Config flow — multi-step onboarding
# =========================================================================


class ContiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a multi-step config flow for Conti.

    Flow: user → detect → (cloud_assist?) → review → (learn?) → create
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialise per-flow state carried across steps."""
        super().__init__()
        # Accumulated data across steps
        self._flow_data: dict[str, Any] = {}
        self._detected_version: str | None = None
        self._discovered_dps: dict[str, Any] = {}
        self._auto_dp_map: dict[str, Any] = {}
        self._cloud_dp_map: dict[str, Any] = {}
        self._profile_dp_map: dict[str, Any] = {}
        self._final_dp_map: dict[str, Any] = {}
        self._matched_profile: dict[str, Any] | None = None
        self._confidence: float = 0.0
        self._mapping_source: str = "auto"
        self._tuya_category: str | None = None
        # Cached OAuth manager — created once and reused across steps so
        # the sharing SDK's update_device_cache() is not called redundantly.
        self._oauth_manager: Any = None
        self._learn_session: Any = None
        self._learn_steps: list[dict[str, Any]] = []
        self._learn_step_idx: int = 0
        self._device_family: str = "unknown"
        self._gang_count: int = 0
        self._family_reason: str = ""
        self._learn_feedback: str = ""
        self._cloud_auth: dict[str, str] = {}
        self._cloud_candidates: list[dict[str, Any]] = []
        self._lan_candidates: list[str] = []
        self._onboarding_mode: str = "manual"
        self._host_resolution_note: str = ""
        self._low_power_sensor: bool = False
        self._ir_categories: list[dict[str, Any]] = []
        self._ir_brands: list[dict[str, Any]] = []
        self._ir_models: list[dict[str, Any]] = []
        self._ir_category: dict[str, Any] = {}
        self._ir_brand: dict[str, Any] = {}
        self._ir_model: dict[str, Any] = {}
        # Smart Life QR login state
        self._qr_code_url: str = ""
        self._qr_code_token: str = ""
        self._selected_region: str = "eu"

    # -- Options flow entry point -------------------------------------------

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ContiOptionsFlow:
        """Return the options flow handler."""
        return ContiOptionsFlow(config_entry)

    # ═══════════════════════════════════════════════════════════════════
    # Step 1: Credentials
    # ═══════════════════════════════════════════════════════════════════

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1 — collect basic info and choose onboarding path."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._flow_data = {
                CONF_NAME: user_input[CONF_NAME],
                CONF_HOST: user_input.get(CONF_HOST, "").strip(),
                CONF_PORT: user_input.get(CONF_PORT, DEFAULT_PORT),
                CONF_DEVICE_TYPE: user_input[CONF_DEVICE_TYPE],
            }
            mode = user_input.get("onboarding_mode", "smart_life")
            self._onboarding_mode = mode
            if mode == "smart_life":
                return await self.async_step_oauth_login()
            if mode == "cloud_assisted":
                return await self.async_step_cloud_credentials()
            # Manual mode: host is required to be able to connect
            if not self._flow_data[CONF_HOST]:
                errors["base"] = "host_required_manual"
            else:
                return await self.async_step_manual_credentials()

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(),
            errors=errors,
        )

    # ═══════════════════════════════════════════════════════════════════
    # Smart Life OAuth path — global credential storage + device picker
    # ═══════════════════════════════════════════════════════════════════

    async def async_step_oauth_login(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Smart Life login — generate QR code for app authorization.

        No Tuya IoT project credentials are required.  The QR code is
        generated using the shared Tuya HA client ID via the centralised
        ``apigw.iotbing.com`` gateway.

        The user only selects a data-centre region.  On submit a QR code
        is generated and the flow moves to the scan step.
        """
        from .tuya_oauth import TuyaOAuthManager  # noqa: PLC0415

        oauth = TuyaOAuthManager(self.hass)
        await oauth.async_load()

        errors: dict[str, str] = {}

        # NOTE: We intentionally do NOT auto-skip to the device picker when a
        # stored session exists.  The user explicitly chose "Login with Smart
        # Life", so they must always complete the QR scan to authenticate —
        # no session reuse, no cross-account leakage.
        _LOGGER.debug(
            "oauth_login: is_configured=%s is_qr_mode=%s (session will NOT be reused)",
            oauth.is_configured,
            oauth.is_qr_mode,
        )

        if user_input is not None:
            region = user_input.get("tuya_region", "eu")
            user_code = user_input.get("user_code", "").strip()
            _LOGGER.debug(
                "Smart Life QR login: region=%s user_code=%s",
                region, user_code,
            )

            if not user_code:
                errors["base"] = "user_code_required"
            else:
                try:
                    qr_data = await oauth.async_start_qr_login(
                        user_code=user_code, region=region,
                    )
                    self._qr_code_url = qr_data["url"]
                    self._qr_code_token = qr_data["token"]
                    self._selected_region = region
                    return await self.async_step_oauth_qr_scan()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.exception("Smart Life QR code generation failed: %s", exc)
                    errors["base"] = "qr_generation_failed"

        schema = vol.Schema(
            {
                vol.Required("user_code"): str,
                vol.Required("tuya_region", default="eu"): vol.In(
                    {"us": "Americas", "eu": "Europe", "cn": "China", "in": "India"}
                ),
            }
        )

        return self.async_show_form(
            step_id="oauth_login",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_oauth_qr_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show QR code and wait for the user to scan with Smart Life app.

        Uses HA's built-in QrCodeSelector to render the QR code inline in
        the form — no external image services or CSP issues.

        The user scans with the Smart Life app then clicks Submit.  If the
        scan is not yet confirmed the token is refreshed so it does not
        expire between retries.
        """
        from homeassistant.helpers.selector import (  # noqa: PLC0415
            QrCodeSelector,
            QrCodeSelectorConfig,
            QrErrorCorrectionLevel,
        )

        from .tuya_oauth import TuyaOAuthManager  # noqa: PLC0415

        # Guard: if no QR token the user arrived here out of sequence.
        if not self._qr_code_token:
            _LOGGER.warning(
                "oauth_qr_scan reached without a QR token; redirecting to login"
            )
            return await self.async_step_oauth_login()

        errors: dict[str, str] = {}

        if user_input is not None:
            # User clicked Submit — poll for scan confirmation.
            oauth = TuyaOAuthManager(self.hass)
            await oauth.async_load()

            _LOGGER.debug(
                "Polling QR login for token prefix=%s…", self._qr_code_token[:8]
            )
            uid = await oauth.async_poll_qr_login(self._qr_code_token)

            if uid:
                _LOGGER.debug("QR login successful, uid=%s", uid)
                self._cloud_auth = {
                    "access_id": oauth.access_id,
                    "access_secret": oauth.access_secret,
                    "region": oauth.region or self._selected_region,
                }
                return await self.async_step_oauth_pick_device()

            # Not yet scanned — refresh QR token so it doesn't expire.
            errors["base"] = "qr_not_scanned"
            _LOGGER.debug("QR not yet scanned; refreshing token")
            try:
                qr_data = await oauth.async_start_qr_login(
                    user_code=oauth.user_code,
                    region=self._selected_region,
                )
                self._qr_code_url = qr_data["url"]
                self._qr_code_token = qr_data["token"]
                _LOGGER.debug(
                    "QR token refreshed, new prefix=%s…", self._qr_code_token[:8]
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("QR token refresh failed; keeping existing token")

        _LOGGER.debug(
            "Showing QR scan form, content prefix=%s", self._qr_code_url[:50]
        )
        return self.async_show_form(
            step_id="oauth_qr_scan",
            data_schema=vol.Schema(
                {
                    vol.Optional("qr_image"): QrCodeSelector(
                        config=QrCodeSelectorConfig(
                            data=self._qr_code_url,
                            scale=5,
                            error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_oauth_pick_device(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Smart Life device picker — auto-discovered from cloud account."""
        from .tuya_oauth import TuyaOAuthManager  # noqa: PLC0415

        errors: dict[str, str] = {}

        if user_input is not None:
            selected_id = user_input.get("cloud_device_id", "")
            if not selected_id:
                errors["base"] = "cloud_no_device_match"
            else:
                # Fetch full device info (with local_key).
                oauth = TuyaOAuthManager(self.hass)
                await oauth.async_load()
                info = await oauth.async_get_device_info(selected_id)

                if info is None:
                    errors["base"] = "cloud_fetch_failed"
                else:
                    local_key = str(info.get("local_key", "")).strip()
                    cloud_ip = str(info.get("ip", "")).strip()
                    category = str(info.get("category", "")).strip()
                    name = str(info.get("name", "")).strip()

                    self._flow_data[CONF_DEVICE_ID] = selected_id
                    self._flow_data[CONF_LOCAL_KEY] = local_key
                    if not self._flow_data.get(CONF_NAME):
                        self._flow_data[CONF_NAME] = name or selected_id
                    if category:
                        self._tuya_category = category
                    if self._is_ir_category(category):
                        _LOGGER.info(
                            "IR device detected device=%s category=%s",
                            selected_id,
                            category,
                        )
                        return await self.async_step_ir_category()

                    # Try to resolve host.
                    await self._apply_cloud_candidate(
                        {
                            "device_id": selected_id,
                            "local_key": local_key,
                            "ip": cloud_ip,
                            "category": category,
                            "name": name,
                        }
                    )

                    await self.async_set_unique_id(selected_id)
                    self._abort_if_unique_id_configured()

                    # Route based on whether we have local_key.
                    if local_key:
                        if not self._flow_data.get(CONF_HOST, "").strip():
                            return await self.async_step_confirm_host()
                        return await self.async_step_detect()
                    else:
                        # No local_key — cloud-only device.
                        _LOGGER.info(
                            "Device %s has no local_key; setting up as cloud-only",
                            selected_id,
                        )
                        # Fetch schema for DP mapping via cloud.
                        schema = await oauth.async_get_device_schema(
                            selected_id
                        )
                        if schema:
                            helper = oauth.get_schema_helper()
                            cloud_map, cat, _hint = helper.schema_to_dp_map(
                                schema
                            )
                            if cloud_map:
                                self._cloud_dp_map = cloud_map
                                self._final_dp_map = cloud_map
                                self._mapping_source = "cloud"
                            if cat:
                                self._tuya_category = cat
                        return await self.async_step_review()

        # Fetch device list from stored OAuth.
        oauth = TuyaOAuthManager(self.hass)
        await oauth.async_load()
        if not oauth.is_configured:
            return await self.async_step_oauth_login()

        try:
            cloud_devices = await oauth.async_list_devices()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("OAuth device list fetch failed")
            errors["base"] = self._cloud_error_key(exc)
            cloud_devices = []

        if not cloud_devices and "base" not in errors:
            errors["base"] = "cloud_no_device_match"

        if errors:
            return self.async_show_form(
                step_id="oauth_pick_device",
                data_schema=vol.Schema(
                    {vol.Required("cloud_device_id"): str}
                ),
                errors=errors,
            )

        # Build choice map.
        choices: dict[str, str] = {}
        for dev in cloud_devices:
            dev_id = str(
                dev.get("id", "") or dev.get("device_id", "")
            ).strip()
            if not dev_id:
                continue
            label_parts = [
                dev.get("name") or dev.get("product_name") or "Unnamed",
                f"ID: {dev_id}",
            ]
            if dev.get("ip"):
                label_parts.append(f"IP: {dev['ip']}")
            if dev.get("category"):
                label_parts.append(f"Cat: {dev['category']}")
            choices[dev_id] = " | ".join(label_parts)

        if not choices:
            return self.async_abort(reason="cloud_no_device_match")

        return self.async_show_form(
            step_id="oauth_pick_device",
            data_schema=vol.Schema(
                {vol.Required("cloud_device_id"): vol.In(choices)}
            ),
            errors=errors,
        )

    # ═══════════════════════════════════════════════════════════════════
    # Step 2a: Cloud-assisted — Tuya credentials + device selection
    # ═══════════════════════════════════════════════════════════════════

    async def async_step_cloud_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Cloud path — collect Tuya API credentials and fetch linked devices."""
        errors: dict[str, str] = {}

        if user_input is not None:
            access_id = user_input.get("tuya_access_id", "").strip()
            access_secret = user_input.get("tuya_access_secret", "").strip()
            region = user_input.get("tuya_region", "eu")

            if not access_id or not access_secret:
                errors["base"] = "cloud_credentials_required"
            else:
                self._cloud_auth = {
                    "access_id": access_id,
                    "access_secret": access_secret,
                    "region": region,
                }
                try:
                    candidates = await self._cloud_fetch_candidates()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.exception("Cloud onboarding candidate fetch failed")
                    errors["base"] = self._cloud_error_key(exc)
                    candidates = []
                if not candidates:
                    if "base" not in errors:
                        errors["base"] = "cloud_no_device_match"
                elif len(candidates) == 1:
                    selected = dict(candidates[0])
                    if self._is_ir_category(str(selected.get("category", ""))):
                        await self._apply_ir_candidate(selected)
                        await self.async_set_unique_id(self._flow_data[CONF_DEVICE_ID])
                        self._abort_if_unique_id_configured()
                        return await self.async_step_ir_category()
                    try:
                        refreshed = await self._cloud_get_credentials(
                            selected.get("device_id", "")
                        )
                        if refreshed:
                            selected.update(refreshed)
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.exception(
                            "Cloud onboarding credential fetch failed for single candidate"
                        )
                        errors["base"] = self._cloud_error_key(exc)

                    if "base" not in errors and not str(selected.get("local_key", "")).strip():
                        errors["base"] = "cloud_device_missing_local_key"

                    if "base" not in errors:
                        await self._apply_cloud_candidate(selected)
                        await self.async_set_unique_id(self._flow_data[CONF_DEVICE_ID])
                        self._abort_if_unique_id_configured()
                        if not self._flow_data.get(CONF_HOST, "").strip():
                            return await self.async_step_confirm_host()
                        return await self.async_step_detect()
                else:
                    self._cloud_candidates = candidates
                    return await self.async_step_cloud_pick_device()

        return self.async_show_form(
            step_id="cloud_credentials",
            data_schema=_cloud_credentials_schema(
                {
                    "tuya_access_id": self._cloud_auth.get("access_id", ""),
                    "tuya_access_secret": self._cloud_auth.get("access_secret", ""),
                    "tuya_region": self._cloud_auth.get("region", "eu"),
                }
            ),
            errors=errors,
        )

    # ═══════════════════════════════════════════════════════════════════
    # Step 2b: Manual path — Device ID + Local Key
    # ═══════════════════════════════════════════════════════════════════

    async def async_step_manual_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manual path — collect Device ID and Local Key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = user_input.get(CONF_DEVICE_ID, "").strip()
            local_key = user_input.get(CONF_LOCAL_KEY, "").strip()

            if not device_id or not local_key:
                errors["base"] = "manual_credentials_required"
            else:
                self._flow_data[CONF_DEVICE_ID] = device_id
                self._flow_data[CONF_LOCAL_KEY] = local_key
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()
                return await self.async_step_detect()

        return self.async_show_form(
            step_id="manual_credentials",
            data_schema=_manual_credentials_schema(self._flow_data),
            errors=errors,
        )

    async def async_step_cloud_pick_device(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select a cloud device when multiple candidates are available."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_id = user_input.get("cloud_device_id", "")
            selected = next(
                (
                    c for c in self._cloud_candidates
                    if c.get("device_id") == selected_id
                ),
                None,
            )
            if not selected:
                errors["base"] = "cloud_no_device_match"
            else:
                if self._is_ir_category(str(selected.get("category", ""))):
                    await self._apply_ir_candidate(selected)
                    await self.async_set_unique_id(self._flow_data[CONF_DEVICE_ID])
                    self._abort_if_unique_id_configured()
                    return await self.async_step_ir_category()

                if not str(selected.get("local_key", "")).strip():
                    try:
                        refreshed = await self._cloud_get_credentials(selected_id)
                        if refreshed:
                            selected.update(refreshed)
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.exception(
                            "Cloud onboarding credential refresh failed for %s",
                            selected_id,
                        )
                        errors["base"] = self._cloud_error_key(exc)

                if not str(selected.get("local_key", "")).strip():
                    if "base" not in errors:
                        errors["base"] = "cloud_device_missing_local_key"
                else:
                    await self._apply_cloud_candidate(selected)
                    await self.async_set_unique_id(self._flow_data[CONF_DEVICE_ID])
                    self._abort_if_unique_id_configured()
                    if not self._flow_data.get(CONF_HOST, "").strip():
                        return await self.async_step_confirm_host()
                    return await self.async_step_detect()

        choices: dict[str, str] = {}
        for cand in self._cloud_candidates:
            dev_id = cand.get("device_id", "")
            if not dev_id:
                continue
            label_parts = [
                cand.get("name") or "Unnamed",
                f"ID: {dev_id}",
            ]
            if cand.get("ip"):
                label_parts.append(f"IP: {cand['ip']}")
            if cand.get("category"):
                label_parts.append(f"Category: {cand['category']}")
            choices[dev_id] = " | ".join(label_parts)

        if not choices:
            return self.async_abort(reason="cloud_no_device_match")

        return self.async_show_form(
            step_id="cloud_pick_device",
            data_schema=vol.Schema(
                {
                    vol.Required("cloud_device_id"): vol.In(choices),
                }
            ),
            errors=errors,
        )

    async def _cloud_fetch_candidates(self) -> list[dict[str, Any]]:
        """Fetch all cloud devices visible to the project for onboarding."""
        auth = self._cloud_auth
        if not auth:
            return []

        from .cloud_schema import TuyaCloudSchemaHelper  # noqa: PLC0415

        helper = TuyaCloudSchemaHelper(
            auth["access_id"], auth["access_secret"], auth["region"]
        )

        cloud_devices = await helper.list_devices(strict=True)
        if not cloud_devices:
            return []

        # If the user supplied an IP in step 1, prefer devices that match it
        # but always fall back to the full list so nothing is hidden.
        host = self._flow_data.get(CONF_HOST, "").strip()
        if host:
            ip_matches = [
                d for d in cloud_devices
                if str(d.get("ip", "")).strip() == host
            ]
            pool = ip_matches if ip_matches else cloud_devices
        else:
            pool = cloud_devices

        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for dev in pool:
            dev_id = str(
                dev.get("id", "") or dev.get("device_id", "")
            ).strip()
            if not dev_id or dev_id in seen:
                continue
            seen.add(dev_id)

            candidate: dict[str, Any] = {
                "device_id": dev_id,
                "name": dev.get("name", "") or dev.get("product_name", ""),
                "ip": dev.get("ip", "") or dev.get("local_ip", "") or dev.get("lan_ip", ""),
                "category": dev.get("category", ""),
                "product_name": dev.get("product_name", ""),
            }

            results.append(candidate)

        return results

    async def _cloud_get_credentials(self, device_id: str) -> dict[str, Any] | None:
        """Fetch credentials for a single cloud device ID."""
        auth = self._cloud_auth
        if not auth:
            return None

        from .cloud_schema import TuyaCloudSchemaHelper  # noqa: PLC0415

        helper = TuyaCloudSchemaHelper(
            auth["access_id"], auth["access_secret"], auth["region"]
        )
        return await helper.get_device_credentials(device_id, strict=True)

    @staticmethod
    def _cloud_error_key(exc: Exception) -> str:
        """Map cloud onboarding exceptions to config-flow error keys."""
        from .cloud_schema import (  # noqa: PLC0415
            TuyaCloudAPIError,
            TuyaCloudAuthError,
            TuyaCloudPaginationError,
            TuyaCloudPermissionExpiredError,
            TuyaCloudPathError,
            TuyaCloudParseError,
            TuyaCloudRegionError,
        )

        if isinstance(exc, TuyaCloudPermissionExpiredError):
            return "cloud_permission_expired"
        if isinstance(exc, TuyaCloudAuthError):
            return "cloud_auth_failed"
        if isinstance(exc, TuyaCloudRegionError):
            return "cloud_region_mismatch"
        if isinstance(exc, TuyaCloudPathError):
            return "cloud_api_path_failed"
        if isinstance(exc, TuyaCloudParseError):
            return "cloud_parse_failed"
        if isinstance(exc, TuyaCloudPaginationError):
            return "cloud_pagination_failed"
        if isinstance(exc, TuyaCloudAPIError):
            return "cloud_api_failed"
        return "cloud_fetch_failed"

    async def _apply_cloud_candidate(self, candidate: dict[str, Any]) -> None:
        """Apply selected cloud candidate into flow data for local runtime."""
        self._flow_data[CONF_DEVICE_ID] = str(candidate.get("device_id", "")).strip()
        self._flow_data[CONF_LOCAL_KEY] = str(candidate.get("local_key", "")).strip()
        self._host_resolution_note = ""

        # In cloud-assisted mode, never replace a user-entered local host.
        # Only use cloud IP when host is missing and the cloud IP is private LAN.
        cloud_ip = str(candidate.get("ip", "")).strip()
        manual_host = self._flow_data.get(CONF_HOST, "").strip()
        host_set = False
        if manual_host:
            self._flow_data[CONF_HOST] = manual_host
            if cloud_ip and manual_host != cloud_ip:
                _LOGGER.info(
                    "Cloud onboarding: keeping manual host %s (ignoring cloud IP %s) "
                    "for device %s",
                    manual_host,
                    cloud_ip,
                    self._flow_data.get(CONF_DEVICE_ID, ""),
                )
            host_set = True
            self._host_resolution_note = (
                "Using your provided host value."
            )
        elif cloud_ip:
            try:
                parsed_ip = ipaddress.ip_address(cloud_ip)
                if parsed_ip.is_private:
                    self._flow_data[CONF_HOST] = cloud_ip
                    host_set = True
                    self._host_resolution_note = (
                        "Host was filled from Tuya cloud device metadata."
                    )
                else:
                    _LOGGER.warning(
                        "Cloud onboarding: cloud IP %s is public; not using it as local host for device %s",
                        cloud_ip,
                        self._flow_data.get(CONF_DEVICE_ID, ""),
                    )
                    self._host_resolution_note = (
                        "Cloud returned a non-local IP, so Conti requires manual local host confirmation."
                    )
            except ValueError:
                _LOGGER.warning(
                    "Cloud onboarding: invalid cloud IP '%s'; not using it as local host for device %s",
                    cloud_ip,
                    self._flow_data.get(CONF_DEVICE_ID, ""),
                )
                self._host_resolution_note = (
                    "Cloud returned an invalid IP value, so Conti requires manual local host confirmation."
                )

        # If host not set, try LAN IP discovery
        if not host_set:
            device_id = self._flow_data.get(CONF_DEVICE_ID, "")
            local_host, lan_candidates = await _discover_confident_lan_host(device_id)
            self._lan_candidates = lan_candidates
            if local_host:
                self._flow_data[CONF_HOST] = local_host
                self._host_resolution_note = (
                    "Host was auto-detected from local LAN scan."
                )
                _LOGGER.info(
                    "LAN discovery: matched device %s to host %s via local scan",
                    device_id,
                    local_host,
                )
            elif len(lan_candidates) > 1:
                self._host_resolution_note = (
                    "Multiple possible local IPs were detected. Please confirm the correct host."
                )
                _LOGGER.info(
                    "LAN discovery: multiple local matches for %s (%s); showing confirm_host",
                    device_id,
                    lan_candidates,
                )
            else:
                if not self._host_resolution_note:
                    self._host_resolution_note = (
                        "No confident LAN IP was detected. This is common in inter-VLAN setups."
                    )
                _LOGGER.info(
                    "LAN discovery: no match for %s; showing confirm_host",
                    device_id,
                )

        category = str(candidate.get("category", "")).strip()
        if category:
            self._tuya_category = category

    @staticmethod
    def _is_ir_category(category: str) -> bool:
        """Return True for Tuya cloud IR hub category names."""
        return category.strip().lower() == "infrared"

    async def _apply_ir_candidate(self, candidate: dict[str, Any]) -> None:
        """Apply selected cloud candidate into flow data for IR runtime."""
        device_id = str(
            candidate.get("device_id") or candidate.get("id") or ""
        ).strip()
        self._flow_data[CONF_DEVICE_ID] = device_id
        self._flow_data[CONF_LOCAL_KEY] = str(candidate.get("local_key", "")).strip()
        self._flow_data[CONF_HOST] = str(candidate.get("ip", "")).strip()
        self._flow_data[CONF_DEVICE_TYPE] = DEVICE_TYPE_IR
        category = str(candidate.get("category", "")).strip()
        if category:
            self._tuya_category = category
        if not self._flow_data.get(CONF_NAME):
            self._flow_data[CONF_NAME] = (
                str(candidate.get("name", "")).strip() or device_id
            )
        _LOGGER.info("IR device detected device=%s category=%s", device_id, category)

    async def _get_ir_cloud(self) -> Any:
        """Return an IR cloud wrapper from current OAuth or project credentials."""
        from .ir_cloud import TuyaIRCloud  # noqa: PLC0415

        if self._onboarding_mode == "smart_life":
            from .tuya_oauth import TuyaOAuthManager  # noqa: PLC0415

            if self._oauth_manager is None:
                self._oauth_manager = TuyaOAuthManager(self.hass)
                await self._oauth_manager.async_load()
            return TuyaIRCloud(self._oauth_manager.get_schema_helper())

        auth = self._cloud_auth
        from .cloud_schema import TuyaCloudSchemaHelper  # noqa: PLC0415

        helper = TuyaCloudSchemaHelper(
            auth["access_id"], auth["access_secret"], auth["region"]
        )
        return TuyaIRCloud(helper)

    async def async_step_ir_category(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """IR setup step 1: select appliance category."""
        errors: dict[str, str] = {}
        device_id = self._flow_data.get(CONF_DEVICE_ID, "")

        if user_input is not None:
            category_id = str(user_input.get("ir_category", "")).strip()
            selected = next(
                (item for item in self._ir_categories if item.get("id") == category_id),
                None,
            )
            if selected is None:
                errors["base"] = "ir_command_not_found"
            else:
                self._ir_category = selected
                return await self.async_step_ir_brand()

        if not self._ir_categories:
            try:
                cloud = await self._get_ir_cloud()
                self._ir_categories = await cloud.list_categories(device_id)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("IR category fetch failed for %s", device_id)
                errors["base"] = "ir_library_fetch_failed"

        choices = {
            item["id"]: item.get("name") or item["id"]
            for item in self._ir_categories
            if item.get("id")
        }
        if not choices and "base" not in errors:
            errors["base"] = "ir_library_fetch_failed"

        return self.async_show_form(
            step_id="ir_category",
            data_schema=vol.Schema({vol.Required("ir_category"): vol.In(choices) if choices else str}),
            errors=errors,
        )

    async def async_step_ir_brand(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """IR setup step 2: select appliance brand."""
        errors: dict[str, str] = {}
        device_id = self._flow_data.get(CONF_DEVICE_ID, "")
        category_id = str(self._ir_category.get("id", "")).strip()

        if user_input is not None:
            brand_id = str(user_input.get("ir_brand", "")).strip()
            selected = next(
                (item for item in self._ir_brands if item.get("id") == brand_id),
                None,
            )
            if selected is None:
                errors["base"] = "ir_command_not_found"
            else:
                self._ir_brand = selected
                return await self.async_step_ir_model()

        if not self._ir_brands:
            try:
                cloud = await self._get_ir_cloud()
                self._ir_brands = await cloud.list_brands(device_id, category_id)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("IR brand fetch failed for %s", device_id)
                errors["base"] = "ir_library_fetch_failed"

        choices = {
            item["id"]: item.get("name") or item["id"]
            for item in self._ir_brands
            if item.get("id")
        }
        if not choices and "base" not in errors:
            errors["base"] = "ir_library_fetch_failed"

        return self.async_show_form(
            step_id="ir_brand",
            data_schema=vol.Schema({vol.Required("ir_brand"): vol.In(choices) if choices else str}),
            errors=errors,
        )

    async def async_step_ir_model(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """IR setup step 3: select appliance model/remote index."""
        errors: dict[str, str] = {}
        device_id = self._flow_data.get(CONF_DEVICE_ID, "")
        category_id = str(self._ir_category.get("id", "")).strip()
        brand_id = str(self._ir_brand.get("id", "")).strip()

        if user_input is not None:
            model_id = str(user_input.get("ir_model", "")).strip()
            selected = next(
                (item for item in self._ir_models if item.get("id") == model_id),
                None,
            )
            if selected is None:
                errors["base"] = "ir_command_not_found"
            else:
                self._ir_model = selected
                return await self.async_step_ir_fetch()

        if not self._ir_models:
            try:
                cloud = await self._get_ir_cloud()
                self._ir_models = await cloud.list_models(
                    device_id, category_id, brand_id
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("IR model fetch failed for %s", device_id)
                errors["base"] = "ir_library_fetch_failed"

        choices = {
            item["id"]: item.get("name") or item["id"]
            for item in self._ir_models
            if item.get("id")
        }
        if not choices and "base" not in errors:
            errors["base"] = "ir_library_fetch_failed"

        warning = ""
        category_name = str(self._ir_category.get("name", "")).lower()
        if category_name in {"ac", "air conditioner"} or category_id in {"5"}:
            warning = "Learned commands may override full device state (temp/mode/fan)"

        return self.async_show_form(
            step_id="ir_model",
            data_schema=vol.Schema({vol.Required("ir_model"): vol.In(choices) if choices else str}),
            description_placeholders={"ac_warning": warning},
            errors=errors,
        )

    async def async_step_ir_fetch(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """IR setup step 4: fetch and store the selected library."""
        errors: dict[str, str] = {}
        device_id = self._flow_data.get(CONF_DEVICE_ID, "")

        try:
            cloud = await self._get_ir_cloud()
            commands = await cloud.fetch_commands(device_id, self._ir_model)
            from .ir_storage import IRStorage  # noqa: PLC0415

            storage = IRStorage(self.hass, device_id)
            await storage.async_save_library(
                category=str(self._ir_category.get("name") or self._ir_category.get("id") or ""),
                brand=str(self._ir_brand.get("name") or self._ir_brand.get("id") or ""),
                model=str(self._ir_model.get("name") or self._ir_model.get("id") or ""),
                commands=commands,
            )
            _LOGGER.info(
                "IR library fetch result device=%s commands=%d",
                device_id,
                len(commands),
            )
            return self._create_ir_config_entry()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("IR library fetch/store failed for %s", device_id)
            errors["base"] = "ir_library_fetch_failed"

        return self.async_show_form(
            step_id="ir_fetch",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    # ═══════════════════════════════════════════════════════════════════
    # Step (cloud only): Confirm host when auto-detection could not fill it
    # ═══════════════════════════════════════════════════════════════════

    async def async_step_confirm_host(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Cloud path — ask for host when LAN auto-detection did not produce
        a confident single match.  Already-set host is never reached here."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input.get(CONF_HOST, "").strip()
            if not host:
                errors["base"] = "host_required_manual"
            else:
                self._flow_data[CONF_HOST] = host
                port = int(self._flow_data.get(CONF_PORT, DEFAULT_PORT))
                ok_tcp, tcp_err, tcp_kind = await _async_probe_tcp(host, port)
                if not ok_tcp:
                    _LOGGER.warning(
                        "Config flow: confirm_host reachability check failed "
                        "host=%s port=%s resolved_error_type=%s",
                        host,
                        port,
                        tcp_kind,
                    )
                    errors["base"] = tcp_err or ERR_DEVICE_NOT_RESPONDING
                else:
                    return await self.async_step_detect()

        # Build a helpful hint listing any LAN scan candidates.
        candidates = self._lan_candidates
        if candidates:
            candidates_hint = (
                "Possible device(s) found on your LAN: "
                + ", ".join(candidates)
                + ".  Pick the correct IP or enter it manually below."
            )
        else:
            candidates_hint = (
                "No device matching your selection was found on the local network.  "
                "Please enter the device\u2019s local IP address below."
            )

        host_resolution = self._host_resolution_note or (
            "Cloud credentials were fetched successfully. "
            "Only local host confirmation is needed before local protocol detection."
        )

        return self.async_show_form(
            step_id="confirm_host",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        default=self._flow_data.get(CONF_HOST, ""),
                    ): str,
                }
            ),
            description_placeholders={
                "candidates_hint": candidates_hint,
                "host_resolution": host_resolution,
            },
            errors=errors,
        )

    # ═══════════════════════════════════════════════════════════════════
    # Step 2: Auto-detect protocol + discover DPs + match profile
    # ═══════════════════════════════════════════════════════════════════

    async def async_step_detect(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2 — auto-detect everything and show results.

        First call (user_input=None): run detection, show form.
        Second call (user_input set): process form, route forward.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            # ── Form submission: route to cloud or review ──
            proto_override = user_input.get("protocol_override", "auto")
            if proto_override != "auto" and proto_override != self._detected_version:
                # Re-test with specific version
                ok, detected, discovered, err = await _test_device(
                    device_id=self._flow_data[CONF_DEVICE_ID],
                    ip=self._flow_data[CONF_HOST],
                    local_key=self._flow_data[CONF_LOCAL_KEY],
                    version=proto_override,
                    port=self._flow_data[CONF_PORT],
                )
                if ok:
                    self._detected_version = detected
                    if discovered:
                        self._discovered_dps = discovered

            if user_input.get("use_cloud_assist", False):
                return await self.async_step_cloud_assist()

            return await self.async_step_review()

        # ── First call: run detection ──
        ok, detected_version, discovered_dps, err = await _test_device(
            device_id=self._flow_data[CONF_DEVICE_ID],
            ip=self._flow_data[CONF_HOST],
            local_key=self._flow_data[CONF_LOCAL_KEY],
            version="auto",
            port=self._flow_data[CONF_PORT],
        )

        if not ok:
            errors["base"] = err or ERR_DEVICE_NOT_RESPONDING

            # Low-power battery sensors may be intentionally sleepy and not
            # continuously reachable on local TCP. Keep normal local behavior
            # unchanged for all other device classes.
            if self._is_strong_low_power_sensor_candidate(errors["base"]):
                self._low_power_sensor = True
                _LOGGER.info(
                    "Config flow: treating %s as low-power sleepy sensor "
                    "after local TCP failure; continuing with cloud-backed mapping",
                    self._flow_data[CONF_DEVICE_ID],
                )

                if not self._final_dp_map:
                    self._apply_safe_profile_fallback_if_needed()

                if not self._final_dp_map:
                    return await self.async_step_cloud_assist(
                        {
                            "tuya_access_id": self._cloud_auth.get("access_id", ""),
                            "tuya_access_secret": self._cloud_auth.get("access_secret", ""),
                            "tuya_region": self._cloud_auth.get("region", "eu"),
                        }
                    )

                return await self.async_step_review()

            # Cloud-assisted flow: keep user in host-confirmation context for
            # host/network failures, which are common in inter-VLAN setups.
            if (
                _is_lan_tcp_reachability_error(errors["base"])
                and self._onboarding_mode == "cloud_assisted"
            ):
                if not self._host_resolution_note:
                    self._host_resolution_note = (
                        "Cloud credentials were fetched successfully, but local TCP connection failed. "
                        "Confirm the local host/IP and network routing/firewall rules."
                    )
                return await self.async_step_confirm_host()

            # Keep protocol/auth failures in detect step so the user sees the
            # correct error classification and can try protocol override.
            if errors["base"] in {"wrong_protocol", "invalid_auth"}:
                return self.async_show_form(
                    step_id="detect",
                    data_schema=vol.Schema(
                        {
                            vol.Optional("use_cloud_assist", default=False): bool,
                            vol.Optional(
                                "protocol_override",
                                default="auto",
                            ): vol.In(SUPPORTED_VERSIONS),
                        }
                    ),
                    description_placeholders={
                        "protocol_version": self._detected_version or "unknown",
                        "dp_count": "0",
                        "mapped_count": str(len(self._final_dp_map)),
                        "profile_name": (
                            self._matched_profile["name"] if self._matched_profile else "No match"
                        ),
                        "confidence": f"{int(self._confidence * 100)}%",
                    },
                    errors=errors,
                )

            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(self._flow_data),
                errors=errors,
            )

        self._detected_version = detected_version
        self._discovered_dps = discovered_dps

        # ── Silent cloud schema fetch (if OAuth session available) ──
        # When the user arrived via Smart Life QR or cloud-assisted path an
        # OAuth manager may already hold the device's Tuya cloud schema.
        # Fetch it silently here (no extra step for the user) so that the
        # cloud DP codes can improve the mapping quality before heuristics run.
        await self._async_try_silent_cloud_schema_fetch()

        # ── Heuristic auto-mapping ──
        from .dp_mapping import auto_map_dps  # noqa: PLC0415

        device_type = self._flow_data[CONF_DEVICE_TYPE]
        if discovered_dps:
            self._auto_dp_map = auto_map_dps(device_type, discovered_dps)

        # ── Profile matching ──
        from .device_profiles import (  # noqa: PLC0415
            best_profile_for_dps,
            dp_map_from_profile,
        )

        profile, confidence = best_profile_for_dps(
            discovered_dps,
            device_type=device_type,
            tuya_category=self._tuya_category,
        )
        self._matched_profile = profile
        self._confidence = confidence

        if profile:
            self._profile_dp_map = dp_map_from_profile(
                profile, discovered_dps, confidence
            )

        # ── Build merged map: cloud > profile > heuristic ──
        # Use strict cloud-priority merge when cloud schema is available so
        # that authoritative Tuya DP assignments are never overridden by
        # heuristic guesses.  Fall back to simple profile < heuristic merge
        # when no cloud data is present.
        if self._cloud_dp_map:
            from .dp_mapping import merge_cloud_priority_dp_maps  # noqa: PLC0415

            self._final_dp_map = merge_cloud_priority_dp_maps(
                self._auto_dp_map,
                self._profile_dp_map,
                self._cloud_dp_map,
            )
            self._mapping_source = "cloud"
        else:
            from .dp_mapping import merge_all_dp_maps  # noqa: PLC0415

            self._final_dp_map = merge_all_dp_maps(
                self._auto_dp_map,
                self._profile_dp_map,
            )
            self._apply_safe_profile_fallback_if_needed()
            self._mapping_source = "auto" if self._final_dp_map else "raw_discovery"

        # ── Last-resort raw DP fallback ──
        if not self._final_dp_map and discovered_dps:
            from .dp_mapping import build_raw_dp_map  # noqa: PLC0415

            self._final_dp_map = build_raw_dp_map(discovered_dps)
            self._mapping_source = "raw_discovery"
            _LOGGER.warning(
                "Config flow: all mapping pipelines empty for %s — "
                "built raw DP map (%d DPs) from discovery",
                self._flow_data.get(CONF_DEVICE_ID, ""),
                len(self._final_dp_map),
            )

        _LOGGER.info(
            "Config flow detect: device=%s v%s, %d DPs discovered — "
            "auto=%d profile=%d cloud=%d final=%d source=%s "
            "profile_match=%s (confidence=%.2f)",
            self._flow_data[CONF_DEVICE_ID],
            detected_version,
            len(discovered_dps),
            len(self._auto_dp_map),
            len(self._profile_dp_map),
            len(self._cloud_dp_map),
            len(self._final_dp_map),
            self._mapping_source,
            profile["id"] if profile else "none",
            confidence,
        )

        # Show detection results with options
        profile_name = profile["name"] if profile else "No match"
        confidence_pct = int(confidence * 100)
        dp_count = len(self._final_dp_map)

        description_placeholders = {
            "protocol_version": detected_version or "unknown",
            "dp_count": str(len(discovered_dps)),
            "mapped_count": str(dp_count),
            "profile_name": profile_name,
            "confidence": f"{confidence_pct}%",
        }

        return self.async_show_form(
            step_id="detect",
            data_schema=vol.Schema(
                {
                    vol.Optional("use_cloud_assist", default=False): bool,
                    vol.Optional(
                        "protocol_override",
                        default="auto",
                    ): vol.In(SUPPORTED_VERSIONS),
                }
            ),
            description_placeholders=description_placeholders,
            errors=errors,
        )

    # ───────────────────────────────────────────────────────────────────
    # Helper: silent OAuth cloud schema fetch
    # ───────────────────────────────────────────────────────────────────

    async def _async_try_silent_cloud_schema_fetch(self) -> None:
        """Silently fetch the Tuya cloud schema for the current device.

        Uses whatever OAuth / QR session is already stored.  Failures are
        logged at DEBUG level and never surfaced to the user — the regular
        heuristic / profile mapping still runs afterwards and acts as the
        fallback.
        """
        device_id = self._flow_data.get(CONF_DEVICE_ID)
        if not device_id or self._cloud_dp_map:
            return  # nothing to do

        try:
            from .cloud_schema import TuyaCloudSchemaHelper  # noqa: PLC0415
            from .tuya_oauth import TuyaOAuthManager  # noqa: PLC0415

            if self._oauth_manager is None:
                self._oauth_manager = TuyaOAuthManager(self.hass)
                await self._oauth_manager.async_load()
            oauth = self._oauth_manager
            if not oauth.is_configured:
                return

            schema = await oauth.async_get_device_schema(device_id)
            if not schema:
                _LOGGER.debug(
                    "Silent cloud schema: no schema returned for %s", device_id
                )
                return

            cloud_map, category, _hint = TuyaCloudSchemaHelper.schema_to_dp_map(schema)
            if cloud_map:
                self._cloud_dp_map = cloud_map
                self._mapping_source = "cloud"
                _LOGGER.info(
                    "Silent cloud schema: %d DPs mapped for %s "
                    "(category=%s device_type_hint=%s)",
                    len(cloud_map),
                    device_id,
                    category,
                    _hint,
                )
            else:
                _LOGGER.debug(
                    "Silent cloud schema: schema returned for %s but no DPs "
                    "were mapped (category=%s) — heuristics will be used",
                    device_id, category,
                )
            if category and not getattr(self, "_tuya_category", None):
                self._tuya_category = category

        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Silent cloud schema fetch skipped for %s (no OAuth session or cloud unreachable)",
                self._flow_data.get(CONF_DEVICE_ID),
            )

    # ═══════════════════════════════════════════════════════════════════
    # Step 3: Cloud-assisted schema mapping (optional)
    # ═══════════════════════════════════════════════════════════════════

    async def async_step_cloud_assist(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 3 — optional cloud-assisted DP schema fetch."""
        errors: dict[str, str] = {}

        if user_input is not None:
            access_id = user_input.get("tuya_access_id", "").strip()
            access_secret = user_input.get("tuya_access_secret", "").strip()
            region = user_input.get("tuya_region", "eu")

            if not access_id and self._cloud_auth.get("access_id"):
                access_id = self._cloud_auth["access_id"]
            if not access_secret and self._cloud_auth.get("access_secret"):
                access_secret = self._cloud_auth["access_secret"]
            if region == "eu" and self._cloud_auth.get("region"):
                region = self._cloud_auth["region"]

            if not access_id or not access_secret:
                # Skip cloud — proceed to review with local-only mapping
                return await self.async_step_review()

            self._cloud_auth = {
                "access_id": access_id,
                "access_secret": access_secret,
                "region": region,
            }

            # Fetch cloud schema
            try:
                from .cloud_schema import TuyaCloudSchemaHelper  # noqa: PLC0415

                helper = TuyaCloudSchemaHelper(access_id, access_secret, region)
                schema = await helper.get_device_schema(
                    self._flow_data[CONF_DEVICE_ID]
                )

                if schema:
                    cloud_map, category, type_hint = helper.schema_to_dp_map(schema)
                    if category:
                        self._tuya_category = category

                    if cloud_map:
                        self._cloud_dp_map = cloud_map
                        self._mapping_source = "cloud"

                        _LOGGER.info(
                            "Cloud schema: %d DPs mapped (category=%s)",
                            len(cloud_map),
                            category,
                        )
                    else:
                        _LOGGER.info("Cloud schema returned no mappable DPs")

                    # Re-run profile matching with category hint even when
                    # cloud schema conversion yields an empty dp_map.
                    from .device_profiles import (  # noqa: PLC0415
                        best_profile_for_dps,
                        dp_map_from_profile,
                    )
                    profile, confidence = best_profile_for_dps(
                        self._discovered_dps,
                        device_type=self._flow_data[CONF_DEVICE_TYPE],
                        tuya_category=self._tuya_category,
                    )
                    if profile and (confidence > self._confidence or not self._profile_dp_map):
                        self._matched_profile = profile
                        self._confidence = max(self._confidence, confidence)
                        self._profile_dp_map = dp_map_from_profile(
                            profile,
                            self._discovered_dps,
                            self._confidence,
                        )

                    # Last-resort safe fallback: if still empty, use a
                    # category-selected profile template only when choice is
                    # unambiguous or discovery evidence is strong enough.
                    self._apply_safe_profile_fallback_if_needed()

                    # Re-merge with cloud as highest-priority source.
                    from .dp_mapping import merge_cloud_priority_dp_maps  # noqa: PLC0415
                    self._final_dp_map = merge_cloud_priority_dp_maps(
                        self._auto_dp_map,
                        self._profile_dp_map,
                        self._cloud_dp_map,
                    )
                    self._mapping_source = "cloud" if self._cloud_dp_map else "auto"
                else:
                    errors["base"] = "cloud_fetch_failed"

            except Exception:  # noqa: BLE001
                _LOGGER.exception("Cloud schema fetch failed")
                errors["base"] = "cloud_fetch_failed"

            if not errors:
                return await self.async_step_review()

        return self.async_show_form(
            step_id="cloud_assist",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "tuya_access_id",
                        default=self._cloud_auth.get("access_id", ""),
                    ): str,
                    vol.Optional(
                        "tuya_access_secret",
                        default=self._cloud_auth.get("access_secret", ""),
                    ): str,
                    vol.Optional(
                        "tuya_region",
                        default=self._cloud_auth.get("region", "eu"),
                    ): vol.In(
                        {"us": "Americas", "eu": "Europe", "cn": "China", "in": "India"}
                    ),
                }
            ),
            errors=errors,
        )

    # ═══════════════════════════════════════════════════════════════════
    # Step 4: Review mapping and confirm
    # ═══════════════════════════════════════════════════════════════════

    async def async_step_review(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 4 — review and confirm the DP mapping."""
        errors: dict[str, str] = {}

        if user_input is not None:
            action = user_input.get("action", "accept")

            if action == "learn":
                return await self.async_step_pre_learn()

            if action == "edit":
                # User wants manual edit — validate JSON
                dp_map_raw = user_input.get(CONF_DP_MAP, "{}")
                try:
                    user_map = json.loads(dp_map_raw)
                    if not isinstance(user_map, dict):
                        raise ValueError
                except (json.JSONDecodeError, ValueError):
                    errors[CONF_DP_MAP] = "invalid_dp_map"
                    # Fall through to re-show form
                else:
                    self._final_dp_map = user_map
                    self._mapping_source = "manual"
                    self._confidence = 1.0

            if action == "accept" and not self._final_dp_map:
                errors["base"] = "empty_dp_map"

            if (
                action == "accept"
                and not errors
                and self._mapping_source != "manual"
                and self._looks_like_cct_light()
                and self._is_incomplete_cct_mapping(self._final_dp_map)
            ):
                errors["base"] = "incomplete_cct_map"

            if not errors and action in ("accept", "edit"):
                return self._create_config_entry()

        # Build description of what was detected
        profile_name = self._matched_profile["name"] if self._matched_profile else "None"
        confidence_pct = int(self._confidence * 100)
        dp_summary = json.dumps(self._final_dp_map, indent=2)

        # Confidence warning for the user
        confidence_warning = ""
        if not self._final_dp_map:
            confidence_warning = (
                "\u26a0 No data points have been mapped. "
                "The device will not create any entities. "
                "Use Guided Learn or Manual Edit to add mappings."
            )
        elif self._confidence < 0.4:
            confidence_warning = (
                "\u26a0 Mapping confidence is low. "
                "Some data points may be missing or incorrectly assigned. "
                "Guided Learn is recommended to improve accuracy."
            )
        elif self._confidence < _CONFIDENCE_THRESHOLD:
            confidence_warning = (
                "Mapping confidence is moderate. "
                "You can accept or use Guided Learn to verify."
            )

        # Always show all action choices
        default_action = (
            "accept" if self._confidence >= _CONFIDENCE_THRESHOLD
            else "learn"
        )
        action_choices: dict[str, str] = {
            "accept": "Accept and finish",
            "learn": (
                "Guided learn (recommended)"
                if self._confidence < _CONFIDENCE_THRESHOLD
                else "Guided learn"
            ),
            "edit": "Manual edit (advanced)",
        }

        description_placeholders = {
            "profile_name": profile_name,
            "confidence": f"{confidence_pct}%",
            "mapping_source": self._mapping_source,
            "dp_summary": dp_summary,
            "confidence_warning": confidence_warning,
        }

        return self.async_show_form(
            step_id="review",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default=default_action): vol.In(
                        action_choices
                    ),
                    vol.Optional(
                        CONF_DP_MAP,
                        default=json.dumps(self._final_dp_map),
                    ): str,
                }
            ),
            description_placeholders=description_placeholders,
            errors=errors,
        )

    # ═══════════════════════════════════════════════════════════════════
    # Step 5: Pre-learn — device classification + action plan
    # ═══════════════════════════════════════════════════════════════════

    async def async_step_pre_learn(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show device classification and prepare guided learn."""
        from .guided_learn import (  # noqa: PLC0415
            classify_device_family,
            generate_learn_steps,
            build_action_plan,
            family_display_name,
            FAMILY_MULTI_GANG_SWITCH,
            FAMILY_POWER_STRIP,
        )

        if user_input is not None:
            # User confirmed — adjust gang count if provided
            if "gang_count" in user_input:
                self._gang_count = int(user_input["gang_count"])

            # Generate steps and proceed to learn
            self._learn_steps = generate_learn_steps(
                self._device_family, self._gang_count
            )
            self._learn_step_idx = 0
            self._learn_session = None  # Fresh session
            self._learn_feedback = ""
            return await self.async_step_learn()

        # First call: classify device
        family, gangs, reason = classify_device_family(
            self._discovered_dps,
            self._flow_data[CONF_DEVICE_TYPE],
            profile=self._matched_profile,
            tuya_category=self._tuya_category,
        )
        self._device_family = family
        self._gang_count = gangs
        self._family_reason = reason

        # Generate steps for the action plan preview
        steps = generate_learn_steps(family, gangs)
        action_plan = build_action_plan(steps)
        display_name = family_display_name(family, gangs)

        description_placeholders = {
            "device_family": display_name,
            "classification_reason": reason,
            "action_plan": action_plan,
            "total_steps": str(len(steps)),
        }

        schema_dict: dict[Any, Any] = {}
        if family in (FAMILY_MULTI_GANG_SWITCH, FAMILY_POWER_STRIP):
            schema_dict[vol.Required("gang_count", default=gangs)] = vol.All(
                int, vol.Range(min=1, max=8)
            )

        return self.async_show_form(
            step_id="pre_learn",
            data_schema=vol.Schema(schema_dict),
            description_placeholders=description_placeholders,
        )

    # ═══════════════════════════════════════════════════════════════════
    # Step 6: Guided learn mode (interactive)
    # ═══════════════════════════════════════════════════════════════════

    async def async_step_learn(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 6 — interactive guided learn for DP mapping."""
        from .guided_learn import (  # noqa: PLC0415
            LearnSession,
            family_display_name,
            validate_evidence,
            describe_missing,
            get_required_roles,
        )

        # Initialise learn session on first entry
        if self._learn_session is None:
            baseline = dict(self._discovered_dps)
            if not baseline:
                _, _, baseline_dps, _ = await _test_device(
                    device_id=self._flow_data[CONF_DEVICE_ID],
                    ip=self._flow_data[CONF_HOST],
                    local_key=self._flow_data[CONF_LOCAL_KEY],
                    version=self._detected_version or "auto",
                    port=self._flow_data[CONF_PORT],
                )
                baseline = baseline_dps or {}
            self._learn_session = LearnSession(baseline)

        session: LearnSession = self._learn_session

        if user_input is not None:
            action = user_input.get("learn_action", "check")

            if action == "finish":
                # Merge whatever was learned and return to review
                from .dp_mapping import merge_all_dp_maps  # noqa: PLC0415
                self._final_dp_map = merge_all_dp_maps(
                    self._final_dp_map,
                    session.learned_map,
                )
                self._mapping_source = "learn"
                # Update confidence based on evidence completeness
                required = get_required_roles(
                    self._device_family, self._gang_count
                )
                if required:
                    matched = len(
                        [r for r in required if r in session.learned_roles]
                    )
                    learn_conf = matched / len(required)
                    self._confidence = max(self._confidence, learn_conf)
                else:
                    self._confidence = max(self._confidence, 0.7)
                return await self.async_step_review()

            if action == "skip":
                self._learn_step_idx += 1
                self._learn_feedback = "Skipped previous step."

            elif action == "check":
                # Read fresh DPS and find changes
                _, _, fresh_dps, _ = await _test_device(
                    device_id=self._flow_data[CONF_DEVICE_ID],
                    ip=self._flow_data[CONF_HOST],
                    local_key=self._flow_data[CONF_LOCAL_KEY],
                    version=self._detected_version or "auto",
                    port=self._flow_data[CONF_PORT],
                )
                if fresh_dps:
                    changes = session.apply_diff(fresh_dps)
                    if changes and len(changes) == 1:
                        dp_id, _old, new_val = changes[0]
                        current = self._learn_steps[self._learn_step_idx]
                        session.set_pending_role(
                            current["role"], current["type"]
                        )
                        session.assign_change(dp_id, new_val)
                        self._learn_feedback = (
                            f"\u2705 Detected! DP {dp_id} assigned to "
                            f"'{current['role']}'."
                        )
                        self._learn_step_idx += 1
                        _LOGGER.info(
                            "Learn: DP %s changed \u2192 assigned to '%s'",
                            dp_id,
                            current["role"],
                        )
                    elif changes and len(changes) > 1:
                        # Multiple DPs changed — pick the best one
                        # based on expected type and change magnitude.
                        current = self._learn_steps[self._learn_step_idx]
                        expected_type = current["type"]

                        # Filter by expected value type
                        type_matches = []
                        for dp_id, old_val, new_val in changes:
                            if isinstance(new_val, bool):
                                val_type = "bool"
                            elif isinstance(new_val, (int, float)):
                                val_type = "int"
                            else:
                                val_type = "str"
                            if val_type == expected_type:
                                type_matches.append(
                                    (dp_id, old_val, new_val)
                                )

                        # Exclude already-learned DPs
                        unlearned = [
                            c for c in type_matches
                            if c[0] not in session.learned_map
                        ]
                        candidates = unlearned if unlearned else type_matches

                        if len(candidates) == 1:
                            dp_id, _old, new_val = candidates[0]
                            session.set_pending_role(
                                current["role"], current["type"]
                            )
                            session.assign_change(dp_id, new_val)
                            self._learn_feedback = (
                                f"\u2705 Detected! DP {dp_id} assigned to "
                                f"'{current['role']}'."
                            )
                            self._learn_step_idx += 1
                            _LOGGER.info(
                                "Learn: DP %s \u2192 '%s' "
                                "(filtered from %d changes)",
                                dp_id,
                                current["role"],
                                len(changes),
                            )
                        elif (
                            len(candidates) > 1
                            and expected_type == "int"
                        ):
                            # Multiple int changes: score each candidate using
                            # role-aware DP-ID hints and change magnitude.
                            current_role = current["role"]
                            preferred_ids = {
                                "brightness": {"2", "22"},
                                "color_temp": {"3", "23"},
                            }.get(current_role, set())

                            known_role_by_dp = {
                                dp: spec.get("key")
                                for dp, spec in self._final_dp_map.items()
                                if isinstance(spec, dict)
                            }

                            def _delta(entry: tuple[str, Any, Any]) -> float:
                                _, ov, nv = entry
                                try:
                                    return abs(float(nv) - float(ov or 0))
                                except (TypeError, ValueError):
                                    return 0.0

                            def _score(entry: tuple[str, Any, Any]) -> tuple[int, int, float]:
                                dp_id, _ov, _nv = entry
                                preferred = 1 if dp_id in preferred_ids else 0
                                role_match = 0
                                if known_role_by_dp.get(dp_id) == current_role:
                                    role_match = 1
                                return (role_match, preferred, _delta(entry))

                            best = max(candidates, key=_score)
                            dp_id, _old, new_val = best
                            session.set_pending_role(
                                current["role"], current["type"]
                            )
                            session.assign_change(dp_id, new_val)
                            self._learn_feedback = (
                                f"\u2705 Detected! DP {dp_id} assigned "
                                f"to '{current['role']}' "
                                f"(picked from {len(changes)} changes "
                                f"using role-aware scoring)."
                            )
                            self._learn_step_idx += 1
                            _LOGGER.info(
                                "Learn: DP %s \u2192 '%s' "
                                "(largest delta, %d candidates)",
                                dp_id,
                                current["role"],
                                len(candidates),
                            )
                        elif (
                            len(candidates) > 1
                            and expected_type == "bool"
                        ):
                            # Multiple bool changes are common on plugs/switches
                            # (e.g. relay + indicator/lock). Prefer channel-like
                            # IDs and existing role hints instead of failing.
                            current_role = current["role"]
                            preferred_ids: set[str] = set()
                            if current_role == "power":
                                preferred_ids = {"1", "20"}
                            elif current_role.startswith("switch_"):
                                suffix = current_role.replace("switch_", "")
                                if suffix.isdigit():
                                    preferred_ids = {suffix}

                            known_role_by_dp = {
                                dp: spec.get("key")
                                for dp, spec in self._final_dp_map.items()
                                if isinstance(spec, dict)
                            }

                            channel_like_ids = {
                                "1", "2", "3", "4", "5", "6", "7", "8",
                                "20", "21", "22", "23", "24", "25",
                            }

                            def _score_bool(entry: tuple[str, Any, Any]) -> tuple[int, int, int, int]:
                                dp_id, _ov, _nv = entry
                                role_match = 1 if known_role_by_dp.get(dp_id) == current_role else 0
                                preferred = 1 if dp_id in preferred_ids else 0
                                channel_like = 1 if dp_id in channel_like_ids else 0
                                # Smaller numeric IDs are more commonly main relays.
                                try:
                                    inv_numeric = -int(dp_id)
                                except (TypeError, ValueError):
                                    inv_numeric = -10_000
                                return (role_match, preferred, channel_like, inv_numeric)

                            best = max(candidates, key=_score_bool)
                            dp_id, _old, new_val = best
                            session.set_pending_role(
                                current["role"], current["type"]
                            )
                            session.assign_change(dp_id, new_val)
                            self._learn_feedback = (
                                f"\u2705 Detected! DP {dp_id} assigned "
                                f"to '{current['role']}' "
                                f"(picked from {len(changes)} changes "
                                f"using bool-role scoring)."
                            )
                            self._learn_step_idx += 1
                            _LOGGER.info(
                                "Learn: DP %s \u2192 '%s' "
                                "(bool-role scoring, %d candidates)",
                                dp_id,
                                current["role"],
                                len(candidates),
                            )
                        else:
                            self._learn_feedback = (
                                f"\u26a0 {len(changes)} data points "
                                f"changed at once. Try changing "
                                f"ONLY ONE thing at a time, "
                                f"then check again."
                            )
                            _LOGGER.info(
                                "Learn: %d DPs changed "
                                "simultaneously \u2014 ambiguous",
                                len(changes),
                            )
                    else:
                        self._learn_feedback = (
                            "\u26a0 No change detected. Make sure you "
                            "performed the action, wait 2 seconds, "
                            "and try again."
                        )
                else:
                    self._learn_feedback = (
                        "\u26a0 Could not read device state. "
                        "Check that the device is still reachable."
                    )

        # Check if we've gone through all learn steps
        if self._learn_step_idx >= len(self._learn_steps):
            from .dp_mapping import merge_all_dp_maps  # noqa: PLC0415
            self._final_dp_map = merge_all_dp_maps(
                self._final_dp_map,
                session.learned_map,
            )
            self._mapping_source = "learn"
            required = get_required_roles(
                self._device_family, self._gang_count
            )
            if required:
                matched = len(
                    [r for r in required if r in session.learned_roles]
                )
                learn_conf = matched / len(required)
                self._confidence = max(self._confidence, learn_conf)
            else:
                self._confidence = max(self._confidence, 0.7)
            return await self.async_step_review()

        # Show current learn instruction
        current_step = self._learn_steps[self._learn_step_idx]
        session.set_pending_role(current_step["role"], current_step["type"])

        # Build progress info
        _ev_ok, missing_roles = validate_evidence(
            self._device_family,
            session.learned_roles,
            self._gang_count,
        )
        missing_text = describe_missing(missing_roles, self._device_family)

        learned_summary = ""
        if session.learned_count > 0:
            parts = [
                f"{info['key']} (DP {dp})"
                for dp, info in session.learned_map.items()
            ]
            learned_summary = (
                f"Learned {session.learned_count}: " + ", ".join(parts)
            )

        display_name = family_display_name(
            self._device_family, self._gang_count
        )

        description_placeholders = {
            "device_family": display_name,
            "instruction": current_step["instruction"],
            "step_number": str(self._learn_step_idx + 1),
            "total_steps": str(len(self._learn_steps)),
            "feedback": self._learn_feedback,
            "learned_summary": learned_summary,
            "missing_summary": missing_text,
        }

        # Build action choices
        actions: dict[str, str] = {
            "check": "I did it \u2014 check for changes",
        }
        if not current_step.get("required", True):
            actions["skip"] = "Skip this step (optional)"
        else:
            actions["skip"] = "Skip this step"
        actions["finish"] = "Finish learning and review"

        return self.async_show_form(
            step_id="learn",
            data_schema=vol.Schema(
                {
                    vol.Required("learn_action", default="check"): vol.In(
                        actions
                    ),
                }
            ),
            description_placeholders=description_placeholders,
        )

    def _apply_safe_profile_fallback_if_needed(self) -> None:
        """Apply category/profile fallback only when evidence is safe.

        This avoids reaching review with an empty map in cloud-assisted
        onboarding when local DP discovery is weak, while keeping conservative
        behavior for ambiguous categories.
        """
        if self._final_dp_map:
            return

        category = (self._tuya_category or "").strip()
        device_type = self._flow_data.get(CONF_DEVICE_TYPE)

        from .device_profiles import (  # noqa: PLC0415
            DEVICE_PROFILES,
            dp_map_from_profile,
            match_profile_by_category,
            score_profile_against_dps,
        )

        # Build candidate list: category-matched first, then device_type only
        candidates: list[dict[str, Any]] = []
        if category:
            candidates = [
                p for p in match_profile_by_category(category)
                if p.get("device_type") == device_type
            ]
        if not candidates and device_type:
            candidates = [
                p for p in DEVICE_PROFILES
                if p.get("device_type") == device_type
            ]
        if not candidates:
            return

        discovered = self._discovered_dps or {}
        scored = [
            (score_profile_against_dps(p, discovered), p)
            for p in candidates
        ]
        scored.sort(key=lambda item: item[0], reverse=True)

        chosen: dict[str, Any] | None = None
        score = 0.0

        if scored and scored[0][0] > 0:
            score, chosen = scored[0]
            if len(scored) > 1 and scored[1][0] == score:
                chosen = None
        elif len(candidates) == 1:
            chosen = candidates[0]
            score = 0.5
        elif candidates:
            # Multiple candidates, all score 0 — pick the simplest profile
            # (smallest dp_template) as a best-effort fallback.
            sorted_by_size = sorted(
                candidates, key=lambda p: len(p.get("dp_template", {}))
            )
            chosen = sorted_by_size[0]
            score = 0.3

        if not chosen:
            return

        self._matched_profile = chosen
        self._confidence = max(self._confidence, score)
        self._profile_dp_map = dp_map_from_profile(
            chosen,
            discovered,
            self._confidence,
        )

        from .dp_mapping import merge_all_dp_maps  # noqa: PLC0415

        self._final_dp_map = merge_all_dp_maps(
            self._auto_dp_map,
            self._profile_dp_map,
            self._cloud_dp_map,
        )
        _LOGGER.info(
            "Profile fallback applied from category '%s': profile=%s, mapped=%d",
            category,
            chosen.get("id", "unknown"),
            len(self._final_dp_map),
        )

    def _looks_like_cct_light(self) -> bool:
        """Return True when device evidence strongly suggests a CCT light."""
        if self._flow_data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_LIGHT:
            return False

        from .guided_learn import (  # noqa: PLC0415
            FAMILY_CCT_LIGHT,
            classify_device_family,
        )

        family, _gangs, _reason = classify_device_family(
            self._discovered_dps,
            DEVICE_TYPE_LIGHT,
            profile=self._matched_profile,
            tuya_category=self._tuya_category,
        )
        return family == FAMILY_CCT_LIGHT

    @staticmethod
    def _is_incomplete_cct_mapping(dp_map: dict[str, Any]) -> bool:
        """A CCT-capable light needs power + brightness + color_temp roles."""
        roles = {
            str(spec.get("key", ""))
            for spec in dp_map.values()
            if isinstance(spec, dict)
        }
        required = {"power", "brightness", "color_temp"}
        return not required.issubset(roles)

    def _is_strong_low_power_sensor_candidate(self, error_key: str) -> bool:
        """Return True when evidence strongly suggests a sleepy battery sensor."""
        if not _is_lan_tcp_reachability_error(error_key):
            return False

        if self._flow_data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_SENSOR:
            return False

        # We only auto-switch to low-power path when cloud-assisted onboarding
        # is active and cloud credentials are available.
        if self._onboarding_mode != "cloud_assisted" or not self._cloud_auth:
            return False

        category = (self._tuya_category or "").strip().lower()
        if category in _LOW_POWER_SENSOR_CATEGORIES:
            return True

        profile_id = str((self._matched_profile or {}).get("id", "")).strip()
        if profile_id in _LOW_POWER_SENSOR_PROFILE_IDS:
            return True

        sensor_keys = {
            "contact",
            "door_state",
            "motion",
            "battery",
            "temperature",
            "humidity",
        }
        for info in self._final_dp_map.values():
            if isinstance(info, dict) and str(info.get("key", "")) in sensor_keys:
                return True

        return False

    # ═══════════════════════════════════════════════════════════════════
    # Entry creation helper
    # ═══════════════════════════════════════════════════════════════════

    def _create_config_entry(self) -> config_entries.ConfigFlowResult:
        """Build and persist the config entry from accumulated flow data."""
        local_key = self._flow_data.get(CONF_LOCAL_KEY, "")
        host = self._flow_data.get(CONF_HOST, "")

        entry_data: dict[str, Any] = {
            CONF_DEVICE_ID: self._flow_data[CONF_DEVICE_ID],
            CONF_HOST: host,
            CONF_PORT: self._flow_data.get(CONF_PORT, DEFAULT_PORT),
            CONF_LOCAL_KEY: local_key,
            CONF_PROTOCOL_VERSION: "auto",
            CONF_DEVICE_TYPE: self._flow_data[CONF_DEVICE_TYPE],
            CONF_DP_MAP: json.dumps(self._final_dp_map),
            CONF_MAPPING_SOURCE: self._mapping_source,
            CONF_MAPPING_CONFIDENCE: self._confidence,
        }

        # Persist auto-detected version
        if self._detected_version:
            entry_data[CONF_DETECTED_VERSION] = self._detected_version

        # Persist raw discovered DPS for diagnostics / re-mapping
        if self._discovered_dps:
            entry_data[CONF_DISCOVERED_DPS] = json.dumps(self._discovered_dps)

        # Persist profile ID
        if self._matched_profile:
            entry_data[CONF_DEVICE_PROFILE] = self._matched_profile["id"]

        # Persist Tuya category if from cloud
        if self._tuya_category:
            entry_data[CONF_TUYA_CATEGORY] = self._tuya_category

        if self._low_power_sensor:
            entry_data[CONF_LOW_POWER_DEVICE] = True
            entry_data[CONF_RUNTIME_CHANNEL] = RUNTIME_CHANNEL_CLOUD_SENSOR
            if self._cloud_auth.get("access_id") and self._cloud_auth.get("access_secret"):
                entry_data[CONF_CLOUD_ACCESS_ID] = self._cloud_auth["access_id"]
                entry_data[CONF_CLOUD_ACCESS_SECRET] = self._cloud_auth["access_secret"]
                entry_data[CONF_CLOUD_REGION] = self._cloud_auth.get("region", "eu")
        elif local_key and host:
            # Full local runtime — standard path.
            entry_data[CONF_RUNTIME_CHANNEL] = RUNTIME_CHANNEL_LOCAL
        else:
            # No local_key or no host — cloud-only runtime via global OAuth.
            entry_data[CONF_RUNTIME_CHANNEL] = RUNTIME_CHANNEL_CLOUD
            # Store cloud auth if available for per-entry fallback.
            if self._cloud_auth.get("access_id") and self._cloud_auth.get("access_secret"):
                entry_data[CONF_CLOUD_ACCESS_ID] = self._cloud_auth["access_id"]
                entry_data[CONF_CLOUD_ACCESS_SECRET] = self._cloud_auth["access_secret"]
                entry_data[CONF_CLOUD_REGION] = self._cloud_auth.get("region", "eu")

        return self.async_create_entry(
            title=self._flow_data[CONF_NAME],
            data=entry_data,
        )

    def _create_ir_config_entry(self) -> config_entries.ConfigFlowResult:
        """Build and persist an isolated IR config entry."""
        entry_data: dict[str, Any] = {
            CONF_DEVICE_ID: self._flow_data[CONF_DEVICE_ID],
            CONF_HOST: self._flow_data.get(CONF_HOST, ""),
            CONF_PORT: self._flow_data.get(CONF_PORT, DEFAULT_PORT),
            CONF_LOCAL_KEY: self._flow_data.get(CONF_LOCAL_KEY, ""),
            CONF_PROTOCOL_VERSION: "auto",
            CONF_DEVICE_TYPE: DEVICE_TYPE_IR,
            CONF_DP_MAP: "{}",
            CONF_MAPPING_SOURCE: "ir_library",
            CONF_MAPPING_CONFIDENCE: 1.0,
            CONF_RUNTIME_CHANNEL: RUNTIME_CHANNEL_IR,
            CONF_TUYA_CATEGORY: self._tuya_category or "infrared",
            CONF_IR_CATEGORY: str(
                self._ir_category.get("name") or self._ir_category.get("id") or ""
            ),
            CONF_IR_BRAND: str(
                self._ir_brand.get("name") or self._ir_brand.get("id") or ""
            ),
            CONF_IR_MODEL: str(
                self._ir_model.get("name") or self._ir_model.get("id") or ""
            ),
        }

        if self._cloud_auth.get("access_id") and self._cloud_auth.get("access_secret"):
            entry_data[CONF_CLOUD_ACCESS_ID] = self._cloud_auth["access_id"]
            entry_data[CONF_CLOUD_ACCESS_SECRET] = self._cloud_auth["access_secret"]
            entry_data[CONF_CLOUD_REGION] = self._cloud_auth.get("region", "eu")

        return self.async_create_entry(
            title=self._flow_data[CONF_NAME],
            data=entry_data,
        )


# =========================================================================
# Options flow — verbose logging, DP map editing, re-discovery
# =========================================================================


class ContiOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Conti — device settings + external-ON profile."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._pending_options: dict[str, Any] | None = None
        self._ir_learn_action: str = ""
        self._ir_learning_time: str = ""
        self._ir_overwrite_allowed: bool = False

    # -- Init step: device settings + navigate to external-ON ---------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Device settings form with option to configure external-ON."""
        if self._entry.data.get(CONF_RUNTIME_CHANNEL) == RUNTIME_CHANNEL_IR:
            return await self.async_step_ir_options(user_input)

        errors: dict[str, str] = {}

        if user_input is not None:
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

                # If user checked "configure external-ON", go to that step
                if user_input.get("configure_external_on", False):
                    self._pending_options = new_options
                    return await self.async_step_external_on()

                return self.async_create_entry(title="", data=new_options)

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
                    vol.Optional("configure_external_on", default=False): bool,
                }
            ),
            errors=errors,
        )

    # -- External-ON correction profile (real UI fields) --------------------

    async def async_step_external_on(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Configure per-device time-based external-ON correction."""
        from homeassistant.helpers.selector import (  # noqa: PLC0415
            BooleanSelector,
            NumberSelector,
            NumberSelectorConfig,
            NumberSelectorMode,
            TimeSelector,
        )

        if user_input is not None:
            # Start from pending options (from init step) or current options
            new_options = (
                dict(self._pending_options)
                if self._pending_options is not None
                else dict(self._entry.options)
            )
            for key in (
                CONF_EXTERNAL_ON_ENABLED,
                CONF_EXTERNAL_ON_APPLY,
                CONF_MORNING_START,
                CONF_MORNING_END,
                CONF_MORNING_BRIGHTNESS,
                CONF_MORNING_KELVIN,
                CONF_DAY_START,
                CONF_DAY_END,
                CONF_DAY_BRIGHTNESS,
                CONF_DAY_KELVIN,
                CONF_NIGHT_START,
                CONF_NIGHT_END,
                CONF_NIGHT_BRIGHTNESS,
                CONF_NIGHT_KELVIN,
            ):
                if key in user_input:
                    new_options[key] = user_input[key]
            self._pending_options = None
            return self.async_create_entry(title="", data=new_options)

        opts = self._entry.options
        has_ct = self._device_supports_color_temp()

        brightness_sel = NumberSelector(
            NumberSelectorConfig(
                min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER,
                unit_of_measurement="%",
            )
        )
        kelvin_sel = NumberSelector(
            NumberSelectorConfig(
                min=2000, max=6535, step=50, mode=NumberSelectorMode.SLIDER,
                unit_of_measurement="K",
            )
        )
        time_sel = TimeSelector()
        bool_sel = BooleanSelector()

        schema_fields: dict[vol.Optional | vol.Required, Any] = {}

        # -- Toggles --
        schema_fields[vol.Optional(
            CONF_EXTERNAL_ON_ENABLED,
            default=opts.get(CONF_EXTERNAL_ON_ENABLED, False),
        )] = bool_sel
        schema_fields[vol.Optional(
            CONF_EXTERNAL_ON_APPLY,
            default=opts.get(CONF_EXTERNAL_ON_APPLY, True),
        )] = bool_sel

        # -- Morning slot --
        schema_fields[vol.Optional(
            CONF_MORNING_START,
            default=opts.get(CONF_MORNING_START, "06:00:00"),
        )] = time_sel
        schema_fields[vol.Optional(
            CONF_MORNING_END,
            default=opts.get(CONF_MORNING_END, "12:00:00"),
        )] = time_sel
        schema_fields[vol.Optional(
            CONF_MORNING_BRIGHTNESS,
            default=opts.get(CONF_MORNING_BRIGHTNESS, 70),
        )] = brightness_sel
        if has_ct:
            schema_fields[vol.Optional(
                CONF_MORNING_KELVIN,
                default=opts.get(CONF_MORNING_KELVIN, 4000),
            )] = kelvin_sel

        # -- Day slot --
        schema_fields[vol.Optional(
            CONF_DAY_START,
            default=opts.get(CONF_DAY_START, "12:00:00"),
        )] = time_sel
        schema_fields[vol.Optional(
            CONF_DAY_END,
            default=opts.get(CONF_DAY_END, "22:00:00"),
        )] = time_sel
        schema_fields[vol.Optional(
            CONF_DAY_BRIGHTNESS,
            default=opts.get(CONF_DAY_BRIGHTNESS, 100),
        )] = brightness_sel
        if has_ct:
            schema_fields[vol.Optional(
                CONF_DAY_KELVIN,
                default=opts.get(CONF_DAY_KELVIN, 5000),
            )] = kelvin_sel

        # -- Night slot --
        schema_fields[vol.Optional(
            CONF_NIGHT_START,
            default=opts.get(CONF_NIGHT_START, "22:00:00"),
        )] = time_sel
        schema_fields[vol.Optional(
            CONF_NIGHT_END,
            default=opts.get(CONF_NIGHT_END, "06:00:00"),
        )] = time_sel
        schema_fields[vol.Optional(
            CONF_NIGHT_BRIGHTNESS,
            default=opts.get(CONF_NIGHT_BRIGHTNESS, 15),
        )] = brightness_sel
        if has_ct:
            schema_fields[vol.Optional(
                CONF_NIGHT_KELVIN,
                default=opts.get(CONF_NIGHT_KELVIN, 2700),
            )] = kelvin_sel

        return self.async_show_form(
            step_id="external_on",
            data_schema=vol.Schema(schema_fields),
        )

    async def async_step_ir_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """IR options menu."""
        if user_input is not None:
            if user_input.get("ir_action") == "add_missing_command":
                return await self.async_step_ir_learn()
            return self.async_create_entry(title="", data=dict(self._entry.options))

        return self.async_show_form(
            step_id="ir_options",
            data_schema=vol.Schema(
                {
                    vol.Required("ir_action", default="add_missing_command"): vol.In(
                        {
                            "add_missing_command": "Add missing command",
                            "finish": "Finish",
                        }
                    )
                }
            ),
        )

    async def async_step_ir_learn(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Start learning mode for a missing IR command."""
        errors: dict[str, str] = {}

        if user_input is not None:
            action = str(user_input.get("ir_command_action", "")).strip()
            overwrite = bool(user_input.get("overwrite", False))
            if not action:
                errors["base"] = "ir_learning_failed"
            else:
                try:
                    device_id = self._entry.data[CONF_DEVICE_ID]
                    from .ir_actions import normalize_ir_action  # noqa: PLC0415
                    from .ir_storage import IRStorage  # noqa: PLC0415

                    storage = IRStorage(self.hass, device_id)
                    canonical_action = normalize_ir_action(action)
                    if await storage.async_get_command(canonical_action) and not overwrite:
                        errors["base"] = "ir_command_exists"
                    else:
                        session = await self._create_ir_learning_session(device_id)
                        self._ir_learn_action = canonical_action
                        self._ir_overwrite_allowed = overwrite
                        self._ir_learning_time = await session.start_learning(device_id)
                        return await self.async_step_ir_learn_capture()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("IR learning start failed")
                    errors["base"] = "ir_learning_failed"

        warning = ""
        category = str(self._entry.data.get(CONF_IR_CATEGORY, "")).lower()
        if category in {"ac", "air conditioner"}:
            warning = "Learned commands may override full device state (temp/mode/fan)"

        return self.async_show_form(
            step_id="ir_learn",
            data_schema=vol.Schema(
                {
                    vol.Required("ir_command_action", default="swing_vertical_on"): str,
                    vol.Optional("overwrite", default=False): bool,
                }
            ),
            description_placeholders={"ac_warning": warning},
            errors=errors,
        )

    async def async_step_ir_learn_capture(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Capture and store the learned IR code."""
        errors: dict[str, str] = {}
        device_id = self._entry.data[CONF_DEVICE_ID]

        if user_input is not None:
            try:
                session = await self._create_ir_learning_session(device_id)
                payload = await session.capture_learned_payload(
                    device_id,
                    self._ir_learning_time,
                )
                await session.learn_command(
                    device_id,
                    self._ir_learn_action,
                    payload,
                    overwrite=self._ir_overwrite_allowed,
                )
                return self.async_create_entry(
                    title="",
                    data=dict(self._entry.options),
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("IR learning capture failed")
                errors["base"] = "ir_learning_failed"

        return self.async_show_form(
            step_id="ir_learn_capture",
            data_schema=vol.Schema({}),
            description_placeholders={"action": self._ir_learn_action},
            errors=errors,
        )

    async def _create_ir_learning_session(self, device_id: str) -> Any:
        """Create an IR learning session using stored cloud credentials."""
        from .ir_cloud import TuyaIRCloud  # noqa: PLC0415
        from .ir_learning import IRLearningSession  # noqa: PLC0415
        from .ir_storage import IRStorage  # noqa: PLC0415

        storage = IRStorage(self.hass, device_id)

        access_id = str(self._entry.data.get(CONF_CLOUD_ACCESS_ID, "")).strip()
        access_secret = str(self._entry.data.get(CONF_CLOUD_ACCESS_SECRET, "")).strip()
        region = str(self._entry.data.get(CONF_CLOUD_REGION, "eu")).strip() or "eu"
        if access_id and access_secret:
            from .cloud_schema import TuyaCloudSchemaHelper  # noqa: PLC0415

            cloud = TuyaIRCloud(TuyaCloudSchemaHelper(access_id, access_secret, region))
            return IRLearningSession(storage, cloud=cloud)

        from .tuya_oauth import TuyaOAuthManager  # noqa: PLC0415

        oauth = TuyaOAuthManager(self.hass, entry_id=self._entry.entry_id)
        await oauth.async_load()
        if not oauth.is_configured:
            oauth = TuyaOAuthManager(self.hass)
            await oauth.async_load()
        cloud = TuyaIRCloud(oauth.get_schema_helper()) if oauth.is_configured else None
        return IRLearningSession(storage, cloud=cloud)

    # -- Helpers ------------------------------------------------------------

    def _device_supports_color_temp(self) -> bool:
        """Return True if the device dp_map contains a color_temp DP."""
        raw = (
            self._entry.options.get(CONF_DP_MAP)
            or self._entry.data.get(CONF_DP_MAP, "{}")
        )
        try:
            dp_map = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, ValueError):
            return False
        if not isinstance(dp_map, dict):
            return False
        for info in dp_map.values():
            if isinstance(info, dict) and info.get("key") == "color_temp":
                return True
        return False
