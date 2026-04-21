"""Tuya Cloud helper for onboarding and low-power sensor status polling.

This module is primarily used during config flow to fetch device schema
(DP definitions). It is also used at runtime only for explicitly flagged
low-power sleepy sensors that use cloud-backed status polling.

The cloud helper translates Tuya DP code names (e.g. ``switch_led``,
``bright_value_v2``) into Conti internal keys (``power``, ``brightness``)
and produces a ready-to-use ``dp_map``.

If cloud credentials are not provided, or the API is unreachable, the
onboarding flow falls back to local-only heuristics — cloud is never
mandatory.

Security
~~~~~~~~
* API credentials are normally used only during config flow.
* For low-power sleepy sensors, credentials may be persisted in the
    config entry to allow cloud runtime status polling for that device.
* HTTPS requests use standard ``aiohttp`` with TLS verification.
* No device commands or state changes are made through the cloud.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import aiohttp

from .device_profiles import TUYA_CODE_TO_CONTI_KEY, TUYA_TYPE_MAP

_LOGGER = logging.getLogger(__name__)

# Tuya OpenAPI base URLs by region
_BASE_URLS: dict[str, str] = {
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}

# Tuya QR-login gateway (centralised, not regional)
_QR_LOGIN_BASE = "https://apigw.iotbing.com"

# Shared Tuya client ID used by the official HA integration for QR login.
# This is a public, well-known value — NOT a secret.
TUYA_HA_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"

# Default schema for the QR-login gateway (matches official HA integration).
TUYA_HA_SCHEMA = "haauthorize"

# QR content format scanned by the Smart Life / Tuya Smart app
_QR_CONTENT_FMT = "tuyaSmart--qrLogin?token={token}"

# Default timeout for cloud API calls (seconds)
_API_TIMEOUT = 10


class TuyaCloudOnboardingError(Exception):
    """Base class for cloud onboarding failures."""


class TuyaCloudAuthError(TuyaCloudOnboardingError):
    """Auth/permission failure for Tuya cloud onboarding calls."""


class TuyaCloudPermissionExpiredError(TuyaCloudOnboardingError):
    """Cloud project permission/subscription is expired for onboarding calls."""


class TuyaCloudRegionError(TuyaCloudOnboardingError):
    """Region/data-center mismatch for Tuya cloud onboarding calls."""


class TuyaCloudParseError(TuyaCloudOnboardingError):
    """Unexpected response shape while parsing Tuya cloud payloads."""


class TuyaCloudPaginationError(TuyaCloudOnboardingError):
    """Pagination did not converge safely while listing cloud devices."""


class TuyaCloudPathError(TuyaCloudOnboardingError):
    """Requested Tuya API path is not available for this project/region."""


class TuyaCloudAPIError(TuyaCloudOnboardingError):
    """Network/API failure for Tuya cloud onboarding calls."""


class TuyaCloudSchemaHelper:
    """Fetch device schema from Tuya Cloud API for onboarding assistance.

    Usage::

        helper = TuyaCloudSchemaHelper(access_id, access_secret, region)
        schema = await helper.get_device_schema(device_id)
        dp_map = helper.schema_to_dp_map(schema)
    """

    def __init__(
        self,
        access_id: str,
        access_secret: str,
        region: str = "eu",
    ) -> None:
        self._access_id = access_id
        self._access_secret = access_secret
        self._base_url = _BASE_URLS.get(region.lower(), _BASE_URLS["eu"])
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: float = 0.0
        self._uid: str | None = None

    # ── Token management ──────────────────────────────────────────────

    async def _ensure_token(self, strict: bool = False) -> bool:
        """Obtain or refresh an access token from Tuya Cloud."""
        if self._token and time.time() < self._token_expiry:
            return True

        # Credentials are required for HMAC-signed token acquisition.
        # In QR-login mode (no access_id/secret) this path must not be taken —
        # use the tuya_sharing SDK instead.
        if not self._access_id or not self._access_secret:
            _LOGGER.error(
                "Tuya OpenAPI token fetch requires credentials but "
                "access_id=%r is empty.  HMAC signing will fail with "
                "code=1004.  QR-login accounts must use the "
                "tuya-device-sharing-sdk path.",
                (self._access_id[:4] + "…") if self._access_id else "<empty>",
            )
            if strict:
                raise TuyaCloudAuthError(
                    "Tuya OpenAPI access_id/access_secret not configured."
                )
            return False

        # Try refresh token first if available
        if self._refresh_token:
            refreshed = await self._try_refresh_token(strict=False)
            if refreshed:
                return True

        url = f"{self._base_url}/v1.0/token?grant_type=1"
        headers = self._sign_request("GET", "/v1.0/token?grant_type=1", "")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=_API_TIMEOUT),
                    ssl=True,
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception as exc:  # noqa: BLE001
                        if strict:
                            raise TuyaCloudParseError("Non-JSON token response") from exc
                        _LOGGER.warning("Tuya cloud token parse error: %s", exc)
                        return False

            if not isinstance(data, dict):
                if strict:
                    raise TuyaCloudParseError("Unexpected token response shape")
                _LOGGER.warning("Tuya cloud token request returned non-dict payload")
                return False

            if data.get("success"):
                result = data["result"]
                self._token = result["access_token"]
                self._refresh_token = result.get("refresh_token") or self._refresh_token
                # Expire 60s early to avoid edge cases
                self._token_expiry = time.time() + result.get("expire_time", 7200) - 60
                _LOGGER.debug("Tuya cloud token obtained (expires in %ds)", result.get("expire_time", 0))
                return True

            code = str(data.get("code", "")).strip()
            msg = str(data.get("msg", "unknown"))
            body_preview = str(data)[:1000]
            _LOGGER.error(
                "Tuya token request failed: status=%s code=%s msg=%s body=%s",
                resp.status,
                code or "none",
                msg,
                body_preview,
            )

            if strict:
                self._raise_cloud_error_from_response(
                    status=resp.status,
                    path="/v1.0/token?grant_type=1",
                    code=code,
                    msg=msg,
                )
            return False

        except Exception as exc:
            if strict and isinstance(exc, TuyaCloudOnboardingError):
                raise
            if strict:
                raise TuyaCloudAPIError(f"Token request failed: {exc}") from exc
            _LOGGER.warning("Tuya cloud token request error: %s", exc)
            return False

    async def _try_refresh_token(self, strict: bool = False) -> bool:
        """Attempt to refresh the access token using the stored refresh_token."""
        if not self._refresh_token:
            return False

        path = f"/v1.0/token/{self._refresh_token}"
        url = f"{self._base_url}{path}"
        headers = self._sign_request("GET", path, "")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=_API_TIMEOUT),
                    ssl=True,
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception:  # noqa: BLE001
                        return False

            if isinstance(data, dict) and data.get("success"):
                result = data["result"]
                self._token = result["access_token"]
                self._refresh_token = result.get("refresh_token") or self._refresh_token
                self._token_expiry = time.time() + result.get("expire_time", 7200) - 60
                _LOGGER.debug("Tuya cloud token refreshed (expires in %ds)", result.get("expire_time", 0))
                return True

        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Tuya token refresh failed: %s", exc)

        return False

    async def discover_uid(self) -> str | None:
        """Discover the primary user UID from associated users.

        Returns the first UID found, or None.
        """
        if not await self._ensure_token(strict=False):
            return None

        payload = await self._api_get("/v1.0/iot-01/associated-users", strict=False)
        if isinstance(payload, dict):
            users = payload.get("list") or payload.get("users") or []
            if isinstance(users, list):
                for user in users:
                    if isinstance(user, dict):
                        uid = user.get("uid") or user.get("user_id")
                        if uid:
                            self._uid = str(uid)
                            return self._uid

        # Fallback: extract uid from device list
        devices = await self.list_devices(max_items=1, strict=False)
        for dev in devices:
            uid = dev.get("uid")
            if uid:
                self._uid = str(uid)
                return self._uid

        return None

    async def list_user_devices(self, uid: str) -> list[dict[str, Any]]:
        """List devices for a specific user via /v1.0/users/{uid}/devices."""
        if not await self._ensure_token(strict=False):
            return []

        payload = await self._api_get(f"/v1.0/users/{uid}/devices", strict=False)
        if payload is None:
            return []

        if isinstance(payload, dict):
            normalized = payload
        elif isinstance(payload, list):
            normalized = {"list": payload}
        else:
            normalized = {}
        return self._extract_device_list(normalized)

    @property
    def access_token(self) -> str | None:
        """Return the current access token."""
        return self._token

    @property
    def refresh_token(self) -> str | None:
        """Return the current refresh token."""
        return self._refresh_token

    @property
    def token_expiry(self) -> float:
        """Return the token expiry timestamp."""
        return self._token_expiry

    @property
    def uid(self) -> str | None:
        """Return the discovered user UID."""
        return self._uid

    def restore_tokens(
        self,
        access_token: str,
        refresh_token: str,
        token_expiry: float,
        uid: str | None = None,
    ) -> None:
        """Restore previously saved tokens (from persistent storage)."""
        self._token = access_token
        self._refresh_token = refresh_token
        self._token_expiry = token_expiry
        if uid:
            self._uid = uid

    def _sign_request(
        self,
        method: str,
        path: str,
        body: str,
        token: str | None = None,
    ) -> dict[str, str]:
        """Generate Tuya Cloud API HMAC-SHA256 signature headers."""
        _LOGGER.debug(
            "Signing %s %s — access_id=%s base_url=%s has_token=%s",
            method,
            path,
            (self._access_id[:4] + "…") if self._access_id else "<empty>",
            self._base_url,
            bool(token),
        )
        t = str(int(time.time() * 1000))

        # String to sign: method + SHA256(body) + headers + path
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        # No custom signed headers for simple requests
        headers_str = ""
        string_to_sign = f"{method}\n{content_hash}\n{headers_str}\n{path}"

        # Sign string
        sign_str = self._access_id + (token or "") + t + string_to_sign
        sign = hmac.new(
            self._access_secret.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest().upper()

        result = {
            "client_id": self._access_id,
            "sign": sign,
            "t": t,
            "sign_method": "HMAC-SHA256",
        }
        if token:
            result["access_token"] = token

        return result

    # ── Schema fetching ──────────────────────────────────────────────

    async def get_device_schema(
        self, device_id: str
    ) -> dict[str, Any] | None:
        """Fetch the DP schema for a device from Tuya Cloud.

        Returns a dict with keys like:
        - ``category``: Tuya product category code (e.g. "dj")
        - ``functions``: list of DP function defs
        - ``status``: list of DP status defs

        Returns ``None`` if the request fails or cloud is unavailable.
        """
        if not await self._ensure_token(strict=False):
            return None

        # Fetch device details (includes category)
        device_info = await self._api_get(f"/v1.0/devices/{device_id}")
        if not device_info:
            return None

        category = device_info.get("category", "")
        product_id = device_info.get("product_id", "")

        # Fetch DP specifications
        specs = await self._api_get(f"/v1.0/devices/{device_id}/specifications")

        # Fetch device functions
        functions = await self._api_get(f"/v1.0/devices/{device_id}/functions")

        result: dict[str, Any] = {
            "category": category,
            "product_id": product_id,
            "product_name": device_info.get("product_name", ""),
            "model": device_info.get("model", ""),
            "functions": [],
            "status": [],
        }

        if specs:
            result["functions"] = specs.get("functions", [])
            result["status"] = specs.get("status", [])
        elif functions:
            result["functions"] = functions.get("functions", [])

        return result

    async def list_devices(
        self,
        max_items: int | None = None,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        """Return cloud devices visible to this project/account.

        Tuya OpenAPI deployments expose slightly different list endpoints
        depending on project type and region. We try a short list of known
        endpoints and normalize the result into:

        ``[{"id": ..., "name": ..., "ip": ..., "category": ...}, ...]``

        Pagination is handled for paged endpoints so linked devices from
        additional pages are not missed.
        """
        if not await self._ensure_token(strict=strict):
            if strict:
                raise TuyaCloudAuthError("Unable to obtain Tuya cloud token")
            return []

        limit = max_items if isinstance(max_items, int) and max_items > 0 else 1000
        page_size = 100

        # Merge results across endpoint variants instead of returning from the
        # first non-empty endpoint; different Tuya deployments expose different
        # subsets.
        merged_by_id: dict[str, dict[str, Any]] = {}
        last_endpoint_error: TuyaCloudOnboardingError | None = None

        # Primary flow for app-account linked devices.
        # This is the onboarding-relevant list source and avoids the iot-03
        # endpoint shape that can require explicit device_ids.
        try:
            linked_items = await self._list_associated_user_devices(
                limit=limit,
                strict=strict,
            )
            for item in linked_items:
                dev_id = item.get("id")
                if dev_id and dev_id not in merged_by_id:
                    merged_by_id[dev_id] = item
                if len(merged_by_id) >= limit:
                    break
        except (TuyaCloudPathError, TuyaCloudAPIError) as exc:
            _LOGGER.warning("Tuya linked-device endpoint rejected: %s", exc)
            last_endpoint_error = exc

        # Fallback for project-level device lists in some deployments.
        # Only run this when associated-users produced no devices.
        if not merged_by_id:
            paged_paths = ["/v1.0/devices"]
            for base_path in paged_paths:
                page_no = 1
                while len(merged_by_id) < limit:
                    if page_no > 50:
                        raise TuyaCloudPaginationError(
                            f"Exceeded pagination safety limit for {base_path}"
                        )
                    path = f"{base_path}?page_no={page_no}&page_size={page_size}"
                    try:
                        payload = await self._api_get(path, strict=strict)
                    except (TuyaCloudPathError, TuyaCloudAPIError) as exc:
                        # Some Tuya projects reject specific endpoint shapes
                        # (e.g. code 1109 requiring device_ids). Treat this as an
                        # endpoint mismatch and continue with alternate paths.
                        _LOGGER.warning(
                            "Tuya list endpoint rejected: %s (%s)",
                            path,
                            exc,
                        )
                        last_endpoint_error = exc
                        break
                    page_items = self._extract_device_list(payload)

                    if page_items:
                        for item in page_items:
                            dev_id = item.get("id")
                            if dev_id and dev_id not in merged_by_id:
                                merged_by_id[dev_id] = item

                    if not self._has_next_page(payload, page_no, page_size, len(page_items)):
                        break

                    page_no += 1

        if not merged_by_id and strict and last_endpoint_error is not None:
            raise last_endpoint_error

        return list(merged_by_id.values())[:limit]

    async def _list_associated_user_devices(
        self,
        limit: int,
        strict: bool,
    ) -> list[dict[str, Any]]:
        """List devices via associated-users endpoint using cursor pagination."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        size = 100

        for _ in range(50):
            path = f"/v1.0/iot-01/associated-users/devices?size={size}"
            if cursor:
                path = f"{path}&last_row_key={cursor}"

            payload = await self._api_get(path, strict=strict)
            page_items = self._extract_device_list(payload)
            if page_items:
                items.extend(page_items)

            if len(items) >= limit:
                return items[:limit]

            if not isinstance(payload, dict):
                break

            has_more = payload.get("has_more")
            next_cursor = payload.get("last_row_key")

            if not (isinstance(has_more, bool) and has_more):
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                break

            cursor = next_cursor

        return items[:limit]

    async def get_device_credentials(
        self,
        device_id: str,
        strict: bool = False,
    ) -> dict[str, Any] | None:
        """Fetch onboarding credentials and hints for one device.

        Returns a dict with at least ``device_id`` and optionally ``local_key``,
        ``name``, ``ip``, ``category``, ``product_name``.
        """
        if not await self._ensure_token(strict=strict):
            if strict:
                raise TuyaCloudAuthError("Unable to obtain Tuya cloud token")
            return None

        device_info = await self._api_get(
            f"/v1.0/devices/{device_id}", strict=strict
        )
        if not device_info:
            return None
        if not isinstance(device_info, dict):
            if strict:
                raise TuyaCloudParseError(
                    f"Unexpected credentials payload type for device {device_id}"
                )
            return None

        result: dict[str, Any] = {
            "device_id": str(device_info.get("id") or device_id),
            "name": device_info.get("name") or device_info.get("product_name") or "",
            "category": device_info.get("category") or "",
            "product_name": device_info.get("product_name") or "",
            "ip": (
                device_info.get("ip")
                or device_info.get("local_ip")
                or device_info.get("lan_ip")
                or ""
            ),
        }

        local_key = (
            device_info.get("local_key")
            or device_info.get("key")
            or device_info.get("uuid_key")
            or ""
        )
        if local_key:
            result["local_key"] = str(local_key)

        return result

    async def get_device_status(
        self,
        device_id: str,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch current cloud status list for one device.

        Returns a normalized list of ``{"code": ..., "value": ...}`` items.
        """
        if not await self._ensure_token(strict=strict):
            if strict:
                raise TuyaCloudAuthError("Unable to obtain Tuya cloud token")
            return []

        payload = await self._api_get(
            f"/v1.0/devices/{device_id}/status",
            strict=strict,
        )
        if payload is None:
            return []

        if isinstance(payload, list):
            raw_items = payload
        elif isinstance(payload, dict):
            nested = payload.get("status")
            raw_items = nested if isinstance(nested, list) else []
        else:
            if strict:
                raise TuyaCloudParseError(
                    f"Unexpected status payload type for device {device_id}"
                )
            return []

        result: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            result.append({"code": code, "value": item.get("value")})
        return result

    async def _api_get(
        self,
        path: str,
        strict: bool = False,
    ) -> dict[str, Any] | None:
        """Make an authenticated GET request to the Tuya Cloud API."""
        if not self._access_id or not self._access_secret:
            _LOGGER.error(
                "Tuya OpenAPI GET %s skipped: access_id/access_secret are "
                "empty.  Requests would fail with code=1004 (sign invalid).  "
                "Use the tuya-device-sharing-sdk path for QR-login accounts.",
                path,
            )
            if strict:
                raise TuyaCloudAuthError(
                    "Tuya OpenAPI credentials not configured (QR-login mode)."
                )
            return None
        if not self._token:
            if strict:
                raise TuyaCloudAuthError("Missing Tuya access token")
            return None

        url = f"{self._base_url}{path}"
        headers = self._sign_request("GET", path, "", self._token)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=_API_TIMEOUT),
                    ssl=True,
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception as exc:  # noqa: BLE001
                        if strict:
                            raise TuyaCloudParseError(
                                f"Non-JSON response for {path}"
                            ) from exc
                        _LOGGER.error(
                            "Tuya cloud API parse error: path=%s status=%s error=%s",
                            path,
                            resp.status,
                            exc,
                        )
                        return None

                    if resp.status in (401, 403) and strict:
                        raise TuyaCloudAuthError(f"HTTP {resp.status} for {path}")

                    if resp.status == 404 and strict:
                        raise TuyaCloudPathError(f"HTTP 404 for {path}")

            if not isinstance(data, dict):
                if strict:
                    raise TuyaCloudParseError(
                        f"Unexpected top-level response type for {path}"
                    )
                return None

            if data.get("success"):
                return data.get("result", {})

            code = str(data.get("code", "")).strip()
            msg = data.get("msg", "unknown")
            body_preview = str(data)[:1000]

            _LOGGER.error(
                "Tuya cloud API failed: path=%s status=%s code=%s msg=%s body=%s",
                path,
                resp.status,
                code or "none",
                msg,
                body_preview,
            )

            if strict:
                self._raise_cloud_error_from_response(
                    status=resp.status,
                    path=path,
                    code=code,
                    msg=str(msg),
                )

            _LOGGER.debug(
                "Tuya cloud API %s returned: %s",
                path,
                msg,
            )
            return None

        except Exception as exc:
            if strict and isinstance(exc, TuyaCloudOnboardingError):
                raise
            if strict:
                raise TuyaCloudAPIError(f"Cloud API call failed for {path}: {exc}") from exc
            _LOGGER.debug("Tuya cloud API %s error: %s", path, exc)
            return None

    async def _api_post(
        self,
        path: str,
        body: dict[str, Any],
        strict: bool = False,
    ) -> dict[str, Any] | None:
        """Make an authenticated POST request to the Tuya Cloud API."""
        import json as _json  # noqa: PLC0415

        if not self._access_id or not self._access_secret:
            _LOGGER.error(
                "Tuya OpenAPI POST %s skipped: access_id/access_secret are "
                "empty.  Requests would fail with code=1004 (sign invalid).",
                path,
            )
            if strict:
                raise TuyaCloudAuthError(
                    "Tuya OpenAPI credentials not configured (QR-login mode)."
                )
            return None

        if not self._token:
            if strict:
                raise TuyaCloudAuthError("Missing Tuya access token")
            return None

        body_str = _json.dumps(body)
        url = f"{self._base_url}{path}"
        headers = self._sign_request("POST", path, body_str, self._token)
        headers["Content-Type"] = "application/json"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    headers=headers,
                    data=body_str,
                    timeout=aiohttp.ClientTimeout(total=_API_TIMEOUT),
                    ssl=True,
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception as exc:  # noqa: BLE001
                        if strict:
                            raise TuyaCloudParseError(
                                f"Non-JSON response for POST {path}"
                            ) from exc
                        _LOGGER.error(
                            "Tuya cloud POST parse error: path=%s status=%s",
                            path, resp.status,
                        )
                        return None

            if not isinstance(data, dict):
                if strict:
                    raise TuyaCloudParseError(
                        f"Unexpected response type for POST {path}"
                    )
                return None

            if data.get("success"):
                return data.get("result", {})

            code = str(data.get("code", "")).strip()
            msg = str(data.get("msg", "unknown"))
            _LOGGER.error(
                "Tuya cloud POST failed: path=%s code=%s msg=%s",
                path, code, msg,
            )
            if strict:
                self._raise_cloud_error_from_response(
                    status=resp.status, path=path, code=code, msg=msg,
                )
            return None

        except Exception as exc:
            if strict and isinstance(exc, TuyaCloudOnboardingError):
                raise
            if strict:
                raise TuyaCloudAPIError(
                    f"Cloud POST failed for {path}: {exc}"
                ) from exc
            _LOGGER.debug("Tuya cloud POST %s error: %s", path, exc)
            return None

    @staticmethod
    async def get_login_qr_code(
        user_code: str,
        client_id: str = TUYA_HA_CLIENT_ID,
        schema: str = TUYA_HA_SCHEMA,
    ) -> dict[str, Any]:
        """Request a QR code for Smart Life app authorization.

        Uses the centralised Tuya QR-login gateway (``apigw.iotbing.com``)
        which does **not** require HMAC signing — only query-string
        parameters.  The default ``client_id`` is the shared HA Tuya
        client so that **no project credentials are needed**.

        ``user_code`` is the account identifier shown under "User Code"
        on the Tuya IoT platform or simply the user's country-code-based
        identifier.  It is **required** by the gateway.

        Returns a dict with:
        * ``url`` — the content to encode as a QR code image
          (format: ``tuyaSmart--qrLogin?token=…``).
        * ``token`` — the ticket for polling scan status.

        Raises :class:`TuyaCloudAPIError` on failure.
        """
        import json as _json  # noqa: PLC0415

        url = (
            f"{_QR_LOGIN_BASE}/v1.0/m/life/home-assistant/qrcode/tokens"
            f"?clientid={client_id}"
            f"&usercode={user_code}"
            f"&schema={schema}"
        )
        _LOGGER.debug("QR login request: POST %s", url)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    timeout=aiohttp.ClientTimeout(total=_API_TIMEOUT),
                    ssl=True,
                ) as resp:
                    raw_body = await resp.text()
                    content_type = resp.headers.get("Content-Type", "")
                    _LOGGER.debug(
                        "QR login response: status=%s ct=%s body=%s",
                        resp.status, content_type, raw_body[:500],
                    )
        except Exception as exc:
            raise TuyaCloudAPIError(
                f"QR code request failed: {exc}"
            ) from exc

        # Parse JSON safely — the gateway sometimes returns text/plain.
        try:
            data = _json.loads(raw_body)
        except (ValueError, TypeError) as exc:
            raise TuyaCloudAPIError(
                "Tuya QR login endpoint returned an unexpected response "
                f"(content-type={content_type}). "
                "Check region, project type, and QR login parameters. "
                f"Body preview: {raw_body[:200]}"
            ) from exc

        if not isinstance(data, dict) or not data.get("success"):
            code = data.get("code", "") if isinstance(data, dict) else ""
            msg = data.get("msg", "unknown") if isinstance(data, dict) else str(data)
            raise TuyaCloudAPIError(
                f"QR code generation failed: code={code} msg={msg}"
            )

        result = data.get("result", {})
        qr_token = result.get("qrcode", "")
        if not qr_token:
            raise TuyaCloudAPIError(
                f"QR code response missing qrcode field: {result}"
            )

        return {
            "url": _QR_CONTENT_FMT.format(token=qr_token),
            "token": qr_token,
        }

    @staticmethod
    async def poll_login_qr_code(
        token: str,
        user_code: str,
        client_id: str = TUYA_HA_CLIENT_ID,
    ) -> dict[str, Any] | None:
        """Poll the QR code scan status.

        Uses the centralised Tuya QR-login gateway (no HMAC signing).

        Returns the result dict when the user has scanned and authorised,
        or ``None`` if still pending.  On success the dict contains at
        least ``uid``, ``access_token``, ``refresh_token``,
        ``expire_time``, ``endpoint``, and ``terminal_id``.
        """
        import json as _json  # noqa: PLC0415

        url = (
            f"{_QR_LOGIN_BASE}/v1.0/m/life/home-assistant/qrcode/tokens/{token}"
            f"?clientid={client_id}"
            f"&usercode={user_code}"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=_API_TIMEOUT),
                    ssl=True,
                ) as resp:
                    raw_body = await resp.text()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("QR poll request failed for token=%s", token)
            return None

        try:
            data = _json.loads(raw_body)
        except (ValueError, TypeError):
            _LOGGER.debug(
                "QR poll returned non-JSON for token=%s: %s",
                token, raw_body[:200],
            )
            return None

        if not isinstance(data, dict) or not data.get("success"):
            return None

        return data.get("result")

    @staticmethod
    def _raise_cloud_error_from_response(
        *,
        status: int,
        path: str,
        code: str,
        msg: str,
    ) -> None:
        """Map Tuya HTTP/code/message into specific onboarding exceptions."""
        msg_l = msg.lower()

        if code == "28841002":
            _LOGGER.error(
                "Tuya cloud permission expired: path=%s status=%s code=%s msg=%s",
                path,
                status,
                code,
                msg,
            )
            raise TuyaCloudPermissionExpiredError(
                f"status={status} path={path} code={code} msg={msg}"
            )

        if status in (401, 403) or code in {"1004", "1010", "1011", "1106", "2406"}:
            raise TuyaCloudAuthError(f"status={status} path={path} code={code} msg={msg}")

        if status == 404:
            raise TuyaCloudPathError(f"status=404 path={path} code={code} msg={msg}")

        if code in {"1109"} or "device_ids param is illegal" in msg_l:
            raise TuyaCloudPathError(f"status={status} path={path} code={code} msg={msg}")

        if (
            "region" in msg_l
            or "data center" in msg_l
            or "datacenter" in msg_l
            or "endpoint" in msg_l
        ):
            raise TuyaCloudRegionError(f"status={status} path={path} code={code} msg={msg}")

        if "path" in msg_l and ("invalid" in msg_l or "not found" in msg_l):
            raise TuyaCloudPathError(f"status={status} path={path} code={code} msg={msg}")

        raise TuyaCloudAPIError(f"status={status} path={path} code={code} msg={msg}")

    @staticmethod
    def _extract_device_list(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Extract/normalize device list from different Tuya response shapes."""
        if not isinstance(payload, dict):
            return []

        if isinstance(payload.get("list"), list):
            raw_items = payload["list"]
        elif isinstance(payload.get("devices"), list):
            raw_items = payload["devices"]
        elif isinstance(payload.get("result"), list):
            raw_items = payload["result"]
        elif isinstance(payload.get("items"), list):
            raw_items = payload["items"]
        elif isinstance(payload.get("uid"), str):
            # Some endpoints return a user shell object, not devices.
            return []
        else:
            return []

        devices: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            dev_id = item.get("id") or item.get("device_id")
            if not dev_id:
                continue
            devices.append(
                {
                    "id": str(dev_id),
                    "name": item.get("name") or item.get("product_name") or "",
                    "ip": item.get("ip") or item.get("local_ip") or item.get("lan_ip") or "",
                    "category": item.get("category") or "",
                    "product_name": item.get("product_name") or "",
                }
            )
        return devices

    @staticmethod
    def _has_next_page(
        payload: dict[str, Any] | None,
        page_no: int,
        page_size: int,
        page_count: int,
    ) -> bool:
        """Infer whether a paged list endpoint likely has another page."""
        if not isinstance(payload, dict):
            return False

        has_more = payload.get("has_more")
        if isinstance(has_more, bool):
            return has_more

        total = payload.get("total")
        if isinstance(total, int) and total >= 0:
            return page_no * page_size < total

        # Fallback heuristic: full pages usually indicate there may be more.
        return page_count >= page_size and page_count > 0

    # ── Schema → dp_map conversion ───────────────────────────────────

    @staticmethod
    def schema_to_dp_map(
        schema: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], str | None, str | None]:
        """Convert a Tuya cloud schema into a Conti dp_map.

        Returns ``(dp_map, category, device_type_hint)``.

        The ``category`` is the Tuya product category code.
        The ``device_type_hint`` is the suggested Conti device type
        based on the category (e.g. "dj" → "light").
        """
        category = schema.get("category", "")
        device_type_hint = _category_to_device_type(category)

        dp_map: dict[str, dict[str, Any]] = {}

        # Process both functions and status DPs
        all_dps: list[dict[str, Any]] = []
        all_dps.extend(schema.get("functions", []))
        all_dps.extend(schema.get("status", []))

        seen_dp_ids: set[str] = set()

        for dp_def in all_dps:
            code = dp_def.get("code", "")
            dp_id = str(dp_def.get("dp_id", ""))

            if not dp_id or dp_id in seen_dp_ids:
                continue
            seen_dp_ids.add(dp_id)

            # Translate Tuya code to Conti key
            conti_key = TUYA_CODE_TO_CONTI_KEY.get(code)
            if not conti_key:
                _LOGGER.debug(
                    "Cloud schema: unmapped Tuya code '%s' (dp_id=%s)",
                    code, dp_id,
                )
                continue

            # Determine type
            tuya_type = dp_def.get("type", "")
            conti_type = TUYA_TYPE_MAP.get(tuya_type, "str")

            entry: dict[str, Any] = {
                "key": conti_key,
                "type": conti_type,
                "code": code,
            }

            # Extract range constraints from values JSON
            values = dp_def.get("values", "")
            if isinstance(values, str):
                try:
                    import json
                    values = json.loads(values)
                except (ValueError, TypeError):
                    values = {}

            if isinstance(values, dict):
                if "min" in values and "max" in values:
                    entry["min"] = values["min"]
                    entry["max"] = values["max"]
                if "scale" in values:
                    entry["scale"] = values["scale"]
                if "range" in values and isinstance(values["range"], list):
                    entry["values"] = values["range"]

            dp_map[dp_id] = entry

        _LOGGER.info(
            "Cloud schema → dp_map: %d DPs mapped (category=%s, "
            "device_type_hint=%s)",
            len(dp_map),
            category,
            device_type_hint,
        )

        return dp_map, category, device_type_hint


def _category_to_device_type(category: str) -> str | None:
    """Map a Tuya product category to a Conti device type."""
    cat = category.lower()
    _CAT_MAP: dict[str, str] = {
        "dj": "light",      # Light
        "dd": "light",      # Strip light
        "tgq": "light",     # Dimmer
        "fwd": "light",     # Ambient light
        "xdd": "light",     # Ceiling light
        "dc": "light",      # String light
        "kg": "switch",     # Switch
        "cz": "switch",     # Socket / plug
        "pc": "switch",     # Power strip
        "tdq": "switch",    # Circuit breaker
        "fs": "fan",        # Fan
        "fsd": "fan",       # Ceiling fan light
        "kt": "climate",    # AC
        "wk": "climate",    # Thermostat
        "wsdcg": "sensor",  # Temp+humidity sensor
        "pir": "sensor",    # PIR sensor
        "mcs": "sensor",    # Contact sensor
        "ywbj": "sensor",   # Smoke sensor
        "rqbj": "sensor",   # Gas sensor
        "sj": "sensor",     # Water leak sensor
    }
    return _CAT_MAP.get(cat)
