"""Tuya protocol v3.4 handler — AES-ECB with mandatory session negotiation.

Handshake flow
--------------
1. Client sends ``SESS_KEY_NEG_START`` with its nonce (AES-ECB encrypted
   with ``local_key``).
2. Device responds with ``SESS_KEY_NEG_RESP`` containing its nonce
   (AES-ECB encrypted with ``local_key``).
3. Client derives session key:
   ``session_key = AES-ECB(local_key, XOR(client_nonce, device_nonce))``.
4. Client sends ``SESS_KEY_NEG_FINISH`` with
   ``HMAC-SHA256(session_key, client_nonce)``.
5. All subsequent frames use **AES-128-ECB with the session_key** and
   **HMAC-SHA256** for integrity (not CRC32).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .base import (
    DEFAULT_TIMEOUT,
    PROTO_34,
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
    pack_frame_v34,
    pack_handshake_frame,
    unpack_frame_v34,
    unpack_handshake_frame,
)

_LOGGER = logging.getLogger(__name__)


class TuyaV34(TuyaProtocol):
    """Protocol handler for Tuya v3.4 (AES-ECB, session negotiation required)."""

    def __init__(self, local_key: bytes, device_id: str) -> None:
        super().__init__(local_key, device_id)
        self._session_key: Optional[bytes] = None

    @property
    def version(self) -> str:
        return PROTO_34

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
        """Perform v3.4 session key negotiation.

        Returns ``True`` when the session key is ready for use.
        """
        _LOGGER.info("v3.4 handshake starting for %s", self._device_id)
        self._session_key = None

        try:
            # --- Step 1: send client nonce ---------------------------------
            client_nonce = generate_nonce()
            _LOGGER.debug(
                "v3.4 client_nonce for %s: %s",
                self._device_id, client_nonce.hex(),
            )
            encrypted_nonce = aes_ecb_encrypt_raw(client_nonce, self._local_key)

            start_frame = pack_handshake_frame(
                cmd=TuyaCommand.SESS_KEY_NEG_START,
                payload=encrypted_nonce,
                seqno=1,
            )
            _LOGGER.debug(
                "v3.4 sending SESS_KEY_NEG_START (%d bytes) for %s",
                len(start_frame), self._device_id,
            )
            writer.write(start_frame)
            await writer.drain()

            # --- Step 2: receive device nonce ------------------------------
            resp_data = await asyncio.wait_for(
                reader.read(4096), timeout=timeout
            )
            if not resp_data:
                _LOGGER.error(
                    "v3.4 handshake failed for %s: empty response", self._device_id
                )
                return False

            _LOGGER.debug(
                "v3.4 received handshake response (%d bytes) for %s",
                len(resp_data), self._device_id,
            )

            resp_frame = unpack_handshake_frame(resp_data)
            if resp_frame is None:
                _LOGGER.error(
                    "v3.4 handshake failed for %s: could not parse response"
                    " (raw hex: %s)",
                    self._device_id, resp_data[:64].hex(),
                )
                return False

            if resp_frame.cmd != TuyaCommand.SESS_KEY_NEG_RESP:
                _LOGGER.error(
                    "v3.4 handshake failed for %s: expected cmd 0x%02X, got 0x%02X",
                    self._device_id,
                    TuyaCommand.SESS_KEY_NEG_RESP,
                    resp_frame.cmd,
                )
                return False

            # The response payload contains the encrypted device nonce
            # possibly followed by an HMAC.  We only need the first 16 bytes.
            if len(resp_frame.payload) < 16:
                _LOGGER.error(
                    "v3.4 handshake failed for %s: payload too short (%d bytes)",
                    self._device_id, len(resp_frame.payload),
                )
                return False

            device_nonce_encrypted = resp_frame.payload[:16]
            device_nonce = aes_ecb_decrypt_raw(
                device_nonce_encrypted, self._local_key
            )
            _LOGGER.debug(
                "v3.4 device_nonce for %s: %s",
                self._device_id, device_nonce.hex(),
            )

            # --- Step 3: derive session key --------------------------------
            session_key = derive_session_key(
                self._local_key, client_nonce, device_nonce
            )
            _LOGGER.debug(
                "v3.4 session_key for %s: %s",
                self._device_id, session_key.hex(),
            )

            # --- Step 4: send confirmation ---------------------------------
            # v3.4 HMAC confirmation covers ONLY the client nonce.
            confirmation = hmac_sha256(session_key, client_nonce)
            _LOGGER.debug(
                "v3.4 HMAC confirmation length: %d bytes", len(confirmation)
            )
            finish_frame = pack_handshake_frame(
                cmd=TuyaCommand.SESS_KEY_NEG_FINISH,
                payload=confirmation,
                seqno=2,
            )
            writer.write(finish_frame)
            await writer.drain()

            # --- Step 5: wait for device ACK -------------------------------
            # Some v3.4 devices send an acknowledgment after FINISH.
            # Read it to prevent stale data in the TCP stream, but don't
            # fail if it doesn't arrive — not all firmware versions send one.
            try:
                ack_data = await asyncio.wait_for(
                    reader.read(4096), timeout=min(timeout, 2.0)
                )
                if ack_data:
                    ack_frame = unpack_handshake_frame(ack_data)
                    _LOGGER.debug(
                        "v3.4 handshake ACK for %s: cmd=0x%02X (%d bytes)",
                        self._device_id,
                        ack_frame.cmd if ack_frame else -1,
                        len(ack_data),
                    )
            except asyncio.TimeoutError:
                _LOGGER.debug(
                    "v3.4 handshake ACK timeout for %s (not all devices send one)",
                    self._device_id,
                )

            self._session_key = session_key
            _LOGGER.info(
                "v3.4 handshake SUCCESS for %s (session_key=%s…)",
                self._device_id, session_key[:4].hex(),
            )
            return True

        except asyncio.TimeoutError:
            _LOGGER.error(
                "v3.4 handshake failed for %s: timeout waiting for device",
                self._device_id,
            )
            return False
        except (ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            _LOGGER.error(
                "v3.4 handshake failed for %s: %s", self._device_id, exc
            )
            return False
        except Exception:
            _LOGGER.exception(
                "v3.4 handshake failed for %s: unexpected error", self._device_id
            )
            return False

    # -- Encode / Decode -----------------------------------------------------

    def encode(self, cmd: int, payload: bytes, seqno: int) -> bytes:
        """Encode using AES-ECB with the negotiated **session** key + HMAC."""
        if self._session_key is None:
            raise RuntimeError(
                f"Cannot encode for {self._device_id}: session not established. "
                "Call perform_handshake() first."
            )
        frame = pack_frame_v34(
            cmd=cmd,
            payload=payload,
            seqno=seqno,
            session_key=self._session_key,
        )
        _LOGGER.debug(
            "v3.4 encode: cmd=0x%02X seqno=%d payload_len=%d frame_len=%d for %s",
            cmd, seqno, len(payload), len(frame), self._device_id,
        )
        return frame

    def decode(self, data: bytes) -> Optional[TuyaFrame]:
        """Decode a v3.4 HMAC-authenticated frame using the session key."""
        if self._session_key is None:
            _LOGGER.warning(
                "v3.4 decode error for %s: no session key", self._device_id
            )
            return None
        _LOGGER.debug(
            "v3.4 decode: %d bytes received for %s",
            len(data), self._device_id,
        )
        frame = unpack_frame_v34(data, self._session_key)
        if frame is None:
            _LOGGER.warning(
                "v3.4 decode failed for %s (first 64 bytes: %s)",
                self._device_id, data[:64].hex(),
            )
        else:
            _LOGGER.debug(
                "v3.4 decoded: cmd=0x%02X retcode=%d payload_len=%d for %s",
                frame.cmd, frame.retcode, len(frame.payload), self._device_id,
            )
        return frame

    def reset_session(self) -> None:
        """Clear the session key, forcing a fresh handshake on reconnect."""
        self._session_key = None
        _LOGGER.debug("v3.4 session reset for %s", self._device_id)
