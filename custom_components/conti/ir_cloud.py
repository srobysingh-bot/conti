"""Tuya IR OpenAPI wrapper for Conti."""

from __future__ import annotations

import logging
import time
from typing import Any

from .ir_actions import normalize_ir_action

_LOGGER = logging.getLogger(__name__)


class TuyaIRCloud:
    """Small wrapper around the existing Tuya cloud helper for IR APIs."""

    def __init__(self, cloud_helper: Any) -> None:
        self._helper = cloud_helper

    async def list_categories(self, device_id: str) -> list[dict[str, Any]]:
        """List IR appliance categories supported by an IR hub."""
        result = await self._api_get(f"/v1.0/infrareds/{device_id}/categories")
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
        result = await self._api_get(
            f"/v1.0/infrareds/{device_id}/categories/{category}/brands"
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
        paths = [
            f"/v2.0/infrareds/{device_id}/categories/{category}/brands/{brand}/remote-indexs",
            f"/v1.0/infrareds/{device_id}/categories/{category}/brands/{brand}",
        ]
        last: list[dict[str, Any]] = []
        for path in paths:
            result = await self._api_get(path, strict=False)
            items = _coerce_list(result)
            if items:
                last = [
                    {
                        "id": str(
                            item.get("remote_index")
                            or item.get("remote_index_id")
                            or item.get("id")
                            or ""
                        ).strip(),
                        "name": str(
                            item.get("remote_name")
                            or item.get("model")
                            or item.get("name")
                            or item.get("remote_index")
                            or ""
                        ).strip(),
                        "category_id": category,
                        "brand_id": brand,
                        "remote_index": str(
                            item.get("remote_index")
                            or item.get("remote_index_id")
                            or item.get("id")
                            or ""
                        ).strip(),
                        "raw": item,
                    }
                    for item in items
                    if isinstance(item, dict)
                ]
                break
        return [item for item in last if item.get("id")]

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

        path = (
            f"/v2.0/infrareds/{device_id}/categories/{category_id}/brands/"
            f"{brand_id}/remotes/{remote_index}/rules"
        )
        result = await self._api_get(path, strict=False)
        if not result:
            path = (
                f"/v1.0/infrareds/{device_id}/categories/{category_id}/brands/"
                f"{brand_id}/remotes/{remote_index}/rules"
            )
            result = await self._api_get(path, strict=True)

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
        """Send a stored command through Tuya Cloud."""
        payload = command.get("payload", command)
        if not isinstance(payload, dict):
            return False

        path = str(payload.get("path", "")).strip()
        body = payload.get("body")
        if path and isinstance(body, dict):
            result = await self._api_post(path, body, strict=False)
            return result is not None

        body = {
            key: value
            for key, value in payload.items()
            if key not in {"path", "method"}
        }
        if not body:
            return False

        if payload.get("remote_id"):
            path = f"/v2.0/infrareds/{device_id}/remotes/{payload['remote_id']}/raw/command"
        else:
            path = f"/v2.0/infrareds/{device_id}/testing/raw/command"
        result = await self._api_post(path, body, strict=False)
        return result is not None

    async def start_learning(self, device_id: str) -> str:
        """Enable IR learning mode and return the learning timestamp."""
        result = await self._api_put(
            f"/v1.0/infrareds/{device_id}/learning-state?state=true",
            {},
            strict=True,
        )
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
        result = await self._api_get(
            f"/v1.0/infrareds/{device_id}/learning-codes?learning_time={learning_time}",
            strict=True,
        )
        if not isinstance(result, dict):
            return None
        code = result.get("code")
        success = bool(result.get("success", bool(code)))
        if not success or not code:
            return None
        return {"code": code, "learning_time": learning_time}

    async def _api_get(
        self, path: str, strict: bool = True
    ) -> dict[str, Any] | list[Any] | None:
        ensure = getattr(self._helper, "_ensure_token", None)
        if ensure is not None and not await ensure(strict=strict):
            return None
        api_get = getattr(self._helper, "_api_get")
        return await api_get(path, strict=strict)

    async def _api_post(
        self, path: str, body: dict[str, Any], strict: bool = True
    ) -> dict[str, Any] | None:
        ensure = getattr(self._helper, "_ensure_token", None)
        if ensure is not None and not await ensure(strict=strict):
            return None
        api_post = getattr(self._helper, "_api_post")
        return await api_post(path, body, strict=strict)

    async def _api_put(
        self, path: str, body: dict[str, Any], strict: bool = True
    ) -> dict[str, Any] | None:
        """Make an authenticated PUT request using the existing helper."""
        import json as _json  # noqa: PLC0415

        import aiohttp  # noqa: PLC0415

        ensure = getattr(self._helper, "_ensure_token", None)
        if ensure is not None and not await ensure(strict=strict):
            return None

        token = getattr(self._helper, "_token", "")
        base_url = getattr(self._helper, "_base_url", "")
        sign_request = getattr(self._helper, "_sign_request")
        body_str = _json.dumps(body) if body else ""
        headers = sign_request("PUT", path, body_str, token)
        headers["Content-Type"] = "application/json"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    f"{base_url}{path}",
                    headers=headers,
                    data=body_str if body_str else None,
                    timeout=aiohttp.ClientTimeout(total=10),
                    ssl=True,
                ) as resp:
                    data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise
            _LOGGER.debug("Tuya cloud PUT %s error: %s", path, exc)
            return None

        if isinstance(data, dict) and data.get("success"):
            result = data.get("result", {})
            if isinstance(result, dict):
                result.setdefault("t", data.get("t"))
            return result if isinstance(result, dict) else {"result": result, "t": data.get("t")}

        if strict:
            raise RuntimeError(f"Tuya cloud PUT failed for {path}: {data}")
        return None


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
