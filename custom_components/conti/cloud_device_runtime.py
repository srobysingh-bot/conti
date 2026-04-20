"""Runtime cloud polling for devices without local access.

Used for devices discovered via Smart Life OAuth that do not have a
``local_key`` or are not reachable on the local network. This path
uses the global :class:`TuyaOAuthManager` to poll device status from
the Tuya Cloud API.

This does NOT affect local-runtime devices. Only devices whose config
entry has ``runtime_channel == "cloud"`` will use this module.
"""

from __future__ import annotations

import logging
from typing import Any

from .device_profiles import TUYA_CODE_TO_CONTI_KEY

_LOGGER = logging.getLogger(__name__)


class CloudDeviceRuntime:
    """Poll Tuya Cloud status for a device via the global OAuth manager."""

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

        # Build code→dp_id and key→dp_id lookup tables.
        self._code_to_dp: dict[str, str] = {}
        self._key_to_dp_ids: dict[str, list[str]] = {}
        for dp_id, spec in self._dp_map.items():
            if not isinstance(spec, dict):
                continue
            code = str(spec.get("code", "")).strip()
            key = str(spec.get("key", "")).strip()
            if code:
                self._code_to_dp[code] = str(dp_id)
            if key:
                self._key_to_dp_ids.setdefault(key, []).append(str(dp_id))

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
