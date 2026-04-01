from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi import Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
import uvicorn

from agent.reasoning_stream import ReasoningStreamMux, strip_reasoning_blocks
from hermes_state import SessionDB
from hermes_cli.product_runtime import _RUNTIME_WORKSPACE_PATH
from session_reset import SessionResetPolicy, session_reset_reason

_ACTIVE_AGENT_LOCK = threading.Lock()
_ACTIVE_AGENTS: dict[str, Any] = {}
_ACTIVE_SESSION_LOCK = threading.Lock()


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


def _active_session_id_path() -> Path:
    return Path(_required_env("HERMES_HOME")) / ".product-runtime-session-id"


def _load_runtime_reset_policy() -> SessionResetPolicy:
    from hermes_cli.config import load_config

    config = load_config()
    return SessionResetPolicy.from_dict(config.get("session_reset") if isinstance(config, dict) else {})


def _read_active_session_id() -> str:
    path = _active_session_id_path()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    return _session_id()


def _write_active_session_id(session_id: str) -> None:
    _active_session_id_path().write_text(f"{session_id}\n", encoding="utf-8")


def _last_runtime_activity_ts(db: SessionDB, session_id: str, session_row: dict[str, Any]) -> float:
    messages = db.get_messages(session_id)
    if messages:
        message_ts = messages[-1].get("timestamp")
        if message_ts is not None:
            return float(message_ts)
    started_at = session_row.get("started_at")
    if started_at is not None:
        return float(started_at)
    return float(time.time())


def _runtime_reset_reason(db: SessionDB, session_id: str) -> str | None:
    policy = _load_runtime_reset_policy()
    session_row = db.get_session(session_id)
    if not session_row or session_row.get("ended_at") is not None:
        return None

    last_activity = datetime.fromtimestamp(_last_runtime_activity_ts(db, session_id, session_row))
    return session_reset_reason(last_activity=last_activity, policy=policy)


def _new_runtime_session_id(base_session_id: str) -> str:
    return f"{base_session_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _resolve_runtime_session_id(db: SessionDB) -> str:
    base_session_id = _session_id()
    with _ACTIVE_SESSION_LOCK:
        active_session_id = _read_active_session_id()
        reset_reason = _runtime_reset_reason(db, active_session_id)
        if not reset_reason:
            if active_session_id != _read_active_session_id():
                _write_active_session_id(active_session_id)
            return active_session_id

        session_row = db.get_session(active_session_id)
        if session_row and session_row.get("ended_at") is None:
            db.end_session(active_session_id, "session_reset")

        new_session_id = _new_runtime_session_id(base_session_id)
        db.create_session(
            session_id=new_session_id,
            source="product-runtime",
            parent_session_id=active_session_id,
        )
        _write_active_session_id(new_session_id)
        return new_session_id


def _runtime_token() -> str:
    return _required_env("HERMES_PRODUCT_RUNTIME_TOKEN")


def _require_runtime_token(header_value: str | None, expected: str) -> None:
    candidate = header_value or ""
    if not candidate or not secrets.compare_digest(candidate, expected):
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
        if role == "assistant" and isinstance(message.get("tool_calls"), list) and message.get("tool_calls"):
            continue
        content = str(message.get("content") or "")
        if role != "user":
            content = strip_reasoning_blocks(content)
        if role != "user" and not content.strip():
            continue
        visible.append({"role": role, "content": content})
    return visible


def _register_active_agent(session_id: str, agent: Any) -> None:
    with _ACTIVE_AGENT_LOCK:
        _ACTIVE_AGENTS[session_id] = agent


def _clear_active_agent(session_id: str, agent: Any) -> None:
    with _ACTIVE_AGENT_LOCK:
        if _ACTIVE_AGENTS.get(session_id) is agent:
            _ACTIVE_AGENTS.pop(session_id, None)


def _interrupt_active_agent(session_id: str) -> bool:
    with _ACTIVE_AGENT_LOCK:
        agent = _ACTIVE_AGENTS.get(session_id)
    if agent is None:
        return False
    agent.interrupt()
    return True


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
    runtime_token = _runtime_token()
    _load_runtime_soul()

    app = FastAPI(title="Hermes Core Product Runtime", version="0.1.0")

    @app.get("/healthz", response_model=RuntimeHealthResponse)
    def healthz() -> RuntimeHealthResponse:
        db = SessionDB()
        try:
            session_id = _resolve_runtime_session_id(db)
        finally:
            db.close()
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
            session_id = _resolve_runtime_session_id(db)
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
            session_id = _resolve_runtime_session_id(db)
            agent = build_runtime_agent(db, session_id)
            _register_active_agent(session_id, agent)
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
            if "agent" in locals():
                _clear_active_agent(session_id, agent)
            if "db" in locals():
                db.close()
        final_response = strip_reasoning_blocks(str(result.get("final_response") or result.get("response") or ""))
        return RuntimeTurnResponse(
            final_response=final_response,
            session_id=session_id,
            messages=[RuntimeMessage(**message) for message in _visible_messages(updated_messages)],
            runtime_mode=runtime_mode,
            runtime_toolsets=runtime_toolsets,
        )

    @app.post("/runtime/turn/stop")
    def runtime_turn_stop(x_hermes_product_runtime_token: str | None = Header(default=None)) -> dict[str, bool]:
        _require_runtime_token(x_hermes_product_runtime_token, runtime_token)
        session_id = _read_active_session_id()
        return {"stopped": _interrupt_active_agent(session_id)}

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
                reasoning_emitter = lambda text: event_queue.put(("reasoning", {"delta": str(text or "")}))
                answer_emitter = lambda text: event_queue.put(("answer", {"delta": str(text or "")}))
                mux = ReasoningStreamMux(on_answer=answer_emitter, on_reasoning=reasoning_emitter)
                session_id = _resolve_runtime_session_id(db)
                event_queue.put(("start", {"session_id": session_id}))
                agent = build_runtime_agent(
                    db,
                    session_id,
                    reasoning_callback=reasoning_emitter,
                )
                _register_active_agent(session_id, agent)
                setattr(
                    agent,
                    "reasoning_callback",
                    reasoning_emitter,
                )
                history = _load_session_messages(db, session_id)
                result = agent.run_conversation(
                    request.user_message,
                    conversation_history=_conversation_for_agent(history),
                    stream_callback=mux.feed,
                    sync_honcho=False,
                )
                mux.flush()
                updated_messages = _load_session_messages(db, session_id)
                event_queue.put(
                    (
                        "final",
                        RuntimeTurnResponse(
                            final_response=strip_reasoning_blocks(
                                str(result.get("final_response") or result.get("response") or "")
                            ),
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
                if "agent" in locals():
                    _clear_active_agent(session_id, agent)
                if "db" in locals():
                    db.close()
                event_queue.put(("done", {}))

        def _stream() -> Iterator[bytes]:
            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            while True:
                try:
                    event, payload = event_queue.get(timeout=1.0)
                except queue.Empty:
                    yield b": keepalive\n\n"
                    continue
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
