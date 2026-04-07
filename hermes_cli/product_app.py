"""Tailnet-only product app surface for hermes-core."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from hermes_cli.product_app_admin_routes import register_admin_routes
from hermes_cli.product_app_auth_routes import register_auth_routes
from hermes_cli.product_app_chat_routes import register_chat_routes
from hermes_cli.product_app_root_routes import register_root_routes
from hermes_cli.product_app_support import (
    ProductAdminEntry,
    ProductAdminUsersResponse,
    ProductAppContext,
    ProductChatMessage,
    ProductChatSessionResponse,
    ProductChatTurnRequest,
    ProductCreateUserRequest,
    ProductCreateWorkspaceFolderRequest,
    ProductDeleteWorkspacePathRequest,
    ProductHealthResponse,
    ProductMoveWorkspacePathRequest,
    ProductSessionResponse,
    ProductWorkspaceResponse,
    _build_product_app_context,
    _canonical_request_redirect,
    _client_ip,
    _create_invited_user,
    _csrf_token,
    _current_app_base_url,
    _current_product_urls,
    _deactivate_product_user,
    _enforce_auth_rate_limit,
    _handle_tsidp_identity,
    _list_admin_entries,
    _mark_bootstrap_completed_if_admin,
    _origin_from_url,
    _pending_bootstrap_token,
    _pending_invite_identity,
    _pending_invite_token,
    _pending_invite_record,
    _provider_user_session_payload,
    _require_admin_user,
    _require_csrf,
    _require_product_user,
    _resolve_session_user,
    _runtime_session_payload,
    _session_response_payload,
    _session_secret,
    _set_notice,
    _set_pending_bootstrap_token,
    _set_pending_invite_identity,
    _set_pending_invite_token,
    _start_tsidp_login,
    _store_session_user,
    _tailscale_identity_from_claims,
)
from hermes_cli.product_app_workspace_routes import register_workspace_routes


def create_product_app() -> FastAPI:
    context = _build_product_app_context()

    app = FastAPI(title="Hermes Core Product App", version="0.1.0")
    app.state.product_app_context = context
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(),
        session_cookie="hermes_product_session",
        same_site="lax",
        max_age=43200,
        https_only=context.app_base_url.startswith("https://"),
    )

    @app.middleware("http")
    async def enforce_canonical_origin(request: Request, call_next):
        urls = _current_product_urls()
        redirect_url = _canonical_request_redirect(request, urls)
        if redirect_url is not None:
            return RedirectResponse(redirect_url, status_code=307)
        return await call_next(request)

    register_root_routes(app, context)
    register_auth_routes(app, context)
    register_chat_routes(app)
    register_workspace_routes(app)
    register_admin_routes(app)
    return app


def create_product_auth_proxy_app() -> FastAPI:
    """Compatibility factory for legacy auth-proxy service units."""
    return create_product_app()
