"""Tuya IR OAuth wrapper for Conti.

IR onboarding uses the Smart Life QR session managed by TuyaOAuthManager.
Do not route IR library or command calls through signed Tuya OpenAPI helpers:
QR-login accounts do not have access_id/access_secret.
"""

from __future__ import annotations

import asyncio
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
        self._library_supported = True

    async def list_categories(self, device_id: str) -> list[dict[str, Any]]:
        """List IR appliance categories supported by an IR hub."""
        result = await _retry_ir_api(self._oauth.async_get_ir_categories, device_id)
        if result in (None, {}, []):
            self._mark_library_unavailable(device_id)
            return []
        items = _coerce_list(result)
        categories = [
            {
                "id": str(
                    item.get("category")
                    or item.get("category_code")
                    or item.get("category_id")
                    or item.get("id")
                    or ""
                ).strip(),
                "name": str(item.get("category_name") or item.get("name") or "").strip(),
                "raw": item,
            }
            for item in items
            if isinstance(item, dict)
        ]
        _LOGGER.info("IR: Categories found=%d device=%s", len(categories), device_id)
        return categories

    async def list_device_remotes(self, device_id: str) -> list[dict[str, Any]]:
        """List remotes already available on an IR hub."""
        result = await _retry_ir_api(self._oauth.async_get_ir_device_remotes, device_id)
        if result in (None, {}, []):
            self._mark_library_unavailable(device_id)
            return []
        remotes = _normalize_remote_items(_coerce_list(result))
        _LOGGER.info("IR: Remotes found=%d device=%s", len(remotes), device_id)
        return remotes

    async def resolve_infrared_id(self, device_id: str) -> str:
        """Return the Tuya infrared_id used by the IR APIs."""
        infrared_id = await self._oauth.async_get_infrared_id(device_id)
        return str(infrared_id or "").strip()

    async def list_brands(
        self, device_id: str, category: str
    ) -> list[dict[str, Any]]:
        """List brands for an IR category."""
        result = await _retry_ir_api(
            self._oauth.async_get_ir_brands,
            category,
            device_id=device_id,
        )
        if result in (None, {}, []):
            self._mark_library_unavailable(device_id)
            return []
        items = _coerce_list(result)
        return [
            {
                "id": str(
                    item.get("brand_id")
                    or item.get("brand_code")
                    or item.get("brand")
                    or item.get("id")
                    or ""
                ).strip(),
                "name": str(
                    item.get("brand_name")
                    or item.get("name")
                    or item.get("brand")
                    or ""
                ).strip(),
                "raw": item,
            }
            for item in items
            if isinstance(item, dict)
        ]

    async def list_models(
        self, device_id: str, category: str, brand: str
    ) -> list[dict[str, Any]]:
        """List model/remote indexes for an IR brand."""
        result = await _retry_ir_api(
            self._oauth.async_get_ir_remotes,
            category,
            brand,
            device_id=device_id,
        )
        if result in (None, {}, []):
            self._mark_library_unavailable(device_id)
            return []
        items = _coerce_list(result)
        models = _normalize_remote_items(
            items,
            default_category=category,
            default_brand=brand,
        )
        return [item for item in models if item.get("id")]

    async def fetch_commands(
        self, device_id: str, model: dict[str, Any] | str
    ) -> dict[str, dict[str, Any]]:
        """Create the selected remote, then fetch and normalize its keys."""
        model_data = _parse_model(model)
        category_id = str(model_data.get("category_id", "")).strip()
        brand_id = str(model_data.get("brand_id", "")).strip()
        remote_index = str(
            model_data.get("remote_index")
            or model_data.get("remote_id")
            or model_data.get("id")
            or ""
        ).strip()
        if not category_id or not brand_id or not remote_index:
            raise ValueError("IR model must include category_id, brand_id and remote_index")

        remote_id = str(model_data.get("remote_id") or "").strip()
        if not remote_id:
            remote_id = await self.ensure_remote(
                device_id,
                category_id,
                brand_id,
                remote_index,
            )

        if remote_id:
            result = await _retry_ir_api(
                self._oauth.async_get_ir_remote_keys,
                device_id,
                remote_id,
            )
            if result in (None, {}, []):
                self._mark_library_unavailable(device_id)
                return {}
            commands = _normalize_commands(
                result,
                category_id=category_id,
                brand_id=brand_id,
                remote_index=remote_index,
                remote_id=remote_id,
            )
            _LOGGER.info(
                "Fetched IR keys for %s remote_id=%s remote_index=%s (%d commands)",
                device_id,
                remote_id,
                remote_index,
                len(commands),
            )
            return commands

        _LOGGER.warning(
            "IR: Cannot fetch keys without remote_id device=%s remote_index=%s",
            device_id,
            remote_index,
        )
        return {}

    async def ensure_remote(
        self,
        device_id: str,
        category_id: str,
        brand_id: str,
        remote_index: str,
    ) -> str:
        """Ensure the selected library remote exists and return remote_id."""
        await _retry_ir_api(
            self._oauth.async_add_ir_remote,
            device_id,
            category_id,
            brand_id,
            remote_index,
            name="Conti Remote",
        )
        remotes = await self.list_device_remotes(device_id)
        remote_id = _select_remote_id(remotes, category_id, brand_id, remote_index)
        if remote_id:
            _LOGGER.info(
                "IR: Created/resolved remote_id=%s device=%s remote_index=%s",
                remote_id,
                device_id,
                remote_index,
            )
        else:
            _LOGGER.warning(
                "IR: add-remote did not yield a remote_id device=%s remote_index=%s remotes=%s",
                device_id,
                remote_index,
                remotes,
            )
        return remote_id

    async def send_command(self, device_id: str, command: dict[str, Any]) -> bool:
        """Send a stored command through the Smart Life OAuth session."""
        return await self._oauth.async_send_ir_command(device_id, command)

    async def is_device_online(self, device_id: str) -> bool:
        """Return whether the physical IR hub is reachable through cloud state."""
        online = getattr(self._oauth, "async_is_device_online", None)
        if online is None:
            return False
        return bool(await online(device_id))

    async def send_raw_runtime_command(
        self,
        device_id: str,
        raw_code: Any,
        *,
        infrared_id: str = "",
        remote_id: str = "",
    ) -> bool:
        """Send raw IR through Tuya's normal runtime remote endpoint."""
        resolved_infrared_id = infrared_id or await self.resolve_infrared_id(device_id)
        return await self._oauth.send_raw_runtime_command(
            resolved_infrared_id,
            remote_id,
            raw_code,
            device_id=device_id,
        )

    async def send_ac_runtime_command(
        self,
        device_id: str,
        state_payload: dict[str, Any],
        *,
        infrared_id: str = "",
        remote_id: str = "",
    ) -> bool:
        """Send structured AC state through Tuya's runtime AC endpoint."""
        resolved_infrared_id = infrared_id or await self.resolve_infrared_id(device_id)
        return await self._oauth.send_ac_runtime_command(
            resolved_infrared_id,
            remote_id,
            state_payload,
            device_id=device_id,
        )

    async def send_raw_command(
        self,
        device_id: str,
        payload: Any,
        *,
        remote_id: str = "",
    ) -> bool:
        """Send a raw IR payload through a bound Tuya runtime remote."""
        body = payload if isinstance(payload, dict) else {"code": payload}
        command = {
            "source": "raw",
            "payload": {
                **body,
                "remote_id": remote_id,
            },
        }
        return await self.send_raw_runtime_command(
            device_id,
            body,
            remote_id=remote_id,
        )

    async def start_learning(self, device_id: str, remote_id: str = "") -> str:
        """Enable IR learning mode and return the learning timestamp."""
        infrared_id = await self.resolve_infrared_id(device_id)
        _LOGGER.info(
            "IR learning start device_id=%s infrared_id=%s remote_id=%s",
            device_id,
            infrared_id,
            remote_id,
        )
        result = await self._oauth.async_start_ir_learning(device_id, remote_id)
        _LOGGER.debug("IR LEARN START response=%s", result)
        if result in (None, False):
            _LOGGER.warning(
                "IR learning start failed device=%s infrared_id=%s response=%s",
                device_id,
                infrared_id,
                result,
            )
            return ""
        learning_time = ""
        if isinstance(result, dict):
            learning_time = str(
                result.get("t")
                or result.get("learning_time")
                or result.get("learningTime")
                or result.get("time")
                or ""
            )
        if not learning_time:
            learning_time = str(int(time.time() * 1000))
        _LOGGER.info(
            "IR learning mode started device=%s infrared_id=%s learning_time=%s",
            device_id,
            infrared_id,
            learning_time,
        )
        return learning_time

    async def stop_learning(self, device_id: str, remote_id: str = "") -> None:
        """Disable IR learning mode."""
        stop = getattr(self._oauth, "async_stop_ir_learning", None)
        if stop is None:
            return
        result = await stop(device_id, remote_id)
        _LOGGER.debug("IR LEARN STOP response=%s", result)
        _LOGGER.info("IR learning mode stopped device=%s response=%s", device_id, result)

    async def capture_learning_code(
        self, device_id: str, learning_time: str, remote_id: str = ""
    ) -> dict[str, Any] | None:
        """Query the IR code captured during learning mode."""
        result = await self._oauth.async_capture_ir_learning_code(
            device_id,
            learning_time,
            remote_id,
        )
        _LOGGER.debug("IR LEARN POLL response=%s", result)
        if not isinstance(result, dict):
            _LOGGER.warning(
                "IR learning capture returned non-dict device=%s response=%s",
                device_id,
                result,
            )
            return None
        code = (
            result.get("code")
            or result.get("codes")
            or result.get("key")
            or result.get("data")
            or result.get("payload")
        )
        success = bool(result.get("success", bool(code)))
        if not success or not code:
            _LOGGER.warning(
                "IR learning capture empty device=%s learning_time=%s response=%s",
                device_id,
                learning_time,
                result,
            )
            return None
        return {"code": code, "learning_time": learning_time}

    def _mark_library_unavailable(self, device_id: str) -> None:
        """Record optional library failure without blocking raw IR operation."""
        if self._library_supported:
            _LOGGER.warning(
                "IR cloud library unavailable, continuing in raw mode device=%s",
                device_id,
            )
        self._library_supported = False


