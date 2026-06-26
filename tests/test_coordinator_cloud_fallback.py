"""Tests for DALI cloud-fallback coordinator routing."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.conti.coordinator import _cloud_fallback_is_active


def test_active_fallback_coordinator_uses_cloud_runtime() -> None:
    manager = MagicMock()
    manager.get_device_diagnostics.return_value = {
        "control_path": "cloud_fallback"
    }
    cloud = MagicMock()

    assert _cloud_fallback_is_active(manager, "dali1", cloud) is True


def test_local_recovery_switches_coordinator_back_to_local_runtime() -> None:
    manager = MagicMock()
    manager.get_device_diagnostics.return_value = {"control_path": "local"}
    cloud = MagicMock()

    assert _cloud_fallback_is_active(manager, "dali1", cloud) is False
