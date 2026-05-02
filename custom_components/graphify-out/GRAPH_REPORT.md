# Graph Report - custom_components  (2026-05-02)

## Corpus Check
- 40 files · ~84,130 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 937 nodes · 2772 edges · 45 communities detected
- Extraction: 47% EXTRACTED · 53% INFERRED · 0% AMBIGUOUS · INFERRED: 1476 edges (avg confidence: 0.52)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]

## God Nodes (most connected - your core abstractions)
1. `TuyaCloudSchemaHelper` - 117 edges
2. `TinyTuyaDevice` - 105 edges
3. `TuyaIRCloud` - 103 edges
4. `IRStorage` - 99 edges
5. `TuyaOAuthManager` - 94 edges
6. `ContiCoordinator` - 73 edges
7. `TuyaCloudAuthError` - 69 edges
8. `IRLearningSession` - 68 edges
9. `TuyaCloudParseError` - 67 edges
10. `TuyaCloudAPIError` - 67 edges

## Surprising Connections (you probably didn't know these)
- `Climate (AC) platform for Conti.  Maps Tuya DPs to HA :class:`ClimateEntity`:` --uses--> `ContiCoordinator`  [INFERRED]
  climate.py → coordinator.py
- `Representation of a Tuya AC / climate device.` --uses--> `ContiCoordinator`  [INFERRED]
  climate.py → coordinator.py
- `Runtime polling helper for low-power Tuya Wi-Fi sensors.  This path is used on` --uses--> `TuyaCloudSchemaHelper`  [INFERRED]
  low_power_runtime.py → cloud_schema.py
- `Map Tuya cloud status codes to Conti DP IDs for sleepy sensors.` --uses--> `TuyaCloudSchemaHelper`  [INFERRED]
  low_power_runtime.py → cloud_schema.py
- `Fetch cloud status and translate it into a DP dictionary.` --uses--> `TuyaCloudSchemaHelper`  [INFERRED]
  low_power_runtime.py → cloud_schema.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (116): ABC, IntEnum, NamedTuple, Tuya protocol constants, command codes, frame structures, and protocol ABC.  A, Perform session negotiation.  Returns ``True`` on success., Reset any session state (called on reconnect)., Tuya protocol command identifiers., Parsed Tuya protocol frame. (+108 more)

### Community 1 - "Community 1"
Cohesion: 0.1
Nodes (108): _extract_device_list(), _raise_cloud_error_from_response(), Obtain or refresh an access token from Tuya Cloud., Attempt to refresh the access token using the stored refresh_token., Discover the primary user UID from associated users.          Returns the firs, List devices for a specific user via /v1.0/users/{uid}/devices., Generate Tuya Cloud API HMAC-SHA256 signature headers., Fetch the DP schema for a device from Tuya Cloud.          Returns a dict with (+100 more)

### Community 2 - "Community 2"
Cohesion: 0.03
Nodes (74): Detect external power changes and log to HA Activity panel., CloudDeviceRuntime, Poll Tuya Cloud status for a device via the global OAuth manager., Fetch cloud status and translate into a DP dictionary., ContiCoordinator, Fetch data for this coordinator's device.          Falls back to the cached DPs, Availability helper used by entities., Detect state changes in low-power sensors and fire activity events. (+66 more)

### Community 3 - "Community 3"
Cohesion: 0.04
Nodes (61): Runtime cloud polling for devices without local access.  Used for devices disc, _async_probe_tcp(), _classify_tcp_connect_error(), _cloud_credentials_schema(), _cloud_error_key(), ContiConfigFlow, _discover_confident_lan_host(), _is_incomplete_cct_mapping() (+53 more)

### Community 4 - "Community 4"
Cohesion: 0.03
Nodes (57): BaseContiLight, DataUpdateCoordinator for Conti.  Each config entry (= one device) creates its o, External-on correction profile engine for Conti lights.  When a light is turne, Return the first rule that matches the current local time.      Parameters, resolve_active_rule(), async_setup_entry(), Light platform for Conti.  This module is the Home Assistant platform entry-po, Set up Conti light entities from a config entry. (+49 more)

