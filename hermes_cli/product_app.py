"""Authenticated product app surface for hermes-core."""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from hermes_cli.product_config import load_product_config
from hermes_cli.product_invites import (
    list_pending_product_signup_invites,
    register_product_signup_invite,
)
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
    load_first_admin_enrollment_state,
    mark_first_admin_bootstrap_completed,
    resolve_product_urls,
)
from hermes_cli.product_users import (
    ProductCreatedUser,
    ProductSignupToken,
    ProductUser,
    create_product_user_with_signup,
    deactivate_product_user,
    get_product_user_by_id,
    list_product_users,
)
from hermes_cli.product_workspace import (
    ProductWorkspaceEntry,
    ProductWorkspaceQuotaError,
    create_workspace_folder,
    get_workspace_state,
    store_workspace_file,
)
from hermes_cli.product_web import build_product_index_html

logger = logging.getLogger(__name__)

class ProductHealthResponse(BaseModel):
    status: str = "ok"
    auth_provider: str
    issuer_url: str
    app_base_url: str


class ProductSessionResponse(BaseModel):
    authenticated: bool
    user: dict[str, Any] | None = None
    csrf_token: str | None = None


class ProductAdminUsersResponse(BaseModel):
    users: list["ProductAdminEntry"]


class ProductAdminEntry(BaseModel):
    id: str
    type: str = "user"
    username: str | None = None
    display_name: str
    email: str | None = None
    is_admin: bool = False
    disabled: bool = False
    status: str


class ProductCreateUserRequest(BaseModel):
    username: str | None = None
    display_name: str | None = None
    email: str | None = None


class ProductChatMessage(BaseModel):
    role: str
    content: str


class ProductChatSessionResponse(BaseModel):
    session_id: str
    messages: list[ProductChatMessage]


class ProductChatTurnRequest(BaseModel):
    user_message: str


class ProductWorkspaceResponse(BaseModel):
    current_path: str
    entries: list[ProductWorkspaceEntry]
    used_bytes: int
    limit_bytes: int


class ProductCreateWorkspaceFolderRequest(BaseModel):
    path: str = ""
    name: str


def _workspace_response_payload(payload: Any) -> ProductWorkspaceResponse:
    if hasattr(payload, "model_dump"):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    return ProductWorkspaceResponse(**data)


def _session_secret() -> str:
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


def _require_csrf(request: Request) -> None:
    session_token = _csrf_token(request)
    header_token = request.headers.get("X-Hermes-CSRF-Token", "").strip()
    if not header_token or header_token != session_token:
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def _session_user_payload(userinfo: dict[str, Any], provider_user: ProductUser | None = None) -> dict[str, Any]:
    email = userinfo.get("email")
    if isinstance(email, str) and email.endswith("@users.local.invalid"):
        email = None
    provider_username = getattr(provider_user, "username", None) if provider_user is not None else None
    provider_display_name = getattr(provider_user, "display_name", None) if provider_user is not None else None
    provider_email = getattr(provider_user, "email", None) if provider_user is not None else None
    provider_is_admin = bool(getattr(provider_user, "is_admin", False)) if provider_user is not None else False
    provider_username = provider_username or userinfo.get("preferred_username")
    provider_display_name = provider_display_name or (
        userinfo.get("name") or userinfo.get("preferred_username") or userinfo.get("sub", "")
    )
    provider_email = provider_email or email
    return {
        "id": userinfo.get("sub", ""),
        "sub": userinfo.get("sub", ""),
        "email": provider_email,
        "name": provider_display_name,
        "preferred_username": provider_username,
        "email_verified": userinfo.get("email_verified"),
        "is_admin": provider_is_admin,
    }


