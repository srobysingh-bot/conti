"""Tests for tuya_protocol.base — constants and data structures."""

from __future__ import annotations

from custom_components.conti.tuya_protocol.base import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    HEADER_SIZE,
    HEARTBEAT_INTERVAL,
    MIN_FRAME_SIZE,
    PREFIX_BYTES,
    PREFIX_VALUE,
    PROTO_31,
    PROTO_33,
    PROTO_34,
    SUFFIX_BYTES,
    SUFFIX_VALUE,
    TuyaCommand,
    TuyaFrame,
    VERSION_HEADER_33,
    VERSION_HEADER_34,
)


class TestBaseConstants:
    def test_prefix_suffix_length(self) -> None:
        assert len(PREFIX_BYTES) == 4
        assert len(SUFFIX_BYTES) == 4

    def test_prefix_value(self) -> None:
        assert int.from_bytes(PREFIX_BYTES, "big") == PREFIX_VALUE

    def test_suffix_value(self) -> None:
        assert int.from_bytes(SUFFIX_BYTES, "big") == SUFFIX_VALUE

    def test_header_size(self) -> None:
        assert HEADER_SIZE == 16

    def test_version_headers(self) -> None:
        assert len(VERSION_HEADER_33) == 15
        assert VERSION_HEADER_33[:3] == b"3.3"
        assert len(VERSION_HEADER_34) == 15
        assert VERSION_HEADER_34[:3] == b"3.4"


class TestTuyaCommand:
    def test_heartbeat(self) -> None:
        assert TuyaCommand.HEARTBEAT == 0x09

    def test_control(self) -> None:
        assert TuyaCommand.CONTROL == 0x07

    def test_status(self) -> None:
        assert TuyaCommand.STATUS == 0x08

    def test_dp_query(self) -> None:
        assert TuyaCommand.DP_QUERY == 0x0A


class TestTuyaFrame:
    def test_named_tuple(self) -> None:
        f = TuyaFrame(seqno=1, cmd=7, retcode=0, payload=b"hello")
        assert f.seqno == 1
        assert f.cmd == 7
        assert f.retcode == 0
        assert f.payload == b"hello"
