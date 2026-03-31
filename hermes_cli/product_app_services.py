from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class RootRouteServices:
    build_product_index_html: Callable[..., str]
    set_pending_invite_token: Callable[[Any, str | None], None]
    set_pending_bootstrap_token: Callable[[Any, str | None], None]
    current_product_urls: Callable[[], dict[str, str]]
    current_app_base_url: Callable[[], str]
    product_health_response_model: type


@dataclass(frozen=True)
class AuthRouteServices:
    enforce_auth_rate_limit: Callable[[Any, str], None]
    csrf_token: Callable[[Any], str]
    clear_notice: Callable[[Any], None]
    set_pending_invite_identity: Callable[[Any, dict[str, str] | None], None]
    pending_invite_token: Callable[[Any], str]
    pending_bootstrap_token: Callable[[Any], str]
    resolve_session_user: Callable[[Any], dict[str, Any]]
    mark_bootstrap_completed_if_admin: Callable[[dict[str, Any]], None]
    current_app_base_url: Callable[[], str]
    start_tsidp_login: Callable[[Any, Any], Any]
    load_product_oidc_client_settings: Callable[..., Any]
    discover_product_oidc_provider_metadata: Callable[[Any], Any]
    exchange_product_oidc_code: Callable[..., dict[str, Any]]
    validate_product_oidc_id_token: Callable[..., dict[str, Any]]
    fetch_product_oidc_userinfo: Callable[..., dict[str, Any]]
    tailscale_identity_from_claims: Callable[[dict[str, Any]], dict[str, str]]
    handle_tsidp_identity: Callable[[Any, dict[str, str]], Any]
    store_session_user: Callable[[Any, dict[str, Any]], dict[str, Any]]
    provider_user_session_payload: Callable[[Any], dict[str, Any]]
    session_response_payload: Callable[[Any], Any]
    require_csrf: Callable[[Any], None]
    pending_invite_identity: Callable[[Any], dict[str, str] | None]
    claim_product_user_from_invite: Callable[..., Any]
    set_pending_invite_token: Callable[[Any, str | None], None]
    set_notice: Callable[[Any, str], None]
    product_session_response_model: type


@dataclass(frozen=True)
class ChatRouteServices:
    require_product_user: Callable[[Any], dict[str, Any]]
    require_csrf: Callable[[Any], None]
    runtime_session_payload: Callable[[dict[str, Any]], dict[str, Any]]
    stream_product_runtime_turn: Callable[..., Any]
    stop_product_runtime_turn: Callable[..., bool]
    product_chat_session_response_model: type
    product_chat_turn_request_model: type


@dataclass(frozen=True)
class WorkspaceRouteServices:
    require_product_user: Callable[[Any], dict[str, Any]]
    require_csrf: Callable[[Any], None]
    get_workspace_state: Callable[..., Any]
    resolve_workspace_file: Callable[..., Any]
    create_workspace_folder: Callable[..., Any]
    store_workspace_file: Callable[..., Any]
    delete_workspace_path: Callable[..., Any]
    product_workspace_response_model: type
    product_create_workspace_folder_request_model: type
    product_delete_workspace_path_request_model: type
    product_workspace_quota_error: type[Exception]
    workspace_response_payload: Callable[[Any], Any]


@dataclass(frozen=True)
class AdminRouteServices:
    require_admin_user: Callable[[Any], dict[str, Any]]
    require_csrf: Callable[[Any], None]
    product_admin_users_response_model: type
    product_created_user_model: type
    product_create_user_request_model: type
    product_user_model: type
    list_admin_entries: Callable[[dict[str, Any]], Any]
    create_invited_user: Callable[[Any], Any]
    deactivate_product_user: Callable[[str], Any]
