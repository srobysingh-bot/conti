"""Tuya IR OAuth wrapper for Conti.

IR onboarding uses the Smart Life QR session managed by TuyaOAuthManager.
Do not route IR library or command calls through signed Tuya OpenAPI helpers:
QR-login accounts do not have access_id/access_secret.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .ir_actions import normalize_ir_action
from .tuya_oauth import TuyaOAuthManager

_LOGGER = logging.getLogger(__name__)


class TuyaIRCloud:
    """Small wrapper around the Smart Life OAuth session for IR APIs."""

    def __init__(self, oauth_manager: TuyaOAuthManager) -> None:
        self._oauth = oauth_manager

    async def list_categories(self, device_id: str) -> list[dict[str, Any]]:
        """List IR appliance categories supported by an IR hub."""
        result = await self._oauth.async_get_ir_categories(device_id)
        items = _coerce_list(result)
        return [
            {
                "id": str(item.get("category_id") or item.get("id") or "").strip(),
                "name": str(item.get("category_name") or item.get("name") or "").strip(),
                "raw": item,
            }
            for item in items
            if isinstance(item, dict)
        ]

    async def list_brands(
        self, device_id: str, category: str
    ) -> list[dict[str, Any]]:
        """List brands for an IR category."""
        result = await self._oauth.async_get_ir_brands(
            category,
            device_id=device_id,
        )
        items = _coerce_list(result)
        return [
            {
                "id": str(item.get("brand_id") or item.get("id") or "").strip(),
                "name": str(item.get("brand_name") or item.get("name") or "").strip(),
                "raw": item,
            }
            for item in items
            if isinstance(item, dict)
        ]

    async def list_models(
        self, device_id: str, category: str, brand: str
    ) -> list[dict[str, Any]]:
        """List model/remote indexes for an IR brand."""
        result = await self._oauth.async_get_ir_remotes(
            category,
            brand,
            device_id=device_id,
        )
        items = _coerce_list(result)
        models = [
            {
                "id": str(
                    item.get("remote_index")
                    or item.get("remote_index_id")
                    or item.get("remote_id")
                    or item.get("id")
                    or ""
                ).strip(),
                "name": str(
                    item.get("remote_name")
                    or item.get("model")
                    or item.get("name")
                    or item.get("remote_index")
                    or item.get("remote_id")
                    or ""
                ).strip(),
                "category_id": category,
                "brand_id": brand,
                "remote_index": str(
                    item.get("remote_index")
                    or item.get("remote_index_id")
                    or item.get("remote_id")
                    or item.get("id")
                    or ""
                ).strip(),
                "raw": item,
            }
            for item in items
            if isinstance(item, dict)
        ]
        return [item for item in models if item.get("id")]

    async def fetch_commands(
        self, device_id: str, model: dict[str, Any] | str
    ) -> dict[str, dict[str, Any]]:
        """Fetch and normalize the command library for a remote index."""
        model_data = _parse_model(model)
        category_id = str(model_data.get("category_id", "")).strip()
        brand_id = str(model_data.get("brand_id", "")).strip()
        remote_index = str(
            model_data.get("remote_index") or model_data.get("id") or ""
        ).strip()
        if not category_id or not brand_id or not remote_index:
            raise ValueError("IR model must include category_id, brand_id and remote_index")

        result = await self._oauth.async_get_ir_remote_commands(
            device_id,
            category_id,
            brand_id,
            remote_index,
        )
        commands = _normalize_commands(
            result,
            category_id=category_id,
            brand_id=brand_id,
            remote_index=remote_index,
        )
        _LOGGER.info(
            "Fetched IR library for %s remote_index=%s (%d commands)",
            device_id,
            remote_index,
            len(commands),
        )
        return commands

    async def send_command(self, device_id: str, command: dict[str, Any]) -> bool:
        """Send a stored command through the Smart Life OAuth session."""
        return await self._oauth.async_send_ir_command(device_id, command)

    async def start_learning(self, device_id: str) -> str:
        """Enable IR learning mode and return the learning timestamp."""
        result = await self._oauth.async_start_ir_learning(device_id)
        learning_time = ""
        if isinstance(result, dict):
            learning_time = str(result.get("t") or result.get("learning_time") or "")
        if not learning_time:
            learning_time = str(int(time.time() * 1000))
        _LOGGER.info("IR learning mode started device=%s", device_id)
        return learning_time

    async def capture_learning_code(
        self, device_id: str, learning_time: str
    ) -> dict[str, Any] | None:
        """Query the IR code captured during learning mode."""
        result = await self._oauth.async_capture_ir_learning_code(
            device_id,
            learning_time,
        )
        if not isinstance(result, dict):
            return None
        code = result.get("code")
        success = bool(result.get("success", bool(code)))
        if not success or not code:
            return None
        return {"code": code, "learning_time": learning_time}


def _coerce_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("list", "items", "result", "records", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _parse_model(model: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(model, dict):
        return model
    parts = str(model).split(":")
    if len(parts) == 3:
        return {
            "category_id": parts[0],
            "brand_id": parts[1],
            "remote_index": parts[2],
        }
    return {"remote_index": str(model)}


def _normalize_commands(
    payload: Any,
    *,
    category_id: str,
    brand_id: str,
    remote_index: str,
) -> dict[str, dict[str, Any]]:
    items = _coerce_list(payload)
    if not items and isinstance(payload, dict):
        for key in ("rules", "keys", "commands"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break

    commands: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        action = _command_action(item)
        if not action:
            continue
        commands[action] = {
            "source": "cloud",
            "payload": {
                "category_id": category_id,
                "brand_id": brand_id,
                "remote_index": remote_index,
                "key_id": item.get("key_id") or item.get("keyId") or item.get("id"),
                "key": item.get("key") or item.get("code") or action,
                "rule": item,
            },
        }
    return commands


def _command_action(item: dict[str, Any]) -> str:
    raw = (
        item.get("code")
        or item.get("key")
        or item.get("key_name")
        or item.get("name")
        or item.get("key_id")
        or item.get("keyId")
        or ""
    )
    action = str(raw).strip().lower()
    for old, new in ((" ", "_"), ("-", "_"), ("/", "_")):
        action = action.replace(old, new)
    return normalize_ir_action("_".join(part for part in action.split("_") if part))
