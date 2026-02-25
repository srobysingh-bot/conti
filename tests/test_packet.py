"""Tests for tuya_protocol.packet — frame packing/unpacking."""

from __future__ import annotations

import json
import struct

import pytest

from custom_components.conti.tuya_protocol.base import (
    GCM_IV_SIZE,
    GCM_TAG_SIZE,
    HEADER_SIZE,
    PREFIX_BYTES,
    SUFFIX_BYTES,
    TuyaCommand,
    VERSION_HEADER_35,
)
from custom_components.conti.tuya_protocol.crypto import (
    aes_gcm_encrypt,
    derive_session_key,
)
from custom_components.conti.tuya_protocol.packet import (
    pack_frame,
    pack_frame_v35,
    pack_handshake_frame,
    unpack_frame,
    unpack_frame_v35,
    unpack_handshake_frame,
)


@pytest.fixture
def session_key(local_key_bytes: bytes) -> bytes:
    """Derive a session key from two fixed nonces."""
    client_nonce = b"\x01" * 16
    device_nonce = b"\x02" * 16
    return derive_session_key(local_key_bytes, client_nonce, device_nonce)


# ---------------------------------------------------------------------------
# Legacy pack/unpack (backward compat)
# ---------------------------------------------------------------------------


class TestLegacyPackUnpack:
    """Ensure pack_frame/unpack_frame from packet.py match original behavior."""

    def test_v33_roundtrip(self, local_key_bytes: bytes) -> None:
        payload = json.dumps({"dps": {"1": True}}).encode()
        frame = pack_frame(
            cmd=TuyaCommand.CONTROL,
            payload=payload,
            seqno=1,
            local_key=local_key_bytes,
            version="3.3",
        )
        assert frame[:4] == PREFIX_BYTES
        assert frame[-4:] == SUFFIX_BYTES

        parsed = unpack_frame(frame, local_key_bytes)
        assert parsed is not None
        assert parsed.seqno == 1
        assert json.loads(parsed.payload) == {"dps": {"1": True}}

    def test_v34_roundtrip(self, local_key_bytes: bytes) -> None:
        payload = json.dumps({"dps": {"2": 100}}).encode()
        frame = pack_frame(
            cmd=TuyaCommand.CONTROL,
            payload=payload,
            seqno=5,
            local_key=local_key_bytes,
            version="3.4",
        )
        parsed = unpack_frame(frame, local_key_bytes)
        assert parsed is not None
        assert parsed.seqno == 5
        assert json.loads(parsed.payload) == {"dps": {"2": 100}}


# ---------------------------------------------------------------------------
# v3.5 pack/unpack
# ---------------------------------------------------------------------------


class TestV35PackUnpack:
    def test_roundtrip(self, session_key: bytes) -> None:
        payload = json.dumps({"dps": {"1": True}}).encode()
        frame = pack_frame_v35(
            cmd=TuyaCommand.CONTROL,
            payload=payload,
            seqno=3,
            session_key=session_key,
        )
        assert frame[:4] == PREFIX_BYTES
        assert frame[-4:] == SUFFIX_BYTES

        parsed = unpack_frame_v35(frame, session_key)
        assert parsed is not None
        assert parsed.seqno == 3
        assert parsed.cmd == TuyaCommand.CONTROL
        assert json.loads(parsed.payload) == {"dps": {"1": True}}

    def test_heartbeat_roundtrip(self, session_key: bytes) -> None:
        frame = pack_frame_v35(
            cmd=TuyaCommand.HEARTBEAT,
            payload=b"",
            seqno=10,
            session_key=session_key,
        )
        parsed = unpack_frame_v35(frame, session_key)
        assert parsed is not None
        assert parsed.cmd == TuyaCommand.HEARTBEAT
        assert parsed.seqno == 10

    def test_wrong_session_key_fails(self, session_key: bytes) -> None:
        payload = json.dumps({"dps": {"1": False}}).encode()
        frame = pack_frame_v35(
            cmd=TuyaCommand.CONTROL,
            payload=payload,
            seqno=1,
            session_key=session_key,
        )
        wrong_key = b"\xff" * 16
        assert unpack_frame_v35(frame, wrong_key) is None

    def test_v35_contains_version_header(self, session_key: bytes) -> None:
        frame = pack_frame_v35(
            cmd=TuyaCommand.CONTROL,
            payload=b"test",
            seqno=1,
            session_key=session_key,
        )
        # After PREFIX(4)+SEQNO(4)+CMD(4)+LEN(4)+RETCODE(4) = 20 bytes,
        # the version header "3.5" should appear
        assert frame[20:23] == b"3.5"

    def test_invalid_frame_returns_none(self, session_key: bytes) -> None:
        assert unpack_frame_v35(b"\x00" * 10, session_key) is None

    def test_corrupted_suffix_returns_none(self, session_key: bytes) -> None:
        frame = pack_frame_v35(
            cmd=TuyaCommand.CONTROL,
            payload=b"x",
            seqno=1,
            session_key=session_key,
        )
        corrupted = frame[:-4] + b"\x00\x00\x00\x00"
        assert unpack_frame_v35(corrupted, session_key) is None


# ---------------------------------------------------------------------------
# Handshake frame pack/unpack
# ---------------------------------------------------------------------------


class TestHandshakeFrame:
    def test_roundtrip(self) -> None:
        payload = b"\x01\x02\x03" * 5 + b"\x04"  # 16 bytes
        frame = pack_handshake_frame(
            cmd=TuyaCommand.SESS_KEY_NEG_START,
            payload=payload,
            seqno=1,
        )
        assert frame[:4] == PREFIX_BYTES
        assert frame[-4:] == SUFFIX_BYTES

        parsed = unpack_handshake_frame(frame)
        assert parsed is not None
        assert parsed.cmd == TuyaCommand.SESS_KEY_NEG_START
        assert parsed.seqno == 1
        assert parsed.payload == payload

    def test_finish_frame(self) -> None:
        # 32-byte HMAC confirmation
        payload = b"\xaa" * 32
        frame = pack_handshake_frame(
            cmd=TuyaCommand.SESS_KEY_NEG_FINISH,
            payload=payload,
            seqno=2,
        )
        parsed = unpack_handshake_frame(frame)
        assert parsed is not None
        assert parsed.cmd == TuyaCommand.SESS_KEY_NEG_FINISH
        assert parsed.payload == payload

    def test_invalid_returns_none(self) -> None:
        assert unpack_handshake_frame(b"\xff" * 10) is None
