"""Virtual climate entity backed by Conti IR command libraries."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
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
SCAN_INTERVAL = timedelta(seconds=30)

IR_HVAC_MODES: dict[HVACMode, str] = {
    HVACMode.COOL: "cool",
    HVACMode.HEAT: "heat",
    HVACMode.DRY: "dry",
    HVACMode.FAN_ONLY: "fan",
    HVACMode.AUTO: "auto",
}
TUYA_AC_MODES: dict[str, str] = {
    "cool": "cold",
    "heat": "hot",
    "dry": "wet",
    "fan": "wind",
    "auto": "auto",
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
IR_FAN_MODE_BY_CODE = {
    "auto": "auto",
    "f1": "low",
    "f2": "medium",
    "f3": "medium",
    "f4": "high",
    "f5": "high",
}
AC_STATE_RE = re.compile(
    r"^ac_(?P<mode>cool|heat|dry|auto)_(?P<temp>\d{2})_"
    r"(?P<fan>auto|f[1-5])_swing_(?P<swing>on|off)$"
)
AC_FAN_RE = re.compile(r"^ac_fan_(?P<fan>auto|f[1-5])_swing_(?P<swing>on|off)$")
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
    _attr_should_poll = True
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
        self._state_commands: set[tuple[str, int, str, str]] = set()
        self._state_key_by_tuple: dict[tuple[str, int, str, str], str] = {}
        self._available_temps: set[int] = set()
        self._available_fan_codes: set[str] = set()
        self._available_modes: set[str] = set()
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
        self._refresh_capabilities_from_commands()
        self._sync_dynamic_fan_modes()
        self._attr_available = bool(
            self._commands
            and await self._manager.async_is_device_available(self._device_id)
        )
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
            "supported_temperatures": sorted(self._available_temps),
            "supported_modes": sorted(self._available_modes),
            "supported_fan_codes": sorted(self._available_fan_codes),
        }

    async def async_turn_on(self) -> None:
        """Turn the virtual AC on."""
        old_state = self._snapshot_state()
        self._power = True
        self._coerce_fan_for_current_state()
        if not await self._send_state():
            if not await self._send_first_available(["power_on", "on", "power"]):
                self._restore_state(old_state)
                raise HomeAssistantError("ir_command_not_found")
        self._sync_dynamic_fan_modes()
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the virtual AC off."""
        self._power = False
        if await self._send_ac_runtime_state():
            self.async_write_ha_state()
            return

        if not await self._send_first_available(["ac_off", "power_off", "off"]):
            if not await self._send_first_available(["power"]):
                self._power = True
                raise HomeAssistantError("ir_command_not_found")
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode through an exact or fallback IR command."""
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return

        old_state = self._snapshot_state()
        self._power = True
        self._hvac_mode = hvac_mode
        self._coerce_fan_for_current_state()
        if not await self._send_state():
            mode_actions = [
                action
                for mode in self._mode_candidates()
                for action in (f"mode_{mode}", mode)
            ]
            if not await self._send_first_available([*mode_actions, "mode"]):
                self._restore_state(old_state)
                raise HomeAssistantError("ir_command_not_found")
        self._sync_dynamic_fan_modes()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature using full-state, direct, or step commands."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return

        new_temp = max(self.min_temp, min(self.max_temp, int(round(float(temp)))))
        old_state = self._snapshot_state()
        self._target_temp = new_temp
        self._power = True
        self._coerce_fan_for_current_state()
        if not await self._send_state():
            if not await self._send_first_available([f"temp_{new_temp}", str(new_temp)]):
                if not await self._send_temperature_steps(old_state["target_temp"], new_temp):
                    self._restore_state(old_state)
                    raise HomeAssistantError("ir_command_not_found")
        self._sync_dynamic_fan_modes()
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan mode through full-state or fan fallback commands."""
        normalized = _normalize_ac_fan(fan_mode)
        if normalized not in self._fan_modes_for_current_state():
            _LOGGER.debug(
                "IR climate fan mode unsupported fan=%s supported=%s",
                normalized,
                self._fan_modes_for_current_state(),
            )
            raise HomeAssistantError("ir_command_not_found")
        old_state = self._snapshot_state()
        self._fan_mode = normalized if normalized in IR_FAN_MODES else "auto"
        self._power = True
        if not await self._send_state():
            fan_code = self._fan_code()
            if not await self._send_first_available(
                [f"fan_{fan_code}", f"fan_speed_{fan_code}", "fan_speed", "fan"]
            ):
                self._restore_state(old_state)
                raise HomeAssistantError("ir_command_not_found")
        self._sync_dynamic_fan_modes()
        self.async_write_ha_state()

    async def _send_state(self) -> bool:
        """Send a full-state AC command when the raw code pack exposes one."""
        state_command = self._state_command_name()
        _LOGGER.debug("Generated climate key=%s", state_command)
        resolved = self._resolve_state_command()
        if resolved is None:
            _LOGGER.debug(
                "IR climate command unresolved generated=%s available_sample=%s count=%s",
                state_command,
                sorted(self._commands)[:12],
                len(self._commands),
            )
            return False

        resolved_key, mode, temp, fan_code, swing = resolved
        if resolved_key != state_command:
            _LOGGER.debug(
                "IR climate resolved fallback generated=%s resolved=%s",
                state_command,
                resolved_key,
            )
        self._hvac_mode = _hvac_from_ir_mode(mode)
        self._target_temp = temp
        self._fan_mode = IR_FAN_MODE_BY_CODE.get(fan_code, self._fan_mode)
        self._swing_on = swing == "on"

        if await self._send_ac_runtime_state():
            _LOGGER.debug("IR climate emit transport=ac_runtime key=%s", resolved_key)
            return True

        _LOGGER.debug("IR climate emit transport=raw_pack key=%s", resolved_key)
        await self._send_command(resolved_key)
        return True

    async def _send_ac_runtime_state(self) -> bool:
        """Send structured AC state before falling back to raw pack packets."""
        mode = IR_HVAC_MODES.get(self._hvac_mode, "cool")
        payload = {
            "power": bool(self._power),
            "mode": TUYA_AC_MODES.get(mode, mode),
            "temp": max(MIN_TEMP, min(MAX_TEMP, int(self._target_temp))),
            "wind": self._fan_mode,
            "swing": bool(self._swing_on),
        }
        send_ac = getattr(self._manager, "send_ac_command", None)
        if send_ac is None:
            return False
        try:
            return bool(await send_ac(self._device_id, payload))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "IR structured AC send failed device=%s payload=%s error=%s",
                self._device_id,
                payload,
                exc,
            )
            return False

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
        """Return compatible fan token fallbacks for the current state."""
        preferred = self._fan_code()
        mode = IR_HVAC_MODES.get(self._hvac_mode, "cool")
        temp = int(self._target_temp)
        swing = "on" if self._swing_on else "off"
        current_fan_mode = IR_FAN_MODE_BY_CODE.get(preferred, self._fan_mode)
        compatible = [
            fan_code
            for state_mode, state_temp, fan_code, state_swing in self._state_commands
            if state_mode == mode
            and state_temp == temp
            and state_swing == swing
            and IR_FAN_MODE_BY_CODE.get(fan_code) == current_fan_mode
        ]
        candidates = [preferred, *sorted(compatible)]
        deduped: list[str] = []
        for candidate in candidates:
            if candidate not in deduped:
                deduped.append(candidate)
        return deduped

    def _resolve_state_command(self) -> tuple[str, str, int, str, str] | None:
        """Resolve the current climate state to a supported full-state key."""
        mode = IR_HVAC_MODES.get(self._hvac_mode, "cool")
        temp = max(self.min_temp, min(self.max_temp, int(self._target_temp)))
        swing = "on" if self._swing_on else "off"
        if mode == "fan":
            for fan_code in self._fan_code_candidates():
                key = normalize_ir_action(f"ac_fan_{fan_code}_swing_{swing}")
                if key in self._commands:
                    return key, mode, temp, fan_code, swing
            return None

        for fan_code in self._fan_code_candidates():
            state = (mode, temp, fan_code, swing)
            key = self._state_key_by_tuple.get(state)
            if key:
                return key, mode, temp, fan_code, swing
        return None

    def _refresh_capabilities_from_commands(self) -> None:
        """Derive exposed climate capabilities from actual raw pack keys."""
        self._state_commands.clear()
        self._state_key_by_tuple.clear()
        self._available_temps.clear()
        self._available_fan_codes.clear()
        self._available_modes.clear()

        has_fan_only = False
        for key in self._commands:
            normalized = normalize_ir_action(key)
            if match := AC_STATE_RE.match(normalized):
                mode = match.group("mode")
                temp = int(match.group("temp"))
                fan_code = match.group("fan")
                swing = match.group("swing")
                state = (mode, temp, fan_code, swing)
                self._state_commands.add(state)
                self._state_key_by_tuple[state] = normalized
                self._available_modes.add(mode)
                self._available_temps.add(temp)
                self._available_fan_codes.add(fan_code)
                continue
            if match := AC_FAN_RE.match(normalized):
                has_fan_only = True
                self._available_modes.add("fan")
                self._available_fan_codes.add(match.group("fan"))

        hvac_modes = [HVACMode.OFF]
        for hvac_mode, ir_mode in IR_HVAC_MODES.items():
            if ir_mode in self._available_modes:
                hvac_modes.append(hvac_mode)
        self._attr_hvac_modes = hvac_modes

        fan_modes = [
            fan_mode
            for fan_mode in IR_FAN_MODES
            if any(
                IR_FAN_MODE_BY_CODE.get(code) == fan_mode
                for code in self._available_fan_codes
            )
        ]
        self._attr_fan_modes = fan_modes or ["auto"]

        if self._available_temps:
            self._attr_min_temp = min(self._available_temps)
            self._attr_max_temp = max(self._available_temps)
            if int(self._target_temp) not in self._available_temps:
                self._target_temp = min(self._available_temps, key=lambda item: abs(item - DEFAULT_TEMP))
        else:
            self._attr_min_temp = MIN_TEMP
            self._attr_max_temp = MAX_TEMP

        if IR_HVAC_MODES.get(self._hvac_mode, "cool") not in self._available_modes:
            first_mode = next(
                (mode for mode in hvac_modes if mode != HVACMode.OFF),
                HVACMode.COOL,
            )
            self._hvac_mode = first_mode
        if self._fan_mode not in self._attr_fan_modes:
            self._fan_mode = self._attr_fan_modes[0]
        if has_fan_only and HVACMode.FAN_ONLY not in self._attr_hvac_modes:
            self._attr_hvac_modes = [*self._attr_hvac_modes, HVACMode.FAN_ONLY]
        self._sync_dynamic_fan_modes()

    def _fan_modes_for_current_state(self) -> list[str]:
        mode = IR_HVAC_MODES.get(self._hvac_mode, "cool")
        temp = int(self._target_temp)
        swing = "on" if self._swing_on else "off"
        fan_modes = sorted(
            {
                IR_FAN_MODE_BY_CODE.get(fan_code, fan_code)
                for state_mode, state_temp, fan_code, state_swing in self._state_commands
                if state_mode == mode and state_temp == temp and state_swing == swing
            }
        )
        if mode == "fan":
            fan_modes.extend(
                sorted(
                    {
                        IR_FAN_MODE_BY_CODE.get(fan_code, fan_code)
                        for state_mode, _temp, fan_code, state_swing in self._state_commands
                        if state_mode == "fan" and state_swing == swing
                    }
                )
            )
        ordered = [fan for fan in IR_FAN_MODES if fan in fan_modes]
        return ordered or list(self._attr_fan_modes or ["auto"])

    def _sync_dynamic_fan_modes(self) -> None:
        self._attr_fan_modes = self._fan_modes_for_current_state()
        if self._fan_mode not in self._attr_fan_modes:
            self._fan_mode = self._attr_fan_modes[0]

    def _coerce_fan_for_current_state(self) -> None:
        self._sync_dynamic_fan_modes()

    def _snapshot_state(self) -> dict[str, Any]:
        return {
            "power": self._power,
            "target_temp": self._target_temp,
            "hvac_mode": self._hvac_mode,
            "fan_mode": self._fan_mode,
            "swing_on": self._swing_on,
        }

    def _restore_state(self, state: dict[str, Any]) -> None:
        self._power = bool(state["power"])
        self._target_temp = int(state["target_temp"])
        self._hvac_mode = state["hvac_mode"]
        self._fan_mode = str(state["fan_mode"])
        self._swing_on = bool(state["swing_on"])

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


def _hvac_from_ir_mode(mode: str) -> HVACMode:
    for hvac_mode, ir_mode in IR_HVAC_MODES.items():
        if ir_mode == mode:
            return hvac_mode
    return HVACMode.COOL
