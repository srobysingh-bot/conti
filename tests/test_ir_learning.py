"""Tests for IR learning lifecycle."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.conti import ir_learning
from custom_components.conti.ir_learning import IRLearningError, IRLearningSession


class FakeCloud:
    """Minimal IR cloud stand-in for learning lifecycle tests."""

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def capture_learning_code(
        self,
        device_id: str,
        learning_time: str,
    ) -> dict[str, Any] | None:
        self.calls.append(("capture", device_id))
        return self.payload

    async def stop_learning(self, device_id: str) -> None:
        self.calls.append(("stop", device_id))


@pytest.mark.asyncio
async def test_learning_waits_captures_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(ir_learning.asyncio, "sleep", fake_sleep)

    cloud = FakeCloud({"code": "0123456789abcdef"})
    session = IRLearningSession(object(), cloud=cloud)  # type: ignore[arg-type]

    payload = await session.capture_learned_payload("irhub1", "123")

    assert payload == {"code": "0123456789abcdef"}
    assert sleeps == [4]
    assert cloud.calls == [("capture", "irhub1"), ("stop", "irhub1")]


@pytest.mark.asyncio
async def test_learning_stops_when_capture_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(ir_learning.asyncio, "sleep", fake_sleep)

    cloud = FakeCloud(None)
    session = IRLearningSession(object(), cloud=cloud)  # type: ignore[arg-type]

    with pytest.raises(IRLearningError):
        await session.capture_learned_payload("irhub1", "123")

    assert cloud.calls == [("capture", "irhub1"), ("stop", "irhub1")]
