"""Product runtime facade.

This module intentionally exposes a small public entry surface for the product
app and tests while delegating most work to staging/container/template helpers.
Keep it as a thin composition layer rather than growing business logic here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from hermes_cli.product_config import load_product_config
from hermes_cli.product_runtime_common import (
    ProductRuntimeEvent,
    ProductRuntimeLaunchSettings,
    ProductRuntimeRecord,
    ProductRuntimeSession,
    ProductRuntimeTurnRequest,
    _RUNTIME_ENV_MATCH_KEYS,
    _RUNTIME_HEALTH_CACHE,
    _RUNTIME_HEALTH_TTL_SECONDS,
    _RUNTIME_WORKSPACE_PATH,
    secure_container_readable_file as _secure_container_readable_file,
    secure_runtime_dir as _secure_runtime_dir,
    secure_runtime_file as _secure_runtime_file,
    secure_runtime_writable_dir as _secure_runtime_writable_dir,
)
from hermes_cli.product_runtime_container import (
    container_env_map as _container_env_map,
    delete_product_runtime as _delete_product_runtime_impl,
    docker_inspect_state as _docker_inspect_state,
    docker_run_command as _docker_run_command,
    ensure_runtime_container,
    get_product_runtime_session as _get_product_runtime_session_impl,
    normalize_runtime_session_payload as _normalize_runtime_session_payload,
    remove_container_if_exists as _remove_container_if_exists,
    running_container_matches_record as _running_container_matches_record,
    runtime_base_url,
    runtime_container_user as _runtime_container_user,
    runtime_launch_env as _runtime_launch_env,
    runtime_mounts as _runtime_mounts,
    stop_product_runtime_turn as _stop_product_runtime_turn_impl,
    stream_product_runtime_turn as _stream_product_runtime_turn_impl,
    wait_for_runtime_health as _wait_for_runtime_health,
)
from hermes_cli.product_runtime_staging import (
    env_path as _env_path,
    hermes_home as _hermes_home,
    profile_root as _profile_root,
    user_install_root as _install_root,
    legacy_user_ids as _legacy_user_ids,
    load_runtime_record,
    manifest_path as _manifest_path,
    migrate_legacy_runtime as _migrate_legacy_runtime,
    product_runtime_session_id,
    product_storage_root as _product_storage_root,
    product_users_root as _product_users_root,
    resolve_runtime_api_key as _resolve_runtime_api_key,
    resolve_runtime_launch_settings as _resolve_runtime_launch_settings,
    resolve_runtime_model_base_url as _resolve_runtime_model_base_url,
    resolve_runtime_port as _resolve_runtime_port,
    runtime_binary as _runtime_binary,
    runtime_config_path as _runtime_config_path,
    runtime_environment as _runtime_environment,
    runtime_image as _runtime_image,
    runtime_internal_port as _runtime_internal_port,
    runtime_key as _runtime_key,
    runtime_port_range as _runtime_port_range,
    runtime_root as _runtime_root,
    runtime_toolsets as _runtime_toolsets,
    stage_product_runtime,
    user_id as _user_id,
    user_storage_root as _user_storage_root,
    workspace_root as _workspace_root,
    write_runtime_cli_config as _write_runtime_cli_config,
    write_runtime_env_file as _write_runtime_env_file,
    write_runtime_record as _write_runtime_record,
    write_runtime_text_if_changed as _write_runtime_text_if_changed,
)
from hermes_cli.product_runtime_template import runtime_profile_name as _runtime_profile_name


def ensure_product_runtime(user: dict[str, Any], *, config: dict[str, Any] | None = None) -> ProductRuntimeRecord:
    product_config = config or load_product_config()
    staged = stage_product_runtime(user, config=product_config)
    return ensure_runtime_container(staged, product_config)


def get_product_runtime_session(user: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    record = ensure_product_runtime(user, config=config)
    return _get_product_runtime_session_impl(record)


def stream_product_runtime_turn(
    user: dict[str, Any],
    user_message: str,
    *,
    config: dict[str, Any] | None = None,
) -> Iterator[str]:
    record = ensure_product_runtime(user, config=config)
    yield from _stream_product_runtime_turn_impl(record, user_message)


def stop_product_runtime_turn(user: dict[str, Any], *, config: dict[str, Any] | None = None) -> bool:
    record = ensure_product_runtime(user, config=config)
    return _stop_product_runtime_turn_impl(record)


def delete_product_runtime(
    user_id: str,
    *,
    config: dict[str, Any] | None = None,
    delete_workspace: bool = False,
) -> None:
    _delete_product_runtime_impl(user_id, config=config, delete_workspace=delete_workspace)


__all__ = [
    "ProductRuntimeEvent",
    "ProductRuntimeLaunchSettings",
    "ProductRuntimeRecord",
    "ProductRuntimeSession",
    "ProductRuntimeTurnRequest",
    "_RUNTIME_ENV_MATCH_KEYS",
    "_RUNTIME_HEALTH_CACHE",
    "_RUNTIME_HEALTH_TTL_SECONDS",
    "_RUNTIME_WORKSPACE_PATH",
    "_container_env_map",
    "_docker_inspect_state",
    "_docker_run_command",
    "_env_path",
    "_hermes_home",
    "_install_root",
    "_legacy_user_ids",
    "_manifest_path",
    "_migrate_legacy_runtime",
    "_normalize_runtime_session_payload",
    "_product_storage_root",
    "_product_users_root",
    "_profile_root",
    "_remove_container_if_exists",
    "_resolve_runtime_api_key",
    "_resolve_runtime_launch_settings",
    "_resolve_runtime_model_base_url",
    "_resolve_runtime_port",
    "_runtime_binary",
    "_runtime_config_path",
    "_runtime_container_user",
    "_runtime_environment",
    "_runtime_image",
    "_runtime_internal_port",
    "_runtime_key",
    "_runtime_launch_env",
    "_runtime_mounts",
    "_runtime_port_range",
    "_runtime_profile_name",
    "_runtime_root",
    "_runtime_toolsets",
    "_running_container_matches_record",
    "_secure_container_readable_file",
    "_secure_runtime_dir",
    "_secure_runtime_file",
    "_secure_runtime_writable_dir",
    "_user_id",
    "_user_storage_root",
    "_wait_for_runtime_health",
    "_workspace_root",
    "_write_runtime_cli_config",
    "_write_runtime_env_file",
    "_write_runtime_record",
    "_write_runtime_text_if_changed",
    "delete_product_runtime",
    "ensure_product_runtime",
    "get_product_runtime_session",
    "load_runtime_record",
    "product_runtime_session_id",
    "runtime_base_url",
    "stage_product_runtime",
    "stop_product_runtime_turn",
    "stream_product_runtime_turn",
]
