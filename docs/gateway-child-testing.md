# Conti gateway-child implementation and acceptance test

## Checkout and scope

Before editing, `git fetch origin` and `git ls-remote origin HEAD` confirmed that
local `main`, `origin/main`, and GitHub HEAD were all
`caff504c943dafa6ef899a9734b0002fc39c17c4`:
**feat: add global offline alert settings for Conti**. The working tree was clean.
The offline alert changes in that commit are preserved.

This implementation is packaged as **Conti 0.3.14**, tagged `v0.3.14`, for
download through HACS. No installation or deployment to Home Assistant was
performed during development or release. Subsequent read-only LAN checks
verified WG-Z1 protocol 3.4, the Quad child CID, and live child status. No relay
commands were sent. The user subsequently added the Quad in Home Assistant;
all four relay DPs were verified in workspace LAN reads after physical operation.

## Fragmented listener frames fixed in 0.3.14

The YK remote's actual HA 0.3.13 diagnostics recorded 17 identical query results
and zero listener reports. The gateway and two children were connected, with
no child/gateway errors. The remote's cloud ID/CID were subsequently confirmed
from the original account's device details. This rules out the expired account
as the cause of those local results, but does not establish full button forwarding.

Conti's listener imposed a 50-millisecond receive timeout. Installed TinyTuya
discards an incomplete frame if a read times out. A local-socket reproduction
received a complete packet, but lost the same packet when TCP pieces arrived
120 milliseconds apart. This is a confirmed transport bug, not proof that the
user's remote packets were fragmented during the particular capture.

The listener now checks socket readiness without consuming bytes. When a packet
is ready, TinyTuya retains the normal configured timeout to assemble its full
frame. Empty sockets do not enter a blocking receive. TinyTuya's queued sibling
reports bypass the readiness check, so they are drained even on an idle socket.
The existing shared I/O lock, heartbeat, presence checks and routing remain intact.
Direct-device reads are unchanged, and no button meanings/modes are invented.

The installed-library regression test covers complete and delayed fragmented
frames, idle sockets, timeout restoration and queued reports. All protocol
payload/shared-socket contract tests still pass. Update through HACS to 0.3.14,
restart HA normally, and repeat the existing report-capture action with entries
enabled. Full YK button mapping still requires the resulting hardware reports.

## Shared-connection report capture in 0.3.13

The YK remote's raw sensors show settings; they do not make every physical button
usable in HA. To identify the actual incoming actions without a competing LAN
client, 0.3.13 adds opt-in capture on HA's already-managed gateway listener.
The service actions send no commands and create no additional connections.
Results appear in the child's downloadable diagnostics. See
[the capture procedure and limits](gateway-report-capture.md).
The event mapping remains a separate draft pending this hardware evidence.

## Missing raw child entities fixed in 0.3.12

Before these edits, the clean checkout and `origin/main` both matched
`fd0cc87f076d6ebba21e908c41ada0dab6dc90fd` (0.3.11). The user's second gateway
child, a Kerr YK-16 Zigbee remote, had this saved mapping but no entities:

```json
{
  "22": {"key": "dp_22", "type": "str"},
  "23": {"key": "dp_23", "type": "str"},
  "27": {"key": "dp_27", "type": "str"},
  "52": {"key": "dp_52", "type": "str"}
}
```

The gateway transport had produced a nonempty raw fallback map, but no entity
platform consumed the `dp_<id>` string roles. The switch platform requires bool
DPs, and the sensor platform previously accepted only known semantic roles.
Creating a device registry entry therefore did not guarantee any entities.

- The sensor platform now exposes gateway-child raw roles as read-only DP
  sensors, independently of the selected Light/Switch/Sensor category. Existing
  0.3.10/0.3.11 raw mappings work after updating and restarting, without re-adding
  the child or changing its Device ID/CID.
- New gateway discovery preserves unmapped DPs alongside mapped roles. It still
  excludes cloud-only DPs not observed in that child's local response.
- Each sensor uses the child's coordinator and registry identity, links through
  the parent gateway, and follows child availability. Values are unscaled and
  never written to the device. Long/structured payloads retain their full value
  in a `raw_value` attribute while keeping the HA state within 255 characters.
