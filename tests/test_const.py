"""Tests for const.py — sanity checks on constants."""

from __future__ import annotations

from custom_components.conti.const import (
    DEFAULT_PORT,
    DEFAULT_PROTOCOL_VERSION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
    SUPPORTED_DEVICE_TYPES,
    SUPPORTED_VERSIONS,
)


class TestConstants:
    def test_domain(self) -> None:
        assert DOMAIN == "conti"

    def test_default_port(self) -> None:
        assert DEFAULT_PORT == 6668

    def test_default_version(self) -> None:
        assert DEFAULT_PROTOCOL_VERSION == "auto"

    def test_supported_versions(self) -> None:
        assert "auto" in SUPPORTED_VERSIONS
        assert "3.3" in SUPPORTED_VERSIONS
        assert "3.4" in SUPPORTED_VERSIONS
        assert "3.5" in SUPPORTED_VERSIONS

    def test_platforms_not_empty(self) -> None:
        assert len(PLATFORMS) >= 1
        for p in PLATFORMS:
            assert isinstance(p, str)

    def test_device_types(self) -> None:
        expected = {"light", "fan", "climate", "switch", "sensor"}
        assert set(SUPPORTED_DEVICE_TYPES) == expected

    def test_scan_interval_positive(self) -> None:
        assert DEFAULT_SCAN_INTERVAL > 0
