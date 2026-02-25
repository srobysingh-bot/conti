"""Shared test fixtures for Conti integration tests."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub the 'homeassistant' namespace so imports of custom_components.conti
# succeed outside a real Home Assistant environment.
#
# MagicMock modules auto-create any attribute on access, which satisfies
# arbitrary ``from homeassistant.X import Y`` statements.
# ---------------------------------------------------------------------------

_HA_MODULES = [
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.storage",
    "homeassistant.components",
    "homeassistant.components.climate",
    "homeassistant.components.fan",
    "homeassistant.components.light",
    "homeassistant.components.sensor",
    "homeassistant.components.switch",
    "homeassistant.data_entry_flow",
    "homeassistant.exceptions",
    "homeassistant.util",
    "homeassistant.util.percentage",
]

for _mod_name in _HA_MODULES:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

# ---------------------------------------------------------------------------
# Stub 'tinytuya' so the TinyTuyaDevice wrapper can be imported even
# when tinytuya is not installed in the test environment.
# ---------------------------------------------------------------------------
if "tinytuya" not in sys.modules:
    _tinytuya_mock = MagicMock()
    _tinytuya_mock.CONTROL = 7  # real tinytuya constant for CONTROL cmd
    sys.modules["tinytuya"] = _tinytuya_mock

import pytest


@pytest.fixture
def local_key() -> str:
    """A 16-character test local key."""
    return "0123456789abcdef"


@pytest.fixture
def local_key_bytes(local_key: str) -> bytes:
    return local_key.encode()


@pytest.fixture
def device_id() -> str:
    return "test_device_001"


@pytest.fixture
def device_config(device_id: str, local_key: str) -> dict:
    """Minimal device config dict for DeviceManager."""
    return {
        "device_id": device_id,
        "host": "192.168.1.100",
        "local_key": local_key,
        "protocol_version": "3.3",
        "port": 6668,
    }
