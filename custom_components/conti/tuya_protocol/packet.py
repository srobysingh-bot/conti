"""Tuya frame packing and unpacking.

Supports v3.1, v3.3, v3.4, and v3.5 frame formats.

Frame layout (v3.1 / v3.3 / v3.4)
-----------------------------------
::

    [PREFIX 4B][SEQNO 4B][CMD 4B][LENGTH 4B]
    [RETCODE 4B][PAYLOAD …]
    [CRC32 4B][SUFFIX 4B]

``LENGTH`` counts: retcode(4) + payload + crc(4) + suffix(4).

Frame layout (v3.5, post-handshake)
------------------------------------
::

    [PREFIX 4B][SEQNO 4B][CMD 4B][LENGTH 4B]
    [RETCODE 4B][VERSION_HDR 15B][IV 12B][CIPHERTEXT …][TAG 16B]
    [CRC32 4B][SUFFIX 4B]
"""

from __future__ import annotations

import base64 as _b64
import binascii
import logging
import struct
from typing import Optional

_LOGGER = logging.getLogger(__name__)

from .base import (
    GCM_IV_SIZE,
    GCM_TAG_SIZE,
    HEADER_SIZE,
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
    VERSION_HEADER_35,
)

# ---------------------------------------------------------------------------
# Lazy import for crypto to avoid circular dependency
# (crypto.py re-exports from this module at module level)
# ---------------------------------------------------------------------------
_crypto = None


def _get_crypto():  # noqa: ANN202
    global _crypto  # noqa: PLW0603
    if _crypto is None:
        from . import crypto as _mod  # noqa: PLC0415

        _crypto = _mod
    return _crypto


# ---------------------------------------------------------------------------
# Local CRC-32 (avoids importing crypto at module level)
# ---------------------------------------------------------------------------


