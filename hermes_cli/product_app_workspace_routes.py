from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse


def register_workspace_routes(app: FastAPI, hooks: Any) -> None:
    @app.get("/api/workspace", response_model=hooks.ProductWorkspaceResponse)
    def workspace_state(request: Request, path: str = "") -> Any:
        user = hooks._require_product_user(request)
        try:
            payload = hooks.get_workspace_state(user, path=path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return hooks._workspace_response_payload(payload)

    @app.get("/api/workspace/download")
    def workspace_download(request: Request, path: str) -> FileResponse:
        user = hooks._require_product_user(request)
        try:
            target, _normalized = hooks.resolve_workspace_file(user, path=path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")

    @app.post("/api/workspace/folders", response_model=hooks.ProductWorkspaceResponse)
    def workspace_create_folder(request: Request, payload: dict[str, Any] = Body(...)) -> Any:
        user = hooks._require_product_user(request)
        hooks._require_csrf(request)
        validated = hooks.ProductCreateWorkspaceFolderRequest.model_validate(payload)
        try:
            state = hooks.create_workspace_folder(user, parent_path=validated.path, folder_name=validated.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return hooks._workspace_response_payload(state)

    @app.post("/api/workspace/files", response_model=hooks.ProductWorkspaceResponse)
    async def workspace_upload_files(
        request: Request,
        path: str = Form(default=""),
        files: list[UploadFile] = File(...),
    ) -> Any:
        user = hooks._require_product_user(request)
        hooks._require_csrf(request)
        current_state = None
        try:
            for upload in files:
                content = await upload.read()
                current_state = hooks.store_workspace_file(
                    user,
                    parent_path=path,
                    filename=upload.filename or "",
                    content=content,
                )
        except hooks.ProductWorkspaceQuotaError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            for upload in files:
                await upload.close()
        if current_state is None:
            raise HTTPException(status_code=400, detail="At least one file is required")
        return hooks._workspace_response_payload(current_state)

    @app.post("/api/workspace/delete", response_model=hooks.ProductWorkspaceResponse)
    def workspace_delete(request: Request, payload: dict[str, Any] = Body(...)) -> Any:
        user = hooks._require_product_user(request)
        hooks._require_csrf(request)
        validated = hooks.ProductDeleteWorkspacePathRequest.model_validate(payload)
        try:
            state = hooks.delete_workspace_path(user, path=validated.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return hooks._workspace_response_payload(state)
