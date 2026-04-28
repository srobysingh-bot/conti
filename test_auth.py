"""Test script — validates Conti OAuth and cloud_schema auth functions.

Run with:  python test_auth.py

Tests:
1. TuyaCloudSchemaHelper: token obtain, sign request, refresh
2. TuyaOAuthManager: persistent storage cycle, token lifecycle
3. CloudDeviceRuntime: instantiation and DP mapping logic
4. Config-flow constants and import consistency
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import traceback

# ── Setup path ────────────────────────────────────────────────────────
# Allow running from workspace root
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_COMPONENTS = os.path.join(_SCRIPT_DIR, "custom_components")
if _COMPONENTS not in sys.path:
    sys.path.insert(0, _COMPONENTS)

_PASSED = 0
_FAILED = 0


def ok(name: str):
    global _PASSED
    _PASSED += 1
    print(f"  ✓ {name}")


def fail(name: str, err: str):
    global _FAILED
    _FAILED += 1
    print(f"  ✗ {name}: {err}")


# ======================================================================
# Test 1 — Imports and constants
# ======================================================================
print("\n=== Test 1: Imports & Constants ===")

try:
    from conti.const import (
        CONF_CLOUD_ACCESS_ID,
        CONF_CLOUD_ACCESS_SECRET,
        CONF_CLOUD_REGION,
        CONF_DEVICE_ID,
        CONF_DP_MAP,
        CONF_LOCAL_KEY,
        CONF_OAUTH_CONFIGURED,
        CONF_OAUTH_UID,
        CONF_RUNTIME_CHANNEL,
        DOMAIN,
        RUNTIME_CHANNEL_CLOUD,
        RUNTIME_CHANNEL_CLOUD_SENSOR,
        RUNTIME_CHANNEL_LOCAL,
    )
    ok("All new constants imported from const.py")
except ImportError as e:
    fail("Import const.py", str(e))

try:
    assert RUNTIME_CHANNEL_CLOUD == "cloud"
    assert RUNTIME_CHANNEL_LOCAL == "local"
    assert RUNTIME_CHANNEL_CLOUD_SENSOR == "cloud_sensor"
    assert CONF_OAUTH_CONFIGURED == "oauth_configured"
    assert CONF_OAUTH_UID == "oauth_uid"
    ok("Constant values correct")
except AssertionError as e:
    fail("Constant values", str(e))


# ======================================================================
# Test 2 — TuyaCloudSchemaHelper: sign_request
# ======================================================================
print("\n=== Test 2: TuyaCloudSchemaHelper._sign_request ===")

try:
    from conti.cloud_schema import TuyaCloudSchemaHelper

    helper = TuyaCloudSchemaHelper(
        access_id="test_access_id_123",
        access_secret="test_secret_456",
        region="eu",
    )
    ok("TuyaCloudSchemaHelper instantiated")
except Exception as e:
    fail("TuyaCloudSchemaHelper instantiation", str(e))

try:
    headers = helper._sign_request("GET", "/v1.0/token?grant_type=1", "")
    assert "client_id" in headers
    assert headers["client_id"] == "test_access_id_123"
    assert "sign" in headers
    assert "t" in headers
    assert headers["sign_method"] == "HMAC-SHA256"
    assert len(headers["sign"]) == 64  # SHA-256 hex digest
    assert headers["sign"] == headers["sign"].upper()  # Should be uppercase
    ok("Sign request produces valid headers")
except Exception as e:
    fail("Sign request", str(e))

# Verify sign is deterministic with same timestamp
try:
    # Manually compute expected sign
    t = headers["t"]
    path = "/v1.0/token?grant_type=1"
    body = ""
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    string_to_sign = f"GET\n{content_hash}\n\n{path}"
    sign_str = "test_access_id_123" + "" + t + string_to_sign
    expected = hmac.new(
        "test_secret_456".encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()
    assert headers["sign"] == expected, f"Sign mismatch: {headers['sign']} != {expected}"
    ok("HMAC-SHA256 signature verified independently")
except Exception as e:
    fail("Signature verification", str(e))

# Test with token (authenticated request)
try:
    headers_auth = helper._sign_request(
        "GET", "/v1.0/devices/abc123", "", token="mock_token"
    )
    assert headers_auth["access_token"] == "mock_token"
    assert headers_auth["client_id"] == "test_access_id_123"
    assert "sign" in headers_auth
    ok("Authenticated sign request includes access_token")
except Exception as e:
    fail("Authenticated sign request", str(e))


# ======================================================================
# Test 3 — TuyaCloudSchemaHelper: properties and restore_tokens
# ======================================================================
print("\n=== Test 3: TuyaCloudSchemaHelper token properties ===")

try:
    helper2 = TuyaCloudSchemaHelper("id2", "secret2", "us")
    assert helper2.access_token is None
    assert helper2.refresh_token is None
    assert helper2.token_expiry == 0.0
    assert helper2.uid is None
    ok("Initial token state is None/0.0")
except Exception as e:
    fail("Initial token state", str(e))

try:
    future = time.time() + 3600
    helper2.restore_tokens(
        access_token="at_123",
        refresh_token="rt_456",
        token_expiry=future,
        uid="uid_789",
    )
    assert helper2.access_token == "at_123"
    assert helper2.refresh_token == "rt_456"
    assert helper2.token_expiry == future
    assert helper2.uid == "uid_789"
    ok("restore_tokens sets all fields correctly")
except Exception as e:
    fail("restore_tokens", str(e))

# After restore, _ensure_token should return True (token still valid)
try:
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(helper2._ensure_token(strict=False))
    assert result is True, "Expected True for valid restored token"
    ok("_ensure_token returns True for valid restored token")
    loop.close()
except Exception as e:
    fail("_ensure_token with valid token", str(e))

# With expired token and no refresh, should try network (and fail safely)
try:
    helper3 = TuyaCloudSchemaHelper("id3", "secret3", "eu")
    helper3.restore_tokens("expired_tok", "", time.time() - 100)
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(helper3._ensure_token(strict=False))
    # Will fail because no real server, but should not crash
    # (returns False gracefully)
    assert result is False or result is True  # Either is fine for test
    ok("_ensure_token handles expired token without crash")
    loop.close()
except Exception as e:
    fail("_ensure_token expired token", str(e))


# ======================================================================
# Test 4 — TuyaCloudSchemaHelper: _try_refresh_token
# ======================================================================
print("\n=== Test 4: _try_refresh_token ===")

try:
    helper4 = TuyaCloudSchemaHelper("id4", "secret4", "eu")
    loop = asyncio.new_event_loop()
    # No refresh token → should return False
    result = loop.run_until_complete(helper4._try_refresh_token(strict=False))
    assert result is False
    ok("_try_refresh_token returns False when no refresh_token")
    loop.close()
except Exception as e:
    fail("_try_refresh_token no token", str(e))

try:
    helper5 = TuyaCloudSchemaHelper("id5", "secret5", "eu")
    helper5._refresh_token = "fake_refresh_token"
    loop = asyncio.new_event_loop()
    # Will fail (no real server) but should not crash
    result = loop.run_until_complete(helper5._try_refresh_token(strict=False))
    assert result is False  # Expected: network failure → False
    ok("_try_refresh_token handles network error gracefully")
    loop.close()
except Exception as e:
    fail("_try_refresh_token network error", str(e))


# ======================================================================
# Test 5 — TuyaCloudSchemaHelper: schema_to_dp_map
# ======================================================================
print("\n=== Test 5: schema_to_dp_map ===")

try:
    mock_schema = {
        "category": "dj",
        "product_id": "prod123",
        "functions": [
            {
                "code": "switch_led",
                "dp_id": "1",
                "type": "Boolean",
                "values": "{}",
            },
            {
                "code": "bright_value_v2",
                "dp_id": "2",
                "type": "Integer",
                "values": '{"min": 10, "max": 1000}',
            },
            {
                "code": "temp_value_v2",
                "dp_id": "3",
                "type": "Integer",
                "values": '{"min": 0, "max": 1000}',
            },
        ],
        "status": [],
    }

    dp_map, category, device_type_hint = TuyaCloudSchemaHelper.schema_to_dp_map(
        mock_schema
    )
    assert category == "dj"
    assert device_type_hint == "light"
    assert "1" in dp_map
    assert dp_map["1"]["key"] == "power"
    assert dp_map["1"]["type"] == "bool"
    ok("schema_to_dp_map converts light schema correctly")
except Exception as e:
    fail("schema_to_dp_map light", str(e))

try:
    assert "2" in dp_map
    assert dp_map["2"]["key"] == "brightness"
    assert dp_map["2"]["min"] == 10
    assert dp_map["2"]["max"] == 1000
    ok("schema_to_dp_map extracts range constraints")
except Exception as e:
    fail("schema_to_dp_map range", str(e))


# ======================================================================
# Test 6 — TuyaCloudSchemaHelper: error classes
# ======================================================================
print("\n=== Test 6: Cloud error classes ===")

try:
    from conti.cloud_schema import (
        TuyaCloudOnboardingError,
        TuyaCloudAuthError,
        TuyaCloudPermissionExpiredError,
        TuyaCloudRegionError,
        TuyaCloudParseError,
        TuyaCloudPaginationError,
        TuyaCloudPathError,
        TuyaCloudAPIError,
    )

    assert issubclass(TuyaCloudAuthError, TuyaCloudOnboardingError)
    assert issubclass(TuyaCloudPermissionExpiredError, TuyaCloudOnboardingError)
    assert issubclass(TuyaCloudRegionError, TuyaCloudOnboardingError)
    assert issubclass(TuyaCloudAPIError, TuyaCloudOnboardingError)
    ok("All error classes inherit from TuyaCloudOnboardingError")
except Exception as e:
    fail("Error class hierarchy", str(e))


# ======================================================================
# Test 7 — CloudDeviceRuntime: DP mapping logic
# ======================================================================
print("\n=== Test 7: CloudDeviceRuntime ===")

try:
    from conti.cloud_device_runtime import CloudDeviceRuntime

    class MockOAuth:
        async def async_get_device_status(self, device_id):
            return [
                {"code": "switch_led", "value": True},
                {"code": "bright_value_v2", "value": 500},
                {"code": "unknown_code", "value": 42},
            ]

    runtime = CloudDeviceRuntime(
        device_id="test_dev",
        oauth_manager=MockOAuth(),
        dp_map={
            "1": {"key": "power", "type": "bool", "code": "switch_led"},
            "2": {"key": "brightness", "type": "int", "code": "bright_value_v2"},
        },
    )
    ok("CloudDeviceRuntime instantiated")
except Exception as e:
    fail("CloudDeviceRuntime instantiation", str(e))

try:
    loop = asyncio.new_event_loop()
    dps = loop.run_until_complete(runtime.async_get_dps())
    assert "1" in dps
    assert dps["1"] is True
    assert "2" in dps
    assert dps["2"] == 500
    # unknown_code should be skipped
    assert len(dps) == 2
    ok("CloudDeviceRuntime.async_get_dps maps codes to DP IDs correctly")
    loop.close()
except Exception as e:
    fail("CloudDeviceRuntime.async_get_dps", str(e))

# Test with empty response
try:
    class MockOAuthEmpty:
        async def async_get_device_status(self, device_id):
            return []

    runtime_empty = CloudDeviceRuntime(
        device_id="test_dev",
        oauth_manager=MockOAuthEmpty(),
        dp_map={"1": {"key": "power", "type": "bool", "code": "switch_led"}},
    )
    loop = asyncio.new_event_loop()
    dps = loop.run_until_complete(runtime_empty.async_get_dps())
    assert dps == {}
    ok("CloudDeviceRuntime handles empty status gracefully")
    loop.close()
except Exception as e:
    fail("CloudDeviceRuntime empty status", str(e))


# ======================================================================
# Test 8 — LowPowerSensorCloudRuntime
# ======================================================================
print("\n=== Test 8: LowPowerSensorCloudRuntime ===")

try:
    from conti.low_power_runtime import LowPowerSensorCloudRuntime

    # Can't test full API call without real credentials,
    # but verify instantiation and code/key mapping.
    lp_rt = LowPowerSensorCloudRuntime(
        device_id="sensor_001",
        access_id="fake_id",
        access_secret="fake_secret",
        region="eu",
        dp_map={
            "1": {"key": "contact", "type": "bool", "code": "doorcontact_state"},
            "3": {"key": "battery", "type": "int", "code": "battery_percentage"},
        },
    )
    assert lp_rt._code_to_dp.get("doorcontact_state") == "1"
    assert lp_rt._code_to_dp.get("battery_percentage") == "3"
    assert "contact" in lp_rt._key_to_dp_ids
    assert "battery" in lp_rt._key_to_dp_ids
    ok("LowPowerSensorCloudRuntime code/key mapping correct")
except Exception as e:
    fail("LowPowerSensorCloudRuntime", str(e))


# ======================================================================
# Test 9 — _extract_device_list & _has_next_page
# ======================================================================
print("\n=== Test 9: Helper static methods ===")

try:
    # _extract_device_list
    payload_with_list = {
        "list": [
            {"id": "dev1", "name": "Light", "ip": "192.168.1.1", "category": "dj"},
            {"id": "dev2", "name": "Switch", "category": "kg"},
        ]
    }
    devices = TuyaCloudSchemaHelper._extract_device_list(payload_with_list)
    assert len(devices) == 2
    assert devices[0]["id"] == "dev1"
    assert devices[0]["name"] == "Light"
    assert devices[1]["ip"] == ""  # No IP provided
    ok("_extract_device_list normalizes device list")
except Exception as e:
    fail("_extract_device_list", str(e))

try:
    # None payload
    assert TuyaCloudSchemaHelper._extract_device_list(None) == []
    # Empty dict
    assert TuyaCloudSchemaHelper._extract_device_list({}) == []
    # Items with no id
    assert TuyaCloudSchemaHelper._extract_device_list({"list": [{"name": "no_id"}]}) == []
    ok("_extract_device_list handles edge cases")
except Exception as e:
    fail("_extract_device_list edge cases", str(e))

try:
    # _has_next_page
    assert TuyaCloudSchemaHelper._has_next_page({"has_more": True}, 1, 100, 100) is True
    assert TuyaCloudSchemaHelper._has_next_page({"has_more": False}, 1, 100, 100) is False
    assert TuyaCloudSchemaHelper._has_next_page({"total": 200}, 1, 100, 100) is True
    assert TuyaCloudSchemaHelper._has_next_page({"total": 50}, 1, 100, 50) is False
    assert TuyaCloudSchemaHelper._has_next_page(None, 1, 100, 0) is False
    ok("_has_next_page pagination logic correct")
except Exception as e:
    fail("_has_next_page", str(e))


# ======================================================================
# Test 10 — _raise_cloud_error_from_response
# ======================================================================
print("\n=== Test 10: Error mapping ===")

try:
    try:
        TuyaCloudSchemaHelper._raise_cloud_error_from_response(
            status=401, path="/v1.0/token", code="1010", msg="permission denied"
        )
    except TuyaCloudAuthError:
        ok("Code 1010 → TuyaCloudAuthError")

    try:
        TuyaCloudSchemaHelper._raise_cloud_error_from_response(
            status=200, path="/v1.0/devices", code="28841002",
            msg="Development plan permission has expired"
        )
    except TuyaCloudPermissionExpiredError:
        ok("Code 28841002 → TuyaCloudPermissionExpiredError")

    try:
        TuyaCloudSchemaHelper._raise_cloud_error_from_response(
            status=200, path="/v1.0/devices", code="1109",
            msg="device_ids param is illegal"
        )
    except TuyaCloudPathError:
        ok("Code 1109 → TuyaCloudPathError")

    try:
        TuyaCloudSchemaHelper._raise_cloud_error_from_response(
            status=200, path="/v1.0/devices", code="999",
            msg="Region mismatch"
        )
    except TuyaCloudRegionError:
        ok("'Region mismatch' msg → TuyaCloudRegionError")

except Exception as e:
    fail("Error mapping", str(e))


# ======================================================================
# Test 11 — Config entry data structure validation
# ======================================================================
print("\n=== Test 11: Config entry data shape ===")

try:
    # Simulate what _create_config_entry produces for local device
    local_entry = {
        CONF_DEVICE_ID: "dev123",
        "host": "192.168.1.100",
        "port": 6668,
        CONF_LOCAL_KEY: "abcdef1234567890",
        "protocol_version": "auto",
        "device_type": "light",
        CONF_DP_MAP: json.dumps({"1": {"key": "power", "type": "bool"}}),
        "mapping_source": "auto",
        "mapping_confidence": 0.85,
        CONF_RUNTIME_CHANNEL: RUNTIME_CHANNEL_LOCAL,
    }
    assert local_entry[CONF_RUNTIME_CHANNEL] == "local"
    assert local_entry[CONF_LOCAL_KEY] != ""
    ok("Local device entry has correct shape")

    # Cloud-only device
    cloud_entry = {
        CONF_DEVICE_ID: "dev456",
        "host": "",
        "port": 6668,
        CONF_LOCAL_KEY: "",
        "protocol_version": "auto",
        "device_type": "switch",
        CONF_DP_MAP: json.dumps({"1": {"key": "power", "type": "bool"}}),
        "mapping_source": "cloud",
        "mapping_confidence": 0.9,
        CONF_RUNTIME_CHANNEL: RUNTIME_CHANNEL_CLOUD,
        CONF_CLOUD_ACCESS_ID: "cloud_id",
        CONF_CLOUD_ACCESS_SECRET: "cloud_secret",
        CONF_CLOUD_REGION: "eu",
    }
    assert cloud_entry[CONF_RUNTIME_CHANNEL] == "cloud"
    assert cloud_entry[CONF_LOCAL_KEY] == ""
    ok("Cloud-only device entry has correct shape")

    # Low-power sensor
    sensor_entry = {
        CONF_DEVICE_ID: "sensor789",
        "host": "",
        "port": 6668,
        CONF_LOCAL_KEY: "",
        CONF_RUNTIME_CHANNEL: RUNTIME_CHANNEL_CLOUD_SENSOR,
        "low_power_device": True,
        "device_type": "sensor",
    }
    assert sensor_entry[CONF_RUNTIME_CHANNEL] == "cloud_sensor"
    ok("Low-power sensor entry has correct shape")

except Exception as e:
    fail("Config entry shape", str(e))


# ======================================================================
# Test 12 — Strings JSON validation
# ======================================================================
print("\n=== Test 12: strings.json completeness ===")

try:
    strings_path = os.path.join(
        _SCRIPT_DIR, "custom_components", "conti", "strings.json"
    )
    with open(strings_path, "r", encoding="utf-8") as f:
        strings = json.load(f)

    steps = strings["config"]["step"]
    required_steps = [
        "user", "oauth_login", "oauth_pick_device",
        "cloud_credentials", "manual_credentials", "cloud_pick_device",
        "confirm_host", "detect", "cloud_assist", "review",
        "pre_learn", "learn",
    ]
    for step_id in required_steps:
        assert step_id in steps, f"Missing step: {step_id}"
    ok(f"All {len(required_steps)} config step IDs present in strings.json")
except Exception as e:
    fail("strings.json steps", str(e))

try:
    errors = strings["config"]["error"]
    required_errors = [
        "cannot_connect", "device_not_responding", "device_unreachable_network",
        "port_blocked_local_unsupported",
        "invalid_auth", "wrong_protocol",
        "invalid_dp_map", "cloud_fetch_failed", "cloud_auth_failed",
        "cloud_permission_expired", "cloud_region_mismatch",
        "cloud_credentials_required", "cloud_no_device_match",
        "cloud_device_missing_local_key", "oauth_token_expired",
        "oauth_setup_failed",
    ]
    for err_key in required_errors:
        assert err_key in errors, f"Missing error: {err_key}"
    ok(f"All {len(required_errors)} error keys present in strings.json")
except Exception as e:
    fail("strings.json errors", str(e))


# ======================================================================
# Test 13 — manifest.json
# ======================================================================
print("\n=== Test 13: manifest.json ===")

try:
    manifest_path = os.path.join(
        _SCRIPT_DIR, "custom_components", "conti", "manifest.json"
    )
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    assert manifest["domain"] == "conti"
    assert manifest["config_flow"] is True
    assert manifest["version"] == "0.3.0"
    assert "cryptography>=41.0.0" in manifest["requirements"]
    assert "tinytuya>=1.13.0" in manifest["requirements"]
    ok("manifest.json: v0.3.0, domain=conti, requirements OK")
except Exception as e:
    fail("manifest.json", str(e))


# ======================================================================
# Summary
# ======================================================================
print(f"\n{'='*50}")
print(f"Results: {_PASSED} passed, {_FAILED} failed")
if _FAILED == 0:
    print("ALL TESTS PASSED ✓")
else:
    print(f"FAILURES: {_FAILED}")
    sys.exit(1)
