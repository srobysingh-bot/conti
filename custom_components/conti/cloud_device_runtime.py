"""Runtime cloud polling for devices without local access.

Used for devices discovered via Smart Life OAuth that do not have a
``local_key`` or are not reachable on the local network. This path
uses the global :class:`TuyaOAuthManager` to poll device status from
the Tuya Cloud API.

For cloud-runtime devices it provides DP polling. For local-runtime devices
with a configured Smart Life account it can also be used as a lightweight
cloud availability monitor.
"""

from __future__ import annotations

import logging
from typing import Any

from .device_profiles import TUYA_CODE_TO_CONTI_KEY

_LOGGER = logging.getLogger(__name__)


class CloudDeviceRuntime:
    """Poll Tuya Cloud status/availability via the OAuth manager."""

    def __init__(
        self,
        *,
        device_id: str,
        oauth_manager: Any,
        dp_map: dict[str, Any],
    ) -> None:
        self._device_id = device_id
        self._oauth = oauth_manager
        self._dp_map = dp_map if isinstance(dp_map, dict) else {}
        self._online: bool | None = None

        # Build code→dp_id and key→dp_id lookup tables.
        self._code_to_dp: dict[str, str] = {}
        self._key_to_dp_ids: dict[str, list[str]] = {}
        self._dp_to_code: dict[str, str] = {}
        for dp_id, spec in self._dp_map.items():
            if not isinstance(spec, dict):
                continue
            code = str(spec.get("code", "")).strip()
            key = str(spec.get("key", "")).strip()
            if code:
                self._code_to_dp[code] = str(dp_id)
                self._dp_to_code[str(dp_id)] = code
            if key:
                self._key_to_dp_ids.setdefault(key, []).append(str(dp_id))

    @property
    def last_online_state(self) -> bool | None:
        """Return the last explicit cloud online state, if known."""
        return self._online

    async def async_get_online_state(self) -> bool | None:
        """Fetch Tuya's online state without treating API errors as offline."""
        try:
            online = await self._oauth.async_get_device_online_state(
                self._device_id
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Cloud online check failed for %s: %s",
                self._device_id,
                exc,
            )
            return None

        if online is not None:
            self._online = bool(online)
        return online

    async def async_get_dps(self) -> dict[str, Any]:
        """Fetch cloud status and translate into a DP dictionary."""
        status_items = await self._oauth.async_get_device_status(
            self._device_id
        )
        if not status_items:
            return {}

        mapped: dict[str, Any] = {}
        for item in status_items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            if not code:
                continue

            dp_id = self._code_to_dp.get(code)
            if dp_id is None:
                key = TUYA_CODE_TO_CONTI_KEY.get(code)
                if key:
                    candidates = self._key_to_dp_ids.get(key, [])
                    if len(candidates) == 1:
                        dp_id = candidates[0]

            if dp_id is None:
                continue

            mapped[dp_id] = item.get("value")

        if mapped:
            _LOGGER.debug(
                "Cloud polling update for %s: mapped %d DPs",
                self._device_id,
                len(mapped),
            )

        return mapped

    def supports_dali_cct_fallback(self) -> bool:
        """Return True only for the exact Tuya DALI/CCT DP code layout."""
        required = {
            "20": "switch_led",
            "21": "work_mode",
            "22": "bright_value",
            "23": "temp_value",
        }
        return all(self._dp_to_code.get(dp_id) == code for dp_id, code in required.items())

    async def async_set_dp(self, dp_id: int, value: Any) -> bool:
        """Send one supported DALI/CCT DP through Tuya Cloud."""
        code = self._dp_to_code.get(str(dp_id))
        if not self.supports_dali_cct_fallback() or code is None:
            return False
        commands = []
        if dp_id in {22, 23}:
            commands.append({"code": "work_mode", "value": "white"})
        commands.append({"code": code, "value": value})
        return await self._oauth.async_send_device_commands(
            self._device_id,
            commands,
        )
