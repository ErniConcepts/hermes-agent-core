from __future__ import annotations

import json
import os
import queue
import re
import secrets
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi import Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
import uvicorn

from agent.model_metadata import estimate_messages_tokens_rough
from hermes_state import SessionDB
from hermes_cli.product_runtime import _RUNTIME_WORKSPACE_PATH

_ACTIVE_AGENT_LOCK = threading.Lock()
_ACTIVE_AGENTS: dict[str, Any] = {}
_THINK_OPEN_TAGS = ("<REASONING_SCRATCHPAD>", "<think>", "<reasoning>", "<THINKING>", "<thinking>")
_THINK_CLOSE_TAGS = ("</REASONING_SCRATCHPAD>", "</think>", "</reasoning>", "</THINKING>", "</thinking>")
_PRODUCT_RUNTIME_KEEP_USER_TURNS = 6
_PRODUCT_RUNTIME_MIN_USER_TURNS = 2
_PRODUCT_RUNTIME_WORKING_CONTEXT_BUDGET = 2_500
_PRODUCT_RUNTIME_SUMMARY_MAX_CHARS = 1_600


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
            content = _strip_reasoning_blocks(content)
        if role != "user" and not content.strip():
            continue
        visible.append({"role": role, "content": content})
    return visible


def _strip_reasoning_blocks(content: str, *, trim: bool = True) -> str:
    if not content:
        return ""
    stripped = content
    stripped = re.sub(r"<think>.*?</think>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"<reasoning>.*?</reasoning>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(
        r"<REASONING_SCRATCHPAD>.*?</REASONING_SCRATCHPAD>",
        "",
        stripped,
        flags=re.DOTALL,
    )
    stripped = re.sub(r"</?(?:think|thinking|reasoning)>", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"</REASONING_SCRATCHPAD>", "", stripped)
    if "</think>" in content.lower() and "<think>" not in content.lower():
        trailing = re.split(r"</think>", stripped, flags=re.IGNORECASE)[-1]
        if trailing.strip():
            stripped = trailing
    return stripped.strip() if trim else stripped


class _ReasoningStreamMux:
    def __init__(self, *, on_answer: Any, on_reasoning: Any) -> None:
        self._on_answer = on_answer
        self._on_reasoning = on_reasoning
        self._buffer = ""
        self._in_reasoning = False
        self._has_visible_answer = False

    def feed(self, text: str | None) -> None:
        if not text:
            return
        self._buffer += str(text)
        while self._buffer:
            if self._in_reasoning:
                close_idx, close_tag = self._find_first_tag(self._buffer, _THINK_CLOSE_TAGS)
                if close_tag is None:
                    self._emit_safe_reasoning_tail()
                    return
                reasoning = self._buffer[:close_idx]
                if reasoning:
                    self._on_reasoning(reasoning)
                self._buffer = self._buffer[close_idx + len(close_tag) :]
                self._in_reasoning = False
                continue

            open_idx, open_tag = self._find_first_tag(self._buffer, _THINK_OPEN_TAGS)
            close_idx, close_tag = self._find_first_tag(self._buffer, _THINK_CLOSE_TAGS)
            if close_tag is not None and (open_tag is None or close_idx < open_idx) and not self._has_visible_answer:
                reasoning = self._buffer[:close_idx]
                if reasoning:
                    self._on_reasoning(reasoning)
                self._buffer = self._buffer[close_idx + len(close_tag) :]
                continue
            if open_tag is not None:
                before = self._buffer[:open_idx]
                if before:
                    self._emit_answer(before)
                self._buffer = self._buffer[open_idx + len(open_tag) :]
                self._in_reasoning = True
                continue
            self._emit_safe_answer_tail()
            return

    def flush(self) -> None:
        if not self._buffer:
            return
        if self._in_reasoning:
            self._on_reasoning(self._buffer)
        else:
            self._emit_answer(self._buffer)
        self._buffer = ""
        self._in_reasoning = False

    def _emit_answer(self, text: str) -> None:
        visible = _strip_reasoning_blocks(text, trim=False)
        if visible:
            if visible.strip():
                self._has_visible_answer = True
            self._on_answer(visible)

    def _emit_safe_answer_tail(self) -> None:
        safe = self._buffer
        for tag in _THINK_OPEN_TAGS:
            for i in range(1, len(tag)):
                if self._buffer.endswith(tag[:i]):
                    safe = self._buffer[:-i]
                    break
        if safe:
            self._emit_answer(safe)
            self._buffer = self._buffer[len(safe) :]

    def _emit_safe_reasoning_tail(self) -> None:
        max_tag_len = max(len(tag) for tag in _THINK_CLOSE_TAGS)
        if len(self._buffer) <= max_tag_len:
            return
        safe_reasoning = self._buffer[:-max_tag_len]
        if safe_reasoning:
            self._on_reasoning(safe_reasoning)
        self._buffer = self._buffer[-max_tag_len:]

    @staticmethod
    def _find_first_tag(buffer: str, tags: tuple[str, ...]) -> tuple[int, str | None]:
        first_idx = -1
        first_tag = None
        lowered = buffer.lower()
        for tag in tags:
            idx = lowered.find(tag.lower())
            if idx != -1 and (first_idx == -1 or idx < first_idx):
                first_idx = idx
                first_tag = tag
        return first_idx, first_tag


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


def _product_runtime_visible_text(message: dict[str, Any]) -> str:
    role = str(message.get("role", "")).strip()
    content = str(message.get("content") or "")
    return content if role == "user" else _strip_reasoning_blocks(content, trim=True)


def _truncate_runtime_summary_text(text: str, limit: int = _PRODUCT_RUNTIME_SUMMARY_MAX_CHARS) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(limit - 1, 0)].rstrip() + "…"


