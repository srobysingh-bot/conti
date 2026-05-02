"""IR learning flow helpers for Conti."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .ir_cloud import TuyaIRCloud
from .ir_storage import IRStorage

_LOGGER = logging.getLogger(__name__)

IRPayloadCapture = Callable[[str, str], Awaitable[Any]]
MIN_LEARNED_IR_PAYLOAD_SIZE = 16


class IRLearningError(Exception):
    """Raised when IR learning cannot capture or store a payload."""


class IRLearningSession:
    """Capture and persist learned IR commands."""

    def __init__(
        self,
        storage: IRStorage,
        *,
        capture_payload: IRPayloadCapture | None = None,
        cloud: TuyaIRCloud | None = None,
    ) -> None:
        self._storage = storage
        self._capture_payload = capture_payload
        self._cloud = cloud

    async def start_learning(self, device_id: str) -> str:
        """Start learning mode on the IR hub."""
        if self._cloud is None:
            raise IRLearningError("No IR cloud handler configured")
        return await self._cloud.start_learning(device_id)

    async def capture_learned_payload(
        self, device_id: str, learning_time: str
    ) -> dict[str, Any]:
        """Capture a learned payload from Tuya cloud."""
        if self._cloud is None:
            raise IRLearningError("No IR cloud handler configured")
        payload = await self._cloud.capture_learning_code(device_id, learning_time)
        if not payload:
            raise IRLearningError("Captured IR payload is empty")
        self._validate_payload(payload)
        return payload

    async def learn_command(
        self,
        device_id: str,
        action: str,
        payload: Any | None = None,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Capture or save a learned command payload."""
        if payload is None:
            if self._capture_payload is None:
                raise IRLearningError("No IR capture handler configured")
            payload = await self._capture_payload(device_id, action)

        self._validate_payload(payload)

        try:
            await self._storage.async_set_command(
                action,
                payload,
                source="learned",
                overwrite=overwrite,
            )
        except Exception as exc:  # noqa: BLE001
            raise IRLearningError(str(exc)) from exc

        _LOGGER.info("IR learning success device=%s action=%s", device_id, action)
        return {"source": "learned", "payload": payload}

    @staticmethod
    def _validate_payload(payload: Any) -> None:
        """Reject empty or suspiciously small learned IR payloads."""
        if payload in (None, "", {}, []):
            raise IRLearningError("Captured IR payload is empty")
        if isinstance(payload, dict):
            code = payload.get("code") or payload.get("payload") or payload.get("data")
            if code is not None and len(str(code).strip()) < MIN_LEARNED_IR_PAYLOAD_SIZE:
                raise IRLearningError("Captured IR payload is too small")
            if code is None and len(str(payload)) < MIN_LEARNED_IR_PAYLOAD_SIZE:
                raise IRLearningError("Captured IR payload is too small")
            return
        if len(str(payload).strip()) < MIN_LEARNED_IR_PAYLOAD_SIZE:
            raise IRLearningError("Captured IR payload is too small")
