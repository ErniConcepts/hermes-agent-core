"""Tailnet-only product app surface for hermes-core."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import secrets
import time
from collections import deque
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from hermes_cli.config import get_env_value
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
from hermes_cli.product_runtime import delete_product_runtime, get_product_runtime_session, stream_product_runtime_turn
from hermes_cli.product_stack import (
    disable_tailnet_activation,
    enable_tailnet_activation,
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
    store_workspace_file,
)

logger = logging.getLogger(__name__)
_SESSION_REFRESH_TTL_SECONDS = 30
_AUTH_RATE_LIMIT_WINDOW_SECONDS = 300.0
_AUTH_RATE_LIMIT_MAX_REQUESTS = 10
_AUTH_RATE_LIMITS: dict[tuple[str, str], deque[float]] = {}


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
    auth_provider: str
    issuer_url: str
    app_base_url: str


class ProductSessionResponse(BaseModel):
    authenticated: bool
    user: dict[str, Any] | None = None
    csrf_token: str | None = None
    notice: str | None = None
    detected_tailscale_login: str | None = None
    tailnet_enabled: bool = False
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


class ProductAdminNetworkResponse(BaseModel):
    tailscale_configured: bool
    activation_status: str
    app_base_url: str
    issuer_url: str
    tailnet_app_base_url: str | None = None
    tailnet_issuer_url: str | None = None
    tailnet_host: str | None = None


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


def _workspace_response_payload(payload: Any) -> ProductWorkspaceResponse:
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    return ProductWorkspaceResponse(**data)


def _session_secret() -> str:
    product_config = load_product_config()
    secret_ref = str(product_config.get("auth", {}).get("session_secret_ref", "")).strip()
    if secret_ref:
        configured = str(get_env_value(secret_ref) or "").strip()
        if configured:
            return configured
    settings = load_product_oidc_client_settings()
    digest = hashlib.sha256(settings.client_secret.encode("utf-8")).hexdigest()
    return f"hermes-product-session-{digest}"


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
    if not _session_refresh_due(request):
        return user
    refreshed = _refresh_session_user(user)
    if refreshed is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _store_session_user(request, refreshed)


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
    now = time.monotonic()
    bucket = _AUTH_RATE_LIMITS.setdefault((_client_ip(request), route_key), deque())
    cutoff = now - _AUTH_RATE_LIMIT_WINDOW_SECONDS
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _AUTH_RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Too many authentication requests")
    bucket.append(now)


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


def _bootstrap_first_admin_login() -> str:
    product_config = load_product_config()
    return str(product_config.get("bootstrap", {}).get("first_admin_tailscale_login", "")).strip().lower()


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


def _runtime_session_payload(user: dict[str, Any]) -> dict[str, Any]:
    try:
        return get_product_runtime_session(user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc) or "Runtime session unavailable") from exc


def _create_signup_user(payload: ProductCreateUserRequest) -> ProductCreatedUser:
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


def _deactivate_runtime_user(user_id: str) -> ProductUser:
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
        return ProductSessionResponse(
            authenticated=False,
            csrf_token=_csrf_token(request),
            notice=str(request.session.get("auth_notice") or "").strip() or None,
            detected_tailscale_login=str(request.session.get("detected_tailscale_login") or "").strip() or None,
            tailnet_enabled=bool(urls.get("tailnet_active")),
            app_base_url=urls.get("app_base_url"),
        )
    refreshed = _resolve_session_user(request)
    _mark_bootstrap_completed_if_admin(refreshed)
    return ProductSessionResponse(
        authenticated=True,
        user=refreshed,
        csrf_token=_csrf_token(request),
        tailnet_enabled=bool(urls.get("tailnet_active")),
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
    existing_user = get_product_user_by_tailscale_subject(identity["sub"])
    if existing_user is None and identity.get("login"):
        existing_user = get_product_user_by_tailscale_login(identity["login"])
    if existing_user is not None:
        if existing_user.disabled:
            raise HTTPException(status_code=403, detail="This account has been disabled")
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

    invite_token = _pending_invite_token(request)
    if invite_token:
        try:
            user = claim_product_user_from_invite(
                token=invite_token,
                tailscale_subject=identity["sub"],
                tailscale_login=identity["login"],
                display_name=identity.get("name"),
            )
            _set_pending_invite_token(request, None)
            return user
        except ValueError as exc:
            _set_notice(request, str(exc), tailscale_login=identity.get("login"))
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    _set_notice(
        request,
        "This Tailscale account is not invited to this app.",
        tailscale_login=identity.get("login"),
    )
    raise HTTPException(status_code=403, detail="This Tailscale account is not invited to this app.")


def _register_root_routes(app: FastAPI, context: ProductAppContext) -> None:
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(build_product_index_html(product_name=context.product_name, account_url=context.app_base_url))

    @app.get("/healthz", response_model=ProductHealthResponse)
    def healthz() -> ProductHealthResponse:
        return ProductHealthResponse(
            auth_provider=context.auth_provider,
            issuer_url=_current_product_urls()["issuer_url"],
            app_base_url=_current_app_base_url(),
        )

    @app.get("/invite/{token}")
    def invite_login(request: Request, token: str) -> RedirectResponse:
        _set_pending_invite_token(request, token)
        return RedirectResponse("/api/auth/login", status_code=303)

    @app.get("/bootstrap/{token}")
    def bootstrap_login(request: Request, token: str) -> RedirectResponse:
        _set_pending_bootstrap_token(request, token)
        return RedirectResponse("/api/auth/login", status_code=303)


def _register_auth_routes(app: FastAPI, context: ProductAppContext) -> None:
    @app.get("/api/auth/login")
    def auth_login(request: Request) -> RedirectResponse:
        _enforce_auth_rate_limit(request, "login")
        _csrf_token(request)
        _clear_notice(request)
        existing = request.session.get("user")
        if isinstance(existing, dict):
            try:
                refreshed = _resolve_session_user(request)
            except HTTPException:
                refreshed = None
            if refreshed is not None:
                _mark_bootstrap_completed_if_admin(refreshed)
                return RedirectResponse(_current_app_base_url(), status_code=303)
            request.session.clear()
        return _start_tsidp_login(request, context)

    @app.get("/api/auth/oidc/callback")
    def auth_callback(request: Request, code: str, state: str) -> RedirectResponse:
        _enforce_auth_rate_limit(request, "callback")
        pending = request.session.get("oidc_pending")
        if not isinstance(pending, dict):
            return RedirectResponse(_current_app_base_url(), status_code=303)
        if state != pending.get("state"):
            request.session.pop("oidc_pending", None)
            _set_notice(request, "The Tailscale login state was invalid. Please try again.")
            return RedirectResponse(_current_app_base_url(), status_code=303)

        settings = load_product_oidc_client_settings(config=context.product_config)
        metadata = discover_product_oidc_provider_metadata(settings)
        token_response = exchange_product_oidc_code(
            settings,
            metadata,
            code=code,
            verifier=str(pending.get("verifier", "")),
        )
        request.session.pop("oidc_pending", None)
        id_token_claims: dict[str, Any] | None = None
        id_token = str(token_response.get("id_token", "")).strip()
        if id_token:
            id_token_claims = validate_product_oidc_id_token(
                id_token,
                settings,
                metadata,
                nonce=str(pending.get("nonce", "")),
            )
        access_token = str(token_response.get("access_token", "")).strip()
        if access_token and metadata.userinfo_endpoint:
            claims = fetch_product_oidc_userinfo(access_token, metadata)
        elif isinstance(id_token_claims, dict):
            claims = id_token_claims
        else:
            _set_notice(request, "The Tailscale login response did not include identity claims.")
            return RedirectResponse(_current_app_base_url(), status_code=303)

        identity = _tailscale_identity_from_claims(claims)
        if not identity.get("sub") or not identity.get("login"):
            _set_notice(request, "The Tailscale login response did not include a stable account identity.")
            return RedirectResponse(_current_app_base_url(), status_code=303)

        try:
            provider_user = _handle_tsidp_identity(request, identity)
        except HTTPException:
            request.session.pop("user", None)
            request.session.pop("user_refreshed_at", None)
            return RedirectResponse(_current_app_base_url(), status_code=303)

        _clear_notice(request)
        _store_session_user(request, _provider_user_session_payload(provider_user))
        _mark_bootstrap_completed_if_admin(request.session["user"])
        _csrf_token(request)
        return RedirectResponse(_current_app_base_url(), status_code=303)

    @app.get("/api/auth/session", response_model=ProductSessionResponse)
    def auth_session(request: Request) -> ProductSessionResponse:
        try:
            return _session_response_payload(request)
        except HTTPException:
            return ProductSessionResponse(authenticated=False, csrf_token=_csrf_token(request))

    @app.post("/api/auth/logout", response_model=ProductSessionResponse)
    def auth_logout(request: Request) -> ProductSessionResponse:
        _require_csrf(request)
        request.session.clear()
        _csrf_token(request)
        return ProductSessionResponse(authenticated=False, csrf_token=request.session.get("csrf_token"))


def _register_chat_routes(app: FastAPI) -> None:
    @app.get("/api/chat/session", response_model=ProductChatSessionResponse)
    def chat_session(request: Request) -> ProductChatSessionResponse:
        user = _require_product_user(request)
        return ProductChatSessionResponse(**_runtime_session_payload(user))

    @app.post("/api/chat/turn/stream")
    def chat_turn_stream(request: Request, payload: ProductChatTurnRequest) -> StreamingResponse:
        user = _require_product_user(request)
        _require_csrf(request)
        try:
            event_stream = stream_product_runtime_turn(user, payload.user_message)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return StreamingResponse(
            event_stream,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


def _register_workspace_routes(app: FastAPI) -> None:
    @app.get("/api/workspace", response_model=ProductWorkspaceResponse)
    def workspace_state(request: Request, path: str = "") -> ProductWorkspaceResponse:
        user = _require_product_user(request)
        try:
            payload = get_workspace_state(user, path=path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _workspace_response_payload(payload)

    @app.post("/api/workspace/folders", response_model=ProductWorkspaceResponse)
    def workspace_create_folder(request: Request, payload: ProductCreateWorkspaceFolderRequest) -> ProductWorkspaceResponse:
        user = _require_product_user(request)
        _require_csrf(request)
        try:
            state = create_workspace_folder(user, parent_path=payload.path, folder_name=payload.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _workspace_response_payload(state)

    @app.post("/api/workspace/files", response_model=ProductWorkspaceResponse)
    async def workspace_upload_files(
        request: Request,
        path: str = Form(default=""),
        files: list[UploadFile] = File(...),
    ) -> ProductWorkspaceResponse:
        user = _require_product_user(request)
        _require_csrf(request)
        current_state = None
        try:
            for upload in files:
                content = await upload.read()
                current_state = store_workspace_file(
                    user,
                    parent_path=path,
                    filename=upload.filename or "",
                    content=content,
                )
        except ProductWorkspaceQuotaError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            for upload in files:
                await upload.close()
        if current_state is None:
            raise HTTPException(status_code=400, detail="At least one file is required")
        return _workspace_response_payload(current_state)

    @app.post("/api/workspace/delete", response_model=ProductWorkspaceResponse)
    def workspace_delete(request: Request, payload: ProductDeleteWorkspacePathRequest) -> ProductWorkspaceResponse:
        user = _require_product_user(request)
        _require_csrf(request)
        try:
            state = delete_workspace_path(user, path=payload.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _workspace_response_payload(state)


def _network_response_payload() -> ProductAdminNetworkResponse:
    urls = _current_product_urls()
    return ProductAdminNetworkResponse(
        tailscale_configured=bool(urls.get("tailnet_host")),
        activation_status=str(urls.get("tailnet_activation_status", "disabled")),
        app_base_url=urls["app_base_url"],
        issuer_url=urls["issuer_url"],
        tailnet_app_base_url=urls.get("tailnet_app_base_url"),
        tailnet_issuer_url=urls.get("tailnet_issuer_url"),
        tailnet_host=urls.get("tailnet_host"),
    )


def _register_admin_routes(app: FastAPI) -> None:
    @app.get("/api/admin/network", response_model=ProductAdminNetworkResponse)
    def admin_network_state(request: Request) -> ProductAdminNetworkResponse:
        _require_admin_user(request)
        return _network_response_payload()

    @app.post("/api/admin/network/tailscale/enable", response_model=ProductAdminNetworkResponse)
    def admin_enable_tailnet(request: Request) -> ProductAdminNetworkResponse:
        _require_admin_user(request)
        _require_csrf(request)
        try:
            enable_tailnet_activation()
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _network_response_payload()

    @app.post("/api/admin/network/tailscale/disable", response_model=ProductAdminNetworkResponse)
    def admin_disable_tailnet(request: Request) -> ProductAdminNetworkResponse:
        _require_admin_user(request)
        _require_csrf(request)
        disable_tailnet_activation()
        return _network_response_payload()

    @app.get("/api/admin/users", response_model=ProductAdminUsersResponse)
    def admin_list_users(request: Request) -> ProductAdminUsersResponse:
        admin_user = _require_admin_user(request)
        return _list_admin_entries(admin_user)

    @app.post("/api/admin/users", response_model=ProductCreatedUser)
    def admin_create_user(request: Request, payload: ProductCreateUserRequest) -> ProductCreatedUser:
        _require_admin_user(request)
        _require_csrf(request)
        return _create_signup_user(payload)

    @app.post("/api/admin/users/{user_id}/deactivate", response_model=ProductUser)
    def admin_deactivate_user(request: Request, user_id: str) -> ProductUser:
        admin_user = _require_admin_user(request)
        _require_csrf(request)
        if user_id == str(admin_user.get("sub") or ""):
            raise HTTPException(status_code=400, detail="Admins cannot deactivate their own account")
        return _deactivate_runtime_user(user_id)


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

    _register_root_routes(app, context)
    _register_auth_routes(app, context)
    _register_chat_routes(app)
    _register_workspace_routes(app)
    _register_admin_routes(app)
    return app
