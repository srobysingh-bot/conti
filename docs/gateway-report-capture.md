# Capture a remote through Home Assistant's existing gateway connection

Conti v0.3.13 added a read-only capture action for devices behind a Tuya gateway.
Use v0.3.14 or newer: 0.3.14 fixes a listener timeout that could discard fragmented
TCP report frames. This does not claim that all physical buttons are forwarded.
It subscribes to the same managed child and serialized parent socket already
used by Home Assistant. It does not open a connection, poll a device, reconnect
it, send a command or change button modes. Normal child state handling continues.
Direct devices are unchanged. No physical button mapping is enabled by capture.

## Procedure for the YK remote

1. Update Conti to **v0.3.14** in HACS and perform the normal HA restart needed
   to load the new Python code. Keep all Conti device entries enabled. Subsequent
   capture actions do not require another restart or disabling entries.
2. In **Developer Tools > Actions**, switch to YAML and run:

```yaml
action: conti.start_gateway_report_capture
data:
  device_id: "bfd0c33349c72a9c67sczh"
  duration: 300
```

3. Keep Smart Life's remote configuration page closed so app setting edits are
   not mistaken for physical reports. On the physical remote, press ON, OFF,
   brightness + and -, K+ and K-, RGB, moon, then S1 twice and S2 through S8.
   Wait about three seconds between presses and record their order.
4. Open the **yk device > Download diagnostics**. The JSON now includes
   `gateway_report_capture`: timestamps, the actual DP values reported, and
   `report_count`. Copy that section and the button order for analysis. It has
   no gateway keys, account tokens or config-entry credentials. Report values
   and the child Device ID are included.
5. Capture stops automatically after 300 seconds or the report limit. To stop
   sooner, run:

```yaml
action: conti.stop_gateway_report_capture
data:
  device_id: "bfd0c33349c72a9c67sczh"
```

For optional live observation, **Developer Tools > Events > Listen to events**
can listen for `conti_gateway_report` before starting capture. Each event contains
the child Device ID, timestamps and the incoming DPS. It is a diagnostic report,
not an assertion that a physical button was pressed. The retained diagnostics
are sufficient; live event viewing is optional.

## What the capture establishes

The supplied YK model identifies DPs21-28 as button-mode settings and DP52 as
button selection. DPs6-10 and DP30 are write-only lighting/scene instructions;
DP2 is a read-only scene report with four advertised scene values. Tuya defines
these access modes in its [Things Model documentation](https://developer.tuya.com/en/docs/cloud/bd68171262?id=Kcp4utbhzzfgo).
Model definitions alone do not establish which physical reports reach the LAN.

Workspace tests successfully read the remote's four settings and captured some
DP52 selection changes. They did not establish full remote behavior, and the
extra workspace TCP client may have competed with HA. This release removes that
ambiguity by observing HA's existing listener, not by synthesizing button events.

If `listener_report_count` is zero, confirm the child/gateway is online and the recorded
capture status, then inspect bindings/button initialization. If reports arrive,
correlate their timing and values with each physical button. Repeated identical
reports are preserved. Each record labels its read path: `gateway_listener` or
`gateway_query`. Query/connection results are included separately because a
physical report can arrive while TinyTuya waits for a status response. Neither
query results nor listener packets are automatically claimed to be button
presses. Cached values are not replayed. Check `listener_report_count` and
`query_report_count` separately when inspecting the capture.

The optional event-entity work remains in draft PR #1 until report meanings and
the required controls are verified. v0.3.13 is a capture/diagnostic release,
not a claim that all 16 remote buttons now work.

## Bounds, lifetime and validation

Capture is opt-in, lasts 10-300 seconds and retains at most 200 reports per
child. Individual reports larger than 8192 UTF-8 bytes or non-JSON values are
ignored and counted. Restarting capture replaces and unsubscribes the previous
one. Entry unload stops it. Captures live only in memory and disappear when HA
restarts. Raw report values are not written to logs or persistent storage.

78 focused gateway/raw-sensor/capture tests pass. The full suite with CI coverage
reports **322 passed, the same six pre-existing failures**, with 40% overall
coverage. Ruff retains 172 existing findings and mypy retains 64 errors in 19
files. Tests verify reuse of one socket, normal state updates during capture,
sibling isolation, repeated reports, query/listener distinction, expiry/replacement/unload
cleanup, bounds, diagnostics and absence of config credentials. No real HA
instance was modified or device commands sent during development.
