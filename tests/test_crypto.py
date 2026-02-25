"""Tests for tuya_protocol.crypto — AES, CRC, frame pack/unpack."""

from __future__ import annotations

import json
import struct

import pytest

from custom_components.conti.tuya_protocol.base import (
    HEADER_SIZE,
    PREFIX_BYTES,
    PREFIX_VALUE,
    SUFFIX_BYTES,
    TuyaCommand,
    TuyaFrame,
    VERSION_HEADER_33,
    VERSION_HEADER_34,
)
from custom_components.conti.tuya_protocol.crypto import (
    aes_decrypt,
    aes_encrypt,
    crc32,
    pack_frame,
    unpack_frame,
)


# ---------------------------------------------------------------------------
# AES encrypt / decrypt
# ---------------------------------------------------------------------------


class TestAES:
    def test_roundtrip(self, local_key_bytes: bytes) -> None:
        plaintext = b"Hello, Tuya!"
        ct = aes_encrypt(plaintext, local_key_bytes)
        assert ct != plaintext
        assert aes_decrypt(ct, local_key_bytes) == plaintext

    def test_roundtrip_json(self, local_key_bytes: bytes) -> None:
        payload = json.dumps({"dps": {"1": True}}).encode()
        ct = aes_encrypt(payload, local_key_bytes)
        assert aes_decrypt(ct, local_key_bytes) == payload

    def test_padding_multiple_blocks(self, local_key_bytes: bytes) -> None:
        """16-byte-aligned input should still round-trip (full pad block added)."""
        data = b"A" * 16
        assert aes_decrypt(aes_encrypt(data, local_key_bytes), local_key_bytes) == data

    def test_empty_plaintext(self, local_key_bytes: bytes) -> None:
        ct = aes_encrypt(b"", local_key_bytes)
        assert aes_decrypt(ct, local_key_bytes) == b""


# ---------------------------------------------------------------------------
# CRC-32
# ---------------------------------------------------------------------------


class TestCRC32:
    def test_deterministic(self) -> None:
        assert crc32(b"hello") == crc32(b"hello")

    def test_different_data(self) -> None:
        assert crc32(b"hello") != crc32(b"world")

    def test_unsigned(self) -> None:
        """Result must always be unsigned 32-bit."""
        val = crc32(b"\xff" * 256)
        assert 0 <= val <= 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Frame pack / unpack  — v3.3
# ---------------------------------------------------------------------------


class TestFrameV33:
    def test_roundtrip_control(self, local_key_bytes: bytes) -> None:
        payload = json.dumps({"dps": {"1": True}}).encode()
        frame_bytes = pack_frame(
            cmd=TuyaCommand.CONTROL,
            payload=payload,
            seqno=1,
            local_key=local_key_bytes,
            version="3.3",
        )
        assert frame_bytes[:4] == PREFIX_BYTES
        assert frame_bytes[-4:] == SUFFIX_BYTES

        parsed = unpack_frame(frame_bytes, local_key_bytes)
        assert parsed is not None
        assert parsed.seqno == 1
        assert parsed.cmd == TuyaCommand.CONTROL
        assert json.loads(parsed.payload) == {"dps": {"1": True}}

    def test_roundtrip_dp_query(self, local_key_bytes: bytes) -> None:
        payload = json.dumps({"gwId": "abc", "devId": "abc"}).encode()
        raw = pack_frame(
            cmd=TuyaCommand.DP_QUERY,
            payload=payload,
            seqno=42,
            local_key=local_key_bytes,
            version="3.3",
        )
        parsed = unpack_frame(raw, local_key_bytes)
        assert parsed is not None
        assert parsed.seqno == 42
        assert parsed.cmd == TuyaCommand.DP_QUERY

    def test_heartbeat(self, local_key_bytes: bytes) -> None:
        raw = pack_frame(
            cmd=TuyaCommand.HEARTBEAT,
            payload=b"",
            seqno=5,
            local_key=local_key_bytes,
            version="3.3",
        )
        parsed = unpack_frame(raw, local_key_bytes)
        assert parsed is not None
        assert parsed.cmd == TuyaCommand.HEARTBEAT
        assert parsed.seqno == 5


# ---------------------------------------------------------------------------
# Frame pack / unpack  — v3.1
# ---------------------------------------------------------------------------


class TestFrameV31:
    def test_control_roundtrip(self, local_key_bytes: bytes) -> None:
        payload = json.dumps({"dps": {"1": False}}).encode()
        raw = pack_frame(
            cmd=TuyaCommand.CONTROL,
            payload=payload,
            seqno=10,
            local_key=local_key_bytes,
            version="3.1",
        )
        parsed = unpack_frame(raw, local_key_bytes)
        assert parsed is not None
        assert parsed.seqno == 10
        assert parsed.cmd == TuyaCommand.CONTROL

    def test_query_no_encryption(self, local_key_bytes: bytes) -> None:
        """v3.1 DP_QUERY is sent plaintext."""
        payload = json.dumps({"gwId": "dev1", "devId": "dev1"}).encode()
        raw = pack_frame(
            cmd=TuyaCommand.DP_QUERY,
            payload=payload,
            seqno=1,
            local_key=local_key_bytes,
            version="3.1",
        )
        parsed = unpack_frame(raw, local_key_bytes)
        assert parsed is not None
        assert b"gwId" in parsed.payload


# ---------------------------------------------------------------------------
# Frame pack / unpack  — v3.4
# ---------------------------------------------------------------------------


class TestFrameV34:
    def test_roundtrip(self, local_key_bytes: bytes) -> None:
        payload = json.dumps({"dps": {"1": 255}}).encode()
        raw = pack_frame(
            cmd=TuyaCommand.CONTROL,
            payload=payload,
            seqno=99,
            local_key=local_key_bytes,
            version="3.4",
        )
        assert raw[:4] == PREFIX_BYTES
        parsed = unpack_frame(raw, local_key_bytes)
        assert parsed is not None
        assert parsed.seqno == 99
        assert json.loads(parsed.payload) == {"dps": {"1": 255}}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestUnpackEdgeCases:
    def test_too_short(self) -> None:
        assert unpack_frame(b"\x00" * 10) is None

    def test_wrong_prefix(self) -> None:
        assert unpack_frame(b"\xff\xff\xff\xff" + b"\x00" * 24) is None

    def test_no_suffix(self, local_key_bytes: bytes) -> None:
        """Valid prefix but missing suffix returns None."""
        frame = pack_frame(
            cmd=TuyaCommand.HEARTBEAT,
            payload=b"",
            local_key=local_key_bytes,
        )
        # Corrupt the last 4 bytes (suffix)
        corrupted = frame[:-4] + b"\x00\x00\x00\x00"
        assert unpack_frame(corrupted, local_key_bytes) is None
