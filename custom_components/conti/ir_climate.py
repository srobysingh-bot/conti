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
    HVACMode.FAN_ONLY: "fan",
    HVACMode.AUTO: "auto",
}

IR_FAN_MODES = ["auto", "low", "medium", "high"]
IR_FAN_ALIASES = {
    "f1": "low",
    "f2": "medium",
    "f3": "medium",
    "f4": "high",
    "f5": "high",
    "mid": "medium",
    "1": "low",
    "2": "medium",
    "3": "medium",
    "4": "high",
    "5": "high",
}
IR_FAN_CODE_BY_MODE = {
    "auto": "auto",
    "low": "f1",
    "medium": "f3",
    "high": "f4",
}
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
        self._swing_on = False
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
        _LOGGER.debug(
            "IR climate available commands count=%s sample=%s",
            len(self._commands),
            sorted(self._commands)[:12],
        )

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
            "ir_swing": "on" if self._swing_on else "off",
            "state_command": self._state_command_name(),
        }

    async def async_turn_on(self) -> None:
        """Turn the virtual AC on."""
        self._power = True
        if not await self._send_state():
            if not await self._send_first_available(["power_on", "on", "power"]):
                raise HomeAssistantError("ir_command_not_found")
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the virtual AC off."""
        if not await self._send_first_available(["ac_off", "power_off", "off"]):
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
        normalized = _normalize_ac_fan(fan_mode)
        self._fan_mode = normalized if normalized in IR_FAN_MODES else "auto"
        self._power = True
        if not await self._send_state():
            fan_code = self._fan_code()
            if not await self._send_first_available(
                [f"fan_{fan_code}", f"fan_speed_{fan_code}", "fan_speed", "fan"]
            ):
                raise HomeAssistantError("ir_command_not_found")
        self.async_write_ha_state()

    async def _send_state(self) -> bool:
        """Send a full-state AC command when the raw code pack exposes one."""
        state_command = self._state_command_name()
        _LOGGER.debug("Generated climate key=%s", state_command)
        candidates = [state_command]
        mode = IR_HVAC_MODES.get(self._hvac_mode, "cool")
        temp = int(self._target_temp)
        swing = "on" if self._swing_on else "off"
        fan_codes = self._fan_code_candidates()
        if mode != "fan":
            for fan_code in fan_codes:
                candidates.append(f"ac_{mode}_{temp}_{fan_code}_swing_{swing}")
                candidates.append(f"{mode}_{temp}_{fan_code}")
            candidates.extend([f"{mode}_{temp}", f"temp_{temp}", str(temp)])
        else:
            for fan_code in fan_codes:
                candidates.append(f"ac_fan_{fan_code}_swing_{swing}")
                candidates.append(f"fan_{fan_code}")
            candidates.append("fan")
        return await self._send_first_available(candidates)

    def _state_command_name(self) -> str:
        """Return canonical full-state AC raw command name."""
        mode = IR_HVAC_MODES.get(self._hvac_mode, "cool")
        swing = "on" if self._swing_on else "off"
        fan_code = self._fan_code()
        if mode == "fan":
            return f"ac_fan_{fan_code}_swing_{swing}"
        temp = max(MIN_TEMP, min(MAX_TEMP, int(self._target_temp)))
        return f"ac_{mode}_{temp}_{fan_code}_swing_{swing}"

    def _fan_code(self) -> str:
        """Return the canonical raw-pack fan token for the selected fan mode."""
        fan_mode = _normalize_ac_fan(self._fan_mode)
        return IR_FAN_CODE_BY_MODE.get(fan_mode, "auto")

    def _fan_code_candidates(self) -> list[str]:
        """Return fan token fallbacks for sparse AC raw code packs."""
        preferred = self._fan_code()
        candidates = [preferred, "auto", "f3", "f2", "f1", "f4", "f5"]
        deduped: list[str] = []
        for candidate in candidates:
            if candidate not in deduped:
                deduped.append(candidate)
        return deduped

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
        preferred = normalize_ir_action(actions[0]) if actions else ""
        for action in actions:
            normalized = normalize_ir_action(action)
            if normalized in self._commands:
                if preferred and normalized != preferred:
                    _LOGGER.debug(
                        "IR climate command translated preferred=%s selected=%s",
                        preferred,
                        normalized,
                    )
                await self._send_command(normalized)
                return True
        _LOGGER.debug(
            "IR climate command fallback exhausted device=%s candidates=%s",
            self._device_id,
            actions,
        )
        _LOGGER.debug(
            "Available command sample=%s",
            sorted(self._commands)[:12],
        )
        _LOGGER.debug("Available command count=%s", len(self._commands))
        return False

    async def _send_command(self, action: str) -> None:
        """Send one normalized IR action and translate failures for HA."""
        try:
            await self._manager.send_ir_command(self._device_id, action)
        except IRCommandNotConfigured as exc:
            raise HomeAssistantError("ir_command_not_found") from exc
        except IRSendError as exc:
            raise HomeAssistantError("ir_send_failed") from exc


def _normalize_ac_fan(fan_mode: str) -> str:
    normalized = normalize_ir_action(fan_mode)
    return IR_FAN_ALIASES.get(normalized, normalized)
