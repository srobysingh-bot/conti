"""Opt-in dashboard commands declared by a user-supplied Tuya data model."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, cast

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CONNECTION_TYPE,
    CONF_CONTROL_MODEL,
    CONF_DEVICE_ID,
    CONF_DEVICE_METADATA,
    CONF_GATEWAY_DEVICE_ID,
    CONNECTION_TYPE_GATEWAY_CHILD,
    DOMAIN,
)
from .coordinator import ContiCoordinator


@dataclass(frozen=True)
class ModelControl:
    """Only explicit writable bool, enum and numeric properties are commands."""

    dp_id: int
    code: str
    access: str
    kind: str
    options: tuple[str, ...] = ()
    minimum: int = 0
    maximum: int = 0
    step: int = 1
    scale: int = 0

    @property
    def name(self) -> str:
        labels = {
            "switch_controller": "Power",
            "mode_controller": "Lighting mode",
            "bright_controller": "Brightness level",
            "temp_controller": "Colour temperature level",
            "scene_switch": "Key selection",
        }
        return labels.get(self.code, self.code.replace("_", " ").capitalize())


def parse_control_model(raw: str) -> tuple[ModelControl, ...]:
    """Accept the model JSON or the API response containing result.model.

    A model documents command types, not evidence of LAN firmware support or
    physical button reports. String/raw formats are deliberately not guessed.
    """
    if not raw.strip():
        return ()
    if len(raw) > 131072:
        raise ValueError("Control model is too large")
    model = json.loads(raw)
    if isinstance(model, dict) and "result" in model:
        result = model["result"]
        model = result.get("model") if isinstance(result, dict) else None
    if isinstance(model, dict) and "model" in model:
        model = model["model"]
    if isinstance(model, str):
        model = json.loads(model)
    if not isinstance(model, dict) or not isinstance(model.get("services"), list):
        raise ValueError("Expected Tuya model services")
    controls: list[ModelControl] = []
    seen: set[int] = set()
    for service in model["services"]:
        if not isinstance(service, dict) or not isinstance(service.get("properties"), list):
            raise ValueError("Expected Tuya model properties")
        for prop in service["properties"]:
            if not isinstance(prop, dict):
                raise ValueError("Invalid model property")
            if prop.get("accessMode") not in {"wr", "rw"}:
                continue
            spec = prop.get("typeSpec")
            if not isinstance(spec, dict) or spec.get("type") not in {"bool", "enum", "value"}:
                continue
            dp_id, code = prop.get("abilityId"), prop.get("code")
            if type(dp_id) is not int or not 1 <= dp_id <= 255 or dp_id in seen:
                raise ValueError("Invalid or duplicate command DP")
            if not isinstance(code, str) or not code or len(code) > 128:
                raise ValueError("Invalid command code")
            seen.add(dp_id)
            common = {"dp_id": dp_id, "code": code, "access": prop["accessMode"]}
            if spec["type"] == "enum":
                options = spec.get("range")
                if (
                    not isinstance(options, list) or not 1 <= len(options) <= 64
                    or any(not isinstance(v, str) or not v or len(v) > 128 for v in options)
                    or len(set(options)) != len(options)
                ):
                    raise ValueError("Invalid enum options")
                controls.append(ModelControl(**common, kind="enum", options=tuple(options)))
            elif spec["type"] == "value":
                numeric = [spec.get(k) for k in ("min", "max", "step", "scale")]
                if any(type(v) is not int for v in numeric):
                    raise ValueError("Invalid numeric range")
                lo, hi, step, scale = cast(list[int], numeric)
                if lo > hi or step <= 0 or not 0 <= scale <= 6:
                    raise ValueError("Invalid numeric range")
                controls.append(ModelControl(
                    **common, kind="value", minimum=lo, maximum=hi, step=step, scale=scale,
                ))
            else:
                controls.append(ModelControl(**common, kind="bool"))
    if not controls:
        raise ValueError("Model has no supported writable controls")
    return tuple(controls)


def canonical_control_model(raw: str) -> str:
    """Store just validated command declarations, excluding API credentials."""
    controls = parse_control_model(raw)
    if not controls:
        return ""
    properties = []
    for control in controls:
        spec: dict[str, Any] = {"type": control.kind}
        if control.kind == "enum":
            spec["range"] = list(control.options)
        elif control.kind == "value":
            spec.update({
                "min": control.minimum, "max": control.maximum,
                "step": control.step, "scale": control.scale,
            })
        properties.append({
            "abilityId": control.dp_id, "code": control.code,
            "accessMode": control.access, "typeSpec": spec,
        })
    return json.dumps({"services": [{"properties": properties}]})


def entry_controls(entry: Any) -> tuple[ModelControl, ...]:
    """No automatic controls, and no changes to direct-device platforms."""
    if entry.data.get(CONF_CONNECTION_TYPE) != CONNECTION_TYPE_GATEWAY_CHILD:
        return ()
    try:
        return parse_control_model(entry.options.get(CONF_CONTROL_MODEL, ""))
    except (ValueError, TypeError, AttributeError):
        return ()


class ModelControlEntity(CoordinatorEntity[ContiCoordinator]):
    """Send through the existing child manager; never query/open another socket."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ContiCoordinator, entry: Any, control: ModelControl) -> None:
        super().__init__(coordinator)
        self.control = control
        self._device_id = entry.data[CONF_DEVICE_ID]
        self._last_command: Any = None
        metadata = entry.data.get(CONF_DEVICE_METADATA, {})
        self._attr_name = control.name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._device_id)},
            "via_device": (DOMAIN, entry.data[CONF_GATEWAY_DEVICE_ID]),
            "name": entry.title,
            "manufacturer": metadata.get("manufacturer") or "Tuya",
            "model": metadata.get("model") or metadata.get("product_name"),
        }

    @property
    def available(self) -> bool:
        return bool(self.coordinator.is_device_available())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"command_only": self.control.access == "wr"}

    def current_value(self) -> Any:
        if self.control.access == "wr":
            return self._last_command
        return (self.coordinator.data or {}).get(self._device_id, {}).get(
            str(self.control.dp_id), self._last_command,
        )

    async def send(self, value: Any) -> None:
        if not self.available:
            raise HomeAssistantError("The Conti gateway child is unavailable.")
        if not await self.coordinator.device_manager.set_dp(
            self._device_id, self.control.dp_id, value,
        ):
            raise HomeAssistantError("The gateway command could not be sent.")
        self._last_command = value
        self.async_write_ha_state()

    def numeric_command(self, value: float) -> int:
        """Validate the exact model range and step before sending an integer."""
        if isinstance(value, bool) or not math.isfinite(value):
            raise HomeAssistantError("Invalid numeric command.")
        scaled = value * 10**self.control.scale
        raw = int(round(scaled))
        if (
            not math.isclose(scaled, raw, abs_tol=1e-6, rel_tol=0)
            or not self.control.minimum <= raw <= self.control.maximum
            or (raw - self.control.minimum) % self.control.step
        ):
            raise HomeAssistantError("Command is outside the model range or step.")
        return raw
