"""Minimal authenticated product app surface for hermes-core."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from hermes_cli.product_config import load_product_config
from hermes_cli.product_oidc import (
    create_oidc_login_request,
    discover_product_oidc_provider_metadata,
    exchange_product_oidc_code,
    fetch_product_oidc_userinfo,
    load_product_oidc_client_settings,
)
from hermes_cli.product_stack import resolve_product_urls


class ProductHealthResponse(BaseModel):
    status: str = "ok"
    auth_provider: str
    issuer_url: str
    app_base_url: str


class ProductSessionResponse(BaseModel):
    authenticated: bool
    user: dict[str, Any] | None = None


def _session_secret() -> str:
    settings = load_product_oidc_client_settings()
    digest = hashlib.sha256(settings.client_secret.encode("utf-8")).hexdigest()
    return f"hermes-product-session-{digest}"


def _session_user_payload(userinfo: dict[str, Any]) -> dict[str, Any]:
    return {
        "sub": userinfo.get("sub", ""),
        "email": userinfo.get("email"),
        "name": userinfo.get("name") or userinfo.get("preferred_username") or userinfo.get("sub", ""),
        "preferred_username": userinfo.get("preferred_username"),
        "email_verified": userinfo.get("email_verified"),
    }


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
        https_only=False,
    )

    @app.get("/healthz", response_model=ProductHealthResponse)
    def healthz() -> ProductHealthResponse:
        return ProductHealthResponse(
            auth_provider=auth_provider,
            issuer_url=issuer_url,
            app_base_url=urls["app_base_url"],
        )

    @app.get("/api/auth/login")
    def auth_login(request: Request) -> RedirectResponse:
        settings = load_product_oidc_client_settings()
        metadata = discover_product_oidc_provider_metadata(settings)
        login_request = create_oidc_login_request(settings, metadata)
        request.session["oidc_pending"] = {
            "state": login_request["state"],
            "nonce": login_request["nonce"],
            "verifier": login_request["verifier"],
        }
        return RedirectResponse(login_request["authorization_url"], status_code=307)

    @app.get("/api/auth/callback")
    def auth_callback(request: Request, code: str, state: str) -> RedirectResponse:
        pending = request.session.get("oidc_pending")
        if not isinstance(pending, dict):
            raise HTTPException(status_code=400, detail="Missing OIDC login state")
        if state != pending.get("state"):
            raise HTTPException(status_code=400, detail="OIDC state mismatch")

        settings = load_product_oidc_client_settings()
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
        userinfo = fetch_product_oidc_userinfo(access_token, metadata)
        request.session.pop("oidc_pending", None)
        request.session["user"] = _session_user_payload(userinfo)
        return RedirectResponse(urls["app_base_url"], status_code=303)

    @app.get("/api/auth/session", response_model=ProductSessionResponse)
    def auth_session(request: Request) -> ProductSessionResponse:
        user = request.session.get("user")
        if not isinstance(user, dict):
            return ProductSessionResponse(authenticated=False)
        return ProductSessionResponse(authenticated=True, user=user)

    @app.post("/api/auth/logout", response_model=ProductSessionResponse)
    def auth_logout(request: Request) -> ProductSessionResponse:
        request.session.clear()
        return ProductSessionResponse(authenticated=False)

    return app
