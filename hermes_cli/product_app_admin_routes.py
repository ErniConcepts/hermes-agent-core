from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException, Request

from hermes_cli.product_app_services import AdminRouteServices


def register_admin_routes(app: FastAPI, services: AdminRouteServices) -> None:
    @app.get("/api/admin/users", response_model=services.product_admin_users_response_model)
    def admin_list_users(request: Request) -> object:
        # Read-only GET: same-origin policy protects the JSON body, so we keep this
        # route session-authenticated but do not require an extra CSRF token.
        admin_user = services.require_admin_user(request)
        return services.list_admin_entries(admin_user)

    @app.post("/api/admin/users", response_model=services.product_created_user_model)
    def admin_create_user(request: Request, payload: dict[str, object] = Body(...)) -> object:
        services.require_admin_user(request)
        services.require_csrf(request)
        validated = services.product_create_user_request_model.model_validate(payload)
        return services.create_invited_user(validated)

    @app.post("/api/admin/users/{user_id}/deactivate", response_model=services.product_user_model)
    def admin_deactivate_user(request: Request, user_id: str) -> object:
        admin_user = services.require_admin_user(request)
        services.require_csrf(request)
        if user_id == str(admin_user.get("sub") or ""):
            raise HTTPException(status_code=400, detail="Admins cannot deactivate their own account")
        return services.deactivate_product_user(user_id)
