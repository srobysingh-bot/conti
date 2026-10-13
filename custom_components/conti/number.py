"""Opt-in bounded numeric commands declared by a Tuya control model."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .model_controls import ModelControl, ModelControlEntity, entry_controls


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([
        ContiModelNumber(coordinator, entry, control)
        for control in entry_controls(entry) if control.kind == "value"
    ])


class ContiModelNumber(ModelControlEntity, NumberEntity):
    """Use schema units; do not invent a Kelvin conversion or starting value."""

    def __init__(self, coordinator: Any, entry: ConfigEntry, control: ModelControl) -> None:
        super().__init__(coordinator, entry, control)
        divisor = 10**control.scale
        self._attr_unique_id = f"conti_{self._device_id}_command_{control.dp_id}"
        self._attr_native_min_value = control.minimum / divisor
        self._attr_native_max_value = control.maximum / divisor
        self._attr_native_step = control.step / divisor
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float | None:
        value = self.current_value()
        if type(value) not in (int, float):
            return None
        return float(value / 10**self.control.scale)

    async def async_set_native_value(self, value: float) -> None:
        await self.send(self.numeric_command(value))
