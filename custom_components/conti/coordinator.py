"""DataUpdateCoordinator for Conti.

Each config entry (= one device) creates its own coordinator instance.
All coordinators share the singleton :class:DeviceManager for actual I/O.

The coordinator is the **single source of truth** for entity state.
Entities read `coordinator.data[device_id]` and never talk to the
device manager or sockets directly.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

try:
    from homeassistant.exceptions import UpdateFailed
except ImportError:
    from homeassistant.helpers.update_coordinator import UpdateFailed  # type: ignore[no-redef]

from .const import (
    COMMAND_TRACK_WINDOW,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_CONSECUTIVE_FAILURES,
)
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
        low_power_cloud: Any | None = None,
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
        self._low_power_cloud = low_power_cloud

        # Track DPs commanded via HA so we can label source in activity
        self._commanded_dps: dict[str, float] = {}  # dp_id → monotonic ts

        # Register per-device push callback
        if self._low_power_cloud is None:
            self.device_manager.register_state_callback(
                device_id, self._on_device_push
            )

    @property
    def device_id(self) -> str:
        return self._device_id

    # -- Lifecycle -----------------------------------------------------------

    async def async_shutdown(self) -> None:
        """Unregister push callback when coordinator is stopped/unloaded."""
        if self._low_power_cloud is None:
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

        if self._low_power_cloud is not None:
            try:
                dps = await self._low_power_cloud.async_get_dps()
            except Exception as exc:  # noqa: BLE001
                self._consecutive_failures += 1
                _LOGGER.debug(
                    "Low-power cloud polling error for %s: %s",
                    self._device_id,
                    exc,
                )
                return self.data or {}

            if dps:
                self._consecutive_failures = 0
                return {self._device_id: dps}

            # Empty cloud status is normal for sleepy sensors; keep last state.
            return self.data or {}

        try:
            dps = await self.device_manager.query_device(self._device_id)
            if dps is None:
                # Poll was skipped (lock busy / command in-flight).
                # Return the EXISTING coordinator data unchanged so we
                # never overwrite optimistic state set by a command.
                return self.data or {}
            elif dps:
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

    def is_device_available(self) -> bool:
        """Availability helper used by entities."""
        if self._low_power_cloud is not None:
            return True
        return self.device_manager.is_online(self._device_id)

    # -- Push callback -------------------------------------------------------

    def _on_device_push(self, device_id: str, dps: dict[str, Any]) -> None:
        """Called by `DeviceManager` when a device pushes new DPs."""
        if self.data is None:
            self.data = {}
        # Merge — push updates may contain only the changed DPs.
        existing = self.data.get(device_id, {})
        existing.update(dps)
        self.data[device_id] = existing
        self._consecutive_failures = 0
        # Schedule an immediate refresh on listeners (entities)
        self.async_set_updated_data(self.data)

    # -- Activity helpers ----------------------------------------------------

    def is_dp_commanded(self, dp_id: str) -> bool:
        """Return True if *dp_id* was commanded via HA within the track window."""
        ts = self._commanded_dps.get(dp_id)
        return ts is not None and (time.monotonic() - ts) < COMMAND_TRACK_WINDOW

    def mark_dp_commanded(self, dp_id: str) -> None:
        """Record that *dp_id* was just commanded by HA.

        Lightweight alternative to :meth:`apply_optimistic_update` for
        platforms that don't use optimistic state but still need correct
        source labelling in the Activity panel.
        """
        self._commanded_dps[dp_id] = time.monotonic()

    # -- Diagnostics ---------------------------------------------------------

    def apply_optimistic_update(
        self, device_id: str, dp_id: str, value: Any
    ) -> None:
        """Apply an optimistic DP update and notify listeners immediately.

        Called by entity platforms (e.g. switch) after a successful ``set_dp``
        so the UI reflects the new state without waiting for a poll round-trip.
        """
        # Track the command time so is_dp_commanded labels poll echoes correctly
        self._commanded_dps[dp_id] = time.monotonic()

        if self.data is None:
            self.data = {}
        device_data = self.data.setdefault(device_id, {})
        device_data[dp_id] = value
        self._consecutive_failures = 0
        self.async_set_updated_data(self.data)

    def get_diagnostics(self) -> dict[str, Any]:
        """Return diagnostic info for this device."""
        diag = self.device_manager.get_device_diagnostics(self._device_id)
        diag["consecutive_poll_failures"] = self._consecutive_failures
        return diag
