"""DataUpdateCoordinator for Conti.

Each config entry (= one device) creates its own coordinator instance.
All coordinators share the singleton :class:DeviceManager for actual I/O.

The coordinator is the **single source of truth** for entity state.
Entities read `coordinator.data[device_id]` and never talk to the
device manager or sockets directly.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

try:
    from homeassistant.exceptions import UpdateFailed
except ImportError:
    from homeassistant.helpers.update_coordinator import UpdateFailed  # type: ignore[no-redef]

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, MAX_CONSECUTIVE_FAILURES
from .device_manager import DeviceManager

_LOGGER = logging.getLogger(__name__)


class ContiCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator that polls a **single** Tuya device.

    `self.data` is a dict keyed by *device_id* whose values are dicts
    of DP string-ids -> values, e.g.::

        {
            "abc123": {"1": True, "3": 500},
        }
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_manager: DeviceManager,
        device_id: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device_id}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.device_manager = device_manager
        self._device_id = device_id
        self._consecutive_failures: int = 0

        # Register per-device push callback
        self.device_manager.register_state_callback(
            device_id, self._on_device_push
        )

    @property
    def device_id(self) -> str:
        return self._device_id

    # -- Lifecycle -----------------------------------------------------------

    async def async_shutdown(self) -> None:
        """Unregister push callback when coordinator is stopped/unloaded."""
        self.device_manager.unregister_state_callback(
            self._device_id, self._on_device_push
        )
        # Parent class may or may not have async_shutdown
        parent_shutdown = getattr(super(), "async_shutdown", None)
        if parent_shutdown is not None:
            await parent_shutdown()

    # -- Polling -------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch data for this coordinator's device.

        Falls back to the cached DPs when a query fails so entities
        don't flip to *unavailable* on a single missed poll.
        After MAX_CONSECUTIVE_FAILURES, raises UpdateFailed so HA
        marks entities as unavailable.
        """
        result: dict[str, dict[str, Any]] = {}

        try:
            dps = await self.device_manager.query_device(self._device_id)
            if dps:
                result[self._device_id] = dps
                self._consecutive_failures = 0
            else:
                # Network query returned nothing - use cache
                self._consecutive_failures += 1
                cached = self.device_manager.get_cached_dps(self._device_id)
                result[self._device_id] = cached

                # Log actionable diagnostics when both live and cached empty
                if not cached and self._consecutive_failures >= 1:
                    diag = self.device_manager.get_device_diagnostics(
                        self._device_id
                    )
                    _LOGGER.warning(
                        "Conti device %s: empty status, no cache "
                        "(failures=%d, online=%s, last_error_class=%s, "
                        "last_error=%s)",
                        self._device_id,
                        self._consecutive_failures,
                        diag.get("online"),
                        diag.get("last_error_class"),
                        str(diag.get("last_error", ""))[:120],
                    )
        except Exception as exc:  # noqa: BLE001
            self._consecutive_failures += 1
            _LOGGER.debug(
                "Error polling device %s: %s", self._device_id, exc
            )
            result[self._device_id] = self.device_manager.get_cached_dps(
                self._device_id
            )

        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            diag = self.device_manager.get_device_diagnostics(self._device_id)
            _LOGGER.warning(
                "Conti device %s: %d consecutive failures - marking "
                "unavailable (last_error_class=%s, last_error=%s)",
                self._device_id,
                self._consecutive_failures,
                diag.get("last_error_class"),
                str(diag.get("last_error", ""))[:120],
            )
            raise UpdateFailed(
                f"Device {self._device_id}: "
                f"{self._consecutive_failures} consecutive failures"
            )

        return result

    # -- Push callback -------------------------------------------------------

    def _on_device_push(self, device_id: str, dps: dict[str, Any]) -> None:
        """Called by `DeviceManager` when a device pushes new DPs."""
        if self.data is None:
            self.data = {}
        self.data[device_id] = dps
        self._consecutive_failures = 0
        # Schedule an immediate refresh on listeners (entities)
        self.async_set_updated_data(self.data)

    # -- Diagnostics ---------------------------------------------------------

    def get_diagnostics(self) -> dict[str, Any]:
        """Return diagnostic info for this device."""
        diag = self.device_manager.get_device_diagnostics(self._device_id)
        diag["consecutive_poll_failures"] = self._consecutive_failures
        return diag
