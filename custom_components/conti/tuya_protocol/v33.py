"""Tuya protocol v3.3 handler.

AES-128-ECB encryption with the device local key.
No session negotiation required.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import PROTO_33, TuyaFrame, TuyaProtocol
from .packet import pack_frame, unpack_frame

_LOGGER = logging.getLogger(__name__)


class TuyaV33(TuyaProtocol):
    """Protocol handler for Tuya v3.3 (AES-ECB, no session negotiation)."""

    @property
    def version(self) -> str:
        return PROTO_33

    def encode(self, cmd: int, payload: bytes, seqno: int) -> bytes:
        """Encode using AES-ECB with ``"3.3"`` version header."""
        return pack_frame(
            cmd=cmd,
            payload=payload,
            seqno=seqno,
            local_key=self._local_key,
            version=PROTO_33,
        )

    def decode(self, data: bytes) -> Optional[TuyaFrame]:
        """Decode a v3.3 frame."""
        return unpack_frame(data, self._local_key)
