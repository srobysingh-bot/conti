"""TinyTuya parent/child transport, with one serialized socket per gateway.

Device IDs identify HA devices; CID is the separate Zigbee node identifier.
No gateway DPS or unidentified packets are accepted as child state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any

import tinytuya

from .const import AUTO_DETECT_ORDER, CONF_CHILD_CID, CONF_GATEWAY_DEVICE_ID
from .tinytuya_client import TinyTuyaDevice

_LOGGER = logging.getLogger(__name__)

METADATA_FIELDS = (
    "name",
    "manufacturer",
    "gateway_id",
    "parent_id",
    "parent_device_id",
    "mesh_id",
    "node_id",
    "cid",
    "sub",
    "is_gateway",
    "product_id",
    "product_name",
    "model",
    "category",
    "online",
)


def device_metadata(value: Any) -> dict[str, Any]:
    """Preserve relationship/product hints actually provided by cloud or SDK."""
    result = {}
    for key in METADATA_FIELDS:
        item = value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
        if isinstance(item, (str, bool, int, float)):
            result[key] = item
    return result


def parent_id(info: Mapping[str, Any]) -> str:
    """Do not infer CID from a cloud device ID or from a mesh/group ID."""
    return str(
        info.get("gateway_id") or info.get("parent_id") or info.get("parent_device_id") or ""
    )


def child_cid(info: Mapping[str, Any]) -> str:
    """TinyTuya explicitly supports node_id as an alias for CID."""
    return str(info.get("cid") or info.get("node_id") or "")


def map_child_dps(
    device_type: str,
    dps: dict[str, Any],
    cloud_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reuse Conti mapping rules, restricted to locally verified child DPs."""
    from .dp_mapping import auto_map_dps, build_raw_dp_map

    # Preserve unmapped reports as read-only values. A gateway child can be
    # a scene remote whose string DPs have no switch/light heuristic at all.
    mapping = build_raw_dp_map(dps)
    for spec in mapping.values():
        spec["read_only"] = True
    mapping.update(auto_map_dps(device_type, dps))
    if device_type == "switch":
        for dp, value in dps.items():
            if isinstance(value, bool) and mapping[dp].get("read_only"):
                mapping[dp] = {"key": f"switch_{dp}", "type": "bool"}
    mapping.update({k: v for k, v in (cloud_map or {}).items() if k in dps})
    verified = {k: v for k, v in mapping.items() if k in dps}
    return verified


def gateway_error(result: Any = None, exc: Exception | None = None) -> str:
    """Classify without storing/logging raw responses or exception secrets."""
    if isinstance(result, dict):
        code = str(result.get("Err", ""))
        if code == "914":
            # TinyTuya uses 914 for key OR version. Never call it proven bad auth.
            return "gateway_key_or_version"
        if code == "902":
            return "gateway_response_timeout"
        if code in {"901", "905"}:
            return "gateway_unreachable"
        if code == "904":
            return "child_protocol_error"
        if code == "907":
            return "child_local_control_unsupported"
    reason = TinyTuyaDevice._classify_status_failure(result=result, exc=exc)
    if reason in {"invalid_key", "decrypt_error"}:
        return "gateway_authentication_failed"
    if reason == "protocol_mismatch":
        return "gateway_protocol_unsupported"
    if reason in {"connection_refused", "timeout"} or isinstance(exc, OSError):
        return "gateway_unreachable"
    return "child_status_query_failed"


