"""Bundled product service generation for the hermes-core distribution."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from hermes_cli.config import _secure_dir, _secure_file, get_env_value, save_env_value_secure
from hermes_cli.product_stack_bootstrap import (
    bootstrap_first_admin_enrollment as _bootstrap_first_admin_enrollment_impl,
    bootstrap_product_oidc_client as _bootstrap_product_oidc_client_impl,
    bootstrap_product_tailscale_oidc_client as _bootstrap_product_tailscale_oidc_client_impl,
    build_tsidp_compose_spec as _build_tsidp_compose_spec_impl,
    build_tsidp_env_file as _build_tsidp_env_file_impl,
    ensure_client_secret as _ensure_client_secret_impl,
    ensure_product_stack_started as _ensure_product_stack_started_impl,
    ensure_product_tsidp_started as _ensure_product_tsidp_started_impl,
    ensure_session_secret as _ensure_session_secret_impl,
    initialize_product_stack as _initialize_product_stack_impl,
    load_first_admin_enrollment_state as _load_first_admin_enrollment_state_impl,
    load_product_tailscale_oidc_client_settings as _load_product_tailscale_oidc_client_settings_impl,
    mark_first_admin_bootstrap_completed as _mark_first_admin_bootstrap_completed_impl,
    required_secret as _required_secret_impl,
    tailscale_oidc_registration_payload as _tailscale_oidc_registration_payload_impl,
    tsidp_auth_key as _tsidp_auth_key_impl,
    tsidp_service_config as _tsidp_service_config_impl,
)
from hermes_cli.product_stack_paths import (
    get_first_admin_enrollment_state_path as _get_first_admin_enrollment_state_path_impl,
    get_product_bootstrap_root as _get_product_bootstrap_root_impl,
    get_product_services_root as _get_product_services_root_impl,
    get_tsidp_compose_path as _get_tsidp_compose_path_impl,
    get_tsidp_data_root as _get_tsidp_data_root_impl,
    get_tsidp_env_path as _get_tsidp_env_path_impl,
    get_tsidp_service_root as _get_tsidp_service_root_impl,
    permission_error_message as _permission_error_message_impl,
    secure_tree as _secure_tree_impl,
)
from hermes_cli.product_stack_tailscale import (
    ensure_product_tailnet_started as _ensure_product_tailnet_started_impl,
    ensure_product_tailnet_stopped as _ensure_product_tailnet_stopped_impl,
    first_admin_bootstrap_completed as _first_admin_bootstrap_completed_impl,
    format_https_url as _format_https_url_impl,
    format_tailscale_reset_error as _format_tailscale_reset_error_impl,
    format_tailscale_serve_error as _format_tailscale_serve_error_impl,
    public_host as _public_host_impl,
    required_tailnet_value as _required_tailnet_value_impl,
    resolve_product_urls as _resolve_product_urls_impl,
    tailscale_command_path as _tailscale_command_path_impl,
    tailscale_config as _tailscale_config_impl,
    tailscale_enabled as _tailscale_enabled_impl,
    tailscale_host as _tailscale_host_impl,
    tailscale_https_port as _tailscale_https_port_impl,
    tailscale_serve_command as _tailscale_serve_command_impl,
    tsidp_host as _tsidp_host_impl,
    tsidp_hostname as _tsidp_hostname_impl,
    tsidp_issuer_url as _tsidp_issuer_url_impl,
    url_scheme as _url_scheme_impl,
    validate_public_host as _validate_public_host_impl,
    wait_for_tsidp_ready as _wait_for_tsidp_ready_impl,
)
from hermes_cli.product_oidc import (
    ProductOIDCClientSettings,
    discover_product_oidc_provider_metadata,
    load_product_oidc_client_settings,
)
from hermes_cli.product_config import (
    ensure_product_home,
    get_product_storage_root,
    load_product_config,
    save_product_config,
)
from utils import atomic_json_write, atomic_yaml_write


TSIDP_IMAGE = "ghcr.io/tailscale/tsidp:latest"
_READY_TIMEOUT_SECONDS = 45.0
logger = logging.getLogger(__name__)


def get_product_services_root() -> Path:
    return _get_product_services_root_impl(sys.modules[__name__])


def get_tsidp_service_root() -> Path:
    return _get_tsidp_service_root_impl(sys.modules[__name__])


def get_tsidp_data_root() -> Path:
    return _get_tsidp_data_root_impl(sys.modules[__name__])


def get_product_bootstrap_root() -> Path:
    return _get_product_bootstrap_root_impl(sys.modules[__name__])


def get_first_admin_enrollment_state_path() -> Path:
    return _get_first_admin_enrollment_state_path_impl(sys.modules[__name__])


def get_tsidp_compose_path() -> Path:
    return _get_tsidp_compose_path_impl(sys.modules[__name__])


def get_tsidp_env_path() -> Path:
    return _get_tsidp_env_path_impl(sys.modules[__name__])


def _secure_tree(*paths: Path) -> None:
    _secure_tree_impl(sys.modules[__name__], *paths)


def _permission_error_message(path: Path) -> str:
    return _permission_error_message_impl(path)


def _public_host(config: Dict[str, Any]) -> str:
    return _public_host_impl(config)


def _url_scheme(config: Dict[str, Any]) -> str:
    return _url_scheme_impl(config)


def _validate_public_host(host: str) -> None:
    _validate_public_host_impl(host)


def _tailscale_config(config: Dict[str, Any]) -> Dict[str, Any]:
    return _tailscale_config_impl(config)


def _tailscale_enabled(config: Dict[str, Any]) -> bool:
    return _tailscale_enabled_impl(config)


def _required_tailnet_value(config: Dict[str, Any], key: str) -> str:
    return _required_tailnet_value_impl(config, key)


def _tailscale_host(config: Dict[str, Any]) -> str:
    return _tailscale_host_impl(config)


def _tailscale_https_port(config: Dict[str, Any], key: str, default: int) -> int:
    return _tailscale_https_port_impl(config, key, default)


def _tsidp_hostname(config: Dict[str, Any]) -> str:
    return _tsidp_hostname_impl(config)


def _tsidp_host(config: Dict[str, Any]) -> str:
    return _tsidp_host_impl(config)


def _tsidp_issuer_url(config: Dict[str, Any]) -> str:
    return _tsidp_issuer_url_impl(config)


def _format_https_url(host: str, port: int) -> str:
    return _format_https_url_impl(host, port)


def _format_tailscale_reset_error(exc: subprocess.CalledProcessError, *, command: list[str]) -> str:
    return _format_tailscale_reset_error_impl(exc, command=command)


def ensure_product_tailnet_stopped(config: Dict[str, Any] | None = None) -> list[subprocess.CompletedProcess[str]]:
    return _ensure_product_tailnet_stopped_impl(sys.modules[__name__], config)


def resolve_product_urls(config: Dict[str, Any] | None = None) -> Dict[str, str]:
    return _resolve_product_urls_impl(sys.modules[__name__], config)


def _tailscale_command_path(config: Dict[str, Any]) -> str:
    return _tailscale_command_path_impl(config)


def _tailscale_serve_command(config: Dict[str, Any], *, https_port: int, target_url: str) -> list[str]:
    return _tailscale_serve_command_impl(config, https_port=https_port, target_url=target_url)


def _format_tailscale_serve_error(
    exc: subprocess.CalledProcessError,
    *,
    command: list[str],
) -> str:
    return _format_tailscale_serve_error_impl(exc, command=command)


def _first_admin_bootstrap_completed() -> bool:
    return _first_admin_bootstrap_completed_impl(sys.modules[__name__])


def ensure_product_tailnet_started(
    config: Dict[str, Any] | None = None,
    *,
    include_app: bool = True,
) -> list[subprocess.CompletedProcess[str]]:
    return _ensure_product_tailnet_started_impl(sys.modules[__name__], config, include_app=include_app)


def _tsidp_service_config(config: Dict[str, Any]) -> Dict[str, Any]:
    return _tsidp_service_config_impl(sys.modules[__name__], config)


def _tsidp_auth_key(config: Dict[str, Any]) -> str:
    return _tsidp_auth_key_impl(sys.modules[__name__], config)


def _build_tsidp_env_file(config: Dict[str, Any]) -> str:
    return _build_tsidp_env_file_impl(sys.modules[__name__], config)


def _build_tsidp_compose_spec(config: Dict[str, Any]) -> Dict[str, Any]:
    return _build_tsidp_compose_spec_impl(sys.modules[__name__], config)


def ensure_product_tsidp_started(config: Dict[str, Any] | None = None) -> subprocess.CompletedProcess[str] | None:
    return _ensure_product_tsidp_started_impl(sys.modules[__name__], config)


def _wait_for_tsidp_ready(config: Dict[str, Any], timeout_seconds: float = _READY_TIMEOUT_SECONDS) -> None:
    _wait_for_tsidp_ready_impl(sys.modules[__name__], config, timeout_seconds)


def _required_secret(env_key: str) -> str:
    return _required_secret_impl(sys.modules[__name__], env_key)


def _ensure_client_secret(config: Dict[str, Any]) -> str:
    return _ensure_client_secret_impl(sys.modules[__name__], config)


def _ensure_session_secret(config: Dict[str, Any]) -> str:
    return _ensure_session_secret_impl(sys.modules[__name__], config)


def initialize_product_stack(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _initialize_product_stack_impl(sys.modules[__name__], config)


def ensure_product_stack_started(config: Dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    return _ensure_product_stack_started_impl(sys.modules[__name__], config)


def bootstrap_product_oidc_client(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _bootstrap_product_oidc_client_impl(sys.modules[__name__], config)


def load_product_tailscale_oidc_client_settings(
    config: Dict[str, Any] | None = None,
) -> ProductOIDCClientSettings:
    return _load_product_tailscale_oidc_client_settings_impl(sys.modules[__name__], config)


def _tailscale_oidc_registration_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    return _tailscale_oidc_registration_payload_impl(sys.modules[__name__], config)


def bootstrap_product_tailscale_oidc_client(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _bootstrap_product_tailscale_oidc_client_impl(sys.modules[__name__], config)


def load_first_admin_enrollment_state() -> Dict[str, Any] | None:
    return _load_first_admin_enrollment_state_impl(sys.modules[__name__])


def mark_first_admin_bootstrap_completed(tailscale_login: str | None = None) -> Dict[str, Any] | None:
    return _mark_first_admin_bootstrap_completed_impl(sys.modules[__name__], tailscale_login)


def bootstrap_first_admin_enrollment(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _bootstrap_first_admin_enrollment_impl(sys.modules[__name__], config)
