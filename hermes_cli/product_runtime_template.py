from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home
from hermes_cli.product_config import load_product_config, resolve_hermes_runtime_toolsets
from hermes_cli.product_identity import render_product_soul
from hermes_cli.product_runtime_common import (
    ProductRuntimeLaunchSettings,
    secure_operator_readable_file,
    secure_runtime_writable_dir,
)
from hermes_cli.runtime_config import build_runtime_cli_config

_DEFAULT_TEMPLATE_NAME = "default"
_DEFAULT_PROFILE_NAME = "product-runtime"


def runtime_template_name() -> str:
    return _DEFAULT_TEMPLATE_NAME


def runtime_profile_name() -> str:
    return _DEFAULT_PROFILE_NAME


def runtime_template_root(config: dict[str, Any] | None = None) -> Path:
    product_config = config or load_product_config()
    return get_hermes_home() / str(product_config.get("storage", {}).get("root", "product")) / "runtime-template" / runtime_template_name()


def runtime_template_manifest_path(config: dict[str, Any] | None = None) -> Path:
    return runtime_template_root(config) / "template.json"


def runtime_template_profile_root(config: dict[str, Any] | None = None) -> Path:
    return runtime_template_root(config) / "profiles" / runtime_profile_name()


def _template_payload(
    *,
    product_config: dict[str, Any],
    launch_settings: ProductRuntimeLaunchSettings,
    runtime_config: dict[str, object],
    soul_text: str,
) -> dict[str, Any]:
    profile_name = runtime_profile_name()
    config_text = yaml.safe_dump(runtime_config, sort_keys=False) if runtime_config else ""
    version_source = json.dumps(
        {
            "profile_name": profile_name,
            "toolsets": resolve_hermes_runtime_toolsets(),
            "model": launch_settings.model,
            "provider": launch_settings.provider,
            "base_url": launch_settings.base_url,
            "api_mode": launch_settings.api_mode,
            "config_text": config_text,
            "soul_text": soul_text,
        },
        sort_keys=True,
    )
    version = hashlib.sha256(version_source.encode("utf-8")).hexdigest()[:16]
    return {
        "template_name": runtime_template_name(),
        "profile_name": profile_name,
        "template_version": version,
        "toolsets": list(launch_settings.toolsets),
        "model": launch_settings.model,
        "provider": launch_settings.provider,
        "base_url": launch_settings.base_url,
        "api_mode": launch_settings.api_mode,
        "product_brand": str(product_config.get("product", {}).get("brand", {}).get("name", "")).strip() or "Hermes Core",
        "has_runtime_config": bool(runtime_config),
    }


def _write_text_if_changed(path: Path, content: str) -> bool:
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    if existing == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def stage_runtime_template(
    launch_settings: ProductRuntimeLaunchSettings,
    *,
    config: dict[str, Any] | None = None,
    root_config: dict[str, Any] | None = None,
    model_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    product_config = config or load_product_config()
    template_root = runtime_template_root(product_config)
    template_root.mkdir(parents=True, exist_ok=True)
    secure_runtime_writable_dir(template_root)

    profile_root = runtime_template_profile_root(product_config)
    profile_root.mkdir(parents=True, exist_ok=True)
    secure_runtime_writable_dir(profile_root)

    runtime_config = build_runtime_cli_config(
        base_url=launch_settings.base_url,
        model=launch_settings.model,
        model_cfg=model_cfg,
        root_config=root_config,
    )
    soul_text = render_product_soul(product_config)
    payload = _template_payload(
        product_config=product_config,
        launch_settings=launch_settings,
        runtime_config=runtime_config,
        soul_text=soul_text,
    )

    soul_paths = [
        template_root / "SOUL.md",
        profile_root / "SOUL.md",
    ]
    for soul_path in soul_paths:
        _write_text_if_changed(soul_path, soul_text)
        secure_operator_readable_file(soul_path)

    config_paths = [
        template_root / "config.yaml",
        profile_root / "config.yaml",
    ]
    config_text = yaml.safe_dump(runtime_config, sort_keys=False) if runtime_config else ""
    for config_path in config_paths:
        if config_text:
            _write_text_if_changed(config_path, config_text)
            secure_operator_readable_file(config_path)
        elif config_path.exists():
            config_path.unlink()

    manifest_path = runtime_template_manifest_path(product_config)
    _write_text_if_changed(manifest_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    secure_operator_readable_file(manifest_path)
    return payload