### Community 5 - "Community 5"
Cohesion: 0.05
Nodes (32): build_action_plan(), classify_device_family(), _classify_light_from_dps(), _classify_value(), _count_bool_dps(), _count_switch_channel_bool_dps(), describe_missing(), family_display_name() (+24 more)

### Community 6 - "Community 6"
Cohesion: 0.05
Nodes (30): ir_action_aliases(), normalize_ir_action(), IR action name normalization for Conti., Return the canonical Conti IR action name., Return all known aliases for an action, including the canonical name., _coerce_list(), _command_action(), _normalize_commands() (+22 more)

### Community 7 - "Community 7"
Cohesion: 0.06
Nodes (18): _build_sharing_schema(), Tuya Smart Life OAuth manager with persistent storage and auto-refresh.  Store, Persist current credentials and tokens., Configure with new credentials, authenticate, and persist.          Returns Tr, Generate a QR code for Smart Life app authorization.          Uses the shared, Poll QR code scan status.          Returns the user UID if the QR code has bee, Ensure a valid token exists, refreshing if needed.          In QR mode (no pro, Refresh a QR-login access token using the stored refresh_token.          Mirro (+10 more)

### Community 8 - "Community 8"
Cohesion: 0.09
Nodes (10): async_setup_entry(), ContiFan, _handle_coordinator_update(), Fan platform for Conti.  Maps Tuya DPs to HA :class:`FanEntity`: * On/off, Convert raw speed DP to HA percentage (0-100)., Convert HA percentage (0-100) to device native speed value., Read raw DPs and update normalized cached state., Turn oscillation on or off. (+2 more)

### Community 9 - "Community 9"
Cohesion: 0.14
Nodes (12): async_setup_entry(), available(), ContiSwitch, is_on(), Switch platform for Conti.  Creates a :class:`SwitchEntity` for every boolean, Representation of a Tuya switch / smart plug (single channel)., Accept coordinator data but let ``is_on`` filter stale values., Optimistically update UI and send command immediately. (+4 more)

### Community 10 - "Community 10"
Cohesion: 0.16
Nodes (10): ClimateEntity, async_setup_entry(), ContiClimate, current_temperature(), fan_mode(), _handle_coordinator_update(), hvac_mode(), Climate (AC) platform for Conti.  Maps Tuya DPs to HA :class:`ClimateEntity`: (+2 more)

### Community 11 - "Community 11"
Cohesion: 0.15
Nodes (7): _category_to_device_type(), get_login_qr_code(), _has_next_page(), Tuya Cloud helper for onboarding and low-power sensor status polling.  This mo, Map a Tuya product category to a Conti device type., schema_to_dp_map(), Runtime polling helper for low-power Tuya Wi-Fi sensors.  This path is used on

### Community 12 - "Community 12"
Cohesion: 0.2
Nodes (5): Synchronous connection attempt for a single protocol version., Close the connection and release the socket., Synchronous status query — returns DP dict or ``{}``.          Parameters, Query the device for current DP values., Query status; return cached DPS when the live query is empty.

### Community 13 - "Community 13"
Cohesion: 0.25
Nodes (5): async_setup_entry(), ContiSensor, Sensor platform for Conti.  Creates one HA sensor entity per sensor-type DP in, A single sensor value from a Tuya device., SensorEntity

### Community 14 - "Community 14"
Cohesion: 0.5
Nodes (2): Enable IR learning mode and return the learning timestamp., Make an authenticated PUT request using the existing helper.

### Community 15 - "Community 15"
Cohesion: 0.67
Nodes (1): Send a stored command through Tuya Cloud.

### Community 16 - "Community 16"
Cohesion: 1.0
Nodes (1): Restore previously saved tokens (from persistent storage).

### Community 17 - "Community 17"
Cohesion: 1.0
Nodes (1): Compare *new_dps* against baseline and return changed DPs.          Returns li

### Community 18 - "Community 18"
Cohesion: 1.0
Nodes (1): Set which role we're waiting for the user to demonstrate.

### Community 19 - "Community 19"
Cohesion: 1.0
Nodes (1): Tell TinyTuya which DPs to include in status queries.          Must be called

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (1): Register a disconnect callback (no-op — detected via poll).

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): Connect to the device.          When ``version`` is ``"auto"``, versions are t

