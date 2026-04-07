from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hermes_cli.product_app_support import ProductHealthResponse, _set_pending_bootstrap_token, _set_pending_invite_token
from hermes_cli.product_web import build_product_index_html


def register_root_routes(app: FastAPI, context: object) -> None:
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(
            build_product_index_html(
                product_name=context.product_name,
                account_url=context.app_base_url,
            )
        )

    @app.get("/healthz", response_model=ProductHealthResponse)
    def healthz() -> object:
        return ProductHealthResponse()

    @app.get("/invite/{token}")
    def invite_login(request: Request, token: str) -> RedirectResponse:
        _set_pending_invite_token(request, token)
        return RedirectResponse("/api/auth/login", status_code=303)

    @app.get("/bootstrap/{token}")
    def bootstrap_login(request: Request, token: str) -> RedirectResponse:
        _set_pending_bootstrap_token(request, token)
        return RedirectResponse("/api/auth/login", status_code=303)
