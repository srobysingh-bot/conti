"""Shared test fixtures for Conti integration tests."""

from __future__ import annotations

from enum import Enum, IntFlag
import sys
import types
from unittest.mock import MagicMock

import pytest

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
    "homeassistant.components.remote",
    "homeassistant.components.climate",
    "homeassistant.components.cover",
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


class _Platform(str, Enum):
    LIGHT = "light"
    SWITCH = "switch"
    SENSOR = "sensor"
    FAN = "fan"
    CLIMATE = "climate"
    REMOTE = "remote"
    COVER = "cover"


class _CoverEntityFeature(IntFlag):
    OPEN = 1
    CLOSE = 2
    SET_POSITION = 4
    STOP = 8


class _CoverDeviceClass(str, Enum):
    CURTAIN = "curtain"


class _CoverState(str, Enum):
    CLOSED = "closed"
    CLOSING = "closing"
    OPEN = "open"
    OPENING = "opening"


class _CoverEntity:
    pass


class _CoordinatorEntity:
    def __init__(self, coordinator=None) -> None:
        self.coordinator = coordinator
        self.hass = getattr(coordinator, "hass", MagicMock())

    def async_write_ha_state(self) -> None:
        return None

    @classmethod
    def __class_getitem__(cls, item):
        return cls


sys.modules["homeassistant.const"].Platform = _Platform
sys.modules[
    "homeassistant.helpers.update_coordinator"
].CoordinatorEntity = _CoordinatorEntity
cover_mod = types.ModuleType("homeassistant.components.cover")
cover_mod.CoverDeviceClass = _CoverDeviceClass
cover_mod.CoverEntity = _CoverEntity
cover_mod.CoverEntityFeature = _CoverEntityFeature
cover_mod.CoverState = _CoverState
sys.modules["homeassistant.components.cover"] = cover_mod

# ---------------------------------------------------------------------------
# Stub 'tinytuya' so the TinyTuyaDevice wrapper can be imported even
# when tinytuya is not installed in the test environment.
# ---------------------------------------------------------------------------
if "tinytuya" not in sys.modules:
    _tinytuya_mock = MagicMock()
    _tinytuya_mock.CONTROL = 7  # real tinytuya constant for CONTROL cmd
    sys.modules["tinytuya"] = _tinytuya_mock


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
