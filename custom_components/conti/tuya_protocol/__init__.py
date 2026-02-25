"""Tuya local LAN protocol implementation.

This package provides direct TCP communication with Tuya-firmware devices
without any cloud dependency.  It supports protocol versions 3.1, 3.3, 3.4,
and 3.5.

Public API
----------
* :class:`TuyaDeviceClient` — high-level async client for a single device.
* :mod:`crypto` — AES-ECB/GCM helpers and session key derivation.
* :mod:`packet` — frame packing/unpacking and TCP stream extraction.
* :mod:`base` — constants, command codes, frame NamedTuple, protocol ABC.
* :mod:`v31` / :mod:`v33` / :mod:`v34` / :mod:`v35` — version-specific handlers.
"""

from __future__ import annotations

from .client import TuyaDeviceClient

__all__ = ["TuyaDeviceClient"]
