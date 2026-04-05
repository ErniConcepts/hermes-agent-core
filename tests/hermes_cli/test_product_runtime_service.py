import importlib

from fastapi.testclient import TestClient

from hermes_cli.product_runtime import _RUNTIME_WORKSPACE_PATH
from hermes_cli.product_runtime_service import create_product_runtime_app
from hermes_cli.product_runtime_service import build_runtime_agent
from hermes_cli.product_runtime_service import _resolve_runtime_session_id
from hermes_cli.product_runtime_service import _visible_messages
from session_reset import SessionResetPolicy


class DummyDB:
    def __init__(self):
        self.sessions = {"product_admin_123": {"id": "product_admin_123", "started_at": 0, "ended_at": None}}

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def get_messages(self, session_id):
        return []

    def create_session(self, session_id, source, model=None, model_config=None, system_prompt=None, user_id=None, parent_session_id=None):
        self.sessions[session_id] = {
            "id": session_id,
            "started_at": 0,
            "ended_at": None,
            "parent_session_id": parent_session_id,
            "source": source,
        }
        return session_id

    def end_session(self, session_id, end_reason):
        if session_id in self.sessions:
            self.sessions[session_id]["ended_at"] = 1
            self.sessions[session_id]["end_reason"] = end_reason

    def close(self):
        return None


class FakeAgent:
    def __init__(self):
        self.session_id = "product_admin_123"
        self.reasoning_callback = None

    def run_conversation(self, user_message, conversation_history=None, stream_callback=None, sync_honcho=None):
        if self.reasoning_callback is not None:
            self.reasoning_callback("thinking")
        if stream_callback is not None:
            stream_callback("answer")
        history = list(conversation_history or [])
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": "done"})
        return {"final_response": "done", "messages": history}


class ThinkStreamingAgent:
    def __init__(self):
        self.session_id = "product_admin_123"
        self.reasoning_callback = None

    def run_conversation(self, user_message, conversation_history=None, stream_callback=None, sync_honcho=None):
        if stream_callback is not None:
            stream_callback("<think>The user is testing</think>Visible answer")
        history = list(conversation_history or [])
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": "<think>The user is testing</think>Visible answer"})
        return {"final_response": "<think>The user is testing</think>Visible answer", "messages": history}


class SpacedStreamingAgent:
    def __init__(self):
        self.session_id = "product_admin_123"
        self.reasoning_callback = None

    def run_conversation(self, user_message, conversation_history=None, stream_callback=None, sync_honcho=None):
        if stream_callback is not None:
            stream_callback("this ")
            stream_callback("is ")
            stream_callback("a demo text")
        history = list(conversation_history or [])
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": "this is a demo text"})
        return {"final_response": "this is a demo text", "messages": history}


class WhitespaceChunkAgent:
    def __init__(self):
        self.session_id = "product_admin_123"
        self.reasoning_callback = None

    def run_conversation(self, user_message, conversation_history=None, stream_callback=None, sync_honcho=None):
        if stream_callback is not None:
            stream_callback("this")
            stream_callback(" ")
            stream_callback("is")
            stream_callback(" ")
            stream_callback("a demo text")
        history = list(conversation_history or [])
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": "this is a demo text"})
        return {"final_response": "this is a demo text", "messages": history}


class InterruptibleAgent:
    def __init__(self):
        self.session_id = "product_admin_123"
        self.reasoning_callback = None
        self.interrupted = False

    def interrupt(self, message=None):
        self.interrupted = True

    def run_conversation(self, user_message, conversation_history=None, stream_callback=None, sync_honcho=None):
        return {"final_response": "done", "messages": list(conversation_history or [])}


class CapturingAgent:
    def __init__(self):
        self.session_id = "product_admin_123"
        self.reasoning_callback = None
        self.history_seen = None

    def run_conversation(self, user_message, conversation_history=None, stream_callback=None, sync_honcho=None):
        self.history_seen = list(conversation_history or [])
        history = list(conversation_history or [])
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": "done"})
        return {"final_response": "done", "messages": history}


def test_visible_messages_hides_intermediate_tool_call_assistant_messages():
    visible = _visible_messages(
        [
            {"role": "user", "content": "create a file"},
            {
                "role": "assistant",
                "content": "Planning the file creation",
                "tool_calls": [{"id": "call_1", "function": {"name": "write_file", "arguments": "{}"}}],
            },
            {"role": "tool", "content": '{"success": true}', "tool_call_id": "call_1"},
            {"role": "assistant", "content": "Done. file.txt created."},
        ]
    )

    assert visible == [
        {"role": "user", "content": "create a file"},
        {"role": "assistant", "content": "Done. file.txt created."},
    ]


