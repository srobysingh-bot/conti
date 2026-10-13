"""Opt-in virtual buttons for writable Tuya model commands."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .model_controls import ModelControl, ModelControlEntity, entry_controls


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = []
    for control in entry_controls(entry):
        if control.kind == "bool":
            for power in (True, False):
                entities.append(ContiModelButton(coordinator, entry, control, power))
        elif control.kind == "enum" and control.code in {"scene_switch", "mode_controller"}:
            for value in control.options:
                entities.append(ContiModelButton(coordinator, entry, control, value))
    async_add_entities(entities)


class ContiModelButton(ModelControlEntity, ButtonEntity):
    """A fixed dashboard command, never an incoming physical button event."""

    def __init__(
        self, coordinator: Any, entry: ConfigEntry, control: ModelControl, value: Any,
    ) -> None:
        super().__init__(coordinator, entry, control)
        self._value = value
        self._attr_unique_id = f"conti_{self._device_id}_command_{control.dp_id}_{value}"
        if isinstance(value, bool):
            self._attr_name = f"{control.name} {'ON' if value else 'OFF'}"
        elif control.code == "scene_switch":
            # scene_switch selects a key/group in the Smart Life panel.
            label = value.replace("switch_", "S")
            self._attr_name = f"Select {label}"
        else:
            self._attr_name = f"{value.capitalize()} mode"

    async def async_press(self) -> None:
        await self.send(self._value)