def _refresh_session_user(user: dict[str, Any]) -> dict[str, Any] | None:
    started = time.perf_counter()
    user_id = str(user.get("sub") or "").strip()
    if not user_id:
        return None
    provider_user = get_product_user_by_id(user_id)
    if provider_user is None or bool(getattr(provider_user, "disabled", False)):
        logger.info(
            "product_app user refresh for %s completed in %.0fms (missing/disabled)",
            user_id,
            (time.perf_counter() - started) * 1000,
        )
        return None
    refreshed = dict(user)
    refreshed["id"] = getattr(provider_user, "id", user_id)
    refreshed["email"] = getattr(provider_user, "email", refreshed.get("email"))
    refreshed["name"] = getattr(provider_user, "display_name", None) or refreshed.get("name")
    refreshed["preferred_username"] = getattr(provider_user, "username", None) or refreshed.get("preferred_username")
    refreshed["is_admin"] = bool(getattr(provider_user, "is_admin", False))
    logger.info(
        "product_app user refresh for %s completed in %.0fms",
        user_id,
        (time.perf_counter() - started) * 1000,
    )
    return refreshed


def _require_product_user(request: Request) -> dict[str, Any]:
    user = request.session.get("user")
    if not isinstance(user, dict):
        raise HTTPException(status_code=401, detail="Not authenticated")
    refreshed = _refresh_session_user(user)
    if refreshed is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not authenticated")
    request.session["user"] = refreshed
    return refreshed


def _require_admin_user(request: Request) -> dict[str, Any]:
    user = _require_product_user(request)
    if not bool(user.get("is_admin")):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _mark_bootstrap_completed_if_admin(user: dict[str, Any]) -> None:
    if bool(user.get("is_admin")):
        mark_first_admin_bootstrap_completed()


def _pocket_id_proxy_base_url(config: dict[str, Any]) -> str:
    services = config.get("services", {}).get("pocket_id", {})
    upstream_port = int(services.get("upstream_port", 19141))
    return f"http://127.0.0.1:{upstream_port}"


def _is_setup_path(path: str) -> bool:
    candidate = (path or "").lstrip("/")
    return candidate == "setup" or candidate.startswith("setup/")


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _filter_proxy_response_headers(headers: dict[str, str]) -> dict[str, str]:
    filtered: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        filtered[key] = value
    return filtered


async def _proxy_pocket_id_request(request: Request, pocket_path: str) -> Response:
    enrollment_state = load_first_admin_enrollment_state() or {}
    if bool(enrollment_state.get("first_admin_login_seen", False)) and _is_setup_path(pocket_path):
        raise HTTPException(status_code=404, detail="Not found")
    base_url = _pocket_id_proxy_base_url(load_product_config()).rstrip("/")
    upstream_url = f"{base_url}/{pocket_path.lstrip('/')}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        upstream = await client.request(
            request.method,
            upstream_url,
            headers=headers,
            content=body,
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_filter_proxy_response_headers(dict(upstream.headers)),
    )


def _canonical_request_redirect(request: Request, urls: dict[str, str]) -> str | None:
    tailnet_host = str(urls.get("tailnet_host", "")).strip().lower()
    if not tailnet_host:
        return None
    canonical_base = urls["app_base_url"].rstrip("/")
    canonical_host = canonical_base.split("://", 1)[-1].lower()
    request_host = str(request.headers.get("host", "")).strip().lower()
    if request_host == canonical_host:
        return None
    target = f"{canonical_base}{request.url.path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return target