- Direct-device raw mappings retain their existing behavior. Known direct
  temperature/energy/battery roles and scaling remain unchanged.

### YK remote verification

1. Update HACS to **v0.3.12** and fully restart Home Assistant. The screenshot
   showed the running integration version **0.3.10**.
2. Keep the existing four-DP mapping above. The `yk` child should now show sensors
   **DP 22, DP 23, DP 27, DP 52**, all under the same physical child device.
3. Press one remote button at a time and inspect each sensor's value. Check that
   sibling children on the gateway do not receive its values. Save the full Tuya
   model and the raw reports for ON/OFF, brightness, colour-temperature and S1-S8
   before assigning button meanings.
4. These sensors expose reported data; they do **not** claim 16 verified button
   events or relay control. Repeated identical button values may not generate a
   sensor state-change trigger. Proper event entities require identification of
   the button/action reports and a live push test; polling a stored scene value
   must not be treated as a new press.

The remote's complete map was supplied by the user. Its live DP values and
physical push behavior have not been tested from this workspace because its
child Device ID/CID were not provided. No live Home Assistant changes were made.

Validation: gateway tests and raw-sensor regression tests **69 passed**. Full
pytest **313 passed, the same 6 pre-existing failures** listed below. Ruff retains
the same 172 existing findings; mypy retains the same 64 errors in 19 files.
The full CI coverage command also returned 313 passed and the same 6 failures,
with **39% overall integration coverage**.
The raw-sensor tests exercise the actual platform class with the supplied map,
legacy entries, options priority, sibling isolation, missing/long values and
direct-device compatibility. Test state values are synthetic, not asserted
physical button codes.

## Home Assistant connection diagnostics in 0.3.11

Conti's exact `probe_child()` code succeeded repeatedly from the development
workspace against the real gateway using protocol **3.4** and child CID
**fc4d6afffe4d4eea**. The Quad returned 21 live datapoints. This verifies local
status reads from that workspace only; it does not prove the Home Assistant
host has the same route, session behavior, or relay control results.

The user confirmed Conti 0.3.10 and a full Home Assistant restart, but setup still
reported `gateway_unreachable` without a Conti log entry. This release fixes the
diagnostic gap; it does not claim to resolve the underlying host-specific failure.

- TinyTuya error 902 is now distinguished as a response timeout.
- Failed onboarding records its stage, selected protocol, normalized error and
  numeric TinyTuya code at warning level, without keys, IDs or raw payloads.
- After a connection/timeout failure, the config flow tests TCP reachability from
  the Home Assistant host and logs `Conti gateway setup diagnostic`.
- A successful TCP test produces `gateway_tcp_reachable_no_session`, explaining
  that the socket can open but the Tuya request failed. A failed TCP test keeps
  the connection/timeout error and logs the network result. TCP success alone
  does not verify the gateway key or protocol.
- Healthy onboarding does not open this extra diagnostic connection.

Update through HACS to **0.3.11**, restart Home Assistant, and submit one setup
attempt. Search HA logs for `Conti gateway setup` and collect the stage, numeric
TinyTuya code and `tcp_result`. These results are required to diagnose the
remaining Home Assistant-specific failure. No hardware commands are sent by
this diagnostic check.

## Setup retry fix in 0.3.10

Home Assistant reserves a device unique ID while its setup flow remains open.
Starting another setup for the same child could therefore abort with the raw
`already_in_progress` message even though no Conti device entry had been created.
The screenshot establishes a conflicting flow; it does not establish why the
first gateway probe failed or whether the supplied credentials/CID are correct.

Gateway retries now use Home Assistant's `next_flow` mechanism to resume the
existing setup without opening another gateway socket or discarding its state.
Already-configured children still reject duplicate entries. Failed gateway probes
release their unique ID while keeping the form and entered values available for
correction. Other devices and reauthentication flows remain independent.

English UI translations are now shipped in `translations/en.json`, including
clear gateway versus child field labels and actionable setup-conflict messages.
After downloading 0.3.10, restart Home Assistant to load the fix and clear any
unfinished 0.3.9 flows. Try adding the child once. A subsequent gateway, key,
protocol, CID or DPS error requires the specific message/log to diagnose.

