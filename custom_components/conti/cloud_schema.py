"""Optional Tuya Cloud schema helper for Conti onboarding.

This module is used **only during config flow** to fetch device schema
(DP definitions) from the Tuya Cloud API.  It is NEVER imported at runtime
by ``__init__.py``, ``device_manager.py``, ``coordinator.py``, or any
entity platform.

The cloud helper translates Tuya DP code names (e.g. ``switch_led``,
``bright_value_v2``) into Conti internal keys (``power``, ``brightness``)
and produces a ready-to-use ``dp_map``.

If cloud credentials are not provided, or the API is unreachable, the
onboarding flow falls back to local-only heuristics — cloud is never
mandatory.

Security
~~~~~~~~
* API credentials are stored in ``hass.data`` only during the config flow
  session and **not** persisted in config entries.
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

# Default timeout for cloud API calls (seconds)
_API_TIMEOUT = 10


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
        self._token_expiry: float = 0.0

    # ── Token management ──────────────────────────────────────────────

    async def _ensure_token(self) -> bool:
        """Obtain or refresh an access token from Tuya Cloud."""
        if self._token and time.time() < self._token_expiry:
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
                    data = await resp.json()

            if data.get("success"):
                result = data["result"]
                self._token = result["access_token"]
                # Expire 60s early to avoid edge cases
                self._token_expiry = time.time() + result.get("expire_time", 7200) - 60
                _LOGGER.debug("Tuya cloud token obtained (expires in %ds)", result.get("expire_time", 0))
                return True

            _LOGGER.warning("Tuya cloud token request failed: %s", data.get("msg", "unknown"))
            return False

        except Exception as exc:
            _LOGGER.warning("Tuya cloud token request error: %s", exc)
            return False

    def _sign_request(
        self,
        method: str,
        path: str,
        body: str,
        token: str | None = None,
    ) -> dict[str, str]:
        """Generate Tuya Cloud API HMAC-SHA256 signature headers."""
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
        if not await self._ensure_token():
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

    async def _api_get(self, path: str) -> dict[str, Any] | None:
        """Make an authenticated GET request to the Tuya Cloud API."""
        if not self._token:
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
                    data = await resp.json()

            if data.get("success"):
                return data.get("result", {})

            _LOGGER.debug(
                "Tuya cloud API %s returned: %s",
                path,
                data.get("msg", "unknown"),
            )
            return None

        except Exception as exc:
            _LOGGER.debug("Tuya cloud API %s error: %s", path, exc)
            return None

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

            entry: dict[str, Any] = {"key": conti_key, "type": conti_type}

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