def _runtime_summary_bullet(message: dict[str, Any], *, limit: int = 220) -> str:
    text = _product_runtime_visible_text(message)
    if not text:
        return ""
    role = str(message.get("role", "")).strip()
    compact = re.sub(r"\s+", " ", text).strip()
    if role == "assistant" and len(compact) > 400:
        return "Assistant completed a large output; rely on the current workspace and recent turns instead of replaying it verbatim."
    return _truncate_runtime_summary_text(compact, limit)


def _extract_runtime_paths(messages: list[dict[str, Any]], *, limit: int = 4) -> list[str]:
    seen: list[str] = []
    for message in messages:
        text = _product_runtime_visible_text(message)
        for match in re.findall(r"(?:^|[\s`'\"])(/?(?:[\w.\-]+/)*[\w.\-]+\.[\w.\-]+|/?(?:[\w.\-]+/)+[\w.\-]+)", text):
            candidate = match.strip("`'\" ")
            if not candidate or candidate in seen:
                continue
            seen.append(candidate)
            if len(seen) >= limit:
                return seen
    return seen


def _build_runtime_history_summary(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    visible = [message for message in messages if message.get("role") in {"user", "assistant"}]
    if not visible:
        return None

    user_lines = [
        _runtime_summary_bullet(message, limit=220)
        for message in visible
        if message.get("role") == "user" and _runtime_summary_bullet(message, limit=220)
    ][-3:]
    assistant_lines = [
        _runtime_summary_bullet(message, limit=220)
        for message in visible
        if message.get("role") == "assistant" and _runtime_summary_bullet(message, limit=220)
    ][-2:]
    mentioned_paths = _extract_runtime_paths(messages)

    lines = [
        "[PRODUCT RUNTIME SUMMARY]",
        "Earlier runtime turns were compacted to keep local-model tool execution reliable.",
    ]
    if user_lines:
        lines.append("Recent user requests:")
        lines.extend(f"- {line}" for line in user_lines)
    if assistant_lines:
        lines.append("Recent completed work:")
        lines.extend(f"- {line}" for line in assistant_lines)
    if mentioned_paths:
        lines.append("Relevant files or folders:")
        lines.extend(f"- {path}" for path in mentioned_paths)
    lines.append("Continue from the current workspace state and avoid repeating finished work.")

    summary = "\n".join(lines).strip()
    if not summary:
        return None
    return {"role": "assistant", "content": summary}


def _runtime_working_context_budget() -> int:
    return _PRODUCT_RUNTIME_WORKING_CONTEXT_BUDGET


def _derive_runtime_conversation(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conversation = _conversation_for_agent(messages)
    if not conversation:
        return []

    user_indexes = [index for index, message in enumerate(conversation) if message.get("role") == "user"]
    if len(user_indexes) <= _PRODUCT_RUNTIME_KEEP_USER_TURNS:
        candidate = list(conversation)
        if estimate_messages_tokens_rough(candidate) <= _runtime_working_context_budget():
            return candidate

    keep_user_turns = min(len(user_indexes), _PRODUCT_RUNTIME_KEEP_USER_TURNS)
    min_keep_turns = min(len(user_indexes), _PRODUCT_RUNTIME_MIN_USER_TURNS)
    omitted: list[dict[str, Any]] = []

    while True:
        if keep_user_turns <= 0:
            recent = list(conversation)
        else:
            start_index = user_indexes[-keep_user_turns]
            omitted = conversation[:start_index]
            recent = conversation[start_index:]
        summary = _build_runtime_history_summary(omitted)
        candidate = ([summary] if summary is not None else []) + recent
        if estimate_messages_tokens_rough(candidate) <= _runtime_working_context_budget():
            return candidate
        if keep_user_turns <= min_keep_turns:
            if len(recent) > 2:
                last_user_indexes = [index for index, message in enumerate(recent) if message.get("role") == "user"]
                if len(last_user_indexes) >= 1:
                    preserve_from = last_user_indexes[-1]
                    omitted = omitted + recent[:preserve_from]
                    recent = recent[preserve_from:]
                    summary = _build_runtime_history_summary(omitted)
                    return ([summary] if summary is not None else []) + recent
            return candidate
        keep_user_turns -= 1


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
            _register_active_agent(session_id, agent)
            history = _load_session_messages(db, session_id)
            working_history = _derive_runtime_conversation(history)
            result = agent.run_conversation(
                request.user_message,
                conversation_history=working_history,
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
        final_response = _strip_reasoning_blocks(str(result.get("final_response") or result.get("response") or ""))
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
                event_queue.put(("start", {"session_id": session_id}))
                reasoning_emitter = lambda text: event_queue.put(("reasoning", {"delta": str(text or "")}))
                answer_emitter = lambda text: event_queue.put(("answer", {"delta": str(text or "")}))
                mux = _ReasoningStreamMux(on_answer=answer_emitter, on_reasoning=reasoning_emitter)
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
                working_history = _derive_runtime_conversation(history)
                result = agent.run_conversation(
                    request.user_message,
                    conversation_history=working_history,
                    stream_callback=mux.feed,
                    sync_honcho=False,
                )
                mux.flush()
                updated_messages = _load_session_messages(db, session_id)
                event_queue.put(
                    (
                        "final",
                        RuntimeTurnResponse(
                            final_response=_strip_reasoning_blocks(
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
