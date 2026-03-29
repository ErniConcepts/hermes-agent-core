"""Tailscale API helpers for product-side tsidp policy automation."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote

import httpx

from hermes_cli.config import _secure_dir, _secure_file, get_env_value
from hermes_cli.product_config import get_product_storage_root, load_product_config


_TSIDP_CAPABILITY = "tailscale.com/cap/tsidp"
_TAILSCALE_API_BASE_URL = "https://api.tailscale.com"


def get_tailscale_policy_backups_root() -> Path:
    return get_product_storage_root() / "tailscale-policy-backups"


def _tsidp_service_config(config: Dict[str, Any]) -> Dict[str, Any]:
    services_cfg = config.get("services", {}).get("tsidp", {})
    if not isinstance(services_cfg, dict):
        raise RuntimeError("services.tsidp must be configured")
    return services_cfg


def _required_tailscale_api_token(config: Dict[str, Any]) -> str:
    env_key = str(_tsidp_service_config(config).get("api_token_ref", "")).strip()
    if not env_key:
        raise RuntimeError("services.tsidp.api_token_ref must be configured")
    token = str(get_env_value(env_key) or "").strip()
    if not token:
        raise RuntimeError(
            "A Tailscale API token is required so setup can update tailnet policy for tsidp automatically."
        )
    return token


def _required_api_tailnet_name(config: Dict[str, Any]) -> str:
    tailscale = config.get("network", {}).get("tailscale", {})
    if not isinstance(tailscale, dict):
        raise RuntimeError("product network.tailscale must be configured")
    value = str(tailscale.get("api_tailnet_name", "")).strip()
    if not value:
        raise RuntimeError("product network.tailscale.api_tailnet_name must be configured")
    return value


def _policy_url(config: Dict[str, Any]) -> str:
    tailnet_name = quote(_required_api_tailnet_name(config), safe="")
    return f"/api/v2/tailnet/{tailnet_name}/acl"


def _member_tsidp_grant() -> Dict[str, Any]:
    return {
        "src": ["autogroup:member"],
        "dst": ["*"],
        "app": {
            _TSIDP_CAPABILITY: [
                {
                    "users": ["*"],
                    "resources": ["*"],
                }
            ]
        },
    }


def _admin_tsidp_grant() -> Dict[str, Any]:
    return {
        "src": ["autogroup:admin"],
        "dst": ["*"],
        "app": {
            _TSIDP_CAPABILITY: [
                {
                    "allow_admin_ui": True,
                    "users": ["*"],
                    "resources": ["*"],
                }
            ]
        },
    }


def _grant_capability_entries(grant: Dict[str, Any]) -> list[Dict[str, Any]]:
    app = grant.get("app", {})
    if not isinstance(app, dict):
        return []
    entries = app.get(_TSIDP_CAPABILITY, [])
    if not isinstance(entries, list):
        return []
    return [item for item in entries if isinstance(item, dict)]


def _is_matching_tsidp_grant(grant: Dict[str, Any], *, allow_admin_ui: bool) -> bool:
    src = grant.get("src")
    dst = grant.get("dst")
    if allow_admin_ui:
        if src != ["autogroup:admin"] or dst != ["*"]:
            return False
    else:
        if src != ["autogroup:member"] or dst != ["*"]:
            return False
    for entry in _grant_capability_entries(grant):
        if entry.get("users") != ["*"] or entry.get("resources") != ["*"]:
            continue
        if bool(entry.get("allow_admin_ui", False)) == allow_admin_ui:
            return True
    return False


def policy_has_required_tsidp_grants(policy: Dict[str, Any]) -> bool:
    grants = policy.get("grants", [])
    if not isinstance(grants, list):
        return False
    return any(_is_matching_tsidp_grant(grant, allow_admin_ui=False) for grant in grants if isinstance(grant, dict)) and any(
        _is_matching_tsidp_grant(grant, allow_admin_ui=True) for grant in grants if isinstance(grant, dict)
    )


def merge_tsidp_policy_grants(policy: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    updated = copy.deepcopy(policy)
    grants = updated.get("grants")
    if not isinstance(grants, list):
        grants = []
        updated["grants"] = grants
    changed = False
    if not any(_is_matching_tsidp_grant(grant, allow_admin_ui=False) for grant in grants if isinstance(grant, dict)):
        grants.append(_member_tsidp_grant())
        changed = True
    if not any(_is_matching_tsidp_grant(grant, allow_admin_ui=True) for grant in grants if isinstance(grant, dict)):
        grants.append(_admin_tsidp_grant())
        changed = True
    return updated, changed


def _backup_policy(policy: Dict[str, Any]) -> Path:
    backup_root = get_tailscale_policy_backups_root()
    backup_root.mkdir(parents=True, exist_ok=True)
    _secure_dir(backup_root)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_path = backup_root / f"acl-{stamp}.json"
    backup_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _secure_file(backup_path)
    return backup_path


def _request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    json_payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    response = client.request(method, path, json=json_payload)
    if response.status_code == 401:
        raise RuntimeError("The Tailscale API token was rejected. Create a valid admin API token and rerun setup.")
    if response.status_code == 403:
        raise RuntimeError("The Tailscale API token does not have permission to edit tailnet policy.")
    if response.status_code >= 400:
        raise RuntimeError(f"Tailscale API {method} {path} failed with {response.status_code}: {response.text}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Tailscale API {method} {path} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Tailscale API {method} {path} returned an unexpected payload")
    return payload


def ensure_tsidp_policy(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    product_config = config or load_product_config()
    token = _required_tailscale_api_token(product_config)
    path = _policy_url(product_config)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    with httpx.Client(base_url=_TAILSCALE_API_BASE_URL, headers=headers, timeout=20.0) as client:
        current_policy = _request_json(client, "GET", path)
        merged_policy, changed = merge_tsidp_policy_grants(current_policy)
        backup_path: Path | None = None
        if changed:
            backup_path = _backup_policy(current_policy)
            _request_json(client, "POST", path, json_payload=merged_policy)
        verified_policy = _request_json(client, "GET", path)
    if not policy_has_required_tsidp_grants(verified_policy):
        raise RuntimeError("Tailnet policy update completed, but the required tsidp grants are still missing.")
    return {
        "changed": changed,
        "backup_path": str(backup_path) if backup_path else "",
        "tailnet": _required_api_tailnet_name(product_config),
    }
