"""Tuya protocol v3.5 handler — AES-GCM with mandatory session negotiation.

Handshake flow
--------------
1. Client sends ``SESS_KEY_NEG_START`` with its nonce (AES-ECB encrypted).
2. Device responds with ``SESS_KEY_NEG_RESP`` containing its nonce
   (AES-ECB encrypted).
3. Client derives session key:
   ``session_key = AES-ECB(local_key, XOR(client_nonce, device_nonce))``.
4. Client sends ``SESS_KEY_NEG_FINISH`` with HMAC-SHA256 confirmation.
5. All subsequent frames use AES-128-GCM with the session key.

The session key is **never** reused across connections.  It is cleared on
every disconnect, forcing a fresh handshake on reconnect.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .base import (
    DEFAULT_TIMEOUT,
    PROTO_35,
    TuyaCommand,
    TuyaFrame,
    TuyaProtocol,
)
from .crypto import (
    aes_ecb_decrypt_raw,
    aes_ecb_encrypt_raw,
    derive_session_key,
    generate_nonce,
    hmac_sha256,
)
from .packet import (
    pack_frame_v35,
    pack_handshake_frame,
    unpack_frame_v35,
    unpack_handshake_frame,
)

_LOGGER = logging.getLogger(__name__)


class TuyaV35(TuyaProtocol):
    """Protocol handler for Tuya v3.5 (AES-GCM, mandatory handshake)."""

    def __init__(self, local_key: bytes, device_id: str) -> None:
        super().__init__(local_key, device_id)
        self._session_key: Optional[bytes] = None

    @property
    def version(self) -> str:
        return PROTO_35

    @property
    def needs_handshake(self) -> bool:
        return True

    @property
    def session_established(self) -> bool:
        """Whether the session key has been negotiated."""
        return self._session_key is not None

    # -- Handshake -----------------------------------------------------------

    async def perform_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bool:
        """Perform v3.5 session key negotiation.

        Returns ``True`` when the handshake completes and the session key
        is ready for use.
        """
        _LOGGER.info("Handshake started for device %s (v3.5)", self._device_id)
        self._session_key = None

        try:
            # --- Step 1: send client nonce ---------------------------------
            client_nonce = generate_nonce()
            encrypted_nonce = aes_ecb_encrypt_raw(client_nonce, self._local_key)

            start_frame = pack_handshake_frame(
                cmd=TuyaCommand.SESS_KEY_NEG_START,
                payload=encrypted_nonce,
                seqno=1,
            )
            writer.write(start_frame)
            await writer.drain()
            _LOGGER.debug("Sent SESS_KEY_NEG_START for %s", self._device_id)

            # --- Step 2: receive device nonce ------------------------------
            resp_data = await asyncio.wait_for(
                reader.read(4096), timeout=timeout
            )
            if not resp_data:
                _LOGGER.error(
                    "Handshake failed for %s: empty response", self._device_id
                )
                return False

            resp_frame = unpack_handshake_frame(resp_data)
            if resp_frame is None:
                _LOGGER.error(
                    "Handshake failed for %s: could not parse response frame",
                    self._device_id,
                )
                return False

            if resp_frame.cmd != TuyaCommand.SESS_KEY_NEG_RESP:
                _LOGGER.error(
                    "Handshake failed for %s: expected cmd 0x%02X, got 0x%02X",
                    self._device_id,
                    TuyaCommand.SESS_KEY_NEG_RESP,
                    resp_frame.cmd,
                )
                return False

            if len(resp_frame.payload) < 16:
                _LOGGER.error(
                    "Handshake failed for %s: response payload too short (%d bytes)",
                    self._device_id,
                    len(resp_frame.payload),
                )
                return False

            device_nonce = aes_ecb_decrypt_raw(
                resp_frame.payload[:16], self._local_key
            )

            # --- Step 3: derive session key --------------------------------
            session_key = derive_session_key(
                self._local_key, client_nonce, device_nonce
            )

            # --- Step 4: send confirmation ---------------------------------
            confirmation = hmac_sha256(
                session_key, client_nonce + device_nonce
            )
            finish_frame = pack_handshake_frame(
                cmd=TuyaCommand.SESS_KEY_NEG_FINISH,
                payload=confirmation,
                seqno=2,
            )
            writer.write(finish_frame)
            await writer.drain()

            # --- Step 5: wait for device ACK -------------------------------
            # v3.5 devices typically send an acknowledgment after FINISH.
            # Read it to prevent stale data in the TCP stream.
            try:
                ack_data = await asyncio.wait_for(
                    reader.read(4096), timeout=min(timeout, 2.0)
                )
                if ack_data:
                    ack_frame = unpack_handshake_frame(ack_data)
                    _LOGGER.debug(
                        "v3.5 handshake ACK for %s: cmd=0x%02X (%d bytes)",
                        self._device_id,
                        ack_frame.cmd if ack_frame else -1,
                        len(ack_data),
                    )
            except asyncio.TimeoutError:
                _LOGGER.debug(
                    "v3.5 handshake ACK timeout for %s (not all devices send one)",
                    self._device_id,
                )

            self._session_key = session_key
            _LOGGER.info(
                "Handshake success for device %s (v3.5)", self._device_id
            )
            return True

        except asyncio.TimeoutError:
            _LOGGER.error(
                "Handshake failed for %s: timeout waiting for device response",
                self._device_id,
            )
            return False
        except (ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            _LOGGER.error(
                "Handshake failed for %s: %s", self._device_id, exc
            )
            return False
        except Exception:
            _LOGGER.exception(
                "Handshake failed for %s: unexpected error", self._device_id
            )
            return False

    # -- Encode / Decode -----------------------------------------------------

    def encode(self, cmd: int, payload: bytes, seqno: int) -> bytes:
        """Encode a command using AES-GCM with the negotiated session key."""
        if self._session_key is None:
            raise RuntimeError(
                f"Cannot encode for {self._device_id}: session not established. "
                "Call perform_handshake() first."
            )
        return pack_frame_v35(
            cmd=cmd,
            payload=payload,
            seqno=seqno,
            session_key=self._session_key,
        )

    def decode(self, data: bytes) -> Optional[TuyaFrame]:
        """Decode a v3.5 AES-GCM encrypted frame."""
        if self._session_key is None:
            _LOGGER.warning(
                "Decryption error for %s: no session key", self._device_id
            )
            return None
        frame = unpack_frame_v35(data, self._session_key)
        if frame is None:
            _LOGGER.debug(
                "Decryption error for %s: failed to unpack v3.5 frame",
                self._device_id,
            )
        return frame

    def reset_session(self) -> None:
        """Clear the session key, forcing a fresh handshake on reconnect."""
        self._session_key = None
        _LOGGER.debug("Session reset for %s", self._device_id)
