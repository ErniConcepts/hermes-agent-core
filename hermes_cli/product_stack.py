"""Bundled product service generation for the hermes-core distribution."""

from __future__ import annotations

import logging

from hermes_cli.config import _secure_dir, _secure_file, get_env_value, save_env_value_secure
from hermes_cli.product_config import (
    ensure_product_home,
    get_product_storage_root,
    load_product_config,
    save_product_config,
)
from hermes_cli.product_oidc import (
    ProductOIDCClientSettings,
    discover_product_oidc_provider_metadata,
    load_product_oidc_client_settings,
)
from hermes_cli.product_stack_bootstrap import (
    READY_TIMEOUT_SECONDS as _READY_TIMEOUT_SECONDS,
    TSIDP_IMAGE,
    active_admin_exists,
    bootstrap_first_admin_enrollment,
    bootstrap_product_oidc_client,
    bootstrap_product_tailscale_oidc_client,
    build_tsidp_compose_spec as _build_tsidp_compose_spec,
    build_tsidp_env_file as _build_tsidp_env_file,
    ensure_client_secret as _ensure_client_secret,
    ensure_product_stack_started,
    ensure_product_tsidp_started,
    ensure_session_secret as _ensure_session_secret,
    first_admin_bootstrap_completed,
    initialize_product_stack,
    load_first_admin_enrollment_state,
    load_product_tailscale_oidc_client_settings,
    mark_first_admin_bootstrap_completed,
    required_secret as _required_secret,
    tailscale_oidc_registration_payload as _tailscale_oidc_registration_payload,
    tsidp_auth_key as _tsidp_auth_key,
    tsidp_service_config as _tsidp_service_config,
)
from hermes_cli.product_stack_paths import (
    get_first_admin_enrollment_state_path,
    get_product_bootstrap_root,
    get_product_services_root,
    get_tsidp_compose_path,
    get_tsidp_data_root,
    get_tsidp_env_path,
    get_tsidp_service_root,
    permission_error_message as _permission_error_message,
    secure_tree as _secure_tree,
)
from hermes_cli.product_stack_tailscale import (
    ensure_product_tailnet_started,
    ensure_product_tailnet_stopped,
    first_admin_bootstrap_completed as _first_admin_bootstrap_completed,
    format_https_url as _format_https_url,
    format_tailscale_reset_error as _format_tailscale_reset_error,
    format_tailscale_serve_error as _format_tailscale_serve_error,
    required_tailnet_value as _required_tailnet_value,
    resolve_product_urls,
    tailscale_command_path as _tailscale_command_path,
    tailscale_config as _tailscale_config,
    tailscale_enabled as _tailscale_enabled,
    tailscale_host as _tailscale_host,
    tailscale_https_port as _tailscale_https_port,
    tailscale_serve_command as _tailscale_serve_command,
    tsidp_host as _tsidp_host,
    tsidp_hostname as _tsidp_hostname,
    tsidp_issuer_url as _tsidp_issuer_url,
    url_scheme as _url_scheme,
    wait_for_tsidp_ready as _wait_for_tsidp_ready,
)
from utils import atomic_json_write, atomic_yaml_write

logger = logging.getLogger(__name__)

