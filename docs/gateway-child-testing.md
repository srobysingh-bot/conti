# Conti gateway-child implementation and acceptance test

## Checkout and scope

Before editing, `git fetch origin` and `git ls-remote origin HEAD` confirmed that
local `main`, `origin/main`, and GitHub HEAD were all
`caff504c943dafa6ef899a9734b0002fc39c17c4`:
**feat: add global offline alert settings for Conti**. The working tree was clean.
The offline alert changes in that commit are preserved.

This implementation is packaged as **Conti 0.3.10**, tagged `v0.3.10`, for
download through HACS. No installation or deployment to Home Assistant was
performed during development or release. The real gateway was not contacted.

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
| `custom_components/conti/manifest.json` | Bump integration version to `0.3.10` and require `tinytuya>=1.17.6`. |
| `pyproject.toml` | Match version `0.3.10` and the TinyTuya dependency floor. |
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
Previously passing tests remain passing. Physical regression tests were not run.

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
| Existing/full pytest suite | 244 passed, 6 failed | 297 passed, same 6 failed |
| New gateway tests, including installed-library contract | Not present | 53 passed |
| Gateway + device manager + config flow + offline alert focused run | Not applicable | 117 passed |
| Full Ruff check | 178 existing findings | 172 existing findings; no findings on added/edited lines |
| Ruff on new gateway module/tests | Not applicable | Passed |
| Full mypy | 64 errors in 19 files | Same 64 errors in 19 files; new gateway module has no errors |
| Python compilation and `git diff --check` | Not recorded | Passed |

The full suite for 0.3.10 was run with `python -m pytest tests/ -q` and
completed with **297 passed, 6 failed**, and 151 warnings. The five added retry
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
   open Conti and download/update to **v0.3.10**, then restart Home Assistant.
   Allow HA to install TinyTuya 1.17.6 or newer. If HACS still lists the older
   version, use Conti's **Update information** action, then select **v0.3.10**
   in the download/redownload version selector. Confirm the installed integration
   manifest reports `0.3.10` before starting the device tests.
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
