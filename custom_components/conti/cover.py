"""Cover platform for Conti curtain motors."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
    CoverState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DP_MAP,
    CONF_INVERT_POSITION,
    DEFAULT_INVERT_POSITION,
    DEVICE_TYPE_CURTAIN,
    DOMAIN,
    DP_KEY_CURTAIN_CONTROL,
    DP_KEY_CURTAIN_CONTROL_2,
    DP_KEY_CURTAIN_CURRENT_POSITION,
    DP_KEY_CURTAIN_POSITION,
    DP_KEY_CURTAIN_WORK_STATE,
    MANUFACTURER,
)
from .coordinator import ContiCoordinator

_LOGGER = logging.getLogger(__name__)

_CONTROL_OPEN = "open"
_CONTROL_CLOSE = "close"
_CONTROL_STOP = "stop"

_WORK_OPENING = {"opening", "opening_up"}
_WORK_CLOSING = {"closing", "closing_down"}
_WORK_STOPPED = {"stop", "stopped", "pause", "paused"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a curtain cover entity for Conti curtain devices."""
    if entry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_CURTAIN:
        return

    coordinator: ContiCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    raw = entry.options.get(CONF_DP_MAP) or entry.data.get(CONF_DP_MAP, "{}")
    dp_map: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else (raw or {})
    device_id: str = entry.data[CONF_DEVICE_ID]

    entities: list[ContiCurtainCover] = []
    entity = ContiCurtainCover(coordinator, entry, device_id, dp_map)
    if entity.has_any_curtain_dp:
        entities.append(entity)

    if any(
        isinstance(info, dict) and info.get("key") == DP_KEY_CURTAIN_CONTROL_2
        for info in dp_map.values()
    ):
        entities.append(
            ContiCurtainCover(
                coordinator,
                entry,
                device_id,
                dp_map,
                control_key=DP_KEY_CURTAIN_CONTROL_2,
                unique_suffix="curtain_2",
                name="Curtain 2",
                expose_position=False,
            )
        )

    if not entities:
        _LOGGER.warning(
            "Conti curtain %s has no curtain DPs in saved dp_map; no cover created",
            device_id,
        )
        return

    async_add_entities(entities, update_before_add=True)


