from __future__ import annotations

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from hermes_cli.product_app_services import WorkspaceRouteServices


def register_workspace_routes(app: FastAPI, services: WorkspaceRouteServices) -> None:
    @app.get("/api/workspace", response_model=services.product_workspace_response_model)
    def workspace_state(request: Request, path: str = "") -> object:
        user = services.require_product_user(request)
        try:
            payload = services.get_workspace_state(user, path=path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return services.workspace_response_payload(payload)

    @app.get("/api/workspace/download")
    def workspace_download(request: Request, path: str) -> FileResponse:
        user = services.require_product_user(request)
        try:
            target, _normalized = services.resolve_workspace_file(user, path=path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")

    @app.post("/api/workspace/folders", response_model=services.product_workspace_response_model)
    def workspace_create_folder(request: Request, payload: dict[str, object] = Body(...)) -> object:
        user = services.require_product_user(request)
        services.require_csrf(request)
        validated = services.product_create_workspace_folder_request_model.model_validate(payload)
        try:
            state = services.create_workspace_folder(user, parent_path=validated.path, folder_name=validated.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return services.workspace_response_payload(state)

    @app.post("/api/workspace/files", response_model=services.product_workspace_response_model)
    async def workspace_upload_files(
        request: Request,
        path: str = Form(default=""),
        files: list[UploadFile] = File(...),
    ) -> object:
        user = services.require_product_user(request)
        services.require_csrf(request)
        current_state = None
        try:
            for upload in files:
                content = await upload.read()
                current_state = services.store_workspace_file(
                    user,
                    parent_path=path,
                    filename=upload.filename or "",
                    content=content,
                )
        except services.product_workspace_quota_error as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            for upload in files:
                await upload.close()
        if current_state is None:
            raise HTTPException(status_code=400, detail="At least one file is required")
        return services.workspace_response_payload(current_state)

    @app.post("/api/workspace/delete", response_model=services.product_workspace_response_model)
    def workspace_delete(request: Request, payload: dict[str, object] = Body(...)) -> object:
        user = services.require_product_user(request)
        services.require_csrf(request)
        validated = services.product_delete_workspace_path_request_model.model_validate(payload)
        try:
            state = services.delete_workspace_path(user, path=validated.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return services.workspace_response_payload(state)
