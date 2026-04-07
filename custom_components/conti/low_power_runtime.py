"""Runtime polling helper for low-power Tuya Wi-Fi sensors.

This path is used only for explicitly flagged low-power sensors.
It does not change runtime behavior for normal local TCP devices.
"""

from __future__ import annotations

import logging
from typing import Any

from .cloud_schema import TuyaCloudSchemaHelper
from .device_profiles import TUYA_CODE_TO_CONTI_KEY

_LOGGER = logging.getLogger(__name__)


class LowPowerSensorCloudRuntime:
    """Map Tuya cloud status codes to Conti DP IDs for sleepy sensors."""

    def __init__(
        self,
        *,
        device_id: str,
        access_id: str,
        access_secret: str,
        region: str,
        dp_map: dict[str, Any],
    ) -> None:
        self._device_id = device_id
        self._dp_map = dp_map if isinstance(dp_map, dict) else {}
        self._helper = TuyaCloudSchemaHelper(access_id, access_secret, region)

        # Prefer exact code->dp_id matches when available.
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
        """Fetch cloud status and translate it into a DP dictionary."""
        status_items = await self._helper.get_device_status(
            self._device_id,
            strict=False,
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
                "Low-power cloud update for %s: mapped %d DPs",
                self._device_id,
                len(mapped),
            )

        return mapped
