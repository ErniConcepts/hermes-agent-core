from __future__ import annotations

import json
import time
from email.header import decode_header
from pathlib import Path
from typing import Any

from hermes_cli.config import _secure_dir, _secure_file
from hermes_cli.product_config import get_product_storage_root
from utils import atomic_json_write


def get_tailnet_identity_bindings_path() -> Path:
    return get_product_storage_root() / "tailnet" / "bindings.json"


def normalize_tailnet_login(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    decoded_parts: list[str] = []
    for chunk, encoding in decode_header(raw):
        if isinstance(chunk, bytes):
            decoded_parts.append(chunk.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded_parts.append(chunk)
    return "".join(decoded_parts).strip().lower()


def _load_bindings() -> list[dict[str, Any]]:
    path = get_tailnet_identity_bindings_path()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else []


def _save_bindings(bindings: list[dict[str, Any]]) -> None:
    path = get_tailnet_identity_bindings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(path.parent)
    atomic_json_write(path, bindings)
    _secure_file(path)


def get_tailnet_login_for_user(user_id: str) -> str | None:
    target = str(user_id or "").strip()
    if not target:
        return None
    for item in _load_bindings():
        if str(item.get("user_id", "")).strip() == target:
            login = normalize_tailnet_login(item.get("tailscale_login"))
            return login or None
    return None


def get_user_id_for_tailnet_login(tailscale_login: str) -> str | None:
    target = normalize_tailnet_login(tailscale_login)
    if not target:
        return None
    for item in _load_bindings():
        if normalize_tailnet_login(item.get("tailscale_login")) == target:
            user_id = str(item.get("user_id", "")).strip()
            return user_id or None
    return None


def bind_tailnet_login(user_id: str, tailscale_login: str) -> dict[str, Any]:
    normalized_user_id = str(user_id or "").strip()
    normalized_login = normalize_tailnet_login(tailscale_login)
    if not normalized_user_id:
        raise ValueError("User id is required")
    if not normalized_login:
        raise ValueError("A verified Tailnet login is required")

    existing_user_id = get_user_id_for_tailnet_login(normalized_login)
    if existing_user_id and existing_user_id != normalized_user_id:
        raise ValueError("This Tailnet identity is already linked to another product user")

    bindings = [
        item
        for item in _load_bindings()
        if str(item.get("user_id", "")).strip() != normalized_user_id
    ]
    payload = {
        "user_id": normalized_user_id,
        "tailscale_login": normalized_login,
        "bound_at": int(time.time()),
    }
    bindings.append(payload)
    _save_bindings(bindings)
    return payload


def unbind_tailnet_login(user_id: str) -> bool:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return False
    bindings = _load_bindings()
    retained = [
        item
        for item in bindings
        if str(item.get("user_id", "")).strip() != normalized_user_id
    ]
    if len(retained) == len(bindings):
        return False
    _save_bindings(retained)
    return True
