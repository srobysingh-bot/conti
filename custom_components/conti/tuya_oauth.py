"""Tuya Smart Life OAuth manager with persistent storage and auto-refresh.

Stores cloud account credentials globally in HA's ``.storage`` directory
so that users enter them only once. Subsequent device additions reuse the
stored tokens automatically.

This module is used by the config flow for device discovery and by the
coordinator for cloud-fallback polling when local access is unavailable.

Security
~~~~~~~~
* Credentials are stored in HA's private ``.storage`` directory.
* Tokens are refreshed automatically before expiry.
* HTTPS with TLS verification is used for all API calls.
"""

from __future__ import annotations

import logging
import time
from typing import Any

# Maps Tuya regional endpoint URLs → short region codes.
_ENDPOINT_TO_REGION: dict[str, str] = {
    "https://openapi.tuyaeu.com": "eu",
    "https://openapi.tuyaus.com": "us",
    "https://openapi.tuyacn.com": "cn",
    "https://openapi.tuyain.com": "in",
}

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "conti_oauth"
STORAGE_VERSION = 1

# Re-authenticate margin: refresh when token expires within this window.
_REFRESH_MARGIN = 120  # seconds


def _storage_key(entry_id: str | None) -> str:
    """Return the storage key for a given config entry (or global fallback).

    Each config entry gets its own storage key so that multiple accounts
    (multi-user) do not share or overwrite each other's tokens.

    During onboarding the entry does not yet exist; in that case the
    temporary global key is used until the entry is created, after which
    the coordinator-side manager is initialised with the real entry_id.
    """
    if entry_id:
        return f"{STORAGE_KEY}_{entry_id}"
    return STORAGE_KEY


