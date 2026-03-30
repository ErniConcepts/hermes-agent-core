from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse


def register_chat_routes(app: FastAPI, hooks: Any) -> None:
    @app.get("/api/chat/session", response_model=hooks.ProductChatSessionResponse)
    def chat_session(request: Request) -> Any:
        user = hooks._require_product_user(request)
        return hooks.ProductChatSessionResponse(**hooks._runtime_session_payload(user))

    @app.post("/api/chat/turn/stream")
    def chat_turn_stream(request: Request, payload: dict[str, Any] = Body(...)) -> StreamingResponse:
        user = hooks._require_product_user(request)
        hooks._require_csrf(request)
        validated = hooks.ProductChatTurnRequest.model_validate(payload)
        try:
            event_stream = hooks.stream_product_runtime_turn(user, validated.user_message)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return StreamingResponse(
            event_stream,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
