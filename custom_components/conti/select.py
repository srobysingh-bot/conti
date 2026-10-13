"""Opt-in enum settings declared by a Tuya control model."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .model_controls import ModelControl, ModelControlEntity, entry_controls


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([
        ContiModelSelect(coordinator, entry, control)
        for control in entry_controls(entry)
        if control.kind == "enum" and control.code not in {"scene_switch", "mode_controller"}
    ])


class ContiModelSelect(ModelControlEntity, SelectEntity):
    """Write-only controls show the last requested value, not confirmed state."""

    def __init__(self, coordinator: Any, entry: ConfigEntry, control: ModelControl) -> None:
        super().__init__(coordinator, entry, control)
        self._attr_unique_id = f"conti_{self._device_id}_command_{control.dp_id}"
        self._attr_options = list(control.options)
        if control.code.startswith("button_mode_"):
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def current_option(self) -> str | None:
        value = self.current_value()
        return value if value in self.control.options else None

    async def async_select_option(self, option: str) -> None:
        if option not in self.control.options:
            raise HomeAssistantError("Option is not declared in the control model.")
        await self.send(option)
