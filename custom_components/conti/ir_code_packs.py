"""Local raw IR code pack helpers for Conti."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ir_actions import normalize_ir_action

PACK_SCHEMA_VERSION = 1
PACK_DIR_NAME = "conti_ir_packs"
BUNDLED_PACK_DIR = Path(__file__).resolve().parent / "ir_packs"


class IRCodePackError(ValueError):
    """Raised when an IR code pack is malformed."""


def normalize_raw_payload(payload: Any) -> dict[str, Any]:
    """Normalize a raw IR payload into the command shape used by storage."""
    if isinstance(payload, dict):
        source = str(payload.get("source") or "raw").strip() or "raw"
        body = payload.get("payload", payload)
        if isinstance(body, dict) and body.get("payload") is not None:
            body = body["payload"]
        return {"source": source, "payload": body}
    return {"source": "raw", "payload": {"code": payload}}


def normalize_code_pack(pack: dict[str, Any]) -> dict[str, Any]:
    """Return normalized pack metadata and command payloads."""
    schema_version = int(pack.get("schema_version") or PACK_SCHEMA_VERSION)
    if schema_version != PACK_SCHEMA_VERSION:
        raise IRCodePackError(
            f"Unsupported IR code pack schema_version={schema_version}"
        )

    raw_commands = pack.get("commands", pack)
    if not isinstance(raw_commands, dict):
        raise IRCodePackError("IR code pack commands must be a mapping")

    commands: dict[str, dict[str, Any]] = {}
    for action, payload in raw_commands.items():
        normalized_action = normalize_ir_action(str(action))
        if not normalized_action:
            continue
        if normalized_action in commands:
            raise IRCodePackError(
                f"Duplicate IR command after normalization: {normalized_action}"
            )
        commands[normalized_action] = normalize_raw_payload(payload)
    if not commands:
        raise IRCodePackError("IR code pack must contain at least one command")

    return {
        "schema_version": schema_version,
        "manufacturer": str(pack.get("manufacturer") or "").strip(),
        "model": str(pack.get("model") or "").strip(),
        "type": str(pack.get("type") or pack.get("profile_type") or "").strip(),
        "commands": commands,
    }


async def async_load_code_pack(path: Path) -> dict[str, Any]:
    """Load a JSON or YAML IR code pack from disk."""
    text = await _async_read_text(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        payload = _load_yaml(text)
    else:
        raise ValueError(f"Unsupported IR code pack file type: {path.suffix}")
    if not isinstance(payload, dict):
        raise ValueError("IR code pack root must be a mapping")
    return normalize_code_pack(payload)


def list_manufacturers() -> list[str]:
    """List bundled IR pack manufacturers."""
    if not BUNDLED_PACK_DIR.exists():
        return []
    return sorted(
        path.name
        for path in BUNDLED_PACK_DIR.iterdir()
        if path.is_dir() and list(path.glob("*.json"))
    )


def list_models(manufacturer: str) -> list[str]:
    """List bundled IR pack model IDs for a manufacturer."""
    manufacturer_slug = _slug(manufacturer)
    pack_dir = BUNDLED_PACK_DIR / manufacturer_slug
    if not pack_dir.exists():
        return []
    return sorted(path.stem for path in pack_dir.glob("*.json") if path.is_file())


def load_ir_pack(manufacturer: str, model: str) -> dict[str, Any]:
    """Load and validate one bundled JSON IR pack."""
    path = BUNDLED_PACK_DIR / _slug(manufacturer) / f"{_slug(model)}.json"
    if not path.exists():
        raise IRCodePackError(f"Bundled IR pack not found: {manufacturer}/{model}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise IRCodePackError("IR code pack root must be a mapping")
    return normalize_code_pack(payload)


def _slug(value: str) -> str:
    return str(value).strip().lower().replace(" ", "_")


async def async_export_code_pack(path: Path, pack: dict[str, Any]) -> None:
    """Write an IR code pack to disk as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PACK_SCHEMA_VERSION,
        "manufacturer": pack.get("manufacturer", ""),
        "model": pack.get("model", ""),
        "type": pack.get("type", ""),
        "commands": pack.get("commands", {}),
    }
    await _async_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def _load_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on HA env
        raise ValueError("YAML IR code packs require PyYAML") from exc
    return yaml.safe_load(text)


async def _async_read_text(path: Path) -> str:
    return await _run_io(path.read_text)


async def _async_write_text(path: Path, text: str) -> None:
    await _run_io(path.write_text, text)


async def _run_io(func: Any, *args: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(func, *args)
