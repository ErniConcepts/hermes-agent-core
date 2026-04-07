from __future__ import annotations

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from hermes_cli.product_app_support import (
    ProductCreateWorkspaceFolderRequest,
    ProductDeleteWorkspacePathRequest,
    ProductMoveWorkspacePathRequest,
    ProductWorkspaceQuotaError,
    ProductWorkspaceResponse,
    _require_csrf,
    _require_product_user,
    _workspace_response_payload,
    create_workspace_folder,
    delete_workspace_path,
    get_workspace_state,
    move_workspace_path,
    resolve_workspace_file,
    store_workspace_file,
)


def register_workspace_routes(app: FastAPI) -> None:
    @app.get("/api/workspace", response_model=ProductWorkspaceResponse)
    def workspace_state(request: Request, path: str = "") -> object:
        user = _require_product_user(request)
        try:
            payload = get_workspace_state(user, path=path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _workspace_response_payload(payload)

    @app.get("/api/workspace/download")
    def workspace_download(request: Request, path: str) -> FileResponse:
        user = _require_product_user(request)
        try:
            target, _normalized = resolve_workspace_file(user, path=path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")

    @app.post("/api/workspace/folders", response_model=ProductWorkspaceResponse)
    def workspace_create_folder(request: Request, payload: dict[str, object] = Body(...)) -> object:
        user = _require_product_user(request)
        _require_csrf(request)
        validated = ProductCreateWorkspaceFolderRequest.model_validate(payload)
        try:
            state = create_workspace_folder(user, parent_path=validated.path, folder_name=validated.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _workspace_response_payload(state)

    @app.post("/api/workspace/files", response_model=ProductWorkspaceResponse)
    async def workspace_upload_files(
        request: Request,
        path: str = Form(default=""),
        files: list[UploadFile] = File(...),
    ) -> object:
        user = _require_product_user(request)
        _require_csrf(request)
        current_state = None
        try:
            for upload in files:
                content = await upload.read()
                current_state = store_workspace_file(
                    user,
                    parent_path=path,
                    filename=upload.filename or "",
                    content=content,
                )
        except ProductWorkspaceQuotaError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            for upload in files:
                await upload.close()
        if current_state is None:
            raise HTTPException(status_code=400, detail="At least one file is required")
        return _workspace_response_payload(current_state)

    @app.post("/api/workspace/delete", response_model=ProductWorkspaceResponse)
    def workspace_delete(request: Request, payload: dict[str, object] = Body(...)) -> object:
        user = _require_product_user(request)
        _require_csrf(request)
        validated = ProductDeleteWorkspacePathRequest.model_validate(payload)
        try:
            state = delete_workspace_path(user, path=validated.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _workspace_response_payload(state)

    @app.post("/api/workspace/move", response_model=ProductWorkspaceResponse)
    def workspace_move(request: Request, payload: dict[str, object] = Body(...)) -> object:
        user = _require_product_user(request)
        _require_csrf(request)
        validated = ProductMoveWorkspacePathRequest.model_validate(payload)
        try:
            state = move_workspace_path(
                user,
                source_path=validated.source_path,
                destination_parent_path=validated.destination_parent_path,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _workspace_response_payload(state)