class TuyaOAuthManager:
    """Global Tuya cloud account manager with persistent token storage.

    Wraps :class:`TuyaCloudSchemaHelper` for API calls and adds:

    * Persistent credential and token storage via HA's ``.storage``.
    * Per-entry isolation: each config entry uses its own storage key so
      multiple Smart Life accounts can coexist without session leakage.
    * Automatic token refresh before expiry.
    * User UID discovery for ``/v1.0/users/{uid}/devices``.
    * Device listing combining user-scoped and project-scoped endpoints.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str | None = None) -> None:
        self._hass = hass
        self._entry_id = entry_id
        key = _storage_key(entry_id)
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, key
        )
        self._access_id: str = ""
        self._access_secret: str = ""
        self._region: str = "eu"
        self._user_code: str = ""
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._token_expiry: float = 0.0
        self._uid: str = ""
        self._terminal_id: str = ""      # from QR poll response
        self._endpoint_url: str = ""     # full URL from QR poll, e.g. https://openapi.tuyaeu.com
        self._loaded: bool = False
        self._helper: Any = None  # Lazy TuyaCloudSchemaHelper
        # Cache of device info dicts from the tuya_sharing SDK (QR mode only).
        self._sharing_device_cache: dict[str, dict[str, Any]] = {}
        # Cache of synthesized schema dicts (functions + status) from the SDK.
        self._sharing_schema_cache: dict[str, dict[str, Any]] = {}

    # ── Properties ────────────────────────────────────────────────────

    @property
    def is_configured(self) -> bool:
        """Return True when a prior login has stored usable credentials."""
        # QR-login flow stores uid + tokens (no access_id/access_secret).
        if self._uid and self._access_token:
            return True
        # Legacy project-credential flow.
        return bool(self._access_id and self._access_secret and self._uid)

    @property
    def uid(self) -> str:
        return self._uid

    @property
    def region(self) -> str:
        return self._region

    @property
    def access_id(self) -> str:
        return self._access_id

    @property
    def access_secret(self) -> str:
        return self._access_secret

    @property
    def user_code(self) -> str:
        return self._user_code

    @property
    def is_qr_mode(self) -> bool:
        """True when this account was configured via QR login (no project credentials).

        In QR mode the account has a user UID and OAuth tokens from the Tuya
        device-sharing gateway, but no Tuya IoT project access_id / access_secret.
        Device listing must use the ``tuya_sharing`` SDK instead of the regular
        Tuya OpenAPI (which requires HMAC signing with project credentials).
        """
        return bool(self._uid and self._access_token and not self._access_id)

    # ── Persistent storage ────────────────────────────────────────────

    async def async_load(self) -> None:
        """Load credentials and tokens from HA storage."""
        if self._loaded:
            return
        data = await self._store.async_load()
        if data and isinstance(data, dict):
            self._access_id = str(data.get("access_id", ""))
            self._access_secret = str(data.get("access_secret", ""))
            self._region = str(data.get("region", "eu"))
            self._user_code = str(data.get("user_code", ""))
            self._access_token = str(data.get("access_token", ""))
            self._refresh_token = str(data.get("refresh_token", ""))
            self._token_expiry = float(data.get("token_expiry", 0.0))
            self._uid = str(data.get("uid", ""))
            self._terminal_id = str(data.get("terminal_id", ""))
            self._endpoint_url = str(data.get("endpoint_url", ""))
        self._loaded = True
    async def async_save(self) -> None:
        """Persist current credentials and tokens."""
        await self._store.async_save(
            {
                "access_id": self._access_id,
                "access_secret": self._access_secret,
                "region": self._region,
                "user_code": self._user_code,
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "terminal_id": self._terminal_id,
                "endpoint_url": self._endpoint_url,
                "token_expiry": self._token_expiry,
                "uid": self._uid,
            }
        )

    # ── Setup / login ─────────────────────────────────────────────────

    async def async_setup(
        self,
        access_id: str,
        access_secret: str,
        region: str,
    ) -> bool:
        """Configure with new credentials, authenticate, and persist.

        Returns True on success (token obtained, uid discovered).
        Raises on auth/permission errors via the helper's strict mode.
        """
        self._access_id = access_id
        self._access_secret = access_secret
        self._region = region
        self._helper = None  # Force re-creation

        helper = self._get_helper()

        # Authenticate — strict mode so errors propagate to the UI.
        await helper._ensure_token(strict=True)

        # Capture tokens from the helper.
        self._access_token = helper.access_token or ""
        self._refresh_token = helper.refresh_token or ""
        self._token_expiry = helper.token_expiry

        # Discover UID.
        uid = await helper.discover_uid()
        if uid:
            self._uid = uid

        await self.async_save()
        return True

    async def async_start_qr_login(
        self,
        user_code: str,
        region: str = "eu",
    ) -> dict[str, Any]:
        """Generate a QR code for Smart Life app authorization.

        Uses the shared Tuya HA client ID — **no project credentials needed**.
        ``user_code`` is the identifier shown as “User Code” in the Tuya
        IoT platform or simply the user’s country dial code.

        Returns a dict with ``url`` (QR content) and ``token`` (poll ticket).
        """
        from .cloud_schema import TuyaCloudSchemaHelper  # noqa: PLC0415

        self._region = region
        self._user_code = user_code
        qr_data = await TuyaCloudSchemaHelper.get_login_qr_code(
            user_code=user_code,
        )
        await self.async_save()
        return qr_data

    async def async_poll_qr_login(self, token: str) -> str | None:
        """Poll QR code scan status.

        Returns the user UID if the QR code has been scanned and
        authorized, or ``None`` if still pending.

        On success the poll response contains a full token set
        (``uid``, ``access_token``, ``refresh_token``, ``expire_time``,
        ``endpoint``).  These are captured into the manager so that
        subsequent device-listing calls work immediately.
        """
        from .cloud_schema import TuyaCloudSchemaHelper  # noqa: PLC0415

        result = await TuyaCloudSchemaHelper.poll_login_qr_code(
            token, user_code=self._user_code,
        )

        if not result or not isinstance(result, dict):
            return None

        uid = result.get("uid")
        if not uid:
            return None

        self._uid = str(uid)

        # Capture tokens returned by the QR-login gateway so that
        # subsequent OpenAPI calls (device listing, schema, etc.) work.
        access_token = result.get("access_token", "")
        refresh_token = result.get("refresh_token", "")
        expire_time = result.get("expire_time", 7200)
        endpoint = result.get("endpoint", "")

        if access_token:
            self._access_token = str(access_token)
            self._refresh_token = str(refresh_token)
            self._token_expiry = time.time() + int(expire_time) - 60

        # Store terminal_id (required by the tuya_sharing Manager).
        terminal_id = result.get("terminal_id", "")
        if terminal_id:
            self._terminal_id = str(terminal_id)

        # Store the endpoint URL separately; map back to a short region code
        # so that helper lookups in _BASE_URLS still work correctly.
        if endpoint:
            self._endpoint_url = str(endpoint)
            mapped_region = _ENDPOINT_TO_REGION.get(
                str(endpoint).rstrip("/"), ""
            )
            if mapped_region:
                self._region = mapped_region
            _LOGGER.debug(
                "QR poll endpoint=%s → region=%s terminal_id=%s",
                endpoint,
                self._region,
                (self._terminal_id[:8] + "…") if self._terminal_id else "<none>",
            )

        # Propagate tokens into the helper so device-listing works.
        self._helper = None  # Re-create with updated creds
        if self._access_id and self._access_secret:
            helper = self._get_helper()
            if access_token:
                helper.restore_tokens(
                    self._access_token,
                    self._refresh_token,
                    self._token_expiry,
                    self._uid,
                )

        await self.async_save()
        _LOGGER.info("Smart Life QR login successful, uid=%s", self._uid)
        return self._uid

    # ── Token lifecycle ───────────────────────────────────────────────

    async def async_ensure_token(self) -> bool:
        """Ensure a valid token exists, refreshing if needed.

        In QR mode (no project credentials) the token was obtained from the
        Tuya device-sharing gateway.  We attempt to refresh it using the
        stored refresh_token before falling back to requiring re-login.
        """
        if self.is_qr_mode:
            # Still valid?
            if self._access_token and time.time() < self._token_expiry - _REFRESH_MARGIN:
                return True
            # Try to refresh via the tuya_sharing gateway.
            if self._refresh_token:
                refreshed = await self._async_refresh_qr_token()
                if refreshed:
                    return True
            _LOGGER.warning(
                "QR-login access token has expired and could not be refreshed "
                "(uid=%s entry_id=%s). User must re-authenticate via Smart Life "
                "QR scan.",
                self._uid,
                self._entry_id or "<global>",
            )
            return False

        helper = self._get_helper()
        ok = await helper._ensure_token(strict=False)
        if ok:
            self._sync_from_helper(helper)
        return ok

    async def _async_refresh_qr_token(self) -> bool:
        """Refresh a QR-login access token using the stored refresh_token.

        Mirrors the tuya_sharing SDK's ``refresh_access_token_if_need`` but
        runs in asyncio via ``aiohttp``.  The endpoint is the tuya_sharing
        gateway (``apigw.iotbing.com``) — NOT the Tuya OpenAPI.
        """
        import json as _json  # noqa: PLC0415

        import aiohttp  # noqa: PLC0415

        from .cloud_schema import TUYA_HA_CLIENT_ID, _QR_LOGIN_BASE  # noqa: PLC0415

        url = f"{_QR_LOGIN_BASE}/v1.0/m/token/{self._refresh_token}"
        headers = {
            "client_id": TUYA_HA_CLIENT_ID,
            "Content-Type": "application/json",
        }

        _LOGGER.debug(
            "Refreshing QR access token for uid=%s entry_id=%s",
            self._uid,
            self._entry_id or "<global>",
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                    ssl=True,
                ) as resp:
                    raw = await resp.text()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("QR token refresh request failed: %s", exc)
            return False

        try:
            data = _json.loads(raw)
        except (ValueError, TypeError):
            _LOGGER.debug("QR token refresh: non-JSON response: %s", raw[:200])
            return False

        if not isinstance(data, dict) or not data.get("success"):
            _LOGGER.debug(
                "QR token refresh failed: code=%s msg=%s",
                data.get("code", "?") if isinstance(data, dict) else "?",
                data.get("msg", "?") if isinstance(data, dict) else raw[:100],
            )
            return False

        result = data.get("result", {})
        if not isinstance(result, dict):
            return False

        new_token = result.get("accessToken") or result.get("access_token", "")
        new_refresh = result.get("refreshToken") or result.get("refresh_token", "")
        expire_time = result.get("expireTime") or result.get("expire_time", 7200)

        if not new_token:
            return False

        self._access_token = str(new_token)
        if new_refresh:
            self._refresh_token = str(new_refresh)
        self._token_expiry = time.time() + int(expire_time) - 60

        await self.async_save()
        _LOGGER.debug(
            "QR token refreshed successfully for uid=%s (expires in %ds)",
            self._uid,
            int(expire_time),
        )
        return True

    # ── Device listing ────────────────────────────────────────────────

    async def async_list_devices_sharing(self) -> list[dict[str, Any]]:
        """List devices using the Tuya Device Sharing SDK (QR-login mode).

        Called when no Tuya IoT project credentials are available.  The
        ``tuya_sharing`` Manager uses AES-GCM encrypted requests signed with
        a key derived from the ``refresh_token`` — no project
        ``access_secret`` is needed.

        Populates ``_sharing_device_cache`` so that
        :meth:`async_get_device_info` can answer without a second SDK call.
        """
        try:
            from tuya_sharing import Manager  # noqa: PLC0415
        except ImportError:
            _LOGGER.error(
                "tuya-device-sharing-sdk is not installed.  Smart Life QR "
                "device listing is unavailable.  Install it with:\n"
                "  pip install tuya-device-sharing-sdk\n"
                "or re-add Conti via the manual / cloud-assisted path."
            )
            return []

        from .cloud_schema import TUYA_HA_CLIENT_ID  # noqa: PLC0415

        endpoint = self._endpoint_url or "https://openapi.tuyaeu.com"
        token_response = {
            "uid": self._uid,
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expire_time": max(0, int(self._token_expiry - time.time())),
            "t": int(time.time() * 1000),
        }

        _LOGGER.debug(
            "QR device listing via tuya_sharing SDK: "
            "client_id=%s endpoint=%s terminal_id=%s uid=%s",
            TUYA_HA_CLIENT_ID,
            endpoint,
            (self._terminal_id[:8] + "…") if self._terminal_id else "<none>",
            self._uid,
        )

        manager = Manager(
            client_id=TUYA_HA_CLIENT_ID,
            user_code=self._user_code,
            terminal_id=self._terminal_id,
            end_point=endpoint,
            token_response=token_response,
        )

        try:
            await self._hass.async_add_executor_job(manager.update_device_cache)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error(
                "Tuya sharing SDK device listing failed: %s", exc
            )
            return []

        devices: list[dict[str, Any]] = []
        for dev in manager.device_map.values():
            info: dict[str, Any] = {
                "id": getattr(dev, "id", ""),
                "name": getattr(dev, "name", ""),
                "local_key": getattr(dev, "local_key", "") or "",
                "ip": getattr(dev, "ip", "") or "",
                "category": getattr(dev, "category", "") or "",
                "product_name": getattr(dev, "product_name", "") or "",
                "uid": self._uid,
            }
            if info["id"]:
                devices.append(info)
                self._sharing_device_cache[info["id"]] = info
                # Build a synthesised schema dict from the SDK's function /
                # status_range attributes so that schema_to_dp_map() can use
                # them for automatic DP detection without any additional API
                # call.
                schema = self._build_sharing_schema(dev)
                if schema:
                    self._sharing_schema_cache[info["id"]] = schema

        _LOGGER.debug(
            "Tuya sharing SDK returned %d device(s) for uid=%s",
            len(devices),
            self._uid,
        )
        return devices

    async def async_list_devices(self) -> list[dict[str, Any]]:
        """List all devices accessible to this account.

        In QR mode (no project credentials) delegates to
        :meth:`async_list_devices_sharing` which uses the tuya_sharing SDK.
        Otherwise uses the Tuya OpenAPI via :class:`TuyaCloudSchemaHelper`.
        """
        if self.is_qr_mode:
            _LOGGER.debug(
                "async_list_devices: QR mode (uid=%s) — using tuya_sharing SDK",
                self._uid,
            )
            return await self.async_list_devices_sharing()

        if not await self.async_ensure_token():
            return []

        helper = self._get_helper()
        devices: list[dict[str, Any]] = []

        # Preferred: user-scoped endpoint.
        if self._uid:
            try:
                devices = await helper.list_user_devices(self._uid)
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "User-scoped device list failed for uid=%s; "
                    "falling back to project-level listing",
                    self._uid,
                )

        # Fallback: project-level listing.
        if not devices:
            try:
                devices = await helper.list_devices(strict=False)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Project-level device list also failed")

        self._sync_from_helper(helper)
        return devices

    async def async_get_device_info(
        self, device_id: str
    ) -> dict[str, Any] | None:
        """Fetch device details including local_key.

        In QR mode, returns data from :attr:`_sharing_device_cache` populated
        by :meth:`async_list_devices_sharing`.  If the cache is empty (e.g.
        user navigated here without going through the device picker) it
        triggers a fresh sharing SDK listing first.
        """
        if self.is_qr_mode:
            cached = self._sharing_device_cache.get(device_id)
            if cached:
                return cached
            # Populate cache then retry.
            await self.async_list_devices_sharing()
            return self._sharing_device_cache.get(device_id)

        if not await self.async_ensure_token():
            return None

        helper = self._get_helper()
        result = await helper.get_device_credentials(device_id, strict=False)
        self._sync_from_helper(helper)
        return result

    async def async_get_device_schema(
        self, device_id: str
    ) -> dict[str, Any] | None:
        """Fetch the DP schema for a device from Tuya Cloud.

        In QR / Smart Life mode the sharing SDK already downloaded the device
        specification during ``update_device_cache()``.  Return the synthesised
        schema dict from that cache so no additional API call is needed.
        """
        if self.is_qr_mode:
            cached = self._sharing_schema_cache.get(device_id)
            if cached:
                return cached
            # Cache miss — try to populate it first, then retry.
            await self.async_list_devices_sharing()
            return self._sharing_schema_cache.get(device_id)

        if not await self.async_ensure_token():
            return None

        helper = self._get_helper()
        result = await helper.get_device_schema(device_id)
        self._sync_from_helper(helper)
        return result

    async def async_get_device_status(
        self, device_id: str
    ) -> list[dict[str, Any]]:
        """Fetch current cloud status for one device."""
        if not await self.async_ensure_token():
            return []

        helper = self._get_helper()
        result = await helper.get_device_status(device_id, strict=False)
        self._sync_from_helper(helper)
        return result

    def get_schema_helper(self) -> Any:
        """Return the underlying TuyaCloudSchemaHelper (for schema_to_dp_map)."""
        return self._get_helper()

    # ── Internal ──────────────────────────────────────────────────────

    @staticmethod
    def _build_sharing_schema(dev: Any) -> dict[str, Any] | None:
        """Synthesise a schema dict from a tuya_sharing CustomerDevice object.

        The sharing SDK populates ``device.function`` and
        ``device.status_range`` (both keyed by DP code) and
        ``device.local_strategy`` which maps dp_id (int) → code string.
        We reassemble those into the same ``{"functions": [...], "status": [...]}``
        shape that :py:meth:`TuyaCloudSchemaHelper.schema_to_dp_map` expects.
        """
        func_attr = getattr(dev, "function", None) or {}
        sr_attr = getattr(dev, "status_range", None) or {}
        if not func_attr and not sr_attr:
            return None

        # Build a reverse map: code → dp_id from local_strategy if available.
        code_to_dp: dict[str, int] = {}
        local_strategy = getattr(dev, "local_strategy", None) or {}
        for dp_id, entry in local_strategy.items():
            try:
                code = entry if isinstance(entry, str) else entry.get("status_code", "")
                if code:
                    code_to_dp[code] = int(dp_id)
            except Exception:  # noqa: BLE001
                pass

        def _serialize_entry(code: str, obj: Any) -> dict[str, Any]:
            dp_id = code_to_dp.get(code, 0)
            type_ = getattr(obj, "type", "") or ""
            values = getattr(obj, "values", "") or ""
            if not isinstance(values, str):
                import json as _json  # noqa: PLC0415
                try:
                    values = _json.dumps(values)
                except Exception:  # noqa: BLE001
                    values = "{}"
            return {"code": code, "dp_id": dp_id, "type": type_, "values": values}

        functions = [_serialize_entry(c, f) for c, f in func_attr.items()]
        status = [_serialize_entry(c, s) for c, s in sr_attr.items()]
        return {"functions": functions, "status": status}

    def _get_helper(self) -> Any:
        """Lazy-create and return the TuyaCloudSchemaHelper."""
        if self._helper is None:
            from .cloud_schema import TuyaCloudSchemaHelper  # noqa: PLC0415

            self._helper = TuyaCloudSchemaHelper(
                self._access_id,
                self._access_secret,
                self._region,
            )
            # Restore persisted tokens so we don't re-authenticate needlessly.
            if self._access_token:
                self._helper.restore_tokens(
                    self._access_token,
                    self._refresh_token,
                    self._token_expiry,
                    self._uid or None,
                )
        return self._helper

    def _sync_from_helper(self, helper: Any) -> None:
        """Sync token state back from the helper after API calls."""
        new_token = helper.access_token or ""
        new_refresh = helper.refresh_token or ""
        new_expiry = helper.token_expiry

        changed = (
            new_token != self._access_token
            or new_refresh != self._refresh_token
            or new_expiry != self._token_expiry
        )

        self._access_token = new_token
        self._refresh_token = new_refresh
        self._token_expiry = new_expiry

        uid = helper.uid
        if uid:
            self._uid = uid

        if changed:
            # Fire-and-forget save; don't block the caller.
            self._hass.async_create_task(self.async_save())
