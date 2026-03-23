from __future__ import annotations

import json
import os
import queue
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi import Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
import uvicorn

from hermes_state import SessionDB
from hermes_cli.product_runtime import _RUNTIME_WORKSPACE_PATH


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise RuntimeError(f"{name} must be configured for the product runtime")


def _runtime_toolsets() -> list[str]:
    raw_toolsets = _required_env("HERMES_PRODUCT_TOOLSETS")
    normalized = [item.strip() for item in raw_toolsets.split(",") if item.strip()]
    if not normalized:
        raise RuntimeError("HERMES_PRODUCT_TOOLSETS must contain at least one toolset")
    return normalized


class RuntimeTurnRequest(BaseModel):
    user_message: str

    @field_validator("user_message")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("user_message must not be empty")
        return stripped


class RuntimeMessage(BaseModel):
    role: str
    content: str


class RuntimeSessionResponse(BaseModel):
    session_id: str
    messages: list[RuntimeMessage]
    runtime_mode: str
    runtime_toolsets: list[str]


class RuntimeTurnResponse(RuntimeSessionResponse):
    final_response: str


class RuntimeHealthResponse(BaseModel):
    status: str = "ok"
    runtime_mode: str
    runtime_toolsets: list[str]
    hermes_home: str
    model: str
    session_id: str


def _encode_sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _load_runtime_soul() -> str:
    hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    soul_path = hermes_home / "SOUL.md"
    if soul_path.exists():
        soul = soul_path.read_text(encoding="utf-8").strip()
        if soul:
            return soul
    raise RuntimeError(f"Product runtime requires a non-empty SOUL.md at {soul_path}")


def _session_id() -> str:
    return _required_env("HERMES_PRODUCT_SESSION_ID")


def _runtime_token() -> str:
    return _required_env("HERMES_PRODUCT_RUNTIME_TOKEN")


def _require_runtime_token(header_value: str | None, expected: str) -> None:
    if header_value != expected:
        raise HTTPException(status_code=401, detail="Unauthorized runtime request")


def _classify_runtime_error(exc: Exception) -> tuple[int, str]:
    detail = str(exc or "").strip()
    normalized = detail.lower()
    if (
        "apiconnectionerror" in normalized
        or "connection error" in normalized
        or "max retries" in normalized
        or "failed to establish a new connection" in normalized
        or "connection refused" in normalized
    ):
        endpoint = _required_env("OPENAI_BASE_URL")
        model = _required_env("HERMES_PRODUCT_MODEL")
        return (
            503,
            f"Model not available. Check that '{model}' is reachable at {endpoint}.",
        )
    return (500, "Runtime request failed")


def _visible_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    visible: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", "")).strip()
        if role not in {"user", "assistant", "system"}:
            continue
        content = str(message.get("content") or "")
        if role != "user" and not content.strip():
            continue
        visible.append({"role": role, "content": content})
    return visible


def _conversation_for_agent(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed_roles = {"user", "assistant", "system", "tool"}
    conversation: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "")).strip()
        if role not in allowed_roles:
            continue
        entry: dict[str, Any] = {"role": role, "content": message.get("content")}
        if message.get("tool_call_id"):
            entry["tool_call_id"] = message["tool_call_id"]
        if message.get("tool_name"):
            entry["tool_name"] = message["tool_name"]
        if isinstance(message.get("tool_calls"), list):
            entry["tool_calls"] = message["tool_calls"]
        conversation.append(entry)
    return conversation


def _load_session_messages(db: SessionDB, session_id: str) -> list[dict[str, Any]]:
    session = db.get_session(session_id)
    if not session:
        return []
    return db.get_messages_as_conversation(session_id)


def build_runtime_agent(db: SessionDB, session_id: str, *, reasoning_callback: Any = None):
    from run_agent import AIAgent
    from tools.terminal_tool import register_task_env_overrides

    provider = _required_env("HERMES_PRODUCT_PROVIDER").lower()
    api_mode = _required_env("HERMES_PRODUCT_API_MODE").lower()
    model = _required_env("HERMES_PRODUCT_MODEL")
    base_url = _required_env("OPENAI_BASE_URL")
    api_key = _required_env("OPENAI_API_KEY")

    # Scope local file/terminal-backed tools to the mounted per-user workspace.
    register_task_env_overrides(session_id, {"cwd": _RUNTIME_WORKSPACE_PATH})

    return AIAgent(
        base_url=base_url,
        api_key=api_key,
        provider=provider,
        api_mode=api_mode,
        model=model,
        quiet_mode=True,
        enabled_toolsets=_runtime_toolsets(),
        session_id=session_id,
        session_db=db,
        platform="product-runtime",
        reasoning_callback=reasoning_callback,
        skip_context_files=False,
        save_trajectories=False,
        verbose_logging=False,
    )