def create_product_app() -> FastAPI:
    product_config = load_product_config()
    urls = resolve_product_urls(product_config)
    auth_provider = str(product_config.get("auth", {}).get("provider", "unknown")).strip() or "unknown"
    issuer_url = str(product_config.get("auth", {}).get("issuer_url", "")).strip() or urls["issuer_url"]

    app = FastAPI(title="Hermes Core Product App", version="0.1.0")
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(),
        session_cookie="hermes_product_session",
        same_site="lax",
        https_only=urls["app_base_url"].startswith("https://"),
    )

    @app.middleware("http")
    async def enforce_canonical_origin(request: Request, call_next):
        redirect_url = _canonical_request_redirect(request, urls)
        if redirect_url is not None:
            return RedirectResponse(redirect_url, status_code=307)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        product_name = (
            str(product_config.get("product", {}).get("brand", {}).get("name", "Hermes Core")).strip()
            or "Hermes Core"
        )
        return HTMLResponse(
            build_product_index_html(
                product_name=product_name,
                account_url=f"{issuer_url.rstrip('/')}/settings/account",
            )
        )

    @app.get("/healthz", response_model=ProductHealthResponse)
    def healthz() -> ProductHealthResponse:
        return ProductHealthResponse(
            auth_provider=auth_provider,
            issuer_url=issuer_url,
            app_base_url=urls["app_base_url"],
        )

    @app.api_route(
        "/__pocket_id_proxy/{pocket_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def pocket_id_proxy(request: Request, pocket_path: str) -> Response:
        return await _proxy_pocket_id_request(request, pocket_path)

    @app.get("/api/auth/login")
    def auth_login(request: Request) -> RedirectResponse:
        _csrf_token(request)
        existing = request.session.get("user")
        if isinstance(existing, dict):
            refreshed = _refresh_session_user(existing)
            if refreshed is not None:
                request.session["user"] = refreshed
                return RedirectResponse(urls["app_base_url"], status_code=303)
            request.session.clear()
        settings = load_product_oidc_client_settings(config=product_config)
        metadata = discover_product_oidc_provider_metadata(settings)
        login_request = create_oidc_login_request(settings, metadata)
        request.session["oidc_pending"] = {
            "state": login_request["state"],
            "nonce": login_request["nonce"],
            "verifier": login_request["verifier"],
        }
        return RedirectResponse(login_request["authorization_url"], status_code=307)

    @app.get("/api/auth/oidc/callback")
    def auth_callback(request: Request, code: str, state: str) -> RedirectResponse:
        pending = request.session.get("oidc_pending")
        if not isinstance(pending, dict):
            return RedirectResponse(urls["app_base_url"], status_code=303)
        if state != pending.get("state"):
            request.session.pop("oidc_pending", None)
            return RedirectResponse(urls["app_base_url"], status_code=303)

        settings = load_product_oidc_client_settings(config=product_config)
        metadata = discover_product_oidc_provider_metadata(settings)
        token_response = exchange_product_oidc_code(
            settings,
            metadata,
            code=code,
            verifier=str(pending.get("verifier", "")),
        )
        access_token = str(token_response.get("access_token", "")).strip()
        if not access_token:
            raise HTTPException(status_code=502, detail="OIDC token response missing access_token")
        id_token = str(token_response.get("id_token", "")).strip()
        if id_token:
            validate_product_oidc_id_token(
                id_token,
                settings,
                metadata,
                nonce=str(pending.get("nonce", "")),
            )
        userinfo = fetch_product_oidc_userinfo(access_token, metadata)
        request.session.pop("oidc_pending", None)
        provider_user = get_product_user_by_id(str(userinfo.get("sub") or "").strip())
        session_user = _session_user_payload(userinfo, provider_user)
        request.session["user"] = session_user
        _mark_bootstrap_completed_if_admin(session_user)
        _csrf_token(request)
        return RedirectResponse(urls["app_base_url"], status_code=303)

    @app.get("/api/auth/session", response_model=ProductSessionResponse)
    def auth_session(request: Request) -> ProductSessionResponse:
        user = request.session.get("user")
        if not isinstance(user, dict):
            return ProductSessionResponse(authenticated=False, csrf_token=_csrf_token(request))
        refreshed = _refresh_session_user(user)
        if refreshed is None:
            request.session.clear()
            return ProductSessionResponse(authenticated=False, csrf_token=_csrf_token(request))
        request.session["user"] = refreshed
        _mark_bootstrap_completed_if_admin(refreshed)
        return ProductSessionResponse(authenticated=True, user=refreshed, csrf_token=_csrf_token(request))

    @app.post("/api/auth/logout", response_model=ProductSessionResponse)
    def auth_logout(request: Request) -> ProductSessionResponse:
        _require_csrf(request)
        request.session.clear()
        _csrf_token(request)
        return ProductSessionResponse(authenticated=False, csrf_token=request.session.get("csrf_token"))

    @app.get("/api/chat/session", response_model=ProductChatSessionResponse)
    def chat_session(request: Request) -> ProductChatSessionResponse:
        started = time.perf_counter()
        user = _require_product_user(request)
        try:
            payload = get_product_runtime_session(user)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc) or "Runtime session unavailable") from exc
        logger.info(
            "product_app /api/chat/session completed in %.0fms",
            (time.perf_counter() - started) * 1000,
        )
        return ProductChatSessionResponse(**payload)

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
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/workspace", response_model=ProductWorkspaceResponse)
    def workspace_state(request: Request, path: str = "") -> ProductWorkspaceResponse:
        started = time.perf_counter()
        user = _require_product_user(request)
        try:
            payload = get_workspace_state(user, path=path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info(
            "product_app /api/workspace completed in %.0fms",
            (time.perf_counter() - started) * 1000,
        )
        return _workspace_response_payload(payload)

    @app.post("/api/workspace/folders", response_model=ProductWorkspaceResponse)
    def workspace_create_folder(
        request: Request,
        payload: ProductCreateWorkspaceFolderRequest,
    ) -> ProductWorkspaceResponse:
        user = _require_product_user(request)
        _require_csrf(request)
        try:
            state = create_workspace_folder(
                user,
                parent_path=payload.path,
                folder_name=payload.name,
            )
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

    @app.get("/api/admin/users", response_model=ProductAdminUsersResponse)
    def admin_list_users(request: Request) -> ProductAdminUsersResponse:
        started = time.perf_counter()
        admin_user = _require_admin_user(request)
        users = list_product_users()
        pending_invites = list_pending_product_signup_invites()
        current_user_id = str(admin_user.get("sub") or "")
        rows: list[ProductAdminEntry] = []
        for user in users:
            rows.append(
                ProductAdminEntry(
                    id=user.id,
                    type="user",
                    username=user.username,
                    display_name=user.display_name,
                    email=user.email,
                    is_admin=user.is_admin,
                    disabled=user.disabled,
                    status="Disabled" if user.disabled else ("You" if user.id == current_user_id else "Active"),
                )
            )
        for invite in pending_invites:
            rows.append(
                ProductAdminEntry(
                    id=invite.invite_id,
                    type="invite",
                    username=None,
                    display_name="User",
                    email=None,
                    is_admin=False,
                    disabled=False,
                    status="No signup",
                )
            )
        response = ProductAdminUsersResponse(users=rows)
        logger.info(
            "product_app /api/admin/users completed in %.0fms",
            (time.perf_counter() - started) * 1000,
        )
        return response

    @app.post("/api/admin/users", response_model=ProductCreatedUser)
    def admin_create_user(request: Request, payload: ProductCreateUserRequest | None = None) -> ProductCreatedUser:
        _require_admin_user(request)
        _require_csrf(request)
        normalized_payload = payload or ProductCreateUserRequest()
        try:
            created = ProductCreatedUser.model_validate(
                create_product_user_with_signup(
                    normalized_payload.username,
                    normalized_payload.display_name,
                    email=normalized_payload.email,
                )
            )
            register_product_signup_invite(created.signup)
            return created
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/admin/users/{user_id}/deactivate", response_model=ProductUser)
    def admin_deactivate_user(request: Request, user_id: str) -> ProductUser:
        admin_user = _require_admin_user(request)
        _require_csrf(request)
        if user_id == str(admin_user.get("sub") or ""):
            raise HTTPException(status_code=400, detail="Admins cannot deactivate their own account")
        try:
            updated = deactivate_product_user(user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        normalized = ProductUser.model_validate(updated)
        delete_product_runtime(normalized.username)
        return normalized

    return app


def create_product_auth_proxy_app() -> FastAPI:
    app = FastAPI(title="Hermes Core Product Auth Proxy", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route(
        "/{pocket_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def pocket_id_proxy_root(request: Request, pocket_path: str) -> Response:
        return await _proxy_pocket_id_request(request, pocket_path)

    return app
