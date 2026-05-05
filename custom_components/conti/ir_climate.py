"""Virtual climate entity backed by Conti IR command libraries."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_ID,
    CONF_IR_MODEL,
    DOMAIN,
    MANUFACTURER,
)
from .ir_actions import normalize_ir_action
from .ir_manager import IRCommandNotConfigured, IRManager, IRSendError
from .ir_storage import IRStorage

_LOGGER = logging.getLogger(__name__)

IR_HVAC_MODES: dict[HVACMode, str] = {
    HVACMode.COOL: "cool",
    HVACMode.HEAT: "heat",
    HVACMode.DRY: "dry",
    HVACMode.FAN_ONLY: "fan_only",
    HVACMode.AUTO: "auto",
}

IR_FAN_MODES = ["auto", "low", "medium", "high"]
DEFAULT_TEMP = 24
MIN_TEMP = 16
MAX_TEMP = 30


async def async_setup_ir_climate_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a virtual climate entity for AC-like IR libraries."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    storage = entry_data.get("ir_storage")
    manager = entry_data.get("ir_manager")
    device_id = str(entry.data.get(CONF_DEVICE_ID, "")).strip()
    if (
        not device_id
        or not isinstance(storage, IRStorage)
        or not isinstance(manager, IRManager)
    ):
        return

    if await storage.async_profile_type() != "ac":
        return

    async_add_entities([ContiIRClimate(entry, device_id, storage, manager)], True)


class ContiIRClimate(ClimateEntity):
    """A stateful Home Assistant climate facade for IR AC libraries."""

    _attr_has_entity_name = True
    _attr_name = "IR Climate"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1.0
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.DRY,
        HVACMode.FAN_ONLY,
        HVACMode.AUTO,
    ]
    _attr_fan_modes = IR_FAN_MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
    )

    def __init__(
        self,
        entry: ConfigEntry,
        device_id: str,
        storage: IRStorage,
        manager: IRManager,
    ) -> None:
        self._entry = entry
        self._device_id = device_id
        self._storage = storage
        self._manager = manager
        self._commands: dict[str, dict[str, Any]] = {}
        self._power = False
        self._target_temp = DEFAULT_TEMP
        self._hvac_mode = HVACMode.COOL
        self._fan_mode = "auto"
        self._attr_unique_id = f"{entry.entry_id}_ir_climate"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "manufacturer": MANUFACTURER,
            "name": entry.title,
            "model": str(entry.data.get(CONF_IR_MODEL, "IR AC") or "IR AC"),
        }

    async def async_added_to_hass(self) -> None:
        """Load the IR command map when Home Assistant adds the entity."""
        await self.async_update()

    async def async_update(self) -> None:
        """Refresh available commands from storage."""
        self._commands = await self._storage.async_all_commands()
        self._attr_available = bool(self._commands)

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the optimistic HVAC mode."""
        return self._hvac_mode if self._power else HVACMode.OFF

    @property
    def target_temperature(self) -> float:
        """Return the optimistic target temperature."""
        return float(self._target_temp)

    @property
    def fan_mode(self) -> str:
        """Return the optimistic fan mode."""
        return self._fan_mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the active IR command shape for diagnostics."""
        return {
            "command_count": len(self._commands),
            "power": self._power,
            "ir_mode": IR_HVAC_MODES.get(self._hvac_mode, "cool"),
            "ir_fan": self._fan_mode,
        }

    async def async_turn_on(self) -> None:
        """Turn the virtual AC on."""
        self._power = True
        if not await self._send_first_available(["power_on", "on", "power"]):
            if not await self._send_state():
                raise HomeAssistantError("ir_command_not_found")
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the virtual AC off."""
        if not await self._send_first_available(["power_off", "off"]):
            if not await self._send_first_available(["power"]):
                raise HomeAssistantError("ir_command_not_found")
        self._power = False
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode through an exact or fallback IR command."""
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return

        self._power = True
        self._hvac_mode = hvac_mode
        if not await self._send_state():
            mode_actions = [
                action
                for mode in self._mode_candidates()
                for action in (f"mode_{mode}", mode)
            ]
            if not await self._send_first_available([*mode_actions, "mode"]):
                raise HomeAssistantError("ir_command_not_found")
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature using full-state, direct, or step commands."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return

        new_temp = max(MIN_TEMP, min(MAX_TEMP, int(round(float(temp)))))
        old_temp = self._target_temp
        self._target_temp = new_temp
        self._power = True
        if not await self._send_state():
            if not await self._send_first_available([f"temp_{new_temp}", str(new_temp)]):
                if not await self._send_temperature_steps(old_temp, new_temp):
                    raise HomeAssistantError("ir_command_not_found")
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan mode through full-state or fan fallback commands."""
        normalized = normalize_ir_action(fan_mode)
        self._fan_mode = normalized if normalized in IR_FAN_MODES else fan_mode
        self._power = True
        if not await self._send_state():
            if not await self._send_first_available(
                [f"fan_{self._fan_mode}", f"fan_speed_{self._fan_mode}", "fan_speed", "fan"]
            ):
                raise HomeAssistantError("ir_command_not_found")
        self.async_write_ha_state()

    async def _send_state(self) -> bool:
        """Send an exact state command when the library exposes one."""
        temp = int(self._target_temp)
        candidates = []
        for mode in self._mode_candidates():
            candidates.extend(
                [
                    f"{mode}_{temp}_{self._fan_mode}",
                    f"{mode}_{temp}",
                    f"{mode}_temp_{temp}_{self._fan_mode}",
                    f"{mode}_temp_{temp}",
                ]
            )
        return await self._send_first_available(candidates)

    def _mode_candidates(self) -> list[str]:
        """Return likely IR action fragments for the selected HVAC mode."""
        mode = IR_HVAC_MODES.get(self._hvac_mode, "cool")
        if self._hvac_mode == HVACMode.FAN_ONLY:
            return ["fan_only", "fan"]
        return [mode]

    async def _send_temperature_steps(self, old_temp: int, new_temp: int) -> bool:
        """Fallback for simple AC libraries that only expose temp up/down."""
        if new_temp == old_temp:
            return True
        action = "temp_up" if new_temp > old_temp else "temp_down"
        steps = abs(new_temp - old_temp)
        if normalize_ir_action(action) not in self._commands:
            return False
        for _ in range(steps):
            await self._send_command(action)
        return True

    async def _send_first_available(self, actions: list[str]) -> bool:
        """Send the first command that exists in the stored library."""
        for action in actions:
            normalized = normalize_ir_action(action)
            if normalized in self._commands:
                await self._send_command(normalized)
                return True
        _LOGGER.debug(
            "IR climate command fallback exhausted device=%s candidates=%s",
            self._device_id,
            actions,
        )
        return False

    async def _send_command(self, action: str) -> None:
        """Send one normalized IR action and translate failures for HA."""
        try:
            await self._manager.send_ir_command(self._device_id, action)
        except IRCommandNotConfigured as exc:
            raise HomeAssistantError("ir_command_not_found") from exc
        except IRSendError as exc:
            raise HomeAssistantError("ir_send_failed") from exc
