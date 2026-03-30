from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse


def register_auth_routes(app: FastAPI, context: Any, hooks: Any) -> None:
    @app.get("/api/auth/login")
    def auth_login(request: Request) -> RedirectResponse:
        hooks._enforce_auth_rate_limit(request, "login")
        hooks._csrf_token(request)
        hooks._clear_notice(request)
        hooks._set_pending_invite_identity(request, None)
        existing = request.session.get("user")
        if isinstance(existing, dict) and not hooks._pending_invite_token(request) and not hooks._pending_bootstrap_token(request):
            try:
                refreshed = hooks._resolve_session_user(request)
            except HTTPException:
                refreshed = None
            if refreshed is not None:
                hooks._mark_bootstrap_completed_if_admin(refreshed)
                return RedirectResponse(hooks._current_app_base_url(), status_code=303)
            request.session.clear()
        return hooks._start_tsidp_login(request, context)

    @app.get("/api/auth/oidc/callback")
    def auth_callback(request: Request, code: str, state: str) -> RedirectResponse:
        hooks._enforce_auth_rate_limit(request, "callback")
        pending = request.session.get("oidc_pending")
        if not isinstance(pending, dict):
            return RedirectResponse(hooks._current_app_base_url(), status_code=303)
        if state != pending.get("state"):
            request.session.pop("oidc_pending", None)
            hooks._set_notice(request, "The Tailscale login state was invalid. Please try again.")
            return RedirectResponse(hooks._current_app_base_url(), status_code=303)

        settings = hooks.load_product_oidc_client_settings(config=context.product_config)
        metadata = hooks.discover_product_oidc_provider_metadata(settings)
        token_response = hooks.exchange_product_oidc_code(
            settings,
            metadata,
            code=code,
            verifier=str(pending.get("verifier", "")),
        )
        request.session.pop("oidc_pending", None)
        id_token_claims: dict[str, Any] | None = None
        id_token = str(token_response.get("id_token", "")).strip()
        if id_token:
            id_token_claims = hooks.validate_product_oidc_id_token(
                id_token,
                settings,
                metadata,
                nonce=str(pending.get("nonce", "")),
            )
        access_token = str(token_response.get("access_token", "")).strip()
        if access_token and metadata.userinfo_endpoint:
            claims = hooks.fetch_product_oidc_userinfo(access_token, metadata)
        elif isinstance(id_token_claims, dict):
            claims = id_token_claims
        else:
            hooks._set_notice(request, "The Tailscale login response did not include identity claims.")
            return RedirectResponse(hooks._current_app_base_url(), status_code=303)

        identity = hooks._tailscale_identity_from_claims(claims)
        if not identity.get("sub") or not identity.get("login"):
            hooks._set_notice(request, "The Tailscale login response did not include a stable account identity.")
            return RedirectResponse(hooks._current_app_base_url(), status_code=303)

        try:
            provider_user = hooks._handle_tsidp_identity(request, identity)
        except HTTPException:
            request.session.pop("user", None)
            request.session.pop("user_refreshed_at", None)
            return RedirectResponse(hooks._current_app_base_url(), status_code=303)

        hooks._clear_notice(request)
        hooks._store_session_user(request, hooks._provider_user_session_payload(provider_user))
        hooks._mark_bootstrap_completed_if_admin(request.session["user"])
        hooks._csrf_token(request)
        return RedirectResponse(hooks._current_app_base_url(), status_code=303)

    @app.get("/api/auth/session", response_model=hooks.ProductSessionResponse)
    def auth_session(request: Request) -> Any:
        try:
            return hooks._session_response_payload(request)
        except HTTPException:
            return hooks.ProductSessionResponse(authenticated=False, csrf_token=hooks._csrf_token(request))

    @app.post("/api/auth/invite/claim", response_model=hooks.ProductSessionResponse)
    def auth_claim_invite(request: Request) -> Any:
        hooks._require_csrf(request)
        invite_token = hooks._pending_invite_token(request)
        identity = hooks._pending_invite_identity(request)
        if not invite_token or identity is None:
            raise HTTPException(status_code=400, detail="No invite claim is pending")
        try:
            provider_user = hooks.claim_product_user_from_invite(
                token=invite_token,
                tailscale_subject=identity["sub"],
                tailscale_login=identity["login"],
                display_name=identity.get("name"),
            )
        except ValueError as exc:
            hooks._set_notice(request, str(exc), tailscale_login=identity.get("login"))
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        hooks._set_pending_invite_token(request, None)
        hooks._set_pending_invite_identity(request, None)
        hooks._clear_notice(request)
        hooks._store_session_user(request, hooks._provider_user_session_payload(provider_user))
        hooks._csrf_token(request)
        return hooks._session_response_payload(request)

    @app.post("/api/auth/logout", response_model=hooks.ProductSessionResponse)
    def auth_logout(request: Request) -> Any:
        hooks._require_csrf(request)
        request.session.clear()
        hooks._csrf_token(request)
        return hooks.ProductSessionResponse(
            authenticated=False,
            csrf_token=request.session.get("csrf_token"),
        )