async def _retry_ir_api(call: Any, *args: Any, attempts: int = 2, **kwargs: Any) -> Any:
    """Retry flaky Tuya IR reads once before falling back."""
    last_result: Any = None
    for attempt in range(attempts):
        try:
            result = await call(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("IR API call failed attempt=%d/%d: %s", attempt + 1, attempts, exc)
            result = None
        if result not in (None, {}, []):
            return result
        last_result = result
        if attempt + 1 < attempts:
            await asyncio.sleep(0.5)
    return last_result


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


def _normalize_remote_items(
    items: list[Any],
    *,
    default_category: str = "",
    default_brand: str = "",
) -> list[dict[str, Any]]:
    """Normalize Tuya remote records to Conti's model shape."""
    remotes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        remote_id = str(
            item.get("remote_id")
            or item.get("id")
            or ""
        ).strip()
        remote_index = str(
            item.get("remote_index")
            or item.get("remote_index_id")
            or remote_id
            or ""
        ).strip()
        model_id = remote_index or remote_id
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        category_id = str(
            item.get("category")
            or item.get("category_code")
            or item.get("category_id")
            or default_category
            or ""
        ).strip()
        brand_id = str(
            item.get("brand_id")
            or item.get("brand_code")
            or item.get("brand")
            or default_brand
            or ""
        ).strip()
        name = str(
            item.get("remote_name")
            or item.get("model")
            or item.get("name")
            or item.get("brand_name")
            or model_id
        ).strip()
        remotes.append(
            {
                "id": model_id,
                "name": name,
                "category_id": category_id,
                "brand_id": brand_id,
                "remote_id": remote_id,
                "remote_index": remote_index,
                "raw": item,
            }
        )
    return remotes


def _normalize_commands(
    payload: Any,
    *,
    category_id: str,
    brand_id: str,
    remote_index: str,
    remote_id: str = "",
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
                "remote_id": remote_id,
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


def _select_remote_id(
    remotes: list[dict[str, Any]],
    category_id: str,
    brand_id: str,
    remote_index: str,
) -> str:
    """Pick the remote_id that best matches the selected library index."""
    fallback = ""
    for remote in remotes:
        remote_id = str(remote.get("remote_id") or remote.get("id") or "").strip()
        if not remote_id:
            continue
        if not fallback:
            fallback = remote_id
        item_category = str(remote.get("category_id") or "").strip()
        item_brand = str(remote.get("brand_id") or "").strip()
        item_index = str(remote.get("remote_index") or remote.get("id") or "").strip()
        if (
            item_index == remote_index
            and (not item_category or item_category == category_id)
            and (not item_brand or item_brand == brand_id)
        ):
            return remote_id
    return fallback