def create_product_runtime_app() -> FastAPI:
    runtime_mode = _required_env("HERMES_PRODUCT_RUNTIME_MODE")
    runtime_toolsets = _runtime_toolsets()
    hermes_home = _required_env("HERMES_HOME")
    model = _required_env("HERMES_PRODUCT_MODEL")
    session_id = _session_id()
    runtime_token = _runtime_token()
    _load_runtime_soul()

    app = FastAPI(title="Hermes Core Product Runtime", version="0.1.0")

    @app.get("/healthz", response_model=RuntimeHealthResponse)
    def healthz() -> RuntimeHealthResponse:
        return RuntimeHealthResponse(
            runtime_mode=runtime_mode,
            runtime_toolsets=runtime_toolsets,
            hermes_home=hermes_home,
            model=model,
            session_id=session_id,
        )

    @app.get("/runtime/session", response_model=RuntimeSessionResponse)
    def runtime_session(x_hermes_product_runtime_token: str | None = Header(default=None)) -> RuntimeSessionResponse:
        _require_runtime_token(x_hermes_product_runtime_token, runtime_token)
        db = SessionDB()
        try:
            messages = _load_session_messages(db, session_id)
        finally:
            db.close()
        return RuntimeSessionResponse(
            session_id=session_id,
            messages=[RuntimeMessage(**message) for message in _visible_messages(messages)],
            runtime_mode=runtime_mode,
            runtime_toolsets=runtime_toolsets,
        )

    @app.post("/runtime/turn", response_model=RuntimeTurnResponse)
    def runtime_turn(
        request: RuntimeTurnRequest,
        x_hermes_product_runtime_token: str | None = Header(default=None),
    ) -> RuntimeTurnResponse:
        _require_runtime_token(x_hermes_product_runtime_token, runtime_token)
        try:
            db = SessionDB()
            agent = build_runtime_agent(db, session_id)
            history = _load_session_messages(db, session_id)
            result = agent.run_conversation(
                request.user_message,
                conversation_history=_conversation_for_agent(history),
                sync_honcho=False,
            )
            updated_messages = _load_session_messages(db, session_id)
        except Exception as exc:
            status_code, detail = _classify_runtime_error(exc)
            raise HTTPException(status_code=status_code, detail=detail) from exc
        finally:
            if "db" in locals():
                db.close()
        final_response = str(result.get("final_response") or result.get("response") or "")
        return RuntimeTurnResponse(
            final_response=final_response,
            session_id=session_id,
            messages=[RuntimeMessage(**message) for message in _visible_messages(updated_messages)],
            runtime_mode=runtime_mode,
            runtime_toolsets=runtime_toolsets,
        )

    @app.post("/runtime/turn/stream")
    def runtime_turn_stream(
        request: RuntimeTurnRequest,
        x_hermes_product_runtime_token: str | None = Header(default=None),
    ) -> StreamingResponse:
        _require_runtime_token(x_hermes_product_runtime_token, runtime_token)
        event_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()

        def _run() -> None:
            try:
                db = SessionDB()
                agent = build_runtime_agent(
                    db,
                    session_id,
                    reasoning_callback=lambda text: event_queue.put(("reasoning", {"delta": str(text or "")})),
                )
                setattr(
                    agent,
                    "reasoning_callback",
                    lambda text: event_queue.put(("reasoning", {"delta": str(text or "")})),
                )
                history = _load_session_messages(db, session_id)
                event_queue.put(("start", {"session_id": session_id}))
                result = agent.run_conversation(
                    request.user_message,
                    conversation_history=_conversation_for_agent(history),
                    stream_callback=lambda text: event_queue.put(("answer", {"delta": str(text or "")})),
                    sync_honcho=False,
                )
                updated_messages = _load_session_messages(db, session_id)
                event_queue.put(
                    (
                        "final",
                        RuntimeTurnResponse(
                            final_response=str(result.get("final_response") or result.get("response") or ""),
                            session_id=session_id,
                            messages=[RuntimeMessage(**message) for message in _visible_messages(updated_messages)],
                            runtime_mode=runtime_mode,
                            runtime_toolsets=runtime_toolsets,
                        ).model_dump(mode="json"),
                    )
                )
            except Exception as exc:
                _, detail = _classify_runtime_error(exc)
                event_queue.put(("error", {"detail": detail}))
            finally:
                if "db" in locals():
                    db.close()
                event_queue.put(("done", {}))

        def _stream() -> Iterator[bytes]:
            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            while True:
                event, payload = event_queue.get()
                if event == "done":
                    break
                yield _encode_sse(event, payload)

        return StreamingResponse(_stream(), media_type="text/event-stream")

    return app


if os.getenv("HERMES_PRODUCT_SESSION_ID"):
    app = create_product_runtime_app()
else:  # pragma: no cover
    app = FastAPI(title="Hermes Core Product Runtime", version="0.1.0")


def main() -> int:
    host = _required_env("HERMES_RUNTIME_HOST")
    port = int(_required_env("HERMES_RUNTIME_PORT"))
    uvicorn.run(create_product_runtime_app(), host=host, port=port, reload=False)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