### Community 22 - "Community 22"
Cohesion: 1.0
Nodes (1): Set a single DP on the device using fire-and-forget CONTROL.

### Community 23 - "Community 23"
Cohesion: 1.0
Nodes (1): Auto-detect available data-points on the device.          Uses ``detect_availa

### Community 24 - "Community 24"
Cohesion: 1.0
Nodes (1): Async wrapper — check for pending unsolicited data.

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (1): Register a push callback (no-op — TinyTuya is polled).

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (1): Set multiple DP values in a single fire-and-forget CONTROL.

### Community 27 - "Community 27"
Cohesion: 1.0
Nodes (1): Non-blocking check for unsolicited data on the persistent socket.          Set

### Community 28 - "Community 28"
Cohesion: 1.0
Nodes (1): Return the current access token.

### Community 29 - "Community 29"
Cohesion: 1.0
Nodes (1): Return the current refresh token.

### Community 30 - "Community 30"
Cohesion: 1.0
Nodes (1): Return the token expiry timestamp.

### Community 31 - "Community 31"
Cohesion: 1.0
Nodes (1): Return the discovered user UID.

### Community 32 - "Community 32"
Cohesion: 1.0
Nodes (1): Request a QR code for Smart Life app authorization.          Uses the centrali

### Community 33 - "Community 33"
Cohesion: 1.0
Nodes (1): Poll the QR code scan status.          Uses the centralised Tuya QR-login gate

### Community 34 - "Community 34"
Cohesion: 1.0
Nodes (1): Map Tuya HTTP/code/message into specific onboarding exceptions.

### Community 35 - "Community 35"
Cohesion: 1.0
Nodes (1): Extract/normalize device list from different Tuya response shapes.

### Community 36 - "Community 36"
Cohesion: 1.0
Nodes (1): Infer whether a paged list endpoint likely has another page.

