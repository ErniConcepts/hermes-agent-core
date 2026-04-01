from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hermes_cli.product_app_services import RootRouteServices


def register_root_routes(app: FastAPI, context: object, services: RootRouteServices) -> None:
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(
            services.build_product_index_html(
                product_name=context.product_name,
                account_url=context.app_base_url,
            )
        )

    @app.get("/healthz", response_model=services.product_health_response_model)
    def healthz() -> object:
        return services.product_health_response_model()

    @app.get("/invite/{token}")
    def invite_login(request: Request, token: str) -> RedirectResponse:
        services.set_pending_invite_token(request, token)
        return RedirectResponse("/api/auth/login", status_code=303)

    @app.get("/bootstrap/{token}")
    def bootstrap_login(request: Request, token: str) -> RedirectResponse:
        services.set_pending_bootstrap_token(request, token)
        return RedirectResponse("/api/auth/login", status_code=303)