def _crc32(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Frame packing — v3.1 / v3.3 / v3.4 (legacy)
# ---------------------------------------------------------------------------


def pack_frame(
    cmd: int,
    payload: bytes,
    seqno: int = 0,
    local_key: Optional[bytes] = None,
    version: str = PROTO_33,
) -> bytes:
    """Build a complete Tuya frame ready to send over the wire.

    Parameters
    ----------
    cmd:
        Command code (see :class:`TuyaCommand`).
    payload:
        Plaintext payload (typically JSON-encoded DPS dict).
    seqno:
        Sequence number.
    local_key:
        16-byte device local key.  Required for v3.3+.
    version:
        Protocol version string: ``"3.1"``, ``"3.3"`` or ``"3.4"``.
    """
    crypto = _get_crypto()

    if version in (PROTO_33, PROTO_34) and local_key:
        encrypted = crypto.aes_encrypt(payload, local_key)
        header = VERSION_HEADER_33 if version == PROTO_33 else VERSION_HEADER_34
        body = header + encrypted
    elif version == PROTO_31:
        if local_key and cmd == TuyaCommand.CONTROL:
            encrypted = crypto.aes_encrypt(payload, local_key)
            b64 = _b64.b64encode(encrypted)
            body = b"3.1" + b"\x00" * 12 + b64
        else:
            body = payload
    else:
        body = payload

    retcode = b"\x00\x00\x00\x00"
    inner = retcode + body
    length = len(inner) + 8  # + crc(4) + suffix(4)

    hdr = struct.pack(">IIII", PREFIX_VALUE, seqno, cmd, length)
    frame_no_crc = hdr + inner
    checksum = _crc32(frame_no_crc)
    return frame_no_crc + struct.pack(">I", checksum) + SUFFIX_BYTES


# ---------------------------------------------------------------------------
# Frame packing — v3.4 (AES-ECB with session key + HMAC-SHA256)
# ---------------------------------------------------------------------------

_HMAC_SIZE: int = 32  # SHA-256 digest length


def pack_frame_v34(
    cmd: int,
    payload: bytes,
    seqno: int,
    session_key: bytes,
) -> bytes:
    """Build a v3.4 AES-ECB frame using HMAC-SHA256 for integrity.

    v3.4 frame layout (post-handshake):

        [PREFIX 4B][SEQNO 4B][CMD 4B][LENGTH 4B]
        [RETCODE 4B][VERSION_HDR 15B][ENCRYPTED_PAYLOAD …]
        [HMAC-SHA256 32B][SUFFIX 4B]

    ``LENGTH`` counts: retcode(4) + body + hmac(32) + suffix(4).
    HMAC is computed over everything from prefix through the encrypted body.
    """
    crypto = _get_crypto()
    encrypted = crypto.aes_encrypt(payload, session_key)
    body = VERSION_HEADER_34 + encrypted

    retcode = b"\x00\x00\x00\x00"
    inner = retcode + body
    length = len(inner) + _HMAC_SIZE + 4  # + hmac(32) + suffix(4)

    hdr = struct.pack(">IIII", PREFIX_VALUE, seqno, cmd, length)
    frame_no_hmac = hdr + inner
    mac = crypto.hmac_sha256(session_key, frame_no_hmac)
    return frame_no_hmac + mac + SUFFIX_BYTES


def unpack_frame_v34(
    data: bytes,
    session_key: bytes,
) -> Optional[TuyaFrame]:
    """Parse a v3.4 HMAC-SHA256 authenticated frame.

    Returns ``None`` on structural, HMAC, or decryption failure.
    """
    # v3.4 minimum: header(16) + retcode(4) + hmac(32) + suffix(4) = 56
    if len(data) < 56:
        return None
    if data[:4] != PREFIX_BYTES:
        return None
    if data[-4:] != SUFFIX_BYTES:
        return None

    _, seqno, cmd, length = struct.unpack(">IIII", data[:HEADER_SIZE])

    # The last 36 bytes of the frame are HMAC(32) + suffix(4).
    # Everything between header and HMAC is: retcode + body
    frame_end = HEADER_SIZE + length
    if frame_end > len(data):
        # Try using actual data length if length field is off
        frame_end = len(data)

    hmac_start = frame_end - 4 - _HMAC_SIZE  # before suffix and hmac
    if hmac_start < HEADER_SIZE:
        return None

    received_hmac = data[hmac_start:hmac_start + _HMAC_SIZE]
    frame_for_hmac = data[:hmac_start]

    # Verify HMAC
    crypto = _get_crypto()
    expected_hmac = crypto.hmac_sha256(session_key, frame_for_hmac)
    if received_hmac != expected_hmac:
        # Some device firmware omits HMAC — fall back to CRC32 parse
        return _unpack_frame_v34_fallback(data, session_key)

    inner = data[HEADER_SIZE:hmac_start]
    retcode = struct.unpack(">I", inner[:4])[0] if len(inner) >= 4 else 0
    raw = inner[4:] if len(inner) > 4 else b""

    payload = _try_decrypt(raw, session_key)
    return TuyaFrame(seqno=seqno, cmd=cmd, retcode=retcode, payload=payload)


def _unpack_frame_v34_fallback(
    data: bytes,
    session_key: bytes,
) -> Optional[TuyaFrame]:
    """Fallback: parse v3.4 frame using CRC32 layout (some firmware variants)."""
    return unpack_frame(data, session_key)


# ---------------------------------------------------------------------------
# Frame packing — v3.5 (AES-GCM, post-handshake)
# ---------------------------------------------------------------------------


def pack_frame_v35(
    cmd: int,
    payload: bytes,
    seqno: int,
    session_key: bytes,
) -> bytes:
    """Build a v3.5 AES-GCM encrypted frame.

    Used after session negotiation is complete.
    """
    crypto = _get_crypto()
    iv, ciphertext, tag = crypto.aes_gcm_encrypt(payload, session_key)
    body = VERSION_HEADER_35 + iv + ciphertext + tag

    retcode = b"\x00\x00\x00\x00"
    inner = retcode + body
    length = len(inner) + 8  # + crc(4) + suffix(4)

    hdr = struct.pack(">IIII", PREFIX_VALUE, seqno, cmd, length)
    frame_no_crc = hdr + inner
    checksum = _crc32(frame_no_crc)
    return frame_no_crc + struct.pack(">I", checksum) + SUFFIX_BYTES


# ---------------------------------------------------------------------------
# Frame packing — handshake (minimal, no version header)
# ---------------------------------------------------------------------------


def pack_handshake_frame(
    cmd: int,
    payload: bytes,
    seqno: int,
) -> bytes:
    """Build a minimal frame for session negotiation (no version header)."""
    retcode = b"\x00\x00\x00\x00"
    inner = retcode + payload
    length = len(inner) + 8

    hdr = struct.pack(">IIII", PREFIX_VALUE, seqno, cmd, length)
    frame_no_crc = hdr + inner
    checksum = _crc32(frame_no_crc)
    return frame_no_crc + struct.pack(">I", checksum) + SUFFIX_BYTES


# ---------------------------------------------------------------------------
# Frame unpacking — legacy (v3.1 / v3.3 / v3.4)
# ---------------------------------------------------------------------------


def unpack_frame(
    data: bytes, local_key: Optional[bytes] = None
) -> Optional[TuyaFrame]:
    """Parse a single Tuya frame from *data*.

    Returns ``None`` when the frame is structurally invalid.
    """
    if len(data) < MIN_FRAME_SIZE:
        return None
    if data[:4] != PREFIX_BYTES:
        return None
    if data[-4:] != SUFFIX_BYTES:
        return None

    _, seqno, cmd, length = struct.unpack(">IIII", data[:HEADER_SIZE])

    payload_end = HEADER_SIZE + length - 8
    if payload_end > len(data) - 8 or payload_end < HEADER_SIZE:
        return None

    inner = data[HEADER_SIZE:payload_end]
    retcode = struct.unpack(">I", inner[:4])[0] if len(inner) >= 4 else 0
    raw = inner[4:] if len(inner) > 4 else b""

    payload = _try_decrypt(raw, local_key)
    return TuyaFrame(seqno=seqno, cmd=cmd, retcode=retcode, payload=payload)


# ---------------------------------------------------------------------------
# Frame unpacking — v3.5 (AES-GCM, post-handshake)
# ---------------------------------------------------------------------------


def unpack_frame_v35(
    data: bytes,
    session_key: bytes,
) -> Optional[TuyaFrame]:
    """Parse a v3.5 AES-GCM encrypted frame.

    Returns ``None`` on structural or decryption failure.
    """
    if len(data) < MIN_FRAME_SIZE:
        return None
    if data[:4] != PREFIX_BYTES:
        return None
    if data[-4:] != SUFFIX_BYTES:
        return None

    _, seqno, cmd, length = struct.unpack(">IIII", data[:HEADER_SIZE])

    payload_end = HEADER_SIZE + length - 8
    if payload_end > len(data) - 8 or payload_end < HEADER_SIZE:
        return None

    inner = data[HEADER_SIZE:payload_end]
    retcode = struct.unpack(">I", inner[:4])[0] if len(inner) >= 4 else 0
    raw = inner[4:] if len(inner) > 4 else b""

    # Strip version header if present
    if len(raw) > 15 and raw[:3] == b"3.5":
        encrypted_blob = raw[15:]
    else:
        encrypted_blob = raw

    if len(encrypted_blob) < GCM_IV_SIZE + GCM_TAG_SIZE:
        # Too short for GCM — might be an unencrypted handshake response
        return TuyaFrame(seqno=seqno, cmd=cmd, retcode=retcode, payload=encrypted_blob)

    iv = encrypted_blob[:GCM_IV_SIZE]
    tag = encrypted_blob[-GCM_TAG_SIZE:]
    ciphertext = encrypted_blob[GCM_IV_SIZE:-GCM_TAG_SIZE]

    crypto = _get_crypto()
    try:
        plaintext = crypto.aes_gcm_decrypt(ciphertext, session_key, iv, tag)
    except Exception:
        return None

    return TuyaFrame(seqno=seqno, cmd=cmd, retcode=retcode, payload=plaintext)


# ---------------------------------------------------------------------------
# Frame unpacking — handshake (no encryption at frame level)
# ---------------------------------------------------------------------------


def unpack_handshake_frame(data: bytes) -> Optional[TuyaFrame]:
    """Parse a handshake frame (raw payload, no version-header decryption)."""
    if len(data) < MIN_FRAME_SIZE:
        return None
    if data[:4] != PREFIX_BYTES:
        return None
    if data[-4:] != SUFFIX_BYTES:
        return None

    _, seqno, cmd, length = struct.unpack(">IIII", data[:HEADER_SIZE])

    payload_end = HEADER_SIZE + length - 8
    if payload_end > len(data) - 8 or payload_end < HEADER_SIZE:
        return None

    inner = data[HEADER_SIZE:payload_end]
    retcode = struct.unpack(">I", inner[:4])[0] if len(inner) >= 4 else 0
    raw = inner[4:] if len(inner) > 4 else b""

    return TuyaFrame(seqno=seqno, cmd=cmd, retcode=retcode, payload=raw)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _try_decrypt(raw: bytes, local_key: Optional[bytes]) -> bytes:
    """Best-effort decrypt a raw payload region (v3.1 / v3.3 / v3.4)."""
    if not local_key or len(raw) == 0:
        return raw

    crypto = _get_crypto()

    # v3.3 / v3.4 — starts with version header "3.3\x00…" or "3.4\x00…"
    if len(raw) > 15 and raw[:3] in (b"3.3", b"3.4", b"3.5"):
        try:
            return crypto.aes_decrypt(raw[15:], local_key)
        except Exception:
            return raw

    # Try blind decrypt (v3.1 response or headerless encrypted block)
    try:
        return crypto.aes_decrypt(raw, local_key)
    except Exception:
        return raw


# ---------------------------------------------------------------------------
# TCP stream frame extraction
# ---------------------------------------------------------------------------


def extract_frames(buf: bytes) -> tuple[list[bytes], bytes]:
    """Extract complete Tuya frames from a byte buffer.

    TCP is a stream protocol — a single ``read()`` may deliver partial
    frames, multiple frames, or a mix.  This function scans *buf* for
    complete frames (prefix … suffix) and returns them as a list along
    with any remaining partial data.

    Returns ``(frames, remainder)`` where *remainder* must be
    prepended to the next ``read()`` result.
    """
    frames: list[bytes] = []
    pos = 0

    while pos <= len(buf) - MIN_FRAME_SIZE:
        # Scan for the next PREFIX_BYTES
        idx = buf.find(PREFIX_BYTES, pos)
        if idx == -1 or idx + HEADER_SIZE > len(buf):
            break

        # Read the length field from the header
        _, _, _, length = struct.unpack(">IIII", buf[idx : idx + HEADER_SIZE])
        frame_size = HEADER_SIZE + length

        # Not enough data yet — wait for more
        if idx + frame_size > len(buf):
            pos = idx
            break

        frame = buf[idx : idx + frame_size]

        # Sanity: check suffix
        if frame[-4:] == SUFFIX_BYTES:
            frames.append(frame)
        else:
            _LOGGER.debug(
                "Frame at offset %d has bad suffix, skipping 4 bytes", idx
            )

        pos = idx + frame_size

    remainder = buf[pos:]
    return frames, remainder
