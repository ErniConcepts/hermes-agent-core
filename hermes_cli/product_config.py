"""Product-layer configuration for the hermes-core distribution.

This file is intentionally separate from ``config.yaml``. ``product.yaml`` is
the canonical source of truth for setup-owned product behavior.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict

import yaml

from hermes_constants import get_hermes_home
from hermes_cli.config import (
    ensure_hermes_home,
    load_config,
    _secure_dir,
    _secure_file,
)
from toolsets import validate_toolset


DEFAULT_PRODUCT_CONFIG: Dict[str, Any] = {
    "product": {
        "brand": {
            "name": "Hermes Core",
            "logo_path": "",
        },
        "agent": {
            "soul_template_path": "",
        },
    },
    "auth": {
        "provider": "tsidp",
        "mode": "oidc",
        "issuer_url": "",
        "client_id": "",
        "client_secret_ref": "HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET",
        "session_secret_ref": "HERMES_PRODUCT_SESSION_SECRET",
        "client_id_ref": "HERMES_PRODUCT_TSIDP_OIDC_CLIENT_ID",
    },
    "network": {
        "bind_host": "127.0.0.1",
        "trusted_proxy_ips": ["127.0.0.1", "::1"],
        "url_scheme": "https",
        "app_port": 8086,
        "tailscale": {
            "enabled": True,
            "tailnet_name": "",
            "api_tailnet_name": "",
            "device_name": "",
            "app_https_port": 443,
            "command_path": "tailscale",
            "idp_hostname": "idp",
        },
    },
    "runtime": {
        "isolation_runtime": "runsc",
        "image": "hermes-core-product-runtime:local",
        "internal_port": 8091,
        "host_port_start": 18091,
        "host_port_end": 18150,
        "host_access_host": "host.docker.internal",
        "pids_limit": 256,
        "backend_policy": "auto_local_managed",
        "tool_call_parser": "hermes",
    },
    "storage": {
        "root": "product",
        "users_root": "product/users",
        "user_workspace_limit_mb": 2048,
    },
    "bootstrap": {
        "first_admin_username": "",
        "first_admin_display_name": "Administrator",
        "first_admin_email": "",
    },
    "services": {
        "tsidp": {
            "mode": "docker",
            "container_name": "hermes-tsidp",
            "image": "ghcr.io/tailscale/tsidp:latest",
            "auth_key_ref": "HERMES_PRODUCT_TAILSCALE_AUTH_KEY",
            "api_token_ref": "HERMES_PRODUCT_TAILSCALE_API_TOKEN",
            "advertise_tags": ["tag:tsidp"],
        },
    },
}


def get_product_config_path() -> Path:
    return get_hermes_home() / "product.yaml"


def _storage_relative_path(config: Dict[str, Any], key: str) -> str:
    relative = str(config.get("storage", {}).get(key, "")).strip()
    if not relative:
        raise ValueError(f"product storage.{key} must be configured")
    return relative


def get_product_storage_root(
    home: Path | None = None,
    *,
    config: Dict[str, Any] | None = None,
) -> Path:
    hermes_home = home or get_hermes_home()
    product_config = config or (DEFAULT_PRODUCT_CONFIG if home is not None else load_product_config())
    return hermes_home / _storage_relative_path(product_config, "root")


def get_product_users_root(
    home: Path | None = None,
    *,
    config: Dict[str, Any] | None = None,
) -> Path:
    hermes_home = home or get_hermes_home()
    product_config = config or (DEFAULT_PRODUCT_CONFIG if home is not None else load_product_config())
    return hermes_home / _storage_relative_path(product_config, "users_root")


def ensure_product_home() -> None:
    ensure_hermes_home()
    hermes_home = get_hermes_home()
    product_root = get_product_storage_root(hermes_home, config=DEFAULT_PRODUCT_CONFIG)
    users_root = get_product_users_root(hermes_home, config=DEFAULT_PRODUCT_CONFIG)
    for path in (
        product_root,
        users_root,
        product_root / "logs",
        product_root / "services",
        product_root / "bootstrap",
    ):
        path.mkdir(parents=True, exist_ok=True)
        _secure_dir(path)


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_product_config() -> Dict[str, Any]:
    ensure_product_home()
    config_path = get_product_config_path()
    config = copy.deepcopy(DEFAULT_PRODUCT_CONFIG)
    if config_path.exists():
        with open(config_path, encoding="utf-8") as handle:
            user_config = yaml.safe_load(handle) or {}
        if isinstance(user_config, dict):
            config = _deep_merge(config, user_config)
    return config


def save_product_config(config: Dict[str, Any]) -> None:
    from utils import atomic_yaml_write

    ensure_product_home()
    config_path = get_product_config_path()
    normalized = _deep_merge(DEFAULT_PRODUCT_CONFIG, config)
    atomic_yaml_write(config_path, normalized)
    _secure_file(config_path)


def initialize_product_config_file() -> Dict[str, Any]:
    config = load_product_config()
    config_path = get_product_config_path()
    if not config_path.exists():
        save_product_config(config)
    return config


def resolve_hermes_runtime_toolsets() -> list[str]:
    config = load_config()
    platform_toolsets = config.get("platform_toolsets", {})
    configured = platform_toolsets.get("cli") if isinstance(platform_toolsets, dict) else None
    if not isinstance(configured, list):
        configured = config.get("toolsets", [])
    if not isinstance(configured, list):
        raise ValueError("Hermes CLI toolsets are invalid. Run 'hermes setup tools'.")
    normalized = [
        str(item).strip()
        for item in configured
        if str(item).strip() and validate_toolset(str(item).strip())
    ]
    if not normalized:
        raise ValueError("Hermes CLI toolsets must contain at least one valid toolset. Run 'hermes setup tools'.")
    return normalized


def resolve_hermes_model_config() -> Dict[str, Any]:
    config = load_config()
    model_cfg = config.get("model")
    if isinstance(model_cfg, dict):
        resolved = copy.deepcopy(model_cfg)
    elif isinstance(model_cfg, str) and model_cfg.strip():
        resolved = {"default": model_cfg.strip()}
    else:
        resolved = {}

    default_model = str(resolved.get("default", "")).strip()
    if not default_model:
        raise ValueError("Hermes model.default must be configured. Run 'hermes setup model'.")
    return resolved


def resolve_runtime_defaults(config: Dict[str, Any] | None = None) -> Dict[str, str]:
    normalized_toolsets = resolve_hermes_runtime_toolsets()
    inference_model = str(resolve_hermes_model_config().get("default", "")).strip()
    return {
        "runtime_mode": "product",
        "runtime_toolsets": ",".join(normalized_toolsets),
        "inference_model": inference_model,
    }


def runtime_host_access_host(config: Dict[str, Any] | None = None) -> str:
    product_config = config or load_product_config()
    runtime_cfg = product_config.get("runtime", {})
    configured = str(runtime_cfg.get("host_access_host", "")).strip()
    if not configured:
        raise ValueError("product runtime.host_access_host must be configured")
    return configured


def runtime_backend_policy(config: Dict[str, Any] | None = None) -> str:
    product_config = config or load_product_config()
    runtime_cfg = product_config.get("runtime", {})
    configured = str(runtime_cfg.get("backend_policy", "auto_local_managed")).strip().lower()
    if configured not in {"auto_local_managed", "standard", "managed"}:
        raise ValueError("product runtime.backend_policy must be auto_local_managed, standard, or managed")
    return configured


def runtime_tool_call_parser(config: Dict[str, Any] | None = None) -> str:
    product_config = config or load_product_config()
    runtime_cfg = product_config.get("runtime", {})
    configured = str(runtime_cfg.get("tool_call_parser", "hermes")).strip()
    if not configured:
        raise ValueError("product runtime.tool_call_parser must not be empty")
    return configured
