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

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "conti_oauth"
STORAGE_VERSION = 1

# Re-authenticate margin: refresh when token expires within this window.
_REFRESH_MARGIN = 120  # seconds


class TuyaOAuthManager:
    """Global Tuya cloud account manager with persistent token storage.

    Wraps :class:`TuyaCloudSchemaHelper` for API calls and adds:

    * Persistent credential and token storage via HA's ``.storage``.
    * Automatic token refresh before expiry.
    * User UID discovery for ``/v1.0/users/{uid}/devices``.
    * Device listing combining user-scoped and project-scoped endpoints.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._access_id: str = ""
        self._access_secret: str = ""
        self._region: str = "eu"
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._token_expiry: float = 0.0
        self._uid: str = ""
        self._username: str = ""
        self._loaded: bool = False
        self._helper: Any = None  # Lazy TuyaCloudSchemaHelper

    # ── Properties ────────────────────────────────────────────────────

    @property
    def is_configured(self) -> bool:
        """Return True when cloud credentials have been stored."""
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
    def username(self) -> str:
        return self._username

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
            self._access_token = str(data.get("access_token", ""))
            self._refresh_token = str(data.get("refresh_token", ""))
            self._token_expiry = float(data.get("token_expiry", 0.0))
            self._uid = str(data.get("uid", ""))
            self._username = str(data.get("username", ""))
        self._loaded = True

    async def async_save(self) -> None:
        """Persist current credentials and tokens."""
        await self._store.async_save(
            {
                "access_id": self._access_id,
                "access_secret": self._access_secret,
                "region": self._region,
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "token_expiry": self._token_expiry,
                "uid": self._uid,
                "username": self._username,
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

    async def async_smart_life_login(
        self,
        access_id: str,
        access_secret: str,
        region: str,
        username: str,
        password: str,
        country_code: str,
    ) -> bool:
        """Authenticate a Smart Life user and persist credentials.

        1. Sets up the helper with project-level ``access_id``/``access_secret``.
        2. Calls Tuya's authorized-login with the user's Smart Life
           username + password (MD5-hashed).
        3. Stores the resulting user-scoped token and UID.

        Returns True on success.  Raises on auth errors (strict mode).
        """
        self._access_id = access_id
        self._access_secret = access_secret
        self._region = region
        self._username = username
        self._helper = None  # Force re-creation

        helper = self._get_helper()

        # The helper's smart_life_login will:
        #  1. Obtain a management token (grant_type=1)
        #  2. POST authorized-login with username + MD5(password)
        #  3. Store user-scoped token & UID on the helper
        result = await helper.smart_life_login(
            username=username,
            password=password,
            country_code=country_code,
            schema="smartlife",
            strict=True,
        )

        # Capture tokens from the helper.
        self._access_token = helper.access_token or ""
        self._refresh_token = helper.refresh_token or ""
        self._token_expiry = helper.token_expiry
        if helper.uid:
            self._uid = str(helper.uid)

        await self.async_save()
        _LOGGER.info(
            "Smart Life login successful for user=%s uid=%s",
            username,
            self._uid,
        )
        return True

    # ── Token lifecycle ───────────────────────────────────────────────

    async def async_ensure_token(self) -> bool:
        """Ensure a valid token exists, refreshing if needed."""
        helper = self._get_helper()
        ok = await helper._ensure_token(strict=False)
        if ok:
            self._sync_from_helper(helper)
        return ok

    # ── Device listing ────────────────────────────────────────────────

    async def async_list_devices(self) -> list[dict[str, Any]]:
        """List all devices accessible to this account.

        Prefers ``/v1.0/users/{uid}/devices`` when UID is known,
        then falls back to the associated-users / project endpoints
        already implemented in :class:`TuyaCloudSchemaHelper`.
        """
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
        """Fetch device details including local_key."""
        if not await self.async_ensure_token():
            return None

        helper = self._get_helper()
        result = await helper.get_device_credentials(device_id, strict=False)
        self._sync_from_helper(helper)
        return result

    async def async_get_device_schema(
        self, device_id: str
    ) -> dict[str, Any] | None:
        """Fetch the DP schema for a device from Tuya Cloud."""
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
