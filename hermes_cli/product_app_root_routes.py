from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse


def register_root_routes(app: FastAPI, context: Any, hooks: Any) -> None:
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(
            hooks.build_product_index_html(
                product_name=context.product_name,
                account_url=context.app_base_url,
            )
        )

    @app.get("/healthz", response_model=hooks.ProductHealthResponse)
    def healthz() -> Any:
        return hooks.ProductHealthResponse(
            auth_provider=context.auth_provider,
            issuer_url=hooks._current_product_urls()["issuer_url"],
            app_base_url=hooks._current_app_base_url(),
        )

    @app.get("/invite/{token}")
    def invite_login(request: Request, token: str) -> RedirectResponse:
        hooks._set_pending_invite_token(request, token)
        return RedirectResponse("/api/auth/login", status_code=303)

    @app.get("/bootstrap/{token}")
    def bootstrap_login(request: Request, token: str) -> RedirectResponse:
        hooks._set_pending_bootstrap_token(request, token)
        return RedirectResponse("/api/auth/login", status_code=303)
