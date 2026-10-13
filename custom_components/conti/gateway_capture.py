"""Bounded, opt-in report capture using HA's existing managed gateway socket."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .gateway import GatewayChild

CAPTURES_KEY = "gateway_report_captures"
REPORT_EVENT = "conti_gateway_report"
MAX_REPORTS = 200
MAX_REPORT_BYTES = 8192


class GatewayReportCapture:
    """Record reports without reads/writes, additional sockets or disk storage."""

    def __init__(
        self, client: GatewayChild, duration: int,
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        self.started_at = datetime.now(UTC).isoformat()
        self._started = time.monotonic()
        self.duration = duration
        self.active = True
        self.reason = "capturing"
        self.reports: list[dict[str, Any]] = []
        self.dropped_reports = 0
        self.ignored_reports = 0
        self._publish = publish
        self._unsubscribe: Callable[[], None] | None = client.subscribe_observations(self._record)
        self._timer: asyncio.TimerHandle | None = asyncio.get_running_loop().call_later(
            duration, self.stop, "completed",
        )

    def _record(self, dps: dict[str, Any], source: str) -> None:
        if not self.active:
            return
        # Subscribe to DPS only, not packets/config/session objects. Restrict
        # keys to DP numbers so packet-level credentials cannot enter captures.
        reported = {str(dp): value for dp, value in dps.items() if str(dp).isdigit()}
        if not reported:
            return
        try:
            serialized = json.dumps(reported, ensure_ascii=False, allow_nan=False)
            if len(serialized.encode("utf-8")) > MAX_REPORT_BYTES:
                self.ignored_reports += 1
                return
            values = json.loads(serialized)
        except (TypeError, ValueError):
            self.ignored_reports += 1
            return
        if len(self.reports) == MAX_REPORTS:
            self.dropped_reports += 1
            self.stop("report_limit")
            return
        report = {
            "elapsed_seconds": round(time.monotonic() - self._started, 3),
            "received_at": datetime.now(UTC).isoformat(),
            "source": source,
            "dps": values,
        }
        self.reports.append(report)
        # Export a copy: observers must not mutate the retained diagnostic data.
        self._publish(json.loads(json.dumps(report)))

    def stop(self, reason: str = "stopped") -> None:
        if not self.active:
            return
        self.active = False
        self.reason = reason
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "status": self.reason,
            "started_at": self.started_at,
            "duration_seconds": self.duration,
            "report_count": len(self.reports),
            "listener_report_count": sum(
                report["source"] == "gateway_listener" for report in self.reports
            ),
            "query_report_count": sum(
                report["source"] == "gateway_query" for report in self.reports
            ),
            "dropped_reports": self.dropped_reports,
            "ignored_reports": self.ignored_reports,
            "source": "managed_gateway_connection",
            "reports": json.loads(json.dumps(self.reports)),
        }


def capture_diagnostics(hass: HomeAssistant, device_id: str) -> dict[str, Any] | None:
    capture = hass.data.get(DOMAIN, {}).get(CAPTURES_KEY, {}).get(device_id)
    return capture.snapshot() if isinstance(capture, GatewayReportCapture) else None


def stop_capture(hass: HomeAssistant, device_id: str, reason: str = "stopped") -> None:
    capture = hass.data.get(DOMAIN, {}).get(CAPTURES_KEY, {}).get(device_id)
    if isinstance(capture, GatewayReportCapture):
        capture.stop(reason)


def register_capture_services(hass: HomeAssistant) -> None:
    """Register lightweight actions; starting never polls/connects/commands."""
    async def start(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        manager = hass.data.get(DOMAIN, {}).get("manager")
        client = manager.get_client(device_id) if manager is not None else None
        if not isinstance(client, GatewayChild):
            raise HomeAssistantError("Select a loaded Conti gateway child for report capture.")
        if not client.connected:
            raise HomeAssistantError("The managed gateway must already be connected.")
        stop_capture(hass, device_id, "restarted")

        def publish(report: dict[str, Any]) -> None:
            hass.bus.async_fire(REPORT_EVENT, {"device_id": device_id, **report})

        capture = GatewayReportCapture(client, call.data.get("duration", 120), publish)
        hass.data[DOMAIN].setdefault(CAPTURES_KEY, {})[device_id] = capture

    async def stop(call: ServiceCall) -> None:
        stop_capture(hass, call.data["device_id"])

    hass.services.async_register(
        DOMAIN, "start_gateway_report_capture", start,
        schema=vol.Schema({
            vol.Required("device_id"): str,
            vol.Optional("duration", default=120): vol.All(
                vol.Coerce(int), vol.Range(min=10, max=300),
            ),
        }),
    )
    hass.services.async_register(
        DOMAIN, "stop_gateway_report_capture", stop,
        schema=vol.Schema({vol.Required("device_id"): str}),
    )
