from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from pydantic import BaseModel
import yaml

from hermes_cli.config import _secure_dir, _secure_file, ensure_hermes_home, get_env_value, get_hermes_home
from hermes_cli.product_config import (
    load_product_config,
    resolve_hermes_model_config,
    resolve_hermes_runtime_toolsets,
    runtime_host_access_host,
)
from hermes_cli.product_identity import render_product_soul
from hermes_cli.product_runtime_container import (
    container_env_map as _container_env_map_impl,
    delete_product_runtime as _delete_product_runtime_impl,
    docker_inspect_state as _docker_inspect_state_impl,
    docker_run_command as _docker_run_command_impl,
    ensure_product_runtime as _ensure_product_runtime_impl,
    get_product_runtime_session as _get_product_runtime_session_impl,
    normalize_runtime_session_payload as _normalize_runtime_session_payload_impl,
    remove_container_if_exists as _remove_container_if_exists_impl,
    running_container_matches_record as _running_container_matches_record_impl,
    runtime_base_url as _runtime_base_url_impl,
    runtime_container_user as _runtime_container_user_impl,
    runtime_launch_env as _runtime_launch_env_impl,
    runtime_mounts as _runtime_mounts_impl,
    stream_product_runtime_turn as _stream_product_runtime_turn_impl,
    wait_for_runtime_health as _wait_for_runtime_health_impl,
)
from hermes_cli.product_runtime_staging import (
    env_path as _env_path_impl,
    hermes_home as _hermes_home_impl,
    legacy_user_ids as _legacy_user_ids_impl,
    manifest_path as _manifest_path_impl,
    migrate_legacy_runtime as _migrate_legacy_runtime_impl,
    product_runtime_session_id as _product_runtime_session_id_impl,
    product_storage_root as _product_storage_root_impl,
    product_users_root as _product_users_root_impl,
    resolve_runtime_api_key as _resolve_runtime_api_key_impl,
    resolve_runtime_launch_settings as _resolve_runtime_launch_settings_impl,
    resolve_runtime_model_base_url as _resolve_runtime_model_base_url_impl,
    resolve_runtime_port as _resolve_runtime_port_impl,
    runtime_binary as _runtime_binary_impl,
    runtime_config_path as _runtime_config_path_impl,
    runtime_environment as _runtime_environment_impl,
    runtime_image as _runtime_image_impl,
    runtime_internal_port as _runtime_internal_port_impl,
    runtime_key as _runtime_key_impl,
    runtime_port_range as _runtime_port_range_impl,
    runtime_root as _runtime_root_impl,
    runtime_toolsets as _runtime_toolsets_impl,
    stage_product_runtime as _stage_product_runtime_impl,
    user_id as _user_id_impl,
    user_storage_root as _user_storage_root_impl,
    workspace_root as _workspace_root_impl,
    write_runtime_cli_config as _write_runtime_cli_config_impl,
    write_runtime_env_file as _write_runtime_env_file_impl,
    write_runtime_record as _write_runtime_record_impl,
    write_runtime_text_if_changed as _write_runtime_text_if_changed_impl,
)
from hermes_cli.runtime_provider import format_runtime_provider_error, resolve_runtime_provider

logger = logging.getLogger(__name__)
_RUNTIME_HEALTH_TTL_SECONDS = 10.0
_RUNTIME_HEALTH_CACHE: dict[str, float] = {}
_RUNTIME_WORKSPACE_PATH = "/workspace"
_RUNTIME_ENV_MATCH_KEYS = {
    "HERMES_PRODUCT_PROVIDER",
    "HERMES_PRODUCT_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "HERMES_PRODUCT_TOOLSETS",
    "HERMES_PRODUCT_API_MODE",
    "HERMES_PRODUCT_RUNTIME_MODE",
}


@dataclass(frozen=True)
class ProductRuntimeLaunchSettings:
    model: str
    provider: str
    base_url: str
    api_mode: str
    api_key: str
    toolsets: list[str]


def _secure_runtime_dir(path: Path) -> None:
    try:
        path.chmod(0o755)
    except (OSError, NotImplementedError):
        pass