## Exact files changed

| File | Change |
| --- | --- |
| `custom_components/conti/gateway.py` | New shared TinyTuya parent/child transport, CID routing, discovery and metadata helpers. |
| `custom_components/conti/device_manager.py` | Register children, share parent sessions, serialize I/O, run one listener/recovery task per gateway and expose diagnostics. |
| `custom_components/conti/config_flow.py` | Connection choice, gateway/child picker, manual CID form, parent credential resolution, verified mapping and child-aware options rediscovery. |
| `custom_components/conti/const.py` | Connection, gateway, CID and metadata constants. |
| `custom_components/conti/__init__.py` | Pass child configuration to the manager, link registry devices and keep gateway-child runtime/availability local. |
| `custom_components/conti/switch.py` | Preserve one physical child device for all channels and its parent/model metadata. |
| `custom_components/conti/cloud_schema.py` | Preserve relationship metadata and use Tuya's documented gateway child-list endpoint. |
| `custom_components/conti/tuya_oauth.py` | Preserve SDK metadata and reuse the existing account/helper for child selection. |
| `custom_components/conti/diagnostics.py` | Export sanitized gateway connection, child count and error fields. |
| `custom_components/conti/strings.json` | Labels and gateway/child errors, including options validation and setup retry messages. |
| `custom_components/conti/translations/en.json` | Packaged English labels/errors for the Home Assistant frontend. |
| `custom_components/conti/manifest.json` | Bump integration version to `0.3.11` and require `tinytuya>=1.17.6`. |
| `pyproject.toml` | Match version `0.3.11` and the TinyTuya dependency floor. |
| `tests/test_gateway.py` | Mocked transport, flow, mapping, sharing, availability, recovery and concurrency coverage. |
| `tests/test_gateway_protocol.py` | Contract check against installed TinyTuya with sockets mocked. |
| `docs/gateway-child-testing.md` | This implementation report and physical test procedure. |

## Addressing, discovery and runtime

The installed dependency inspected was TinyTuya **1.17.6**. Its supported API is:

```python
parent = tinytuya.Device(gateway_device_id, gateway_host, gateway_local_key)
child = tinytuya.Device(child_device_id, parent=parent, cid=child_node_id)
```

The cloud device ID and Zigbee CID are independent. TinyTuya explicitly supports
`node_id` as a CID alias. Conti never substitutes the child cloud ID for a missing
CID and never treats an ambiguous `mesh_id` as a parent or node ID.

Child config entries add `connection_type = gateway_child`, `gateway_device_id`
and `child_cid`. The existing `device_id` remains the child ID. Existing `host`,
`port`, `local_key` and `protocol_version` fields describe the parent transport.
There is no second copy of the key within an entry. Each child entry remains
self-contained for independent Home Assistant reload/removal; siblings must use
consistent parent credentials. Runtime connection objects and credentials are
shared by gateway ID inside the existing DeviceManager.

Discovery sends CID-addressed status requests through TinyTuya. A successful
gateway shell response never supplies child DPS. Only packets carrying the
selected CID are accepted. Both top-level and nested `data` layouts, and
TinyTuya's queued response tuples, are handled. Discovery retains verified child
DPS across partial responses and requires a live child response before success.
Cloud mappings label only IDs actually observed locally. Missing/empty child
status cannot be bypassed by a cloud schema or assumed DP map.

Existing mapping and switch entity creation are reused. Boolean switch DPS at
arbitrary IDs are supported; no gateway, product, IP or channel IDs are hardcoded.
Review/manual mapping and options cannot introduce unobserved child DP IDs.
Guided Learn's existing direct-device probes are not used for gateway children.

All siblings use one persistent TinyTuya parent socket, one shared I/O lock and
one parent listener. Child commands use TinyTuya's CID-bearing control payloads
and the parent's session keys. The parent listener routes each update only to
the matching child's cache and coordinator. Removing a child keeps the socket
open for siblings; removing the last child closes it.

Gateway disconnection makes all its children unavailable. Recovery rebuilds the
parent once, recreates child handles, and refreshes each child separately. Cached
pre-disconnect DPS are retained as state but never prove recovery. Automatic
reconnect honors the existing setting: at least one child must have **Enable
automatic reconnect** enabled. The group uses Conti's existing 15-second initial
and 300-second maximum exponential backoff. Manual reconnect services also work.
An offline child does not independently reconnect its reachable gateway.

