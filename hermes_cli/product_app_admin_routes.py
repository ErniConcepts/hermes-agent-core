from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException, Request

from hermes_cli.product_app_support import (
    ProductAdminUsersResponse,
    ProductCreateUserRequest,
    _create_invited_user,
    _deactivate_product_user,
    _list_admin_entries,
    _require_admin_user,
    _require_csrf,
)
from hermes_cli.product_users import ProductCreatedUser, ProductUser


def register_admin_routes(app: FastAPI) -> None:
    @app.get("/api/admin/users", response_model=ProductAdminUsersResponse)
    def admin_list_users(request: Request) -> object:
        # Read-only GET: same-origin policy protects the JSON body, so we keep this
        # route session-authenticated but do not require an extra CSRF token.
        admin_user = _require_admin_user(request)
        return _list_admin_entries(admin_user)

    @app.post("/api/admin/users", response_model=ProductCreatedUser)
    def admin_create_user(request: Request, payload: dict[str, object] = Body(...)) -> object:
        _require_admin_user(request)
        _require_csrf(request)
        validated = ProductCreateUserRequest.model_validate(payload)
        return _create_invited_user(validated)

    @app.post("/api/admin/users/{user_id}/deactivate", response_model=ProductUser)
    def admin_deactivate_user(request: Request, user_id: str) -> object:
        admin_user = _require_admin_user(request)
        _require_csrf(request)
        if user_id == str(admin_user.get("sub") or ""):
            raise HTTPException(status_code=400, detail="Admins cannot deactivate their own account")
        return _deactivate_product_user(user_id)
