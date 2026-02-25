#!/usr/bin/env python3
"""Standalone Tuya device connection tester.

Run this script directly (no Home Assistant needed) to diagnose
connectivity issues with a Tuya device on the local LAN.

Usage
-----
    python test_device.py --ip 192.168.1.176 --id DEVICE_ID --key LOCAL_KEY [--version auto]

The script will:
  1. Try to connect using the specified (or auto-detected) protocol version.
  2. Send a DP query and print the response.
  3. Optionally set a DP value (--set-dp "1=true").
  4. Listen for push updates for a few seconds.
  5. Print per-device diagnostics.
  6. Disconnect cleanly.

All protocol traffic is logged at DEBUG level.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import os

# ---------------------------------------------------------------------------
# Make sure the custom_components package is importable even when running
# from the repo root or tests/ directory.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR) if os.path.basename(_SCRIPT_DIR) != "" else _SCRIPT_DIR
# If we're inside the repo (e.g. running from project root), add it to path
for candidate in [_REPO_ROOT, os.path.join(_REPO_ROOT, "custom_components", "conti")]:
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

# Now import — try package-relative first, then direct
try:
    from custom_components.conti.tuya_protocol import TuyaDeviceClient
    from custom_components.conti.tuya_protocol.base import (
        PROTO_31,
        PROTO_33,
        PROTO_34,
        PROTO_35,
        PROTO_AUTO,
    )
except ImportError:
    from tuya_protocol import TuyaDeviceClient  # type: ignore[no-redef]
    from tuya_protocol.base import (  # type: ignore[no-redef]
        PROTO_31,
        PROTO_33,
        PROTO_34,
        PROTO_35,
        PROTO_AUTO,
    )


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG

    fmt = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(name)s] %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

_received_dps: dict[str, object] = {}


def _on_dp_update(dps: dict[str, object]) -> None:
    _received_dps.update(dps)
    print(f"\n  >> Push update: {json.dumps(dps, indent=2)}")


def _on_disconnect() -> None:
    print("\n  !! Device disconnected")


# ---------------------------------------------------------------------------
# Main test routine
# ---------------------------------------------------------------------------

async def run_test(
    ip: str,
    device_id: str,
    local_key: str,
    version: str,
    port: int,
    set_dp: str | None,
    listen_seconds: int,
) -> bool:
    """Run the connection test.  Returns True on success."""
    print(f"\n{'='*60}")
    print(f"  Conti – Tuya Device Connection Tester")
    print(f"{'='*60}")
    print(f"  IP:       {ip}:{port}")
    print(f"  Device:   {device_id}")
    print(f"  Key:      {local_key[:4]}{'*' * (len(local_key)-4)}")
    print(f"  Version:  {version}")
    print(f"{'='*60}\n")

    client = TuyaDeviceClient(
        device_id=device_id,
        ip=ip,
        local_key=local_key,
        version=version,
        port=port,
    )
    client.set_dp_callback(_on_dp_update)
    client.set_disconnect_callback(_on_disconnect)

    # --- Connect ---
    print("[1/4] Connecting...")
    ok = await client.connect()
    if not ok:
        print("  FAIL – Could not connect to device.")
        print("  Check: IP reachable? Local key correct? Port open?")
        return False

    proto = client._protocol
    detected_ver = proto.version if proto else "?"
    print(f"  OK – Connected (protocol v{detected_ver})")

    # --- Status query ---
    print("\n[2/4] Querying device status (DP_QUERY)...")
    status = await client.status()
    if status:
        print(f"  OK – DPS: {json.dumps(status, indent=4)}")
    else:
        print("  WARN – Empty status response (device may need a moment)")
        # Try again after a short delay
        await asyncio.sleep(1.0)
        status = await client.status()
        if status:
            print(f"  OK (retry) – DPS: {json.dumps(status, indent=4)}")
        else:
            print("  FAIL – Still empty.  Device may not respond to DP_QUERY.")

    # --- Set DP (optional) ---
    if set_dp:
        print(f"\n[3/4] Setting DP: {set_dp}")
        dp_id_str, _, value_str = set_dp.partition("=")
        try:
            dp_id = int(dp_id_str.strip())
        except ValueError:
            print(f"  FAIL – Invalid DP id '{dp_id_str}' (must be integer)")
            await client.close()
            return False

        # Parse value
        value_str = value_str.strip()
        if value_str.lower() in ("true", "false"):
            value = value_str.lower() == "true"
        else:
            try:
                value = int(value_str)
            except ValueError:
                try:
                    value = float(value_str)
                except ValueError:
                    value = value_str  # string

        ok = await client.set_dp(dp_id, value)
        if ok:
            print(f"  OK – set_dp({dp_id}, {value!r}) sent")
            await asyncio.sleep(1.0)
            print(f"  Updated DPS: {json.dumps(client.cached_dps, indent=4)}")
        else:
            print(f"  FAIL – set_dp({dp_id}, {value!r}) failed")
    else:
        print("\n[3/4] Skipping set_dp (use --set-dp '1=true' to test)")

    # --- Listen for push updates ---
    if listen_seconds > 0:
        print(f"\n[4/4] Listening for push updates ({listen_seconds}s)...")
        try:
            await asyncio.sleep(listen_seconds)
        except asyncio.CancelledError:
            pass
        if _received_dps:
            print(f"  Received push DPS: {json.dumps(_received_dps, indent=4)}")
        else:
            print("  No push updates received during listen period")
    else:
        print("\n[4/4] Skipping listen (use --listen N to wait N seconds)")

    # --- Disconnect ---
    print(f"\n{'='*60}")
    print("  DIAGNOSTICS")
    print(f"{'='*60}")
    print(f"  Protocol version : {client.protocol_version}")
    print(f"  Detected version : {client.detected_version or 'N/A (explicit)'}")
    print(f"  Connected        : {client.connected}")
    print(f"  Last seen        : {client.last_seen:.1f}s (monotonic)")
    print(f"  Last TX (hex)    : {client.last_tx_hex[:64]}")
    print(f"  Last RX (hex)    : {client.last_rx_hex[:64]}")
    print(f"  Cached DPS       : {json.dumps(client.cached_dps)}")

    await client.close()
    print(f"\n{'='*60}")
    print("  Test complete.  Connection closed.")
    print(f"{'='*60}\n")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Standalone Tuya device connection tester (no HA needed)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ip", required=True, help="Device IP address")
    parser.add_argument("--id", required=True, dest="device_id", help="Device ID")
    parser.add_argument("--key", required=True, dest="local_key", help="Local key (16 chars)")
    parser.add_argument(
        "--version",
        default="auto",
        choices=["auto", "3.1", "3.3", "3.4", "3.5"],
        help="Protocol version (default: auto)",
    )
    parser.add_argument("--port", type=int, default=6668, help="TCP port (default: 6668)")
    parser.add_argument(
        "--set-dp",
        default=None,
        metavar="'DP=VALUE'",
        help="Set a DP value, e.g. '1=true' or '3=500'",
    )
    parser.add_argument(
        "--listen",
        type=int,
        default=5,
        metavar="SECONDS",
        help="Seconds to listen for push updates (default: 5)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=1,
        help="Increase verbosity (-v=INFO, -vv=DEBUG)",
    )

    args = parser.parse_args()
    _setup_logging(args.verbose)

    success = asyncio.run(
        run_test(
            ip=args.ip,
            device_id=args.device_id,
            local_key=args.local_key,
            version=args.version,
            port=args.port,
            set_dp=args.set_dp,
            listen_seconds=args.listen,
        )
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
