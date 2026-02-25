# Conti — Local Device Control for Home Assistant

[![CI](https://github.com/conti-addon/conti/actions/workflows/ci.yml/badge.svg)](https://github.com/conti-addon/conti/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Conti** is a Home Assistant custom integration that provides **fully local LAN control** of Tuya-based IoT devices — lights, fans, ACs, switches, and sensors — **without any cloud dependency, Docker container, or external add-on**.

It communicates directly with devices over the Tuya TCP protocol (v3.1 / v3.3 / v3.4 / v3.5), using AES-128-ECB/GCM encryption and CRC-32 checksums.

---

## Architecture

```
Home Assistant Core
└── custom_components/conti/
    ├── Config Flow          (UI-guided device setup with live validation)
    ├── ContiCoordinator     (DataUpdateCoordinator — poll + push)
    ├── DeviceManager        (connection pool, reconnect, state cache)
    ├── DP Mapping           (heuristic auto-entity creation)
    ├── Entity Platforms     (light, switch, sensor, fan, climate)
    └── tuya_protocol/       (TCP client, AES crypto, frame pack/unpack)
          └── Direct TCP → Tuya device on LAN (port 6668)
```

No REST API, no WebSocket server, no Docker container, no database.

## Features

- **100 % Local** — Direct TCP to each device on your LAN
- **Push + Poll** — Instant state updates via TCP push, with periodic polling as a safety net
- **Persistent Connections** — Connection pool with heartbeat and exponential-backoff reconnect
- **Config Flow UI** — Add devices through the standard HA Integrations page; live connection validation
- **Auto Protocol Detection** — Automatically detects protocol v3.3/3.4/3.5/3.1 and persists the result
- **Auto DP Discovery** — Discovers device datapoints (DPS) on connect; persists to `.storage/`
- **Auto Entity Creation** — Heuristically maps DPs to Home Assistant entities based on device type
- **Multi-protocol** — v3.1, v3.3, v3.4, v3.5 Tuya protocol versions
- **Flexible DP Mapping** — JSON-based datapoint map per device with auto-merge
- **Options Flow** — Edit DP map, toggle debug logging, and re-discover DPs at any time
- **Zero External Dependencies** — Only `cryptography` (for AES); no cloud, no broker, no add-on

## Supported Devices

| Type | Features | Example DPs |
|------|----------|-------------|
| Light | On/off, brightness, colour temp, RGB | DP 1, 3, 4 |
| Fan | On/off, speed, direction | DP 1, 2, 3 |
| Climate | Power, target/current temp, HVAC mode, fan mode | DP 1–5 |
| Switch | On/off | DP 1 |
| Sensor | Temperature, humidity, power usage, battery, motion | DP 1–3 |

## Quick Start

### 1. Install

Copy the `custom_components/conti/` folder into your Home Assistant `config/custom_components/` directory:

```
config/
└── custom_components/
    └── conti/
        ├── __init__.py
        ├── manifest.json
        ├── const.py
        ├── config_flow.py
        ├── coordinator.py
        ├── device_manager.py
        ├── light.py
        ├── switch.py
        ├── sensor.py
        ├── fan.py
        ├── climate.py
        ├── strings.json
        └── tuya_protocol/
            ├── __init__.py
            ├── base.py
            ├── crypto.py
            └── client.py
```

Restart Home Assistant.

### 2. Add a Device

**Settings → Devices & Services → Add Integration → Conti**

Fill in:

| Field | Description |
|-------|-------------|
| Name | Friendly name (e.g. "Living Room Light") |
| Host | Device IP on your LAN |
| Device ID | Tuya Device ID |
| Local Key | 16-char local encryption key |
| Protocol Version | auto / 3.1 / 3.3 / 3.4 / 3.5 |
| Device Type | light / switch / sensor / fan / climate |
| DP Map (JSON) | Datapoint mapping (see below) — leave default for auto |

The integration validates the connection *live* before saving.  If protocol is set to *Auto*, versions 3.3 → 3.4 → 3.5 → 3.1 are tried in order and the first success is persisted.

### 3. DP Map Examples

**Light:**
```json
{
  "1": {"key": "power", "type": "bool"},
  "3": {"key": "brightness", "type": "int", "min": 10, "max": 1000},
  "4": {"key": "color_temp", "type": "int", "min": 0, "max": 1000}
}
```

**Switch:**
```json
{
  "1": {"key": "power", "type": "bool"}
}
```

**Sensor:**
```json
{
  "1": {"key": "temperature", "type": "int", "scale": 10},
  "2": {"key": "humidity", "type": "int"},
  "3": {"key": "battery", "type": "int"}
}
```

## Auto Protocol Detection

When you select **Auto** as the protocol version during setup, the integration
tries handshakes in this order: **3.3 → 3.4 → 3.5 → 3.1**.

- The **first version** that successfully connects and responds to a heartbeat
  is locked and persisted in the config entry as `detected_version`.
- On subsequent connects / restarts the detected version is used directly — no
  re-probing delay.
- If the detected version fails **3 times in a row** at runtime, the lock is
  cleared and a full re-detection cycle runs automatically.

You can also select a specific version (e.g. `3.4`) if you already know what
your device requires.

## Auto DP Discovery

After a successful connection in the config flow, the integration queries the
device for its supported **datapoints (DPs)**:

1. Sends a `DP_QUERY` command (v3.1/3.3 style).
2. Sends a `DP_QUERY_NEW` command (v3.4/3.5 style).
3. Sends a `CONTROL` probe with common DP ids (1–28) to trigger a STATUS push.

Discovered DPs are:
- Used to auto-generate the DP mapping (see below).
- Stored in the config entry for reference.
- Cached to `.storage/conti_<device_id>.json` so entities have data immediately
  after a Home Assistant restart.

To **re-discover** DPs at any time, open the integration's **Options** dialog
and check *Re-discover DPs from device*.

## Auto Entity Creation

Based on the **device type** you select and the discovered DPs, the integration
automatically maps datapoints to Home Assistant entities using heuristic rules:

| Device Type | Heuristic Rules |
|-------------|-----------------|
| **switch**  | Every boolean DP → one `SwitchEntity` (multi-gang support) |
| **light**   | Bool → power, 1st int → brightness, 2nd int → color temp, string → RGB |
| **fan**     | Bool → power, int → speed, string → direction |
| **climate** | Bool → power, DP 2 → target temp, DP 3 → current temp, DP 4 → HVAC mode, DP 5 → fan mode |
| **sensor**  | DP 1/18 → temperature, DP 2/19 → humidity, DP 3 → battery |

### Overriding with `dp_map`

The DP map you enter in the config UI *always takes priority*.  Auto-mapped
keys that are **not** present in your user-supplied map are **added** to the
final mapping.

Example — you provide:
```json
{"1": {"key": "power", "type": "bool"}}
```
And the device has DPs 1 (bool), 2 (int), 3 (int).  For a light, auto-mapping
adds brightness (DP 2) and color_temp (DP 3) automatically.

To **disable** auto-mapping for a specific DP, include it in your map with the
role you prefer (or an empty dict to suppress it).

## Enabling Debug Logs

1. **Options flow**: Go to *Settings → Devices & Services → Conti → Options*
   and check **Enable verbose / debug logging**.
2. **Manual**: Add this to `configuration.yaml`:
   ```yaml
   logger:
     logs:
       custom_components.conti: debug
   ```

Debug logs include:
- Chosen / detected protocol version per device (local key is redacted).
- Handshake success / failure per version in auto mode.
- First DPS / status response (secrets redacted).
- Frame hex snippets (TX / RX) for low-level diagnostics.

## Finding Your Device ID & Local Key

You need two pieces of information from the Tuya cloud platform:

1. **Device ID** — visible in the Tuya IoT Platform device list.
2. **Local Key** — can be obtained via the Tuya IoT Developer account or third-party tools such as `tinytuya`.

## Development

```bash
# Clone & install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check custom_components/ tests/

# Type-check
mypy custom_components/conti/ --ignore-missing-imports
```

## Project Structure

```
conti/
├── custom_components/conti/   # HA custom integration
│   ├── tuya_protocol/         # Tuya TCP + crypto (no HA dependency)
│   │   ├── base.py            # Constants, TuyaCommand enum, TuyaFrame
│   │   ├── crypto.py          # AES-ECB, CRC32, frame pack/unpack
│   │   └── client.py          # Async TCP client with heartbeat
│   ├── __init__.py            # Integration setup / teardown
│   ├── config_flow.py         # UI config flow with live validation + options
│   ├── coordinator.py         # DataUpdateCoordinator (poll + push)
│   ├── device_manager.py      # Connection pool, reconnect
│   ├── dp_mapping.py          # Heuristic auto-entity creation
│   ├── const.py               # All constants & DP key definitions
│   ├── manifest.json          # HA integration manifest
│   ├── strings.json           # UI strings
│   ├── light.py               # Light entity platform
│   ├── switch.py              # Switch entity platform (multi-gang)
│   ├── sensor.py              # Sensor entity platform
│   ├── fan.py                 # Fan entity platform
│   └── climate.py             # Climate entity platform
├── tests/                     # pytest test suite
├── pyproject.toml
└── .github/workflows/ci.yml   # CI pipeline
```

## Manual Test Plan

### Prerequisites
- A Tuya device on the local network (e.g., IP `192.168.x.x`, port `6668` open).
- Device ID and Local Key from the Tuya IoT Platform.

### Test 1 — Explicit Protocol (e.g. 3.4)
1. Go to *Settings → Devices & Services → Add Integration → Conti*.
2. Fill in device details, set **Protocol Version** to `3.4`.
3. Submit.  Confirm the config entry is created with `protocol_version: "3.4"`.
4. Verify entities appear (e.g. switch, light) and reflect device state.
5. Toggle the device — confirm state updates in HA within ~10 s.

### Test 2 — Auto Protocol Detection
1. Add a new device with **Protocol Version** set to `auto`.
2. Watch HA logs (`custom_components.conti`) — you should see:
   ```
   Conti connect starting for <id> ..., versions to try: ['3.3', '3.4', '3.5', '3.1']
   Auto-detected and locked protocol v3.X for <id>
   ```
3. Inspect the config entry (Developer Tools → States or `.storage/`):
   - `protocol_version` should be `"auto"`.
   - `detected_version` should be the locked version (e.g. `"3.4"`).
4. Restart Home Assistant — confirm the device reconnects using the detected
   version directly without re-probing all versions.

### Test 3 — DP Discovery & Auto Entities
1. Add a switch device with the default DP map.
2. After setup, check that the integration discovered DPs and auto-mapped them.
3. For a multi-gang switch (DPs 1, 2, 3 are bools), confirm multiple switch
   entities are created: `Switch 1`, `Switch 2`, `Switch 3`.
4. Check `.storage/conti_<device_id>.json` exists with cached DPs.

### Test 4 — Options Flow
1. Go to *Settings → Devices & Services → Conti → Options*.
2. Edit the DP map, enable verbose logging, check "re-discover DPs".
3. Submit and verify logs show debug-level output.
4. Verify entities reflect any DP map changes after a reload.

### Test 5 — Error Cases
1. Try adding a device with a **wrong IP** → confirm `cannot_connect` error.
2. Try adding with a **wrong Local Key** and protocol `3.4` →
   confirm `invalid_auth` error.
3. Try adding with protocol `auto` to a non-Tuya device →
   confirm `wrong_protocol` error after all versions are tried.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This project communicates with IoT devices on your local network. Device local keys are never transmitted externally. Extracting device local keys from the Tuya cloud platform may be subject to Tuya's Terms of Service. Use at your own discretion.
