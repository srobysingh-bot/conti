"""Contract smoke test against installed TinyTuya with all sockets mocked."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_real_tinytuya_parent_payloads_and_shared_socket() -> None:
    # The existing test suite replaces tinytuya with a MagicMock globally.
    # A fresh process imports the installed dependency before HA test stubs.
    script = textwrap.dedent("""
        import asyncio
        import json
        import importlib
        import socket
        import tinytuya
        import tests.conftest
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        from custom_components.conti.gateway import GatewaySession, GatewayChild

        async def check(version):
            config = {
                "gateway_device_id": "parent", "device_id": "cloud-child-a",
                "child_cid": "zigbee-node-a", "host": "192.0.2.1",
                "local_key": "0123456789abcdef", "protocol_version": version,
            }
            gateway = GatewaySession(config)
            a = GatewayChild(config, gateway)
            b = GatewayChild({**config, "device_id": "cloud-child-b",
                              "child_cid": "zigbee-node-b"}, gateway)

            def subdev_query(device):
                assert device._get_socket(False) is True
                return {"reqType": "subdev_online_stat_report", "data": {
                    "online": ["zigbee-node-a", "zigbee-node-b"], "offline": []}}

            def status(device, version):
                command, _ = a._primary_status_command(version)
                raw = json.loads(device.generate_payload(command).payload)
                cid = raw.get("cid") or raw.get("data", {}).get("cid")
                assert cid == device.cid
                return {"cid": cid, "dps": {"17": False}}

            a._send_status_query = status
            b._send_status_query = status
            socket_factory = MagicMock()
            socket_module = SimpleNamespace(**{**vars(socket), "socket": socket_factory})
            core = importlib.import_module("tinytuya.core.XenonDevice")
            with patch.object(core, "socket", socket_module), \\
                 patch.object(tinytuya.Device, "subdev_query", subdev_query), \\
                 patch.object(tinytuya.Device, "_negotiate_session_key", return_value=True):
                assert await gateway.connect()
                assert socket_factory.call_count == 1
                socket_factory.return_value.connect.assert_called_once_with(("192.0.2.1", 6668))
                assert a._device.parent is gateway.device is b._device.parent
                assert a._device.id != a._device.cid
                assert await a.set_dp(17, True)
                assert await b.set_dps({23: False, 47: True})
                assert socket_factory.call_count == 1
                assert socket_factory.return_value.sendall.call_count == 2
                for child in (a, b):
                    raw = json.loads(child._device.generate_payload(
                        tinytuya.CONTROL, {"17": True}).payload)
                    assert (raw.get("cid") or raw.get("data", {}).get("cid")) == child.cid
                await gateway.close()

        for version in ("3.1", "3.3", "3.4", "3.5"):
            asyncio.run(check(version))
        print("TinyTuya", tinytuya.__version__, "parent/CID/payload/socket contracts passed")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_tinytuya_fragmented_listener_frames_and_queued_reports() -> None:
    """Exercise the installed parser against local sockets, not real devices."""
    script = textwrap.dedent("""
        import asyncio
        import json
        import socket
        import threading
        import time
        import tinytuya
        import tests.conftest
        from unittest.mock import patch
        from tinytuya.core.message_helper import TuyaMessage, pack_message
        from custom_components.conti.gateway import GatewaySession, GatewayChild

        async def check(fragmented):
            config = {
                "gateway_device_id": "parent", "device_id": "child",
                "child_cid": "node", "host": "192.0.2.1",
                "local_key": "0123456789abcdef", "protocol_version": "3.1",
            }
            gateway = GatewaySession(config)
            child = GatewayChild(config, gateway)
            gateway.device = tinytuya.Device(
                "parent", address="192.0.2.1", local_key=config["local_key"], version=3.1
            )
            gateway.device.set_socketPersistent(True)
            gateway.device.set_socketRetryLimit(1)
            gateway.device.set_sendWait(None)
            reader, writer = socket.socketpair()
            reader.settimeout(3)
            timer = []
            values = {"cid": "node", "dps": {"52": "switch_1"}}
            # 55AA response packing expects the 4-byte retcode in its payload.
            packet = pack_message(TuyaMessage(
                seqno=1, cmd=tinytuya.STATUS, retcode=0,
                payload=bytes(4) + json.dumps(values).encode(), crc=0,
            ))

            class TrackedSocket:
                def __getattr__(self, name):
                    return getattr(reader, name)

                def recv(self, length):
                    data = reader.recv(length)
                    if fragmented and data and not timer:
                        # Start the delay after the first read, avoiding a
                        # scheduler race that could make both pieces arrive early.
                        delayed = threading.Timer(
                            0.12, writer.sendall, args=(packet[28:],)
                        )
                        timer.append(delayed)
                        delayed.start()
                    return data

            gateway.device.socket = TrackedSocket()
            gateway.device.set_socketTimeout(3)
            gateway._build_child(child)
            gateway.connected = True
            gateway._heartbeat_at = time.monotonic()
            observations = []
            child.subscribe_observations(lambda dps, source: observations.append((dps, source)))
            try:
                # Empty sockets should not enter the blocking frame parser.
                with patch.object(
                    gateway.device, "receive", wraps=gateway.device.receive
                ) as receive:
                    await gateway.receive()
                    receive.assert_not_called()
                writer.sendall(packet[:28] if fragmented else packet)
                await gateway.receive()
                assert child.cached_dps["52"] == "switch_1"
                assert observations == [({"52": "switch_1"}, "gateway_listener")]
                assert reader.gettimeout() == 3
                # TinyTuya queues sibling replies during status queries. They
                # must be drained even when the underlying socket is idle.
                gateway.device.received_wrong_cid_queue.append(
                    (child._device, {"cid": "node", "dps": {"52": "switch_2"}})
                )
                with patch("custom_components.conti.gateway.select.select") as readiness:
                    await gateway.receive()
                    readiness.assert_not_called()
                assert child.cached_dps["52"] == "switch_2"
                assert len(observations) == 2
                assert gateway.connected
            finally:
                for delayed in timer:
                    delayed.join()
                await gateway.close()
                writer.close()

        asyncio.run(check(False))
        asyncio.run(check(True))
        print("Complete/fragmented TCP frames, idle reads and queued reports verified")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