def _secure_runtime_writable_dir(path: Path) -> None:
    try:
        path.chmod(0o777)
    except (OSError, NotImplementedError):
        pass


def _secure_runtime_file(path: Path) -> None:
    try:
        if path.exists():
            path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass


def _secure_container_readable_file(path: Path) -> None:
    try:
        if path.exists():
            path.chmod(0o644)
    except (OSError, NotImplementedError):
        pass

class ProductRuntimeRecord(BaseModel):
    user_id: str
    runtime_key: str | None = None
    display_name: str | None = None
    session_id: str
    container_name: str
    runtime: str
    runtime_port: int
    runtime_root: str
    hermes_home: str
    workspace_root: str
    env_file: str
    manifest_file: str
    auth_token: str | None = None
    status: str = "staged"


class ProductRuntimeSession(BaseModel):
    session_id: str
    messages: list[dict[str, Any]]
    runtime_mode: str
    runtime_toolsets: list[str]


class ProductRuntimeTurnRequest(BaseModel):
    user_message: str


class ProductRuntimeEvent(BaseModel):
    event: str
    payload: dict[str, Any]


def _user_id(user: dict[str, Any]) -> str:
    return _user_id_impl(user)


def _legacy_user_ids(user: dict[str, Any]) -> list[str]:
    return _legacy_user_ids_impl(user)


def _runtime_key(user_id: str) -> str:
    return _runtime_key_impl(user_id)


def product_runtime_session_id(user_id: str) -> str:
    return _product_runtime_session_id_impl(user_id)


def _product_storage_root(config: dict[str, Any]) -> Path:
    return _product_storage_root_impl(sys.modules[__name__], config)


def _product_users_root(config: dict[str, Any]) -> Path:
    return _product_users_root_impl(sys.modules[__name__], config)


def _runtime_root(config: dict[str, Any], user_id: str) -> Path:
    return _runtime_root_impl(sys.modules[__name__], config, user_id)


def _user_storage_root(config: dict[str, Any], user_id: str) -> Path:
    return _user_storage_root_impl(sys.modules[__name__], config, user_id)


def _workspace_root(config: dict[str, Any], user_id: str) -> Path:
    return _workspace_root_impl(sys.modules[__name__], config, user_id)


def _hermes_home(config: dict[str, Any], user_id: str) -> Path:
    return _hermes_home_impl(sys.modules[__name__], config, user_id)


def _manifest_path(config: dict[str, Any], user_id: str) -> Path:
    return _manifest_path_impl(sys.modules[__name__], config, user_id)


def _env_path(config: dict[str, Any], user_id: str) -> Path:
    return _env_path_impl(sys.modules[__name__], config, user_id)


def _runtime_config_path(config: dict[str, Any], user_id: str) -> Path:
    return _runtime_config_path_impl(sys.modules[__name__], config, user_id)


def _runtime_toolsets(config: dict[str, Any]) -> list[str]:
    return _runtime_toolsets_impl(sys.modules[__name__], config)


def _runtime_port_range(config: dict[str, Any]) -> tuple[int, int]:
    return _runtime_port_range_impl(sys.modules[__name__], config)


def _runtime_image(config: dict[str, Any]) -> str:
    return _runtime_image_impl(config)


def _runtime_binary(config: dict[str, Any]) -> str:
    return _runtime_binary_impl(config)


def _runtime_internal_port(config: dict[str, Any]) -> int:
    return _runtime_internal_port_impl(config)


def _resolve_runtime_model_base_url(config: dict[str, Any], base_url: str) -> str:
    return _resolve_runtime_model_base_url_impl(sys.modules[__name__], config, base_url)


def _resolve_runtime_port(config: dict[str, Any], user_id: str) -> int:
    return _resolve_runtime_port_impl(sys.modules[__name__], config, user_id)


def load_runtime_record(user_id: str, *, config: dict[str, Any] | None = None) -> ProductRuntimeRecord | None:
    product_config = config or load_product_config()
    manifest_path = _manifest_path(product_config, user_id)
    if not manifest_path.exists():
        return None
    return ProductRuntimeRecord.model_validate_json(manifest_path.read_text(encoding="utf-8"))


def _write_runtime_record(record: ProductRuntimeRecord) -> None:
    _write_runtime_record_impl(sys.modules[__name__], record)