Supported gateway presence reports distinguish offline children from missing
CIDs. Such reports are refreshed locally every 30 seconds where supported.
Gateway heartbeats are sent locally. Normal child control, polling, push and
availability do not use Tuya cloud.

## Compatibility and limits

Entries without gateway fields still take the existing direct-device path. No
entry version bump or migration is required. The existing protocol wrapper,
entity implementations, direct onboarding and offline alert behavior are retained.
Previously passing tests remain passing. Physical relay regression tests were not run; only read-only gateway/child LAN status was checked.

Smart Life SDK/account metadata varies. Conti preserves parent, CID/node, product,
category and online hints when supplied. Project-based cloud onboarding can also
use `GET /v1.0/devices/{device_id}/sub-devices`. QR-only sessions use SDK metadata;
they are not assumed to authorize project OpenAPI calls. If parent credentials,
relationship or CID are unavailable, the advanced form requires them explicitly.

TinyTuya does not guarantee local child control for every gateway/firmware.
Some children sleep or report DPS only when operated. Setup requires locally
verified DPS; wake/operate such devices and retry. A gateway that never returns
an identifiable child status cannot be accepted by this implementation. Firmware
that exposes only some channels needs further investigation; Conti does not
invent the missing channels. A reachable TCP port alone does not prove protocol,
key, CID or child compatibility.

TinyTuya error **914** means key **or** protocol version, so Conti reports that
ambiguity instead of claiming a proven bad key. Explicit decryption/key errors,
network failures, missing CID, offline/missing children, empty discovery and
unsupported operations have separate errors. New transport diagnostics do not
include keys, tokens, raw exception text or raw response objects.

Protocol references:

