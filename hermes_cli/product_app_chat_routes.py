from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from hermes_cli.product_app_services import ChatRouteServices


def register_chat_routes(app: FastAPI, services: ChatRouteServices) -> None:
    @app.get("/api/chat/session", response_model=services.product_chat_session_response_model)
    def chat_session(request: Request) -> object:
        user = services.require_product_user(request)
        return services.product_chat_session_response_model(**services.runtime_session_payload(user))

    @app.post("/api/chat/turn/stream")
    def chat_turn_stream(request: Request, payload: dict[str, object] = Body(...)) -> StreamingResponse:
        user = services.require_product_user(request)
        services.require_csrf(request)
        validated = services.product_chat_turn_request_model.model_validate(payload)
        try:
            event_stream = services.stream_product_runtime_turn(user, validated.user_message)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return StreamingResponse(
            event_stream,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
