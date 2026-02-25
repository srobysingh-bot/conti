"""Tuya cryptography helpers.

Provides:
  * AES-128-ECB encrypt/decrypt with PKCS7 padding (v3.1–v3.4).
  * AES-128-GCM encrypt/decrypt (v3.5).
  * Raw single-block AES-ECB for session key derivation.
  * Session key derivation for v3.5.
  * HMAC-SHA256.
  * CRC-32.

All encryption logic lives in this module — no other module should import
``cryptography`` directly.
"""

from __future__ import annotations

import binascii
import hashlib
import hmac as _hmac
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .base import GCM_IV_SIZE, GCM_NONCE_SIZE, GCM_TAG_SIZE

# ---------------------------------------------------------------------------
# AES-128-ECB helpers
# ---------------------------------------------------------------------------


def _pad(data: bytes) -> bytes:
    padder = sym_padding.PKCS7(128).padder()
    return padder.update(data) + padder.finalize()


def _unpad(data: bytes) -> bytes:
    unpadder = sym_padding.PKCS7(128).unpadder()
    return unpadder.update(data) + unpadder.finalize()


def aes_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB encrypt *plaintext* with PKCS7 padding."""
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(_pad(plaintext)) + enc.finalize()


def aes_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB decrypt *ciphertext* and strip PKCS7 padding."""
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    dec = cipher.decryptor()
    return _unpad(dec.update(ciphertext) + dec.finalize())


def aes_ecb_encrypt_raw(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB encrypt exactly one block (16 bytes), no padding.

    Used for session key derivation where padding is not desired.
    """
    if len(data) != 16:
        raise ValueError(f"Expected 16 bytes, got {len(data)}")
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()


def aes_ecb_decrypt_raw(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB decrypt exactly one block (16 bytes), no padding."""
    if len(data) != 16:
        raise ValueError(f"Expected 16 bytes, got {len(data)}")
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    dec = cipher.decryptor()
    return dec.update(data) + dec.finalize()


# ---------------------------------------------------------------------------
# AES-128-GCM helpers (v3.5)
# ---------------------------------------------------------------------------


def aes_gcm_encrypt(
    plaintext: bytes,
    key: bytes,
    iv: Optional[bytes] = None,
    aad: bytes = b"",
) -> tuple[bytes, bytes, bytes]:
    """AES-128-GCM encrypt.

    Returns ``(iv, ciphertext, tag)``.  If *iv* is ``None``, a random
    12-byte IV is generated.
    """
    if iv is None:
        iv = os.urandom(GCM_IV_SIZE)
    aesgcm = AESGCM(key)
    ct_and_tag = aesgcm.encrypt(iv, plaintext, aad or None)
    ciphertext = ct_and_tag[:-GCM_TAG_SIZE]
    tag = ct_and_tag[-GCM_TAG_SIZE:]
    return iv, ciphertext, tag


def aes_gcm_decrypt(
    ciphertext: bytes,
    key: bytes,
    iv: bytes,
    tag: bytes,
    aad: bytes = b"",
) -> bytes:
    """AES-128-GCM decrypt and verify authentication tag.

    Raises ``cryptography.exceptions.InvalidTag`` on tag mismatch.
    """
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(iv, ciphertext + tag, aad or None)


# ---------------------------------------------------------------------------
# Session key derivation (v3.5)
# ---------------------------------------------------------------------------


def generate_nonce() -> bytes:
    """Generate a random 16-byte nonce for session negotiation."""
    return os.urandom(GCM_NONCE_SIZE)


def derive_session_key(
    local_key: bytes,
    client_nonce: bytes,
    device_nonce: bytes,
) -> bytes:
    """Derive a 16-byte session key for Tuya v3.5.

    Algorithm: ``AES-ECB-encrypt(local_key, XOR(client_nonce, device_nonce))``

    The result is a deterministic 16-byte key unique per connection that
    cannot be derived without knowing *local_key*.
    """
    if len(client_nonce) != 16 or len(device_nonce) != 16:
        raise ValueError("Both nonces must be exactly 16 bytes")
    xored = bytes(a ^ b for a, b in zip(client_nonce, device_nonce))
    return aes_ecb_encrypt_raw(xored, local_key)


# ---------------------------------------------------------------------------
# HMAC-SHA256
# ---------------------------------------------------------------------------


def hmac_sha256(key: bytes, data: bytes) -> bytes:
    """Compute HMAC-SHA256."""
    return _hmac.new(key, data, hashlib.sha256).digest()


# ---------------------------------------------------------------------------
# CRC-32
# ---------------------------------------------------------------------------


def crc32(data: bytes) -> int:
    """Tuya-style unsigned CRC-32."""
    return binascii.crc32(data) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Backward-compatible re-exports
# ---------------------------------------------------------------------------
# pack_frame and unpack_frame were historically in this module.
# They now live in packet.py but are re-exported here so existing
# code and tests continue to work without import changes.
from .packet import pack_frame, unpack_frame  # noqa: E402, F401