- [TinyTuya 1.17.6 gateway example](https://github.com/jasonacox/tinytuya/blob/v1.17.6/examples/zigbee_gateway.py)
- [TinyTuya API](https://github.com/jasonacox/tinytuya/blob/master/API.md)
- [Tuya gateway child-list API](https://developer.tuya.com/en/docs/archived-documents/0b0f735629?id=Ka7kjwl8lvej4)

## Automated validation

| Check | Before edits | With gateway changes |
| --- | --- | --- |
| Existing/full pytest suite | 244 passed, 6 failed | 304 passed, same 6 failed |
| New gateway tests, including installed-library contract | Not present | 60 passed |
| Gateway + device manager + config flow + offline alert focused run | Not applicable | 117 passed |
| Full Ruff check | 178 existing findings | 172 existing findings; no findings on added/edited lines |
| Ruff on new gateway module/tests | Not applicable | Passed |
| Full mypy | 64 errors in 19 files | Same 64 errors in 19 files; new gateway module has no errors |
| Python compilation and `git diff --check` | Not recorded | Passed |

The full suite for 0.3.11 was run with `python -m pytest tests/ -q` and
completed with **304 passed, 6 failed**, and 156 warnings. Retry and diagnostic
regression tests all pass. The warnings are from existing pytest-asyncio fixtures.
The earlier 0.3.9 coverage run reported 38% overall integration coverage; coverage
was not recalculated for this setup-only hotfix. The 117-test focused run above
was also performed during 0.3.9 validation.

Lint and type-check commands match `.github/workflows/ci.yml`:

```text
python -m ruff check custom_components/ tests/
python -m mypy custom_components/conti/ --ignore-missing-imports
```

The six failures existed before edits and remain unchanged:

1. `tests/test_dp_mapping.py::TestAutoMapSwitch::test_multi_gang`
2. `tests/test_ir_cloud.py::test_ir_cloud_uses_oauth_for_library_fetches`
3. `tests/test_ir_cloud.py::test_ir_cloud_lists_device_remotes_first`
4. `tests/test_ir_cloud.py::test_oauth_resolves_infrared_id_from_ir_list`
5. `tests/test_ir_learning.py::test_learning_waits_captures_and_stops`
6. `tests/test_ir_learning.py::test_learning_stops_when_capture_fails`

The multi-gang test expects the old `power` names while the baseline implementation
already produces `switch_N`. The other failures concern pre-existing IR API/test
mismatches. These were not changed as part of gateway support. Repository-wide
CI is therefore still red. No physical hardware success is claimed.

## WG-Z1 and Quad Switch Pro 2 acceptance procedure

These are manual instructions for testing the reviewed changes. No installation
or test below has been performed on the live Home Assistant instance.

1. Use a test Home Assistant installation with LAN access to the WG-Z1. In HACS,
   open Conti and download/update to **v0.3.11**, then restart Home Assistant.
   Allow HA to install TinyTuya 1.17.6 or newer. If HACS still lists the older
   version, use Conti's **Update information** action, then select **v0.3.11**
   in the download/redownload version selector. Confirm the installed integration
   manifest reports `0.3.11` before starting the device tests.
2. Gather the **WG-Z1 gateway Device ID and gateway local key**, plus the Quad
   Switch Pro 2's **cloud Device ID and separate CID/node_id**. Prefer existing
   Smart Life metadata. If the SDK does not expose the CID, use Tuya device
   metadata or the `node_id` field from TinyTuya wizard's `devices.json`. Keep
   keys/tokens private. Do not assume the cloud child ID is its CID.
3. In the test HA, open **Settings → Devices & services → Add integration → Conti**.
   Choose **Local device / Manual** for a deterministic first local test, or
   **Login with Smart Life** to reuse account metadata.
4. Enter the child name **Quad Switch Pro 2**, choose device type **Switch** and
   connection type **Device behind Tuya Gateway**. Enter gateway address
   **192.168.20.160** and port **6668**.
5. For Smart Life, select the Quad child directly. If the gateway is identified
   from account relationships, selecting WG-Z1 offers its child picker. Confirm
   the automatically filled parent and CID fields. If metadata is incomplete,
   use the advanced manual option.
6. In **Configure gateway and child**, supply the WG-Z1 **gateway Device ID**, LAN
   address and **gateway local key**, then the Quad's **child Device ID** and
   **CID/node_id**. Start with protocol **auto**; use a known protocol override
   if necessary. Submit. Port 6668 being reachable is only the network precheck,
   not evidence that these identifiers or credentials are correct.
7. If discovery reports offline/empty child status, power or operate the Quad
   and retry. Read any specific gateway/key/version/CID error. Do not bypass it
   with an assumed mapping or change the child ID into a guessed CID.
8. Review the **actual discovered child DPS**. Expect one switch entity for
   each verified boolean channel, with no assumed DP numbers. If four real
   channel DPS are reported, accept the mapping and verify one physical Quad
   device with four channel entities, linked through the gateway registry device.
9. Open the child's Conti **Configure** options and enable **Enable automatic
   reconnect**. Keep the existing mapping. This setting is off by default to
   preserve Conti's current reconnect policy.
10. Toggle each channel on and off from HA, one at a time. Confirm the matching
    physical relay changes and sibling channels remain unchanged. Then operate
    each physical switch and confirm HA updates without reloading the integration.
11. If another child is available, add it with **Reuse configured gateway** and
    its own Device ID/CID. Download child diagnostics: `gateway_child_count`
    should reflect both children. Test commands and physical changes on each;
    only the addressed child's entities should update.
12. Restart the test HA. Confirm each child restores its own entities and
    receives fresh local state. Existing storage remains keyed by the **child**
    device ID through the `conti_<device_id>` storage key.
13. Temporarily disconnect or power-cycle the gateway. Confirm all its children
    become unavailable, then recover after the shared reconnect/backoff sequence
    when the gateway returns. An offline individual child should not take a
    reachable sibling offline. Manual recovery is also available through
    `conti.reconnect_device`.
14. After successful onboarding, temporarily block the test HA's Tuya-cloud/WAN
    access while preserving gateway LAN access. Repeat HA commands and physical
    button changes, then restart the test HA and repeat. Success here confirms
    this firmware's local behavior independently of cloud availability.
15. Record the detected gateway protocol, verified channel DP IDs, entity count,
    command results, physical push results, restart/reconnect results and redacted
    diagnostics. WG-Z1/Quad support is physically accepted only after these pass.