class ContiCurtainCover(CoordinatorEntity[ContiCoordinator], CoverEntity):
    """Coordinator-backed Home Assistant cover entity for a Tuya curtain."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = CoverDeviceClass.CURTAIN

    def __init__(
        self,
        coordinator: ContiCoordinator,
        entry: ConfigEntry,
        device_id: str,
        dp_map: dict[str, Any],
        *,
        control_key: str = DP_KEY_CURTAIN_CONTROL,
        unique_suffix: str = "curtain",
        name: str | None = None,
        expose_position: bool = True,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._entry = entry
        self._dp_map = dp_map
        self._invert_position = bool(
            entry.options.get(
                CONF_INVERT_POSITION,
                entry.data.get(CONF_INVERT_POSITION, DEFAULT_INVERT_POSITION),
            )
        )

        self._dp_control = self._find_dp(control_key)
        self._dp_position = (
            self._find_writable_dp(DP_KEY_CURTAIN_POSITION)
            if expose_position
            else None
        )
        self._dp_current_position = (
            self._find_dp(DP_KEY_CURTAIN_CURRENT_POSITION)
            if expose_position
            else None
        )
        self._dp_work_state = (
            self._find_dp(DP_KEY_CURTAIN_WORK_STATE)
            if expose_position
            else None
        )

        self._last_position: int | None = None
        self._last_state: CoverState | None = None

        self._attr_unique_id = f"{DOMAIN}_{device_id}_{unique_suffix}"
        self._attr_name = name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": entry.title,
            "manufacturer": MANUFACTURER,
        }

        features = CoverEntityFeature(0)
        if self._dp_control:
            features |= (
                CoverEntityFeature.OPEN
                | CoverEntityFeature.CLOSE
                | CoverEntityFeature.STOP
            )
        if self._dp_position:
            features |= CoverEntityFeature.SET_POSITION
        self._attr_supported_features = features

    @property
    def has_any_curtain_dp(self) -> bool:
        """Return True when the saved map has at least one cover-relevant DP."""
        return any(
            dp is not None
            for dp in (
                self._dp_control,
                self._dp_position,
                self._dp_current_position,
                self._dp_work_state,
            )
        )

    def _find_dp(self, key: str) -> str | None:
        for dp_id, info in self._dp_map.items():
            if isinstance(info, dict) and info.get("key") == key:
                return str(dp_id)
        return None

    def _find_writable_dp(self, key: str) -> str | None:
        for dp_id, info in self._dp_map.items():
            if (
                isinstance(info, dict)
                and info.get("key") == key
                and not bool(info.get("read_only", False))
            ):
                return str(dp_id)
        return None

    def _dp_info(self, dp_id: str | None) -> dict[str, Any]:
        if dp_id is None:
            return {}
        info = self._dp_map.get(dp_id, {})
        return info if isinstance(info, dict) else {}

    def _dp_value(self, dp_id: str | None) -> Any:
        if dp_id is None:
            return None
        data = self.coordinator.data or {}
        return data.get(self._device_id, {}).get(dp_id)

    @staticmethod
    def _clamp(value: int, min_value: int, max_value: int) -> int:
        lo = min(min_value, max_value)
        hi = max(min_value, max_value)
        return max(lo, min(hi, value))

    def _normalize_reported_position(self, raw: Any) -> int | None:
        if raw is None:
            return None
        try:
            position = int(raw)
        except (TypeError, ValueError):
            return None
        position = self._clamp(position, 0, 100)
        if self._invert_position:
            position = 100 - position
        self._last_position = position
        return position

    def _position_to_device(self, position: Any) -> int | None:
        try:
            requested = int(position)
        except (TypeError, ValueError):
            return None
        requested = self._clamp(requested, 0, 100)
        if self._invert_position:
            requested = 100 - requested
        info = self._dp_info(self._dp_position)
        return self._clamp(
            requested,
            int(info.get("min", 0)),
            int(info.get("max", 100)),
        )

    def _raw_work_state(self) -> str:
        raw = self._dp_value(self._dp_work_state)
        return str(raw or "").strip().lower()

    def _fresh_position(self) -> int | None:
        raw = self._dp_value(self._dp_current_position)
        if raw is None:
            raw = self._dp_value(self._dp_position)
        return self._normalize_reported_position(raw)

    def _state_from_data(self) -> CoverState | None:
        work_state = self._raw_work_state()
        if work_state in _WORK_OPENING:
            self._last_state = CoverState.OPENING
            return self._last_state
        if work_state in _WORK_CLOSING:
            self._last_state = CoverState.CLOSING
            return self._last_state

        position = self._fresh_position()
        if position is not None:
            if position <= 0:
                self._last_state = CoverState.CLOSED
            else:
                self._last_state = CoverState.OPEN
            return self._last_state

        if work_state in _WORK_STOPPED:
            return self._last_state
        return self._last_state

    @property
    def available(self) -> bool:
        return self.coordinator.is_device_available()

    @property
    def current_cover_position(self) -> int | None:
        position = self._fresh_position()
        if position is not None:
            return position
        return self._last_position

    @property
    def is_opening(self) -> bool | None:
        return self._state_from_data() is CoverState.OPENING

    @property
    def is_closing(self) -> bool | None:
        return self._state_from_data() is CoverState.CLOSING

    @property
    def is_closed(self) -> bool | None:
        state = self._state_from_data()
        if state is None or state in (CoverState.OPENING, CoverState.CLOSING):
            return None
        return state is CoverState.CLOSED

    @property
    def state(self) -> CoverState | None:
        return self._state_from_data()

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_send_control(_CONTROL_OPEN)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_send_control(_CONTROL_CLOSE)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._async_send_control(_CONTROL_STOP)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        if not self._dp_position:
            return
        if not self._command_available():
            return

        requested = kwargs.get("position")
        device_value = self._position_to_device(requested)
        if device_value is None:
            return

        self.coordinator.mark_dp_commanded(self._dp_position)
        await self.coordinator.device_manager.set_dp(
            self._device_id, int(self._dp_position), device_value
        )
        self.coordinator.apply_optimistic_update(
            self._device_id, self._dp_position, device_value
        )
        await self.coordinator.async_request_refresh()

    async def _async_send_control(self, command: str) -> None:
        if not self._dp_control:
            return
        if not self._command_available():
            return

        self.coordinator.mark_dp_commanded(self._dp_control)
        await self.coordinator.device_manager.set_dp(
            self._device_id, int(self._dp_control), command
        )
        self.coordinator.apply_optimistic_update(
            self._device_id, self._dp_control, command
        )
        if command == _CONTROL_OPEN:
            self._last_state = CoverState.OPENING
        elif command == _CONTROL_CLOSE:
            self._last_state = CoverState.CLOSING
        await self.coordinator.async_request_refresh()

    def _command_available(self) -> bool:
        if self.coordinator.is_device_available():
            return True
        _LOGGER.warning(
            "Conti curtain command ignored because device %s is unavailable",
            self._device_id,
        )
        return False