class GatewaySession:
    """Synchronous TinyTuya objects accessed only inside the shared async lock."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.device_id = str(config[CONF_GATEWAY_DEVICE_ID])
        self.host = str(config["host"])
        self.key = str(config["local_key"]).strip()
        self.version = str(config.get("protocol_version", "auto"))
        self.port = int(config.get("port", 6668))
        self.lock = asyncio.Lock()
        self.device: Any = None
        self.children: dict[str, GatewayChild] = {}
        self.connected = False
        self.detected_version: str | None = None
        self.error = ""
        self.failure_code = "none"
        self.failure_stage = "gateway_session"
        self.listener_task: asyncio.Task[None] | None = None
        self.listener_stop = asyncio.Event()
        self.reconnect_task: asyncio.Task[Any] | None = None
        self.on_disconnect: Callable[[], None] | None = None
        self.on_child_offline: Callable[[str], None] | None = None
        self._online_cids: set[str] | None = None
        self._offline_cids: set[str] = set()
        self._offline_notifications: set[str] = set()
        self._heartbeat_at = 0.0
        self._presence_at = 0.0

    def attach(self, child: GatewayChild) -> None:
        if child._device_id in self.children:
            raise ValueError("child_identifier_duplicate")
        if any(c.cid == child.cid and c is not child for c in self.children.values()):
            raise ValueError("child_identifier_duplicate")
        if self.device is not None:
            try:
                self._build_child(child)
            except Exception:
                self.device.children.pop(child._device_id, None)
                raise
        self.children[child._device_id] = child

    def _build_child(self, child: GatewayChild) -> None:
        child._device = tinytuya.Device(child._device_id, parent=self.device, cid=child.cid)
        if child._monitored_dp_ids:
            child._device.set_dpsUsed(child._monitored_dp_ids)
        child._protocol_version = str(self.device.version)
        child._detected_version = self.detected_version if self.version == "auto" else None

    def route(self, packet: Any) -> dict[str, dict[str, Any]]:
        """Accept only an explicit, unique CID. TinyTuya can return queued tuples."""
        if isinstance(packet, tuple) and len(packet) == 2:
            packet = packet[1]
        if not isinstance(packet, dict) or "Error" in packet:
            return {}
        data = packet.get("data")
        data = data if isinstance(data, dict) else {}
        if packet.get("reqType") == "subdev_online_stat_report" and (
            isinstance(data.get("online"), list) and isinstance(data.get("offline"), list)
        ):
            self._online_cids = {str(cid) for cid in data["online"]}
            offline = {str(cid) for cid in data["offline"]}
            self._offline_notifications.update(offline - self._offline_cids)
            self._offline_cids = offline
        cid = packet.get("cid") or data.get("cid")
        dps = packet.get("dps", data.get("dps"))
        if not cid or not isinstance(dps, dict) or not dps:
            return {}
        for device_id, child in self.children.items():
            if str(cid) == child.cid:
                return {device_id: {str(k): v for k, v in dps.items()}}
        return {}

    def child_error(self, child: GatewayChild, packet: Any = None) -> str:
        if child.cid in self._offline_cids:
            return "child_offline"
        if self._online_cids is not None and child.cid not in self._online_cids:
            return "gateway_connected_but_child_not_found"
        if isinstance(packet, dict):
            if str(packet.get("Err", "")) in {"900", "904"}:
                return "child_protocol_error"
            if str(packet.get("Err", "")) == "907":
                return "child_local_control_unsupported"
            data = packet.get("data")
            data = data if isinstance(data, dict) else {}
            if str(packet.get("cid") or data.get("cid") or "") == child.cid:
                return "child_dp_discovery_empty"
        return "child_status_query_failed"

    def _publish(
        self, packets: dict[str, dict[str, Any]], *, unsolicited: bool = False,
    ) -> None:
        for device_id, child in self.children.items():
            if (
                child.cid in self._offline_notifications
                and device_id not in packets
                and self.on_child_offline
            ):
                self.on_child_offline(device_id)
        self._offline_notifications.clear()
        for device_id, dps in packets.items():
            target = self.children.get(device_id)
            if target is not None:
                self._offline_cids.discard(target.cid)
                if self._online_cids is not None:
                    self._online_cids.add(target.cid)
                target._cached_dps.update(dps)
                target._last_status_was_live = True
                target._last_failure_reason = ""
                if target._dp_callback:
                    target._dp_callback(dps)
                target.publish_report(dps, unsolicited=unsolicited)

    async def _run(self, operation: Callable[[], Any]) -> Any:
        # Cancellation must not release the lock while the executor still owns
        # the socket. In particular, entry unload must not race an active read.
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def connect(self) -> bool:
        async with self.lock:
            if self.connected:
                return True
            ok, updates = await self._run(self._connect_sync)
            self._publish(updates)
            return bool(ok)

    def _connect_sync(self) -> tuple[bool, dict[str, dict[str, Any]]]:
        updates: dict[str, dict[str, Any]] = {}
        versions = AUTO_DETECT_ORDER if self.version == "auto" else [self.version]
        self.error = "child_status_query_failed"
        self._online_cids = None
        self._offline_cids.clear()
        self._offline_notifications.clear()
        for version in versions:
            if version not in AUTO_DETECT_ORDER:
                self.error = "gateway_protocol_unsupported"
                return False, updates
            self._close_sync()
            self.failure_stage = "gateway_session"
            self.failure_code = "none"
            try:
                self.device = tinytuya.Device(
                    self.device_id,
                    address=self.host,
                    local_key=self.key,
                    version=float(version),
                    port=self.port,
                )
                self.device.set_socketPersistent(True)
                # TinyTuya also uses this limit for the initial socket open;
                # zero disables connecting entirely. Recovery belongs to HA.
                self.device.set_socketRetryLimit(1)
                self.device.set_socketTimeout(3.0)
                self.device.set_sendWait(None)
                for child in self.children.values():
                    self._build_child(child)
                # A valid subdevice-list response verifies gateway decryption
                # even when a selected child is missing or asleep.
                result = self.device.subdev_query()
                self.failure_stage = "gateway_child_list"
                self._presence_at = time.monotonic()
                authenticated = (
                    isinstance(result, dict)
                    and bool(result)
                    and not ("Error" in result or "Err" in result)
                )
                updates.update(self.route(result))
                for child in self.children.values():
                    if authenticated and self.child_error(child) in {
                        "child_offline",
                        "gateway_connected_but_child_not_found",
                    }:
                        child._last_failure_reason = self.child_error(child)
                        continue
                    packet = child._send_status_query(child._device, float(version))
                    self.failure_stage = "child_status"
                    routed = self.route(packet)
                    updates.update(routed)
                    authenticated = authenticated or bool(routed)
                    if not routed:
                        child._last_failure_reason = (
                            self.child_error(child, packet)
                            if authenticated
                            else gateway_error(packet)
                        )
                        if not authenticated and packet is not None:
                            result = packet
                if authenticated and self.device.socket is not None:
                    self.connected = True
                    self.error = ""
                    self.detected_version = version
                    for child in self.children.values():
                        child._detected_version = version if self.version == "auto" else None
                    return True, updates
                self.error = gateway_error(result)
                if isinstance(result, dict) and str(result.get("Err", "")) in {
                    str(code) for code in range(900, 915)
                }:
                    self.failure_code = str(result["Err"])
            except Exception as exc:
                self.error = gateway_error(exc=exc)
        self._close_sync()
        return False, updates

    def _close_sync(self) -> None:
        if self.device is not None:
            with suppress(OSError):
                self.device.close()
        self.device = None
        self.connected = False
        for child in self.children.values():
            child._device = None

    async def close(self) -> None:
        async with self.lock:
            await self._run(self._close_sync)

    def _check_socket(self) -> None:
        if self.device is None or self.device.socket is None:
            self.connected = False

    async def query(self, child: GatewayChild, timeout: float = 2.0) -> dict[str, Any]:
        async with self.lock:
            if not self.connected or child._device is None:
                child._last_status_was_live = False
                return {}
            if self._online_cids is not None and self.child_error(child) in {
                "child_offline",
                "gateway_connected_but_child_not_found",
            }:
                child._last_status_was_live = False
                child._last_failure_reason = self.child_error(child)
                return {}

            def query_sync() -> Any:
                old = self.device.connection_timeout
                self.device.set_socketTimeout(timeout)
                try:
                    return child._send_status_query(child._device, float(child.protocol_version))
                finally:
                    self.device.set_socketTimeout(old)

            try:
                packet = await self._run(query_sync)
                routed = self.route(packet)
                child._last_status_was_live = child._device_id in routed
                child._last_failure_reason = "" if routed else self.child_error(child, packet)
                self._publish(routed)
                return routed.get(child._device_id, {})
            except Exception as exc:
                child._last_status_was_live = False
                child._last_failure_reason = gateway_error(exc=exc)
                return {}
            finally:
                self._check_socket()

    async def command(self, child: GatewayChild, dps: dict[int, Any]) -> bool:
        async with self.lock:
            if not self.connected or child._device is None:
                return False
            device = child._device
            try:
                # TinyTuya generates the CID-bearing CONTROL/CONTROL_NEW
                # payload and delegates encoding/session keys to the parent.
                result = await self._run(
                    lambda: device.set_multiple_values(
                        {str(k): v for k, v in dps.items()}, nowait=True
                    )
                )
                if isinstance(result, dict) and ("Error" in result or "Err" in result):
                    child._last_failure_reason = gateway_error(result)
                    return False
                child._cached_dps.update({str(k): v for k, v in dps.items()})
                return True
            except Exception as exc:
                child._last_failure_reason = gateway_error(exc=exc)
                return False
            finally:
                self._check_socket()

    async def receive(self) -> None:
        async with self.lock:
            if not self.connected:
                return

            def receive_sync() -> Any:
                old = self.device.connection_timeout
                if self._online_cids is not None and time.monotonic() - self._presence_at > 30:
                    self._presence_at = time.monotonic()
                    return self.device.subdev_query()
                self.device.set_socketTimeout(0.05)
                try:
                    if time.monotonic() - self._heartbeat_at > 9:
                        self.device.heartbeat(nowait=True)
                        self._heartbeat_at = time.monotonic()
                    return self.device.receive()
                finally:
                    self.device.set_socketTimeout(old)

            try:
                packet = await self._run(receive_sync)
                self._publish(self.route(packet), unsolicited=True)
            except TimeoutError:
                pass
            except Exception:
                self.connected = False
            self._check_socket()
        if not self.connected and self.on_disconnect:
            self.on_disconnect()


class GatewayChild(TinyTuyaDevice):
    """Existing client interface backed by a shared TinyTuya gateway."""

    def __init__(self, config: dict[str, Any], gateway: GatewaySession) -> None:
        super().__init__(
            config["device_id"], gateway.host, gateway.key, gateway.version, gateway.port
        )
        self.cid = str(config.get(CONF_CHILD_CID, "")).strip()
        if not self.cid:
            raise ValueError("child_identifier_missing")
        self.gateway = gateway
        self._report_callbacks: set[Callable[[dict[str, Any]], None]] = set()
        self._observation_callbacks: set[Callable[[dict[str, Any], str], None]] = set()
        self.set_monitored_dp_ids(list(config.get("dp_map", {})))
        gateway.attach(self)

    def subscribe_reports(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> Callable[[], None]:
        """Subscribe to unsolicited reports, never status polls or cached values."""
        self._report_callbacks.add(callback)
        return lambda: self._report_callbacks.discard(callback)

    def subscribe_observations(
        self, callback: Callable[[dict[str, Any], str], None],
    ) -> Callable[[], None]:
        """Capture live listener/query results separately; never replay cache."""
        self._observation_callbacks.add(callback)
        return lambda: self._observation_callbacks.discard(callback)

    def publish_report(self, dps: dict[str, Any], *, unsolicited: bool = True) -> None:
        """Keep repeated identical reports and isolate listener failures."""
        source = "gateway_listener" if unsolicited else "gateway_query"
        for observer in tuple(self._observation_callbacks):
            try:
                observer(dps, source)
            except Exception:
                _LOGGER.warning("Conti child capture listener failed")
        for callback in tuple(self._report_callbacks) if unsolicited else ():
            try:
                callback(dps)
            except Exception:
                _LOGGER.warning("Conti child report listener failed")

    @property
    def connected(self) -> bool:
        return self.gateway.connected

    async def connect(self) -> bool:
        return await self.gateway.connect()

    async def status(self, timeout: float | None = None) -> dict[str, Any]:
        return await self.gateway.query(self, timeout if timeout is not None else 2.0)

    async def set_dp(self, dp_id: int, value: Any) -> bool:
        return await self.set_dps({dp_id: value})

    async def set_dps(self, dps: dict[int, Any]) -> bool:
        result = await self.gateway.command(self, dps)
        if not self.gateway.connected and self.gateway.on_disconnect:
            self.gateway.on_disconnect()
        return result

    async def detect_dps(self) -> dict[str, Any]:
        # Query only this child. Avoid the direct client's CONTROL/updatedps
        # probes, which are unsafe on gateways and unnecessary for Zigbee.
        live = await self.status()
        return {**self.cached_dps, **live} if live else {}

    async def close(self) -> None:
        # Removing a child must never close a sibling's TCP connection.
        self.gateway.children.pop(self._device_id, None)
        if self.gateway.device is not None:
            self.gateway.device.children.pop(self._device_id, None)
        self._device = None

    async def receive_nowait(self) -> dict[str, Any] | None:
        # DeviceManager runs exactly one listener on the parent instead.
        return None


async def probe_child(
    config: dict[str, Any],
    shared: GatewaySession | None = None,
) -> tuple[bool, str | None, dict[str, Any], str]:
    """Read-only onboarding probe. Reuse an already managed parent if present."""
    if not str(config.get(CONF_CHILD_CID, "")).strip():
        return False, None, {}, "child_identifier_missing"
    gateway = shared or GatewaySession(config)
    if shared is not None and (
        (gateway.host, gateway.key, gateway.port)
        != (
            str(config["host"]),
            str(config["local_key"]).strip(),
            int(config.get("port", 6668)),
        )
        or str(config.get("protocol_version", "auto"))
        not in {
            "auto",
            gateway.version,
            gateway.detected_version,
        }
    ):
        return False, None, {}, "gateway_configuration_conflict"
    child: GatewayChild | None = None
    try:
        async with gateway.lock:
            child = GatewayChild(config, gateway)
        if not await gateway.connect():
            _LOGGER.warning(
                "Conti gateway setup failed: stage=%s protocol=%s "
                "tinytuya_code=%s error=%s",
                gateway.failure_stage, gateway.version, gateway.failure_code, gateway.error,
            )
            return False, None, {}, gateway.error
        dps = await child.detect_dps()
        if not dps:
            _LOGGER.warning(
                "Conti gateway setup failed: stage=child_discovery protocol=%s error=%s",
                gateway.detected_version, child.last_failure_reason or "child_dp_discovery_empty",
            )
            return (
                False,
                gateway.detected_version,
                {},
                (child.last_failure_reason or "child_dp_discovery_empty"),
            )
        return True, gateway.detected_version, dps, ""
    except ValueError as exc:
        error = str(exc)
        return (
            False,
            None,
            {},
            (
                error
                if error in {"child_identifier_duplicate", "child_identifier_missing"}
                else "child_protocol_error"
            ),
        )
    except Exception as exc:
        return False, None, {}, gateway_error(exc=exc)
    finally:
        if child is not None:
            async with gateway.lock:
                await child.close()
        if shared is None:
            await gateway.close()