def _write_runtime_text_if_changed(path: Path, content: str) -> bool:
    return _write_runtime_text_if_changed_impl(path, content)


def _write_runtime_cli_config(config: dict[str, Any], user_id: str, *, base_url: str, model: str) -> None:
    _write_runtime_cli_config_impl(sys.modules[__name__], config, user_id, base_url=base_url, model=model)


def _resolve_runtime_launch_settings(product_config: dict[str, Any]) -> ProductRuntimeLaunchSettings:
    return _resolve_runtime_launch_settings_impl(sys.modules[__name__], product_config)


def _resolve_runtime_api_key(model_cfg: dict[str, Any]) -> str:
    return _resolve_runtime_api_key_impl(sys.modules[__name__], model_cfg)


def _runtime_environment(
    settings: ProductRuntimeLaunchSettings,
    *,
    session_id: str,
    auth_token: str,
    internal_port: int,
) -> dict[str, str]:
    return _runtime_environment_impl(
        sys.modules[__name__], settings, session_id=session_id, auth_token=auth_token, internal_port=internal_port
    )


def _write_runtime_env_file(path: Path, env: dict[str, str]) -> None:
    _write_runtime_env_file_impl(sys.modules[__name__], path, env)


def _runtime_mounts(record: ProductRuntimeRecord) -> list[str]:
    return _runtime_mounts_impl(sys.modules[__name__], record)


def _runtime_container_user(record: ProductRuntimeRecord) -> str | None:
    return _runtime_container_user_impl(record)


def _migrate_legacy_runtime(user: dict[str, Any], product_config: dict[str, Any], stable_user_id: str) -> ProductRuntimeRecord | None:
    return _migrate_legacy_runtime_impl(sys.modules[__name__], user, product_config, stable_user_id)


def stage_product_runtime(user: dict[str, Any], *, config: dict[str, Any] | None = None) -> ProductRuntimeRecord:
    return _stage_product_runtime_impl(sys.modules[__name__], user, config=config)


def _docker_run_command(record: ProductRuntimeRecord, config: dict[str, Any]) -> list[str]:
    return _docker_run_command_impl(sys.modules[__name__], record, config)


def _docker_inspect_state(container_name: str) -> dict[str, Any] | None:
    return _docker_inspect_state_impl(container_name)


def _container_env_map(container_state: dict[str, Any] | None) -> dict[str, str]:
    return _container_env_map_impl(container_state)


def _runtime_launch_env(record: ProductRuntimeRecord) -> dict[str, str]:
    return _runtime_launch_env_impl(record)


def _running_container_matches_record(record: ProductRuntimeRecord, container_state: dict[str, Any] | None) -> bool:
    return _running_container_matches_record_impl(sys.modules[__name__], record, container_state)


def _remove_container_if_exists(container_name: str) -> None:
    _remove_container_if_exists_impl(container_name)


def _wait_for_runtime_health(
    record: ProductRuntimeRecord,
    *,
    timeout_seconds: float = 20.0,
    interval_seconds: float = 0.25,
) -> None:
    _wait_for_runtime_health_impl(
        sys.modules[__name__], record, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds
    )


def ensure_product_runtime(user: dict[str, Any], *, config: dict[str, Any] | None = None) -> ProductRuntimeRecord:
    return _ensure_product_runtime_impl(sys.modules[__name__], user, config=config)


def runtime_base_url(record: ProductRuntimeRecord) -> str:
    return _runtime_base_url_impl(record)


def _normalize_runtime_session_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _normalize_runtime_session_payload_impl(payload)


def get_product_runtime_session(user: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return _get_product_runtime_session_impl(sys.modules[__name__], user, config=config)


def stream_product_runtime_turn(
    user: dict[str, Any],
    user_message: str,
    *,
    config: dict[str, Any] | None = None,
) -> Iterator[str]:
    yield from _stream_product_runtime_turn_impl(sys.modules[__name__], user, user_message, config=config)


def delete_product_runtime(user_id: str, *, config: dict[str, Any] | None = None) -> None:
    _delete_product_runtime_impl(sys.modules[__name__], user_id, config=config)
