"""Shared helpers and models for the product FastAPI app."""

from __future__ import annotations

from dataclasses import dataclass, replace
from ipaddress import ip_address
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field

from hermes_cli.config import get_env_value
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
    ProductOIDCProviderMetadata,
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
_TSIDP_BROWSER_PROXY_PREFIX = "/_hermes/tsidp"
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


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


def _tsidp_browser_proxy_enabled(config: dict[str, Any]) -> bool:
    tailscale = config.get("network", {}).get("tailscale", {})
    if not isinstance(tailscale, dict):
        return False
    return str(tailscale.get("browser_host_mode", "")).strip() == "windows_tailscale"


def _tsidp_browser_proxy_base_url(config: dict[str, Any]) -> str:
    return resolve_product_urls(config)["app_base_url"].rstrip("/") + _TSIDP_BROWSER_PROXY_PREFIX


def _tsidp_browser_proxy_url_for_endpoint(endpoint_url: str, config: dict[str, Any]) -> str:
    issuer_url = str(config.get("auth", {}).get("issuer_url", "")).strip().rstrip("/")
    endpoint = str(endpoint_url or "").strip()
    if not issuer_url:
        return endpoint
    if endpoint == issuer_url:
        suffix = "/"
    elif endpoint.startswith(f"{issuer_url}/"):
        suffix = endpoint[len(issuer_url) :]
    else:
        return endpoint
    if not suffix.startswith("/"):
        suffix = f"/{suffix}"
    return f"{_tsidp_browser_proxy_base_url(config)}{suffix}"


def _metadata_for_browser_login(
    metadata: ProductOIDCProviderMetadata,
    config: dict[str, Any],
) -> ProductOIDCProviderMetadata:
    if not _tsidp_browser_proxy_enabled(config):
        return metadata
    return replace(
        metadata,
        authorization_endpoint=_tsidp_browser_proxy_url_for_endpoint(metadata.authorization_endpoint, config),
    )


def _rewrite_tsidp_browser_location(location: str, config: dict[str, Any]) -> str:
    if str(location or "").startswith("/"):
        return f"{_tsidp_browser_proxy_base_url(config)}{location}"
    rewritten = _tsidp_browser_proxy_url_for_endpoint(location, config)
    return rewritten or location


def _rewrite_tsidp_set_cookie(value: str) -> str:
    parts = [part for part in str(value or "").split(";") if not part.strip().lower().startswith("domain=")]
    return ";".join(parts)


def _tsidp_proxy_target_url(config: dict[str, Any], path: str, query: str) -> str:
    issuer_url = str(config.get("auth", {}).get("issuer_url", "")).strip().rstrip("/")
    if not issuer_url:
        raise HTTPException(status_code=404, detail="tsidp issuer is not configured")
    clean_path = "/" + str(path or "").lstrip("/")
    target_url = f"{issuer_url}{clean_path}"
    if query:
        target_url = f"{target_url}?{query}"
    return target_url


async def _proxy_tsidp_browser_request(request: Request, path: str) -> Response:
    product_config = load_product_config()
    if not _tsidp_browser_proxy_enabled(product_config):
        raise HTTPException(status_code=404, detail="tsidp browser proxy is not enabled")

    target_url = _tsidp_proxy_target_url(product_config, path, request.url.query)
    request_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() not in {"host", "content-length"}
    }
    request_headers["host"] = urlparse(str(product_config.get("auth", {}).get("issuer_url", ""))).netloc
    body = await request.body()
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        upstream = await client.request(request.method, target_url, headers=request_headers, content=body)

    response = Response(content=upstream.content, status_code=upstream.status_code)
    for key, value in upstream.headers.multi_items():
        lowered = key.lower()
        if lowered in _HOP_BY_HOP_HEADERS or lowered in {"content-length", "content-encoding"}:
            continue
        if lowered == "location":
            value = _rewrite_tsidp_browser_location(value, product_config)
        elif lowered == "set-cookie":
            value = _rewrite_tsidp_set_cookie(value)
        response.raw_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    return response


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
    token = secrets.token_urlsafe(32)
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


def _trusted_proxy_ips() -> set[str]:
    configured = load_product_config().get("network", {}).get("trusted_proxy_ips", ["127.0.0.1", "::1"])
    if not isinstance(configured, list):
        return {"127.0.0.1", "::1"}
    trusted: set[str] = set()
    for candidate in configured:
        value = str(candidate or "").strip()
        if not value:
            continue
        try:
            trusted.add(str(ip_address(value)))
        except ValueError:
            continue
    return trusted or {"127.0.0.1", "::1"}


def _normalized_ip(value: str) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        return str(ip_address(candidate))
    except ValueError:
        return None


def _client_ip(request: Request) -> str:
    client = getattr(request, "client", None)
    peer_ip = _normalized_ip(getattr(client, "host", "") or "")
    if peer_ip is None:
        return "unknown"
    if peer_ip not in _trusted_proxy_ips():
        return peer_ip
    forwarded_for = str(request.headers.get("X-Forwarded-For", "")).strip()
    if forwarded_for:
        for part in forwarded_for.split(","):
            forwarded_ip = _normalized_ip(part)
            if forwarded_ip is not None:
                return forwarded_ip
    real_ip = _normalized_ip(request.headers.get("X-Real-IP", ""))
    if real_ip is not None:
        return real_ip
    return peer_ip


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
    except ProductAuthRateLimitExceeded:
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
    metadata = _metadata_for_browser_login(metadata, context.product_config)
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