### Community 37 - "Community 37"
Cohesion: 1.0
Nodes (1): Convert a Tuya cloud schema into a Conti dp_map.          Returns ``(dp_map, c

### Community 38 - "Community 38"
Cohesion: 1.0
Nodes (1): Current DP baseline snapshot.

### Community 39 - "Community 39"
Cohesion: 1.0
Nodes (1): DP map built so far from learned DPs.

### Community 40 - "Community 40"
Cohesion: 1.0
Nodes (1): Set of DP roles (keys) already learned.

### Community 41 - "Community 41"
Cohesion: 1.0
Nodes (1): Protocol version string (e.g. ``'3.3'``, ``'3.5'``).

### Community 42 - "Community 42"
Cohesion: 1.0
Nodes (1): Whether this protocol requires session negotiation.

### Community 43 - "Community 43"
Cohesion: 1.0
Nodes (1): Encode a command + payload into a wire-format frame.

### Community 44 - "Community 44"
Cohesion: 1.0
Nodes (1): Decode raw bytes into a ``TuyaFrame``, or ``None`` on failure.

## Knowledge Gaps
- **147 isolated node(s):** `Detect external power changes and log to HA Activity panel.`, `Runtime cloud polling for devices without local access.  Used for devices disc`, `Poll Tuya Cloud status for a device via the global OAuth manager.`, `Fetch cloud status and translate into a DP dictionary.`, `Tuya Cloud helper for onboarding and low-power sensor status polling.  This mo` (+142 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 14`** (4 nodes): `Enable IR learning mode and return the learning timestamp.`, `Make an authenticated PUT request using the existing helper.`, `._api_put()`, `.start_learning()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 15`** (3 nodes): `Send a stored command through Tuya Cloud.`, `._api_post()`, `.send_command()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 16`** (2 nodes): `Restore previously saved tokens (from persistent storage).`, `.restore_tokens()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 17`** (2 nodes): `.apply_diff()`, `Compare *new_dps* against baseline and return changed DPs.          Returns li`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 18`** (2 nodes): `.set_pending_role()`, `Set which role we're waiting for the user to demonstrate.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 19`** (2 nodes): `Tell TinyTuya which DPs to include in status queries.          Must be called`, `.set_monitored_dp_ids()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 20`** (2 nodes): `Register a disconnect callback (no-op — detected via poll).`, `.set_disconnect_callback()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 21`** (2 nodes): `Connect to the device.          When ``version`` is ``"auto"``, versions are t`, `.connect()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 22`** (2 nodes): `Set a single DP on the device using fire-and-forget CONTROL.`, `.set_dp()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 23`** (2 nodes): `Auto-detect available data-points on the device.          Uses ``detect_availa`, `.detect_dps()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 24`** (2 nodes): `Async wrapper — check for pending unsolicited data.`, `.receive_nowait()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 25`** (2 nodes): `Register a push callback (no-op — TinyTuya is polled).`, `.set_dp_callback()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (2 nodes): `Set multiple DP values in a single fire-and-forget CONTROL.`, `.set_dps()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 27`** (2 nodes): `Non-blocking check for unsolicited data on the persistent socket.          Set`, `._receive_nowait_sync()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 28`** (1 nodes): `Return the current access token.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 29`** (1 nodes): `Return the current refresh token.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 30`** (1 nodes): `Return the token expiry timestamp.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 31`** (1 nodes): `Return the discovered user UID.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 32`** (1 nodes): `Request a QR code for Smart Life app authorization.          Uses the centrali`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 33`** (1 nodes): `Poll the QR code scan status.          Uses the centralised Tuya QR-login gate`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 34`** (1 nodes): `Map Tuya HTTP/code/message into specific onboarding exceptions.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 35`** (1 nodes): `Extract/normalize device list from different Tuya response shapes.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 36`** (1 nodes): `Infer whether a paged list endpoint likely has another page.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 37`** (1 nodes): `Convert a Tuya cloud schema into a Conti dp_map.          Returns ``(dp_map, c`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 38`** (1 nodes): `Current DP baseline snapshot.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 39`** (1 nodes): `DP map built so far from learned DPs.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 40`** (1 nodes): `Set of DP roles (keys) already learned.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 41`** (1 nodes): `Protocol version string (e.g. ``'3.3'``, ``'3.5'``).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 42`** (1 nodes): `Whether this protocol requires session negotiation.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 43`** (1 nodes): `Encode a command + payload into a wire-format frame.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 44`** (1 nodes): `Decode raw bytes into a ``TuyaFrame``, or ``None`` on failure.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ContiCoordinator` connect `Community 2` to `Community 4`, `Community 8`, `Community 9`, `Community 10`, `Community 13`?**
  _High betweenness centrality (0.258) - this node is a cross-community bridge._
- **Why does `TuyaIRCloud` connect `Community 1` to `Community 2`, `Community 3`, `Community 6`, `Community 14`, `Community 15`?**
  _High betweenness centrality (0.202) - this node is a cross-community bridge._
- **Why does `IRStorage` connect `Community 1` to `Community 2`, `Community 3`, `Community 6`?**
  _High betweenness centrality (0.191) - this node is a cross-community bridge._
- **Are the 101 inferred relationships involving `TuyaCloudSchemaHelper` (e.g. with `ContiConfigFlow` and `ContiOptionsFlow`) actually correct?**
  _`TuyaCloudSchemaHelper` has 101 INFERRED edges - model-reasoned connections that need verification._
- **Are the 88 inferred relationships involving `TinyTuyaDevice` (e.g. with `ContiConfigFlow` and `ContiOptionsFlow`) actually correct?**
  _`TinyTuyaDevice` has 88 INFERRED edges - model-reasoned connections that need verification._
- **Are the 90 inferred relationships involving `TuyaIRCloud` (e.g. with `ContiConfigFlow` and `ContiOptionsFlow`) actually correct?**
  _`TuyaIRCloud` has 90 INFERRED edges - model-reasoned connections that need verification._
- **Are the 91 inferred relationships involving `IRStorage` (e.g. with `ContiConfigFlow` and `ContiOptionsFlow`) actually correct?**
  _`IRStorage` has 91 INFERRED edges - model-reasoned connections that need verification._