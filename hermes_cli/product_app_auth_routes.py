from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from hermes_cli.product_app_services import AuthRouteServices


def register_auth_routes(app: FastAPI, context: object, services: AuthRouteServices) -> None:
    @app.get("/api/auth/login")
    def auth_login(request: Request) -> RedirectResponse:
        services.enforce_auth_rate_limit(request, "login")
        services.csrf_token(request)
        services.clear_notice(request)
        services.set_pending_invite_identity(request, None)
        existing = request.session.get("user")
        if isinstance(existing, dict) and not services.pending_invite_token(request) and not services.pending_bootstrap_token(request):
            try:
                refreshed = services.resolve_session_user(request)
            except HTTPException:
                refreshed = None
            if refreshed is not None:
                services.mark_bootstrap_completed_if_admin(refreshed)
                return RedirectResponse(services.current_app_base_url(), status_code=303)
            request.session.clear()
        return services.start_tsidp_login(request, context)

    @app.get("/api/auth/oidc/callback")
    def auth_callback(request: Request, code: str, state: str) -> RedirectResponse:
        services.enforce_auth_rate_limit(request, "callback")
        pending = request.session.get("oidc_pending")
        if not isinstance(pending, dict):
            return RedirectResponse(services.current_app_base_url(), status_code=303)
        if state != pending.get("state"):
            request.session.pop("oidc_pending", None)
            services.set_notice(request, "The Tailscale login state was invalid. Please try again.")
            return RedirectResponse(services.current_app_base_url(), status_code=303)

        settings = services.load_product_oidc_client_settings(config=context.product_config)
        metadata = services.discover_product_oidc_provider_metadata(settings)
        token_response = services.exchange_product_oidc_code(
            settings,
            metadata,
            code=code,
            verifier=str(pending.get("verifier", "")),
        )
        request.session.pop("oidc_pending", None)
        id_token_claims: dict[str, Any] | None = None
        id_token = str(token_response.get("id_token", "")).strip()
        if id_token:
            id_token_claims = services.validate_product_oidc_id_token(
                id_token,
                settings,
                metadata,
                nonce=str(pending.get("nonce", "")),
            )
        access_token = str(token_response.get("access_token", "")).strip()
        if access_token and metadata.userinfo_endpoint:
            claims = services.fetch_product_oidc_userinfo(access_token, metadata)
        elif isinstance(id_token_claims, dict):
            claims = id_token_claims
        else:
            services.set_notice(request, "The Tailscale login response did not include identity claims.")
            return RedirectResponse(services.current_app_base_url(), status_code=303)

        identity = services.tailscale_identity_from_claims(claims)
        if not identity.get("sub") or not identity.get("login"):
            services.set_notice(request, "The Tailscale login response did not include a stable account identity.")
            return RedirectResponse(services.current_app_base_url(), status_code=303)

        try:
            provider_user = services.handle_tsidp_identity(request, identity)
        except HTTPException:
            request.session.pop("user", None)
            request.session.pop("user_refreshed_at", None)
            return RedirectResponse(services.current_app_base_url(), status_code=303)

        services.clear_notice(request)
        services.store_session_user(request, services.provider_user_session_payload(provider_user))
        services.mark_bootstrap_completed_if_admin(request.session["user"])
        services.csrf_token(request)
        return RedirectResponse(services.current_app_base_url(), status_code=303)

    @app.get("/api/auth/session", response_model=services.product_session_response_model)
    def auth_session(request: Request) -> object:
        try:
            return services.session_response_payload(request)
        except HTTPException:
            return services.product_session_response_model(authenticated=False, csrf_token=services.csrf_token(request))

    @app.post("/api/auth/invite/claim", response_model=services.product_session_response_model)
    def auth_claim_invite(request: Request) -> object:
        services.require_csrf(request)
        invite_token = services.pending_invite_token(request)
        identity = services.pending_invite_identity(request)
        if not invite_token or identity is None:
            raise HTTPException(status_code=400, detail="No invite claim is pending")
        try:
            provider_user = services.claim_product_user_from_invite(
                token=invite_token,
                tailscale_subject=identity["sub"],
                tailscale_login=identity["login"],
                display_name=identity.get("name"),
            )
        except ValueError as exc:
            services.set_notice(request, str(exc), tailscale_login=identity.get("login"))
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        services.set_pending_invite_token(request, None)
        services.set_pending_invite_identity(request, None)
        services.clear_notice(request)
        services.store_session_user(request, services.provider_user_session_payload(provider_user))
        services.csrf_token(request)
        return services.session_response_payload(request)

    @app.post("/api/auth/logout", response_model=services.product_session_response_model)
    def auth_logout(request: Request) -> object:
        services.require_csrf(request)
        request.session.clear()
        services.csrf_token(request)
        return services.product_session_response_model(
            authenticated=False,
            csrf_token=request.session.get("csrf_token"),
        )
