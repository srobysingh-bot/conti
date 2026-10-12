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
