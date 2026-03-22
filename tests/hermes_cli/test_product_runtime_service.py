from fastapi.testclient import TestClient
import importlib

from hermes_cli.product_runtime import _RUNTIME_WORKSPACE_PATH
from hermes_cli.product_runtime_service import create_product_runtime_app
from hermes_cli.product_runtime_service import build_runtime_agent


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
    monkeypatch.setattr("hermes_cli.product_runtime_service.build_runtime_agent", lambda db, session_id, reasoning_callback=None: FakeAgent())
    monkeypatch.setattr(
        "hermes_cli.product_runtime_service._load_session_messages",
        lambda db, session_id: [{"role": "assistant", "content": "earlier"}],
    )

    client = TestClient(create_product_runtime_app())
    session = client.get("/runtime/session", headers={"X-Hermes-Product-Runtime-Token": "runtime-token"})
    assert session.status_code == 200
    assert session.json()["session_id"] == "product_admin_123"
    assert session.json()["runtime_mode"] == "product"
    assert session.json()["runtime_toolsets"] == ["memory", "session_search"]

    turn = client.post("/runtime/turn", json={"user_message": "hello"}, headers={"X-Hermes-Product-Runtime-Token": "runtime-token"})
    assert turn.status_code == 200
    assert turn.json()["final_response"] == "done"


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
    monkeypatch.setattr("hermes_cli.product_runtime_service.build_runtime_agent", lambda db, session_id, reasoning_callback=None: FakeAgent())
    monkeypatch.setattr("hermes_cli.product_runtime_service._load_session_messages", lambda db, session_id: [])

    client = TestClient(create_product_runtime_app())
    with client.stream("POST", "/runtime/turn/stream", json={"user_message": "hello"}, headers={"X-Hermes-Product-Runtime-Token": "runtime-token"}) as response:
        assert response.status_code == 200
        payload = "\n".join(response.iter_text())

    assert "event: reasoning" in payload
    assert "\"delta\": \"thinking\"" in payload
    assert "event: final" in payload
    assert "\"final_response\": \"done\"" in payload
    assert "\"runtime_toolsets\": [\"memory\", \"session_search\"]" in payload


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
