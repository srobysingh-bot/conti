"""Tuya protocol v3.1 handler.

AES-128-ECB encryption with the device local key, but ONLY for
CONTROL (0x07) commands.  All other commands (DP_QUERY, HEARTBEAT)
are sent as **plaintext** JSON.

The encrypted CONTROL payload is base64-encoded before being placed
into the frame body with a ``"3.1"`` version header.

Responses from the device may or may not be encrypted — the decoder
tries AES-ECB first and falls back to raw bytes.

No session negotiation is required.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import PROTO_31, TuyaFrame, TuyaProtocol
from .packet import pack_frame, unpack_frame

_LOGGER = logging.getLogger(__name__)


class TuyaV31(TuyaProtocol):
    """Protocol handler for Tuya v3.1 (AES-ECB, limited encryption)."""

    @property
    def version(self) -> str:
        return PROTO_31

    def encode(self, cmd: int, payload: bytes, seqno: int) -> bytes:
        """Encode using v3.1 rules (only CONTROL is encrypted+base64)."""
        return pack_frame(
            cmd=cmd,
            payload=payload,
            seqno=seqno,
            local_key=self._local_key,
            version=PROTO_31,
        )

    def decode(self, data: bytes) -> Optional[TuyaFrame]:
        """Decode a v3.1 frame (response may or may not be encrypted)."""
        return unpack_frame(data, self._local_key)
