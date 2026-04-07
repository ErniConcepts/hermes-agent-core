from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from hermes_cli.product_app_support import (
    ProductChatSessionResponse,
    ProductChatTurnRequest,
    _require_csrf,
    _require_product_user,
    _runtime_session_payload,
    stream_product_chat_turn,
    stop_product_chat_turn,
)


def register_chat_routes(app: FastAPI) -> None:
    @app.get("/api/chat/session", response_model=ProductChatSessionResponse)
    def chat_session(request: Request) -> object:
        user = _require_product_user(request)
        return ProductChatSessionResponse(**_runtime_session_payload(user))

    @app.post("/api/chat/turn/stream")
    def chat_turn_stream(request: Request, payload: dict[str, object] = Body(...)) -> StreamingResponse:
        user = _require_product_user(request)
        _require_csrf(request)
        validated = ProductChatTurnRequest.model_validate(payload)
        try:
            event_stream = stream_product_chat_turn(user, validated.user_message)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return StreamingResponse(
            event_stream,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/chat/turn/stop")
    def chat_turn_stop(request: Request) -> dict[str, bool]:
        user = _require_product_user(request)
        _require_csrf(request)
        return {"stopped": bool(stop_product_chat_turn(user))}