def test_product_runtime_session_and_turn(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory,session_search")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr("hermes_cli.product_runtime_service._resolve_runtime_session_id", lambda db: "product_admin_123")
    monkeypatch.setattr("hermes_cli.product_runtime_service.build_runtime_agent", lambda db, session_id, reasoning_callback=None: FakeAgent())
    monkeypatch.setattr(
        "hermes_cli.product_runtime_service._load_session_messages",
        lambda db, session_id: [{"role": "assistant", "content": "earlier"}],
    )
    monkeypatch.setattr("hermes_cli.product_runtime_service.SessionDB", DummyDB)

    client = TestClient(create_product_runtime_app())
    session = client.get("/runtime/session", headers={"X-Hermes-Product-Runtime-Token": "runtime-token"})
    assert session.status_code == 200
    assert session.json()["session_id"] == "product_admin_123"
    assert session.json()["runtime_mode"] == "product"
    assert session.json()["runtime_toolsets"] == ["memory", "session_search"]

    turn = client.post("/runtime/turn", json={"user_message": "hello"}, headers={"X-Hermes-Product-Runtime-Token": "runtime-token"})
    assert turn.status_code == 200
    assert turn.json()["final_response"] == "done"


def test_product_runtime_turn_uses_full_history(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory,session_search")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr("hermes_cli.product_runtime_service._resolve_runtime_session_id", lambda db: "product_admin_123")

    stored_messages = []
    for idx in range(8):
        stored_messages.append({"role": "user", "content": f"user-{idx}"})
        stored_messages.append({"role": "assistant", "content": f"assistant-{idx}"})

    agent = CapturingAgent()
    monkeypatch.setattr("hermes_cli.product_runtime_service.build_runtime_agent", lambda db, session_id, reasoning_callback=None: agent)
    monkeypatch.setattr("hermes_cli.product_runtime_service._load_session_messages", lambda db, session_id: stored_messages)
    monkeypatch.setattr("hermes_cli.product_runtime_service.SessionDB", DummyDB)

    client = TestClient(create_product_runtime_app())
    session = client.get("/runtime/session", headers={"X-Hermes-Product-Runtime-Token": "runtime-token"})
    turn = client.post("/runtime/turn", json={"user_message": "hello"}, headers={"X-Hermes-Product-Runtime-Token": "runtime-token"})

    assert session.status_code == 200
    assert len(session.json()["messages"]) == len(stored_messages)
    assert turn.status_code == 200
    assert agent.history_seen is not None
    assert agent.history_seen == stored_messages


def test_resolve_runtime_session_id_rotates_when_session_reset_policy_triggers(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setattr(
        "hermes_cli.product_runtime_service._load_runtime_reset_policy",
        lambda: SessionResetPolicy(mode="both", idle_minutes=1, at_hour=4),
    )
    old_started_at = 0

    class ResetDB(DummyDB):
        def __init__(self):
            super().__init__()
            self.sessions["product_admin_123"]["started_at"] = old_started_at

        def get_messages(self, session_id):
            return [{"timestamp": old_started_at}]

    db = ResetDB()

    rotated = _resolve_runtime_session_id(db)

    assert rotated != "product_admin_123"
    assert db.sessions["product_admin_123"]["end_reason"] == "session_reset"
    assert db.sessions[rotated]["parent_session_id"] == "product_admin_123"


def test_product_runtime_stream_emits_reasoning_and_final(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory,session_search")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr("hermes_cli.product_runtime_service._resolve_runtime_session_id", lambda db: "product_admin_123")
    monkeypatch.setattr("hermes_cli.product_runtime_service.build_runtime_agent", lambda db, session_id, reasoning_callback=None: FakeAgent())
    monkeypatch.setattr("hermes_cli.product_runtime_service._load_session_messages", lambda db, session_id: [])
    monkeypatch.setattr("hermes_cli.product_runtime_service.SessionDB", DummyDB)

    client = TestClient(create_product_runtime_app())
    with client.stream("POST", "/runtime/turn/stream", json={"user_message": "hello"}, headers={"X-Hermes-Product-Runtime-Token": "runtime-token"}) as response:
        assert response.status_code == 200
        payload = "\n".join(response.iter_text())

    assert "event: reasoning" in payload
    assert "\"delta\": \"thinking\"" in payload
    assert "event: final" in payload
    assert "\"final_response\": \"done\"" in payload
    assert "\"runtime_toolsets\": [\"memory\", \"session_search\"]" in payload


def test_product_runtime_stream_routes_think_blocks_to_reasoning(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory,session_search")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr("hermes_cli.product_runtime_service._resolve_runtime_session_id", lambda db: "product_admin_123")
    monkeypatch.setattr("hermes_cli.product_runtime_service.build_runtime_agent", lambda db, session_id, reasoning_callback=None: ThinkStreamingAgent())
    monkeypatch.setattr(
        "hermes_cli.product_runtime_service._load_session_messages",
        lambda db, session_id: [{"role": "assistant", "content": "<think>The user is testing</think>Visible answer"}],
    )
    monkeypatch.setattr("hermes_cli.product_runtime_service.SessionDB", DummyDB)

    client = TestClient(create_product_runtime_app())
    with client.stream("POST", "/runtime/turn/stream", json={"user_message": "hello"}, headers={"X-Hermes-Product-Runtime-Token": "runtime-token"}) as response:
        payload = "\n".join(response.iter_text())

    assert response.status_code == 200
    assert "event: reasoning" in payload
    assert "The user is testing" in payload
    assert "event: answer" in payload
    assert "Visible answer" in payload
    assert "\"final_response\": \"Visible answer\"" in payload


def test_product_runtime_stream_preserves_answer_chunk_spaces(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory,session_search")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr("hermes_cli.product_runtime_service._resolve_runtime_session_id", lambda db: "product_admin_123")
    monkeypatch.setattr(
        "hermes_cli.product_runtime_service.build_runtime_agent",
        lambda db, session_id, reasoning_callback=None: SpacedStreamingAgent(),
    )
    monkeypatch.setattr("hermes_cli.product_runtime_service._load_session_messages", lambda db, session_id: [])
    monkeypatch.setattr("hermes_cli.product_runtime_service.SessionDB", DummyDB)

    client = TestClient(create_product_runtime_app())
    with client.stream(
        "POST",
        "/runtime/turn/stream",
        json={"user_message": "hello"},
        headers={"X-Hermes-Product-Runtime-Token": "runtime-token"},
    ) as response:
        payload = "\n".join(response.iter_text())

    assert response.status_code == 200
    assert "\"delta\": \"this \"" in payload
    assert "\"delta\": \"is \"" in payload
    assert "\"delta\": \"a demo text\"" in payload
    assert "\"final_response\": \"this is a demo text\"" in payload


def test_product_runtime_stream_preserves_whitespace_only_chunks(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory,session_search")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr("hermes_cli.product_runtime_service._resolve_runtime_session_id", lambda db: "product_admin_123")
    monkeypatch.setattr(
        "hermes_cli.product_runtime_service.build_runtime_agent",
        lambda db, session_id, reasoning_callback=None: WhitespaceChunkAgent(),
    )
    monkeypatch.setattr("hermes_cli.product_runtime_service._load_session_messages", lambda db, session_id: [])
    monkeypatch.setattr("hermes_cli.product_runtime_service.SessionDB", DummyDB)

    client = TestClient(create_product_runtime_app())
    with client.stream(
        "POST",
        "/runtime/turn/stream",
        json={"user_message": "hello"},
        headers={"X-Hermes-Product-Runtime-Token": "runtime-token"},
    ) as response:
        payload = "\n".join(response.iter_text())

    assert response.status_code == 200
    assert "\"delta\": \"this\"" in payload
    assert "\"delta\": \" \"" in payload
    assert "\"delta\": \"is\"" in payload
    assert "\"final_response\": \"this is a demo text\"" in payload


def test_product_runtime_stop_interrupts_active_agent(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr("hermes_cli.product_runtime_service._resolve_runtime_session_id", lambda db: "product_admin_123")
    agent = InterruptibleAgent()
    monkeypatch.setattr("hermes_cli.product_runtime_service.SessionDB", DummyDB)

    from hermes_cli.product_runtime_service import _register_active_agent

    _register_active_agent("product_admin_123", agent)
    client = TestClient(create_product_runtime_app())
    response = client.post("/runtime/turn/stop", headers={"X-Hermes-Product-Runtime-Token": "runtime-token"})

    assert response.status_code == 200
    assert response.json() == {"stopped": True}
    assert agent.interrupted is True


def test_product_runtime_service_rejects_missing_runtime_token(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")

    client = TestClient(create_product_runtime_app())
    response = client.get("/runtime/session")

    assert response.status_code == 401


def test_product_runtime_service_rejects_mismatched_runtime_token(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")

    client = TestClient(create_product_runtime_app())
    response = client.get("/runtime/session", headers={"X-Hermes-Product-Runtime-Token": "runtime-token "})

    assert response.status_code == 401


def test_classify_runtime_error_treats_timeout_as_model_unavailable(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")

    from hermes_cli.product_runtime_service import _classify_runtime_error

    status_code, detail = _classify_runtime_error(RuntimeError("APITimeoutError: Request timed out."))

    assert status_code == 503
    assert detail == "Model not available. Check the server configuration."


def test_product_runtime_session_filters_blank_assistant_messages(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory,file")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr(
        "hermes_cli.product_runtime_service._load_session_messages",
        lambda db, session_id: [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": ""},
            {"role": "tool", "content": '{"ok": true}'},
            {"role": "assistant", "content": "done"},
        ],
    )
    monkeypatch.setattr("hermes_cli.product_runtime_service.SessionDB", DummyDB)

    client = TestClient(create_product_runtime_app())
    response = client.get("/runtime/session", headers={"X-Hermes-Product-Runtime-Token": "runtime-token"})

    assert response.status_code == 200
    assert response.json()["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "done"},
    ]


def test_build_runtime_agent_scopes_tools_to_workspace(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory,file")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")

    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["agent_kwargs"] = kwargs

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    terminal_tool_module = importlib.import_module("tools.terminal_tool")
    monkeypatch.setattr(
        terminal_tool_module,
        "register_task_env_overrides",
        lambda task_id, overrides: captured.update({"task_id": task_id, "overrides": overrides}),
    )

    build_runtime_agent(object(), "product_admin_123")

    assert captured["task_id"] == "product_admin_123"
    assert captured["overrides"] == {"cwd": _RUNTIME_WORKSPACE_PATH}
    assert captured["agent_kwargs"]["enabled_toolsets"] == ["memory", "file"]
    assert captured["agent_kwargs"]["session_id"] == "product_admin_123"
    assert captured["agent_kwargs"]["platform"] == "product-runtime"


def test_product_runtime_turn_reports_model_not_available(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory,session_search")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr("hermes_cli.product_runtime_service._resolve_runtime_session_id", lambda db: "product_admin_123")

    class FailingAgent:
        def run_conversation(self, *args, **kwargs):
            raise RuntimeError("APIConnectionError: Connection error.")

    monkeypatch.setattr(
        "hermes_cli.product_runtime_service.build_runtime_agent",
        lambda db, session_id, reasoning_callback=None: FailingAgent(),
    )
    monkeypatch.setattr("hermes_cli.product_runtime_service._load_session_messages", lambda db, session_id: [])
    monkeypatch.setattr("hermes_cli.product_runtime_service.SessionDB", DummyDB)

    client = TestClient(create_product_runtime_app())
    response = client.post(
        "/runtime/turn",
        json={"user_message": "hello"},
        headers={"X-Hermes-Product-Runtime-Token": "runtime-token"},
    )

    assert response.status_code == 503
    assert "Model not available" in response.json()["detail"]
    assert "host.docker.internal:8080/v1" not in response.json()["detail"]


def test_product_runtime_stream_reports_model_not_available(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("Runtime identity", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_MODE", "product")
    monkeypatch.setenv("HERMES_PRODUCT_TOOLSETS", "memory,session_search")
    monkeypatch.setenv("HERMES_PRODUCT_PROVIDER", "custom")
    monkeypatch.setenv("HERMES_PRODUCT_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_PRODUCT_MODEL", "qwen3.5-9b-local")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "product-local-route")
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_ID", "product_admin_123")
    monkeypatch.setenv("HERMES_PRODUCT_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr("hermes_cli.product_runtime_service._resolve_runtime_session_id", lambda db: "product_admin_123")

    class FailingAgent:
        def __init__(self):
            self.reasoning_callback = None

        def run_conversation(self, *args, **kwargs):
            raise RuntimeError("APIConnectionError: Connection error.")

    monkeypatch.setattr(
        "hermes_cli.product_runtime_service.build_runtime_agent",
        lambda db, session_id, reasoning_callback=None: FailingAgent(),
    )
    monkeypatch.setattr("hermes_cli.product_runtime_service._load_session_messages", lambda db, session_id: [])
    monkeypatch.setattr("hermes_cli.product_runtime_service.SessionDB", DummyDB)

    client = TestClient(create_product_runtime_app())
    with client.stream(
        "POST",
        "/runtime/turn/stream",
        json={"user_message": "hello"},
        headers={"X-Hermes-Product-Runtime-Token": "runtime-token"},
    ) as response:
        body = "\n".join(response.iter_text())

    assert response.status_code == 200
    assert "event: error" in body
    assert "Model not available" in body
    assert "host.docker.internal:8080/v1" not in body
