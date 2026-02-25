"""Tuya protocol constants, command codes, frame structures, and protocol ABC.

All numeric values follow the Tuya local-control wire format.  This module
has **zero** heavy runtime dependencies so it can be imported anywhere
without circular-dependency risk.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from enum import IntEnum
from typing import NamedTuple, Optional

# ---------------------------------------------------------------------------
# Frame markers
# ---------------------------------------------------------------------------
PREFIX_BYTES: bytes = b"\x00\x00\x55\xaa"
SUFFIX_BYTES: bytes = b"\x00\x00\xaa\x55"
PREFIX_VALUE: int = 0x000055AA
SUFFIX_VALUE: int = 0x0000AA55

# Header: prefix(4) + seqno(4) + cmd(4) + datalen(4) = 16
HEADER_SIZE: int = 16
# Minimum frame: header(16) + retcode(4) + crc(4) + suffix(4) = 28
# (real minimum even shorter for heartbeat, but 24 is the safe minimum)
MIN_FRAME_SIZE: int = 24

# ---------------------------------------------------------------------------
# Default networking
# ---------------------------------------------------------------------------
DEFAULT_PORT: int = 6668
DEFAULT_TIMEOUT: float = 5.0
HEARTBEAT_INTERVAL: float = 10.0
READ_TIMEOUT: float = 30.0

# ---------------------------------------------------------------------------
# Protocol version strings & headers (15 bytes each)
# ---------------------------------------------------------------------------
PROTO_31: str = "3.1"
PROTO_33: str = "3.3"
PROTO_34: str = "3.4"
PROTO_35: str = "3.5"
PROTO_AUTO: str = "auto"

VERSION_HEADER_33: bytes = b"3.3" + b"\x00" * 12
VERSION_HEADER_34: bytes = b"3.4" + b"\x00" * 12
VERSION_HEADER_35: bytes = b"3.5" + b"\x00" * 12

# ---------------------------------------------------------------------------
# AES-GCM parameters (v3.5)
# ---------------------------------------------------------------------------
GCM_IV_SIZE: int = 12
GCM_TAG_SIZE: int = 16
GCM_NONCE_SIZE: int = 16  # Session negotiation nonce size


class TuyaCommand(IntEnum):
    """Tuya protocol command identifiers."""

    SESS_KEY_NEG_START = 0x03
    SESS_KEY_NEG_RESP = 0x04
    SESS_KEY_NEG_FINISH = 0x05
    CONTROL = 0x07          # Set DP values
    STATUS = 0x08           # Device push status update
    HEARTBEAT = 0x09
    DP_QUERY = 0x0A         # Query all DPs
    DP_QUERY_NEW = 0x10     # v3.4+ new DP query


class TuyaFrame(NamedTuple):
    """Parsed Tuya protocol frame."""

    seqno: int
    cmd: int
    retcode: int
    payload: bytes


# ---------------------------------------------------------------------------
# Protocol abstraction
# ---------------------------------------------------------------------------


class TuyaProtocol(ABC):
    """Abstract base for Tuya protocol version handlers.

    Each concrete implementation encapsulates framing, encryption, and
    optional session negotiation for a specific protocol version.
    """

    def __init__(self, local_key: bytes, device_id: str) -> None:
        self._local_key = local_key
        self._device_id = device_id

    @property
    @abstractmethod
    def version(self) -> str:
        """Protocol version string (e.g. ``'3.3'``, ``'3.5'``)."""

    @property
    def needs_handshake(self) -> bool:
        """Whether this protocol requires session negotiation."""
        return False

    async def perform_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bool:
        """Perform session negotiation.  Returns ``True`` on success."""
        return True

    @abstractmethod
    def encode(self, cmd: int, payload: bytes, seqno: int) -> bytes:
        """Encode a command + payload into a wire-format frame."""

    @abstractmethod
    def decode(self, data: bytes) -> Optional[TuyaFrame]:
        """Decode raw bytes into a ``TuyaFrame``, or ``None`` on failure."""

    def reset_session(self) -> None:
        """Reset any session state (called on reconnect)."""
