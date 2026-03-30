from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request


def register_admin_routes(app: FastAPI, hooks: Any) -> None:
    @app.get("/api/admin/users", response_model=hooks.ProductAdminUsersResponse)
    def admin_list_users(request: Request) -> Any:
        admin_user = hooks._require_admin_user(request)
        return hooks._list_admin_entries(admin_user)

    @app.post("/api/admin/users", response_model=hooks.ProductCreatedUser)
    def admin_create_user(request: Request, payload: dict[str, Any] = Body(...)) -> Any:
        hooks._require_admin_user(request)
        hooks._require_csrf(request)
        validated = hooks.ProductCreateUserRequest.model_validate(payload)
        return hooks._create_signup_user(validated)

    @app.post("/api/admin/users/{user_id}/deactivate", response_model=hooks.ProductUser)
    def admin_deactivate_user(request: Request, user_id: str) -> Any:
        admin_user = hooks._require_admin_user(request)
        hooks._require_csrf(request)
        if user_id == str(admin_user.get("sub") or ""):
            raise HTTPException(status_code=400, detail="Admins cannot deactivate their own account")
        return hooks._deactivate_runtime_user(user_id)
