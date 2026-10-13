# YK remote: reports, settings and the missing button stream

## Evidence and limits

The user supplied the Kerr YK-16 remote's Tuya Things Model `fjjr8o` and its
four-DP mapping. The latest released base is `1d0d173` / v0.3.12.

| DP | Tuya code | Access | Meaning in supplied model |
| --- | --- | --- | --- |
| 2 | scene_controller | ro | Scene report, scene_1 through scene_4 |
| 6 | switch_controller | wr | On/off instruction |
| 7 | mode_controller | wr | Lighting-mode instruction |
| 8 | bright_controller | wr | Brightness instruction |
| 9 | temp_controller | wr | Colour-temperature instruction |
| 10 | color_controller | wr | Colour instruction |
| 21–28 | button_mode_1 through button_mode_8 | rw | Button-mode settings |
| 30 | scene_data | wr | Scene instruction |
| 52 | scene_switch | rw | Button selection, switch_1 through switch_8 |

Tuya defines `ro` as reported/read-only, `wr` as sent/write-only, and `rw` as
sent and reported/read-write. The write-only controller properties are not
evidence that the gateway reports corresponding physical presses.

Sources:
- https://developer.tuya.com/en/docs/cloud/bd68171262?id=Kcp4utbhzzfgo
- https://developer.tuya.com/en/docs/iot/remote-control?id=Kaof8v52k1ij3
- https://developers.home-assistant.io/docs/core/entity/event/

Workspace read-only capture identified a candidate CID whose status matches
the user's remote mapping: DP22 and DP23 `wireless_switch`, DP27 `no_initialize`,
and DP52 selection strings. Gateway inventory listed it alongside the known
Quad. Its cloud-ID/CID relationship was not independently queried from Tuya.
Do not infer unrelated child relationships from DP numbers alone.

The first capture received `switch_7`, `switch_8`, then `switch_7` at DP52.
During the user-requested S1/S1/S2 physical test, a subsequent capture received
DP52 `switch_1` and `switch_2` at 169.7 and 177.6 seconds. The capture ended at
180 seconds; it cannot establish results for the later lighting controls.

A longer capture started with DP52 `switch_1`. The user confirmed exercising
ON/OFF, brightness +/- , K+/- , RGB, moon and S1 twice, then a separate S2 check.
No new remote reports were observed by that workspace listener. A fresh status
query after closing the temporary listener succeeded and still returned the
same four settings, including DP52 `switch_1`.

This proves successful local status access and some selection reports. It does
not prove all physical presses reach the LAN API. An additional workspace TCP
connection can compete with the already-running HA connection; the absence of
reports in this capture is not conclusive proof of a firmware limitation. The
next reliable test should capture reports from HA's existing managed gateway
connection, or temporarily pause the competing HA entry with user authorization.
No device commands, mode writes, re-pairing or live HA changes were performed.

## Draft event implementation

The draft adds an opt-in `event` platform for explicitly mapped gateway-child
reports. No button mapping is generated automatically from raw values or this
product's DP numbers. Existing sensors and direct-device behavior remain intact.

- `GatewayChild.subscribe_reports` receives only unsolicited packets handled
  by the parent listener. Polls, connection queries and cached state do not
  produce events. Callbacks remain scoped to the routed child CID.
- An explicit DP map with `type: event` and an `events` value-to-action dictionary
  creates one HA event entity for that DP. Repeated incoming equal reports are
  preserved; the implementation cannot synthesize missing physical reports.
- Entity callbacks are removed on unload. The coordinator drives availability,
  while event timestamps change only when a mapped incoming report arrives.
- A writable selection/configuration DP can also report an app setting change.
  Opting it into event handling does not prove that every report is a physical
  press. Verify the source and semantics before using it for automation.

Example for selection reports ONLY, after verification on the HA connection:

```json
{
  "22": {"key": "dp_22", "type": "str"},
  "23": {"key": "dp_23", "type": "str"},
  "27": {"key": "dp_27", "type": "str"},
  "52": {
    "key": "button_selection",
    "type": "event",
    "events": {
      "switch_1": "selection_1",
      "switch_2": "selection_2"
    }
  }
}
```

This is a draft example, not instructions to install a complete remote fix.
Only S1/S2 selection reports were correlated in the controlled capture. The
model advertises eight selections, but those are not 16 verified physical
button events. DP2 was never observed locally and is not added to the example.

Before release, verify repeated presses and the remaining controls using HA's
managed socket and confirm whether Smart Life initialization/binding is needed.
If the gateway does not expose the required Zigbee actions, further DP mappings
cannot create them. Direct Zigbee coordinator support would then require checking
the actual hardware fingerprint and all buttons, not just the printed YK-16 name.

## Automated validation

75 focused gateway/event/raw-sensor tests pass. The event tests exercise the real
platform class, two identical reports, query/reconnect exclusion, sibling routing,
unknown values, unsubscription, callback failure isolation and direct-device
compatibility. Synthetic actions in tests are not claimed hardware button codes.
Full pytest has 319 passing tests and the same six pre-existing failures. Ruff
retains 172 existing findings; mypy retains 64 existing errors in 19 files, with
no findings in the new event module.
The full CI coverage command also reports 319 passed and the same six failures,
with 40% overall integration coverage.

The draft is deliberately not a versioned HACS release: whole-remote support is
not verified. v0.3.12 remains the latest published release.
