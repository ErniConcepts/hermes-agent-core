"""Tailnet-only product app surface for hermes-core."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from hermes_cli.config import get_env_value
from hermes_cli.product_app_admin_routes import register_admin_routes
from hermes_cli.product_app_auth_routes import register_auth_routes
from hermes_cli.product_app_chat_routes import register_chat_routes
from hermes_cli.product_app_root_routes import register_root_routes
from hermes_cli.product_app_services import (
    AdminRouteServices,
    AuthRouteServices,
    ChatRouteServices,
    RootRouteServices,
    WorkspaceRouteServices,
)
from hermes_cli.product_app_workspace_routes import register_workspace_routes
from hermes_cli.product_auth_rate_limit import (
    ProductAuthRateLimitExceeded,
    enforce_product_auth_rate_limit,
)
from hermes_cli.product_chat_transport import (
    get_product_chat_session,
    stop_product_chat_turn,
    stream_product_chat_turn,
)
from hermes_cli.product_config import load_product_config
from hermes_cli.product_invites import list_pending_product_signup_invites
from hermes_cli.product_oidc import (
    create_oidc_login_request,
    discover_product_oidc_provider_metadata,
    exchange_product_oidc_code,
    fetch_product_oidc_userinfo,
    load_product_oidc_client_settings,
    validate_product_oidc_id_token,
)
from hermes_cli.product_runtime import delete_product_runtime
from hermes_cli.product_stack import (
    load_first_admin_enrollment_state,
    mark_first_admin_bootstrap_completed,
    resolve_product_urls,
)
from hermes_cli.product_users import (
    ProductCreatedUser,
    ProductUser,
    bootstrap_first_admin_user,
    claim_product_user_from_invite,
    create_product_user_with_signup,
    deactivate_product_user,
    get_product_user_by_id,
    get_product_user_by_tailscale_login,
    get_product_user_by_tailscale_subject,
    list_product_users,
)
from hermes_cli.product_web import build_product_index_html
from hermes_cli.product_workspace import (
    ProductWorkspaceEntry,
    ProductWorkspaceQuotaError,
    create_workspace_folder,
    delete_workspace_path,
    get_workspace_state,
    move_workspace_path,
    resolve_workspace_file,
    store_workspace_file,
)

logger = logging.getLogger(__name__)
_SESSION_REFRESH_TTL_SECONDS = 30
_AUTH_RATE_LIMIT_WINDOW_SECONDS = 300.0
_AUTH_RATE_LIMIT_MAX_REQUESTS = 10


@dataclass(frozen=True)
class ProductAppContext:
    product_config: dict[str, Any]
    auth_provider: str
    product_name: str

    @property
    def urls(self) -> dict[str, str]:
        return _current_product_urls()

    @property
    def app_base_url(self) -> str:
        return self.urls["app_base_url"]

    @property
    def app_origin(self) -> str:
        return _origin_from_url(self.app_base_url)


class ProductHealthResponse(BaseModel):
    status: str = "ok"


class ProductSessionResponse(BaseModel):
    authenticated: bool
    user: dict[str, Any] | None = None
    csrf_token: str | None = None
    notice: str | None = None
    detected_tailscale_login: str | None = None
    pending_invite_claim: bool = False
    pending_invite_display_name: str | None = None
    app_base_url: str | None = None


class ProductAdminUsersResponse(BaseModel):
    users: list["ProductAdminEntry"]


class ProductAdminEntry(BaseModel):
    id: str
    type: str = "user"
    username: str | None = None
    display_name: str
    email: str | None = None
    tailscale_login: str | None = None
    is_admin: bool = False
    disabled: bool = False
    status: str


class ProductCreateUserRequest(BaseModel):
    tailscale_login: str | None = None
    display_name: str | None = None


class ProductChatMessage(BaseModel):
    role: str
    content: str


class ProductChatSessionResponse(BaseModel):
    session_id: str
    messages: list[ProductChatMessage]


class ProductChatTurnRequest(BaseModel):
    user_message: str = Field(min_length=1, max_length=16000)


class ProductWorkspaceResponse(BaseModel):
    current_path: str
    entries: list[ProductWorkspaceEntry]
    used_bytes: int
    limit_bytes: int


class ProductCreateWorkspaceFolderRequest(BaseModel):
    path: str = ""
    name: str


class ProductDeleteWorkspacePathRequest(BaseModel):
    path: str


class ProductMoveWorkspacePathRequest(BaseModel):
    source_path: str
    destination_parent_path: str = ""


def _workspace_response_payload(payload: Any) -> ProductWorkspaceResponse:
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    return ProductWorkspaceResponse(**data)


def _load_product_oidc_client_settings(*args, **kwargs):
    return load_product_oidc_client_settings(*args, **kwargs)


def _discover_product_oidc_provider_metadata(*args, **kwargs):
    return discover_product_oidc_provider_metadata(*args, **kwargs)


def _exchange_product_oidc_code(*args, **kwargs):
    return exchange_product_oidc_code(*args, **kwargs)


def _validate_product_oidc_id_token(*args, **kwargs):
    return validate_product_oidc_id_token(*args, **kwargs)


def _fetch_product_oidc_userinfo(*args, **kwargs):
    return fetch_product_oidc_userinfo(*args, **kwargs)


def _claim_product_user_from_invite(*args, **kwargs):
    return claim_product_user_from_invite(*args, **kwargs)


def _session_secret() -> str:
    product_config = load_product_config()
    secret_ref = str(product_config.get("auth", {}).get("session_secret_ref", "")).strip()
    if not secret_ref:
        raise RuntimeError("product auth.session_secret_ref must be configured")
    configured = str(get_env_value(secret_ref) or "").strip()
    if configured:
        return configured
    raise RuntimeError(f"Product session secret env var {secret_ref} is missing or empty")


def _csrf_token(request: Request) -> str:
    existing = request.session.get("csrf_token")
    if isinstance(existing, str) and existing.strip():
        return existing
    token = secrets.token_urlsafe(24)
    request.session["csrf_token"] = token
    return token


def _origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").strip().lower()
    hostname = (parsed.hostname or "").strip().lower()
    if not scheme or not hostname:
        return ""
    port = parsed.port
    if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return f"{scheme}://{hostname}"
    return f"{scheme}://{hostname}:{port}"


def _request_origin(request: Request) -> str:
    origin = str(request.headers.get("origin", "")).strip()
    if origin:
        return _origin_from_url(origin)
    referer = str(request.headers.get("referer", "")).strip()
    if referer:
        return _origin_from_url(referer)
    return ""


def _client_ip(request: Request) -> str:
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "unknown")


def _expected_request_origin() -> str:
    return _origin_from_url(_current_app_base_url())


def _require_same_origin(request: Request) -> None:
    expected_origin = _expected_request_origin()
    if not expected_origin:
        return
    request_origin = _request_origin(request)
    if not request_origin:
        raise HTTPException(status_code=403, detail="Missing request origin")
    if request_origin != expected_origin:
        raise HTTPException(status_code=403, detail="Cross-origin request blocked")


def _require_csrf(request: Request) -> None:
    _require_same_origin(request)
    session_token = _csrf_token(request)
    header_token = request.headers.get("X-Hermes-CSRF-Token", "").strip()
    if not header_token or header_token != session_token:
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def _provider_user_session_payload(provider_user: ProductUser) -> dict[str, Any]:
    return {
        "id": provider_user.id,
        "sub": provider_user.id,
        "email": provider_user.email,
        "name": provider_user.display_name,
        "preferred_username": provider_user.username,
        "is_admin": provider_user.is_admin,
        "tailscale_login": provider_user.tailscale_login,
    }


def _refresh_session_user(user: dict[str, Any]) -> dict[str, Any] | None:
    user_id = str(user.get("sub") or "").strip()
    if not user_id:
        return None
    provider_user = get_product_user_by_id(user_id)
    if provider_user is None or provider_user.disabled:
        return None
    return _provider_user_session_payload(provider_user)


def _session_refresh_due(request: Request) -> bool:
    raw_value = request.session.get("user_refreshed_at")
    try:
        refreshed_at = int(raw_value)
    except (TypeError, ValueError):
        return True
    return time.time() - refreshed_at >= _SESSION_REFRESH_TTL_SECONDS


def _store_session_user(request: Request, user: dict[str, Any]) -> dict[str, Any]:
    request.session["user"] = user
    request.session["user_refreshed_at"] = int(time.time())
    return user


def _clear_notice(request: Request) -> None:
    request.session.pop("auth_notice", None)
    request.session.pop("detected_tailscale_login", None)


def _set_notice(request: Request, message: str, *, tailscale_login: str | None = None) -> None:
    request.session["auth_notice"] = message
    if tailscale_login:
        request.session["detected_tailscale_login"] = tailscale_login
    else:
        request.session.pop("detected_tailscale_login", None)


def _resolve_session_user(request: Request) -> dict[str, Any]:
    user = request.session.get("user")
    if not isinstance(user, dict):
        raise HTTPException(status_code=401, detail="Not authenticated")
    refreshed = _refresh_session_user(user)
    if refreshed is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not authenticated")
    if _session_refresh_due(request) or refreshed != user:
        return _store_session_user(request, refreshed)
    return user


def _require_product_user(request: Request) -> dict[str, Any]:
    return _resolve_session_user(request)


def _require_admin_user(request: Request) -> dict[str, Any]:
    user = _require_product_user(request)
    if not bool(user.get("is_admin")):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _mark_bootstrap_completed_if_admin(user: dict[str, Any]) -> None:
    if bool(user.get("is_admin")):
        mark_first_admin_bootstrap_completed()


def _enforce_auth_rate_limit(request: Request, route_key: str) -> None:
    try:
        enforce_product_auth_rate_limit(
            _client_ip(request),
            route_key,
            max_requests=int(_AUTH_RATE_LIMIT_MAX_REQUESTS),
            window_seconds=int(_AUTH_RATE_LIMIT_WINDOW_SECONDS),
        )
    except ProductAuthRateLimitExceeded as exc:
        raise HTTPException(status_code=429, detail="Too many authentication requests")
    except Exception as exc:
        logger.warning("Product auth rate limiter unavailable: %s", exc)


def _current_product_urls() -> dict[str, str]:
    return resolve_product_urls(load_product_config())


def _current_app_base_url() -> str:
    return _current_product_urls()["app_base_url"]


def _tailscale_identity_from_claims(claims: dict[str, Any]) -> dict[str, str]:
    subject = str(claims.get("sub") or "").strip()
    login = str(claims.get("preferred_username") or claims.get("email") or claims.get("username") or "").strip().lower()
    email = str(claims.get("email") or "").strip().lower()
    display_name = str(claims.get("name") or claims.get("preferred_username") or email or login or subject).strip()
    return {
        "sub": subject,
        "login": login or email,
        "email": email,
        "name": display_name,
    }


def _active_admin_exists() -> bool:
    return any(user.is_admin and not user.disabled for user in list_product_users())


def _pending_invite_token(request: Request) -> str:
    return str(request.session.get("pending_invite_token", "")).strip()


def _set_pending_invite_token(request: Request, token: str | None) -> None:
    candidate = str(token or "").strip()
    if not candidate:
        request.session.pop("pending_invite_token", None)
        return
    request.session["pending_invite_token"] = candidate


def _pending_bootstrap_token(request: Request) -> str:
    return str(request.session.get("pending_bootstrap_token", "")).strip()


def _set_pending_bootstrap_token(request: Request, token: str | None) -> None:
    candidate = str(token or "").strip()
    if not candidate:
        request.session.pop("pending_bootstrap_token", None)
        return
    request.session["pending_bootstrap_token"] = candidate


def _pending_invite_identity(request: Request) -> dict[str, str] | None:
    raw = request.session.get("pending_invite_identity")
    if not isinstance(raw, dict):
        return None
    subject = str(raw.get("sub") or "").strip()
    login = str(raw.get("login") or "").strip().lower()
    name = str(raw.get("name") or "").strip()
    if not subject or not login:
        return None
    return {"sub": subject, "login": login, "name": name}


def _set_pending_invite_identity(request: Request, identity: dict[str, str] | None) -> None:
    if not identity:
        request.session.pop("pending_invite_identity", None)
        return
    request.session["pending_invite_identity"] = {
        "sub": str(identity.get("sub") or "").strip(),
        "login": str(identity.get("login") or "").strip().lower(),
        "name": str(identity.get("name") or "").strip(),
    }


def _pending_invite_record(request: Request) -> Any | None:
    token = _pending_invite_token(request)
    if not token:
        return None
    for invite in list_pending_product_signup_invites():
        if invite.token == token:
            return invite
    return None


def _canonical_request_redirect(request: Request, urls: dict[str, str]) -> str | None:
    canonical_base = urls["app_base_url"].rstrip("/")
    canonical_host = canonical_base.split("://", 1)[-1].lower()
    request_host = str(request.headers.get("host", "")).strip().lower()
    if request_host == canonical_host:
        return None
    if request.url.path == "/healthz":
        return None
    target = f"{canonical_base}{request.url.path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return target


def _build_product_app_context() -> ProductAppContext:
    product_config = load_product_config()
    auth_provider = str(product_config.get("auth", {}).get("provider", "unknown")).strip() or "unknown"
    product_name = str(product_config.get("product", {}).get("brand", {}).get("name", "Hermes Core")).strip() or "Hermes Core"
    return ProductAppContext(product_config=product_config, auth_provider=auth_provider, product_name=product_name)


def _root_route_services() -> RootRouteServices:
    return RootRouteServices(
        build_product_index_html=build_product_index_html,
        set_pending_invite_token=_set_pending_invite_token,
        set_pending_bootstrap_token=_set_pending_bootstrap_token,
        current_product_urls=_current_product_urls,
        current_app_base_url=_current_app_base_url,
        product_health_response_model=ProductHealthResponse,
    )


def _auth_route_services() -> AuthRouteServices:
    return AuthRouteServices(
        enforce_auth_rate_limit=_enforce_auth_rate_limit,
        csrf_token=_csrf_token,
        clear_notice=_clear_notice,
        set_pending_invite_identity=_set_pending_invite_identity,
        pending_invite_token=_pending_invite_token,
        pending_bootstrap_token=_pending_bootstrap_token,
        resolve_session_user=_resolve_session_user,
        mark_bootstrap_completed_if_admin=_mark_bootstrap_completed_if_admin,
        current_app_base_url=_current_app_base_url,
        start_tsidp_login=_start_tsidp_login,
        load_product_oidc_client_settings=_load_product_oidc_client_settings,
        discover_product_oidc_provider_metadata=_discover_product_oidc_provider_metadata,
        exchange_product_oidc_code=_exchange_product_oidc_code,
        validate_product_oidc_id_token=_validate_product_oidc_id_token,
        fetch_product_oidc_userinfo=_fetch_product_oidc_userinfo,
        tailscale_identity_from_claims=_tailscale_identity_from_claims,
        handle_tsidp_identity=_handle_tsidp_identity,
        store_session_user=_store_session_user,
        provider_user_session_payload=_provider_user_session_payload,
        session_response_payload=_session_response_payload,
        require_csrf=_require_csrf,
        pending_invite_identity=_pending_invite_identity,
        claim_product_user_from_invite=_claim_product_user_from_invite,
        set_pending_invite_token=_set_pending_invite_token,
        set_notice=_set_notice,
        product_session_response_model=ProductSessionResponse,
    )


def _chat_route_services() -> ChatRouteServices:
    return ChatRouteServices(
        require_product_user=_require_product_user,
        require_csrf=_require_csrf,
        runtime_session_payload=_runtime_session_payload,
        stream_product_runtime_turn=stream_product_chat_turn,
        stop_product_runtime_turn=stop_product_chat_turn,
        product_chat_session_response_model=ProductChatSessionResponse,
        product_chat_turn_request_model=ProductChatTurnRequest,
    )


def _workspace_route_services() -> WorkspaceRouteServices:
    return WorkspaceRouteServices(
        require_product_user=_require_product_user,
        require_csrf=_require_csrf,
        get_workspace_state=get_workspace_state,
        resolve_workspace_file=resolve_workspace_file,
        create_workspace_folder=create_workspace_folder,
        store_workspace_file=store_workspace_file,
        delete_workspace_path=delete_workspace_path,
        move_workspace_path=move_workspace_path,
        product_workspace_response_model=ProductWorkspaceResponse,
        product_create_workspace_folder_request_model=ProductCreateWorkspaceFolderRequest,
        product_delete_workspace_path_request_model=ProductDeleteWorkspacePathRequest,
        product_move_workspace_path_request_model=ProductMoveWorkspacePathRequest,
        product_workspace_quota_error=ProductWorkspaceQuotaError,
        workspace_response_payload=_workspace_response_payload,
    )


def _admin_route_services() -> AdminRouteServices:
    return AdminRouteServices(
        require_admin_user=_require_admin_user,
        require_csrf=_require_csrf,
        product_admin_users_response_model=ProductAdminUsersResponse,
        product_created_user_model=ProductCreatedUser,
        product_create_user_request_model=ProductCreateUserRequest,
        product_user_model=ProductUser,
        list_admin_entries=_list_admin_entries,
        create_invited_user=_create_invited_user,
        deactivate_product_user=_deactivate_product_user,
    )


def _runtime_session_payload(user: dict[str, Any]) -> dict[str, Any]:
    try:
        return get_product_chat_session(user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc) or "Runtime session unavailable") from exc


def _create_invited_user(payload: ProductCreateUserRequest) -> ProductCreatedUser:
    try:
        return ProductCreatedUser.model_validate(
            create_product_user_with_signup(
                username=payload.tailscale_login,
                display_name=payload.display_name,
                email=payload.tailscale_login,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _deactivate_product_user(user_id: str) -> ProductUser:
    try:
        updated = deactivate_product_user(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    normalized = ProductUser.model_validate(updated)
    delete_product_runtime(normalized.id)
    return normalized


def _list_admin_entries(admin_user: dict[str, Any]) -> ProductAdminUsersResponse:
    current_user_id = str(admin_user.get("sub") or "")
    rows: list[ProductAdminEntry] = []
    for user in list_product_users():
        rows.append(
            ProductAdminEntry(
                id=user.id,
                type="user",
                username=user.username,
                display_name=user.display_name,
                email=user.email,
                tailscale_login=user.tailscale_login,
                is_admin=user.is_admin,
                disabled=user.disabled,
                status="Disabled" if user.disabled else ("You" if user.id == current_user_id else "Active"),
            )
        )
    for invite in list_pending_product_signup_invites():
        rows.append(
            ProductAdminEntry(
                id=invite.invite_id,
                type="invite",
                username=None,
                display_name=invite.display_name,
                email=invite.tailscale_login,
                tailscale_login=invite.tailscale_login,
                status="Pending invite",
            )
        )
    return ProductAdminUsersResponse(users=rows)


def _session_response_payload(request: Request) -> ProductSessionResponse:
    urls = _current_product_urls()
    user = request.session.get("user")
    if not isinstance(user, dict):
        pending_invite = _pending_invite_record(request)
        return ProductSessionResponse(
            authenticated=False,
            csrf_token=_csrf_token(request),
            notice=str(request.session.get("auth_notice") or "").strip() or None,
            detected_tailscale_login=str(request.session.get("detected_tailscale_login") or "").strip() or None,
            pending_invite_claim=_pending_invite_identity(request) is not None and pending_invite is not None,
            pending_invite_display_name=str(getattr(pending_invite, "display_name", "") or "").strip() or None,
            app_base_url=urls.get("app_base_url"),
        )
    refreshed = _resolve_session_user(request)
    _mark_bootstrap_completed_if_admin(refreshed)
    return ProductSessionResponse(
        authenticated=True,
        user=refreshed,
        csrf_token=_csrf_token(request),
        app_base_url=urls.get("app_base_url"),
    )


def _start_tsidp_login(request: Request, context: ProductAppContext) -> RedirectResponse:
    settings = load_product_oidc_client_settings(config=context.product_config)
    metadata = discover_product_oidc_provider_metadata(settings)
    login_request = create_oidc_login_request(settings, metadata)
    request.session["oidc_pending"] = {
        "state": login_request["state"],
        "nonce": login_request["nonce"],
        "verifier": login_request["verifier"],
    }
    return RedirectResponse(login_request["authorization_url"], status_code=307)


def _handle_tsidp_identity(request: Request, identity: dict[str, str]) -> ProductUser:
    invite_token = _pending_invite_token(request)
    existing_user = get_product_user_by_tailscale_subject(identity["sub"])
    if existing_user is None and identity.get("login"):
        existing_user = get_product_user_by_tailscale_login(identity["login"])
    if existing_user is not None:
        if existing_user.disabled:
            raise HTTPException(status_code=403, detail="This account has been disabled")
        if invite_token:
            _set_pending_invite_identity(request, None)
            _set_notice(
                request,
                "This Tailscale account already belongs to an existing Hermes Core user. Use a different Tailscale account to claim this invite.",
                tailscale_login=identity.get("login"),
            )
            raise HTTPException(
                status_code=403,
                detail="This Tailscale account already belongs to an existing Hermes Core user. Use a different Tailscale account to claim this invite.",
            )
        return existing_user

    if not _active_admin_exists():
        enrollment_state = load_first_admin_enrollment_state() or {}
        expected_token = str(enrollment_state.get("bootstrap_token", "")).strip()
        pending_token = _pending_bootstrap_token(request)
        if not expected_token:
            raise HTTPException(status_code=500, detail="First admin bootstrap link is not configured")
        if pending_token != expected_token:
            _set_notice(
                request,
                "Open the one-time bootstrap link from setup to create the first admin.",
                tailscale_login=identity.get("login"),
            )
            raise HTTPException(status_code=403, detail="Open the one-time bootstrap link from setup to create the first admin.")
        user = bootstrap_first_admin_user(
            tailscale_subject=identity["sub"],
            tailscale_login=identity["login"],
            display_name=identity.get("name"),
        )
        _set_pending_bootstrap_token(request, None)
        mark_first_admin_bootstrap_completed(identity.get("login"))
        return user

    if invite_token:
        if _pending_invite_record(request) is None:
            _set_notice(request, "Invite is invalid or expired", tailscale_login=identity.get("login"))
            raise HTTPException(status_code=403, detail="Invite is invalid or expired")
        _set_pending_invite_identity(request, identity)
        _set_notice(
            request,
            "Confirm this Tailscale account to claim the invite.",
            tailscale_login=identity.get("login"),
        )
        raise HTTPException(status_code=403, detail="Confirm this Tailscale account to claim the invite.")

    _set_notice(
        request,
        "This Tailscale account is not invited to this app.",
        tailscale_login=identity.get("login"),
    )
    raise HTTPException(status_code=403, detail="This Tailscale account is not invited to this app.")


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

    register_root_routes(
        app,
        context,
        _root_route_services(),
    )
    register_auth_routes(
        app,
        context,
        _auth_route_services(),
    )
    register_chat_routes(
        app,
        _chat_route_services(),
    )
    register_workspace_routes(
        app,
        _workspace_route_services(),
    )
    register_admin_routes(
        app,
        _admin_route_services(),
    )
    return app
