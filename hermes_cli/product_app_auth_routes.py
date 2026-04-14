from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from hermes_cli.product_app_support import (
    ProductSessionResponse,
    _clear_notice,
    _csrf_token,
    _current_app_base_url,
    _fetch_product_oidc_userinfo,
    _handle_tsidp_identity,
    _load_product_oidc_client_settings,
    _mark_bootstrap_completed_if_admin,
    _pending_bootstrap_token,
    _pending_invite_identity,
    _pending_invite_token,
    _proxy_tsidp_browser_request,
    _provider_user_session_payload,
    _require_csrf,
    _resolve_session_user,
    _session_response_payload,
    _set_notice,
    _set_pending_invite_identity,
    _set_pending_invite_token,
    _start_tsidp_login,
    _store_session_user,
    _tailscale_identity_from_claims,
    _validate_product_oidc_id_token,
    _discover_product_oidc_provider_metadata,
    _enforce_auth_rate_limit,
    _exchange_product_oidc_code,
)
from hermes_cli.product_users import claim_product_user_from_invite


def register_auth_routes(app: FastAPI, context: object) -> None:
    @app.api_route("/_hermes/tsidp", methods=["GET", "POST", "OPTIONS", "HEAD"])
    @app.api_route("/_hermes/tsidp/{path:path}", methods=["GET", "POST", "OPTIONS", "HEAD"])
    async def tsidp_browser_proxy(request: Request, path: str = "") -> object:
        return await _proxy_tsidp_browser_request(request, path)

    @app.get("/api/auth/login")
    def auth_login(request: Request) -> RedirectResponse:
        _enforce_auth_rate_limit(request, "login")
        _csrf_token(request)
        _clear_notice(request)
        _set_pending_invite_identity(request, None)
        existing = request.session.get("user")
        if isinstance(existing, dict) and not _pending_invite_token(request) and not _pending_bootstrap_token(request):
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

        settings = _load_product_oidc_client_settings(config=context.product_config)
        metadata = _discover_product_oidc_provider_metadata(settings)
        token_response = _exchange_product_oidc_code(
            settings,
            metadata,
            code=code,
            verifier=str(pending.get("verifier", "")),
        )
        request.session.pop("oidc_pending", None)
        id_token_claims: dict[str, Any] | None = None
        id_token = str(token_response.get("id_token", "")).strip()
        if id_token:
            id_token_claims = _validate_product_oidc_id_token(
                id_token,
                settings,
                metadata,
                nonce=str(pending.get("nonce", "")),
            )
        access_token = str(token_response.get("access_token", "")).strip()
        if access_token and metadata.userinfo_endpoint:
            claims = _fetch_product_oidc_userinfo(access_token, metadata)
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
    def auth_session(request: Request) -> object:
        try:
            return _session_response_payload(request)
        except HTTPException:
            return ProductSessionResponse(authenticated=False, csrf_token=_csrf_token(request))

    @app.post("/api/auth/invite/claim", response_model=ProductSessionResponse)
    def auth_claim_invite(request: Request) -> object:
        _require_csrf(request)
        invite_token = _pending_invite_token(request)
        identity = _pending_invite_identity(request)
        if not invite_token or identity is None:
            raise HTTPException(status_code=400, detail="No invite claim is pending")
        try:
            provider_user = claim_product_user_from_invite(
                token=invite_token,
                tailscale_subject=identity["sub"],
                tailscale_login=identity["login"],
                display_name=identity.get("name"),
            )
        except ValueError as exc:
            _set_notice(request, str(exc), tailscale_login=identity.get("login"))
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        _set_pending_invite_token(request, None)
        _set_pending_invite_identity(request, None)
        _clear_notice(request)
        _store_session_user(request, _provider_user_session_payload(provider_user))
        _csrf_token(request)
        return _session_response_payload(request)

    @app.post("/api/auth/logout", response_model=ProductSessionResponse)
    def auth_logout(request: Request) -> object:
        _require_csrf(request)
        request.session.clear()
        _csrf_token(request)
        return ProductSessionResponse(
            authenticated=False,
            csrf_token=request.session.get("csrf_token"),
        )
