"""Tests for tuya_protocol.v35 — v3.5 protocol handler (AES-GCM + handshake)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.conti.tuya_protocol.base import (
    TuyaCommand,
)
from custom_components.conti.tuya_protocol.crypto import (
    aes_ecb_decrypt_raw,
    aes_ecb_encrypt_raw,
    aes_gcm_decrypt,
    aes_gcm_encrypt,
    derive_session_key,
    generate_nonce,
    hmac_sha256,
)
from custom_components.conti.tuya_protocol.packet import (
    pack_frame_v35,
    pack_handshake_frame,
    unpack_handshake_frame,
)
from custom_components.conti.tuya_protocol.v33 import TuyaV33
from custom_components.conti.tuya_protocol.v34 import TuyaV34
from custom_components.conti.tuya_protocol.v35 import TuyaV35


# ---------------------------------------------------------------------------
# Crypto helpers for v3.5
# ---------------------------------------------------------------------------


class TestAESGCM:
    def test_encrypt_decrypt_roundtrip(self, local_key_bytes: bytes) -> None:
        plaintext = b"Hello, Tuya 3.5!"
        iv, ct, tag = aes_gcm_encrypt(plaintext, local_key_bytes)
        assert len(iv) == 12
        assert len(tag) == 16
        assert ct != plaintext
        result = aes_gcm_decrypt(ct, local_key_bytes, iv, tag)
        assert result == plaintext

    def test_gcm_with_fixed_iv(self, local_key_bytes: bytes) -> None:
        iv = b"\x00" * 12
        plaintext = b"fixed iv test"
        returned_iv, ct, tag = aes_gcm_encrypt(plaintext, local_key_bytes, iv=iv)
        assert returned_iv == iv
        assert aes_gcm_decrypt(ct, local_key_bytes, iv, tag) == plaintext

    def test_gcm_wrong_key_raises(self, local_key_bytes: bytes) -> None:
        iv, ct, tag = aes_gcm_encrypt(b"secret", local_key_bytes)
        wrong_key = b"\xff" * 16
        with pytest.raises(Exception):
            aes_gcm_decrypt(ct, wrong_key, iv, tag)

    def test_gcm_wrong_tag_raises(self, local_key_bytes: bytes) -> None:
        iv, ct, tag = aes_gcm_encrypt(b"secret", local_key_bytes)
        bad_tag = b"\x00" * 16
        with pytest.raises(Exception):
            aes_gcm_decrypt(ct, local_key_bytes, iv, bad_tag)

    def test_gcm_empty_plaintext(self, local_key_bytes: bytes) -> None:
        iv, ct, tag = aes_gcm_encrypt(b"", local_key_bytes)
        assert aes_gcm_decrypt(ct, local_key_bytes, iv, tag) == b""


class TestRawECB:
    def test_encrypt_decrypt_roundtrip(self, local_key_bytes: bytes) -> None:
        data = b"\xab" * 16
        encrypted = aes_ecb_encrypt_raw(data, local_key_bytes)
        assert len(encrypted) == 16
        assert encrypted != data
        decrypted = aes_ecb_decrypt_raw(encrypted, local_key_bytes)
        assert decrypted == data

    def test_wrong_size_raises(self, local_key_bytes: bytes) -> None:
        with pytest.raises(ValueError):
            aes_ecb_encrypt_raw(b"\x00" * 15, local_key_bytes)
        with pytest.raises(ValueError):
            aes_ecb_decrypt_raw(b"\x00" * 17, local_key_bytes)


class TestSessionKeyDerivation:
    def test_derive_deterministic(self, local_key_bytes: bytes) -> None:
        cn = b"\x01" * 16
        dn = b"\x02" * 16
        sk1 = derive_session_key(local_key_bytes, cn, dn)
        sk2 = derive_session_key(local_key_bytes, cn, dn)
        assert sk1 == sk2
        assert len(sk1) == 16

    def test_derive_different_nonces(self, local_key_bytes: bytes) -> None:
        cn = b"\x01" * 16
        dn1 = b"\x02" * 16
        dn2 = b"\x03" * 16
        sk1 = derive_session_key(local_key_bytes, cn, dn1)
        sk2 = derive_session_key(local_key_bytes, cn, dn2)
        assert sk1 != sk2

    def test_derive_rejects_wrong_length(self, local_key_bytes: bytes) -> None:
        with pytest.raises(ValueError):
            derive_session_key(local_key_bytes, b"\x01" * 15, b"\x02" * 16)

    def test_session_key_not_local_key(self, local_key_bytes: bytes) -> None:
        cn = b"\xaa" * 16
        dn = b"\xbb" * 16
        sk = derive_session_key(local_key_bytes, cn, dn)
        assert sk != local_key_bytes


class TestHMAC:
    def test_hmac_deterministic(self) -> None:
        key = b"secretkey1234567"
        data = b"hello"
        h1 = hmac_sha256(key, data)
        h2 = hmac_sha256(key, data)
        assert h1 == h2
        assert len(h1) == 32

    def test_hmac_different_data(self) -> None:
        key = b"secretkey1234567"
        assert hmac_sha256(key, b"a") != hmac_sha256(key, b"b")


class TestNonceGeneration:
    def test_nonce_length(self) -> None:
        n = generate_nonce()
        assert len(n) == 16

    def test_nonce_random(self) -> None:
        n1 = generate_nonce()
        n2 = generate_nonce()
        assert n1 != n2


# ---------------------------------------------------------------------------
# Protocol handler classes
# ---------------------------------------------------------------------------


class TestTuyaV33Protocol:
    def test_version(self, local_key_bytes: bytes) -> None:
        p = TuyaV33(local_key_bytes, "dev1")
        assert p.version == "3.3"
        assert not p.needs_handshake

    def test_encode_decode_roundtrip(self, local_key_bytes: bytes) -> None:
        p = TuyaV33(local_key_bytes, "dev1")
        payload = json.dumps({"dps": {"1": True}}).encode()
        frame = p.encode(TuyaCommand.CONTROL, payload, seqno=1)
        parsed = p.decode(frame)
        assert parsed is not None
        assert json.loads(parsed.payload) == {"dps": {"1": True}}


class TestTuyaV34Protocol:
    def test_version(self, local_key_bytes: bytes) -> None:
        p = TuyaV34(local_key_bytes, "dev1")
        assert p.version == "3.4"
        assert p.needs_handshake
        assert not p.session_established

    def test_encode_without_session_raises(self, local_key_bytes: bytes) -> None:
        p = TuyaV34(local_key_bytes, "dev1")
        with pytest.raises(RuntimeError, match="session not established"):
            p.encode(TuyaCommand.CONTROL, b"test", seqno=1)

    def test_encode_decode_roundtrip(self, local_key_bytes: bytes) -> None:
        p = TuyaV34(local_key_bytes, "dev1")
        # Manually set session key (normally done by handshake)
        cn = b"\x01" * 16
        dn = b"\x02" * 16
        p._session_key = derive_session_key(local_key_bytes, cn, dn)

        payload = json.dumps({"dps": {"1": 255}}).encode()
        frame = p.encode(TuyaCommand.CONTROL, payload, seqno=10)
        parsed = p.decode(frame)
        assert parsed is not None
        assert json.loads(parsed.payload) == {"dps": {"1": 255}}


class TestTuyaV35Protocol:
    def test_version(self, local_key_bytes: bytes) -> None:
        p = TuyaV35(local_key_bytes, "dev1")
        assert p.version == "3.5"
        assert p.needs_handshake
        assert not p.session_established

    def test_encode_without_session_raises(self, local_key_bytes: bytes) -> None:
        p = TuyaV35(local_key_bytes, "dev1")
        with pytest.raises(RuntimeError, match="session not established"):
            p.encode(TuyaCommand.CONTROL, b"test", seqno=1)

    def test_decode_without_session_returns_none(self, local_key_bytes: bytes) -> None:
        p = TuyaV35(local_key_bytes, "dev1")
        # Build a dummy frame
        frame = pack_frame_v35(
            cmd=TuyaCommand.CONTROL,
            payload=b"test",
            seqno=1,
            session_key=b"\x00" * 16,
        )
        assert p.decode(frame) is None

    def test_encode_decode_with_session(self, local_key_bytes: bytes) -> None:
        p = TuyaV35(local_key_bytes, "dev1")
        # Manually set session key
        cn = b"\x01" * 16
        dn = b"\x02" * 16
        p._session_key = derive_session_key(local_key_bytes, cn, dn)

        payload = json.dumps({"dps": {"1": True}}).encode()
        frame = p.encode(TuyaCommand.CONTROL, payload, seqno=5)
        parsed = p.decode(frame)
        assert parsed is not None
        assert parsed.seqno == 5
        assert json.loads(parsed.payload) == {"dps": {"1": True}}

    def test_reset_session(self, local_key_bytes: bytes) -> None:
        p = TuyaV35(local_key_bytes, "dev1")
        p._session_key = b"\x00" * 16
        assert p.session_established
        p.reset_session()
        assert not p.session_established


# ---------------------------------------------------------------------------
# v3.5 Handshake
# ---------------------------------------------------------------------------


def _build_device_response(
    client_nonce_encrypted: bytes,
    local_key: bytes,
) -> bytes:
    """Simulate a device SESS_KEY_NEG_RESP.

    The device decrypts the client nonce, generates its own nonce,
    encrypts it, and sends back.
    """
    device_nonce = b"\xdd" * 16
    encrypted_device_nonce = aes_ecb_encrypt_raw(device_nonce, local_key)
    return pack_handshake_frame(
        cmd=TuyaCommand.SESS_KEY_NEG_RESP,
        payload=encrypted_device_nonce,
        seqno=1,
    )


class TestV35Handshake:
    @pytest.mark.asyncio
    async def test_handshake_success(self, local_key_bytes: bytes) -> None:
        """Full handshake simulation with mock reader/writer."""
        p = TuyaV35(local_key_bytes, "test_dev")

        # Mock writer
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        # Capture what the protocol sends so we can verify and respond
        sent_data: list[bytes] = []
        original_write = writer.write

        def capture_write(data: bytes) -> None:
            sent_data.append(data)

        writer.write = capture_write

        # Pre-build device response (we need to know the device nonce)
        device_nonce = b"\xdd" * 16
        encrypted_device_nonce = aes_ecb_encrypt_raw(device_nonce, local_key_bytes)
        device_response = pack_handshake_frame(
            cmd=TuyaCommand.SESS_KEY_NEG_RESP,
            payload=encrypted_device_nonce,
            seqno=1,
        )

        # Mock reader to return device response
        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=device_response)

        ok = await p.perform_handshake(reader, writer, timeout=5.0)
        assert ok is True
        assert p.session_established
        assert p._session_key is not None
        assert len(p._session_key) == 16

        # Verify two frames were sent (START + FINISH)
        assert len(sent_data) == 2

        # Verify the START frame has the right command
        start_frame = unpack_handshake_frame(sent_data[0])
        assert start_frame is not None
        assert start_frame.cmd == TuyaCommand.SESS_KEY_NEG_START

        # Verify the FINISH frame has the right command
        finish_frame = unpack_handshake_frame(sent_data[1])
        assert finish_frame is not None
        assert finish_frame.cmd == TuyaCommand.SESS_KEY_NEG_FINISH
        # FINISH payload should be 32-byte HMAC
        assert len(finish_frame.payload) == 32

    @pytest.mark.asyncio
    async def test_handshake_timeout(self, local_key_bytes: bytes) -> None:
        p = TuyaV35(local_key_bytes, "test_dev")

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(side_effect=asyncio.TimeoutError)

        ok = await p.perform_handshake(reader, writer, timeout=0.1)
        assert ok is False
        assert not p.session_established

    @pytest.mark.asyncio
    async def test_handshake_connection_reset(self, local_key_bytes: bytes) -> None:
        p = TuyaV35(local_key_bytes, "test_dev")

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(
            side_effect=ConnectionResetError("Connection reset by peer")
        )

        ok = await p.perform_handshake(reader, writer, timeout=5.0)
        assert ok is False

    @pytest.mark.asyncio
    async def test_handshake_empty_response(self, local_key_bytes: bytes) -> None:
        p = TuyaV35(local_key_bytes, "test_dev")

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")

        ok = await p.perform_handshake(reader, writer, timeout=5.0)
        assert ok is False

    @pytest.mark.asyncio
    async def test_handshake_wrong_cmd(self, local_key_bytes: bytes) -> None:
        """Device responds with wrong command."""
        p = TuyaV35(local_key_bytes, "test_dev")

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        # Send a HEARTBEAT instead of SESS_KEY_NEG_RESP
        bad_response = pack_handshake_frame(
            cmd=TuyaCommand.HEARTBEAT,
            payload=b"\x00" * 16,
            seqno=1,
        )

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=bad_response)

        ok = await p.perform_handshake(reader, writer, timeout=5.0)
        assert ok is False

    @pytest.mark.asyncio
    async def test_handshake_short_payload(self, local_key_bytes: bytes) -> None:
        """Device responds with too-short payload."""
        p = TuyaV35(local_key_bytes, "test_dev")

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        short_response = pack_handshake_frame(
            cmd=TuyaCommand.SESS_KEY_NEG_RESP,
            payload=b"\x00" * 8,  # Only 8 bytes, need 16
            seqno=1,
        )

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=short_response)

        ok = await p.perform_handshake(reader, writer, timeout=5.0)
        assert ok is False

    @pytest.mark.asyncio
    async def test_post_handshake_encode_decode(self, local_key_bytes: bytes) -> None:
        """After successful handshake, encode/decode should work."""
        p = TuyaV35(local_key_bytes, "test_dev")

        # Simulate successful handshake
        device_nonce = b"\xdd" * 16
        encrypted_dn = aes_ecb_encrypt_raw(device_nonce, local_key_bytes)
        device_response = pack_handshake_frame(
            cmd=TuyaCommand.SESS_KEY_NEG_RESP,
            payload=encrypted_dn,
            seqno=1,
        )

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=device_response)

        await p.perform_handshake(reader, writer, timeout=5.0)
        assert p.session_established

        # Now encode and decode a control command
        payload = json.dumps({"dps": {"1": True, "3": 500}}).encode()
        frame = p.encode(TuyaCommand.CONTROL, payload, seqno=3)
        parsed = p.decode(frame)
        assert parsed is not None
        assert parsed.cmd == TuyaCommand.CONTROL
        assert json.loads(parsed.payload) == {"dps": {"1": True, "3": 500}}

    @pytest.mark.asyncio
    async def test_reset_forces_new_handshake(self, local_key_bytes: bytes) -> None:
        """After reset, encode should fail until a new handshake."""
        p = TuyaV35(local_key_bytes, "test_dev")

        # Simulate handshake
        device_nonce = b"\xdd" * 16
        encrypted_dn = aes_ecb_encrypt_raw(device_nonce, local_key_bytes)
        device_response = pack_handshake_frame(
            cmd=TuyaCommand.SESS_KEY_NEG_RESP,
            payload=encrypted_dn,
            seqno=1,
        )

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=device_response)

        await p.perform_handshake(reader, writer, timeout=5.0)
        assert p.session_established

        p.reset_session()
        assert not p.session_established

        with pytest.raises(RuntimeError):
            p.encode(TuyaCommand.CONTROL, b"test", seqno=1)
