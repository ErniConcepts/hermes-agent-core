from pathlib import Path
import os
import pytest
import yaml

from hermes_cli.config import load_config, save_config, save_env_value
from hermes_cli.product_config import load_product_config
from hermes_cli.product_runtime import (
    ProductRuntimeRecord,
    _RUNTIME_WORKSPACE_PATH,
    _running_container_matches_record,
    _runtime_container_user,
    _docker_run_command,
    _normalize_runtime_session_payload,
    _resolve_runtime_launch_settings,
    _resolve_runtime_model_base_url,
    _runtime_config_path,
    _write_runtime_env_file,
    _wait_for_runtime_health,
    get_product_runtime_session,
    load_runtime_record,
    product_runtime_session_id,
    stage_product_runtime,
)


def _configure_hermes_runtime(model_base_url: str = "http://127.0.0.1:8080/v1") -> None:
    config = load_config()
    config["model"] = {
        "provider": "custom",
        "base_url": model_base_url,
        "default": "qwen3.5-9b-local",
    }
    save_config(config)
    save_env_value("OPENAI_BASE_URL", model_base_url)
    save_env_value("OPENAI_API_KEY", "")


def _runtime_user() -> dict[str, str]:
    return {"sub": "user-1", "preferred_username": "admin", "name": "Admin User"}


def _assert_default_runtime_soul_intro(soul_text: str, product_name: str = "Hermes Core") -> None:
    assert f"You are a Hermes Agent running in a {product_name} user runtime." in soul_text
    assert "Your persistent user-visible working area is `/workspace`." in soul_text
    assert "You also have internal temporary storage at `/workspace/.tmp`." in soul_text
    assert "not as part of the normal user-facing workspace" in soul_text


def _assert_runtime_capability_overlay(soul_text: str) -> None:
    assert "Your currently enabled Hermes toolsets are: file, terminal, memory." in soul_text
    assert "The concrete tools currently available in this runtime are:" in soul_text
    for tool_name in ("read_file", "write_file", "patch", "search_files", "terminal", "process", "memory"):
        assert tool_name in soul_text


def test_product_runtime_session_id_is_stable():
    first = product_runtime_session_id("admin")
    second = product_runtime_session_id("admin")

    assert first == second
    assert first.startswith("product_admin_")


def test_stage_product_runtime_writes_soul_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime()

    record = stage_product_runtime({**_runtime_user(), "is_admin": True})

    soul_path = Path(record.hermes_home) / "SOUL.md"
    assert soul_path.exists()
    soul_text = soul_path.read_text(encoding="utf-8")
    _assert_default_runtime_soul_intro(soul_text)
    _assert_runtime_capability_overlay(soul_text)
    manifest = Path(record.manifest_file)
    assert manifest.exists()
    loaded = ProductRuntimeRecord.model_validate_json(manifest.read_text(encoding="utf-8"))
    assert loaded.user_id == "user-1"
    assert loaded.runtime_key
    assert loaded.auth_token
    assert loaded.runtime == "runsc"


def test_stage_product_runtime_carries_session_reset_policy_into_runtime_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime()

    config = load_config()
    config["session_reset"] = {"mode": "both", "idle_minutes": 30, "at_hour": 4}
    save_config(config)

    record = stage_product_runtime(_runtime_user())
    runtime_config = yaml.safe_load((Path(record.hermes_home) / "config.yaml").read_text(encoding="utf-8"))

    assert runtime_config["session_reset"] == {"mode": "both", "idle_minutes": 30, "at_hour": 4}


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are only meaningful on non-Windows hosts")
def test_stage_product_runtime_uses_container_readable_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime()

    record = stage_product_runtime(_runtime_user())

    runtime_root = Path(record.runtime_root)
    hermes_home = Path(record.hermes_home)
    workspace_root = Path(record.workspace_root)
    soul_path = hermes_home / "SOUL.md"
    env_path = Path(record.env_file)

    assert oct(runtime_root.stat().st_mode & 0o777) == "0o755"
    assert oct(hermes_home.stat().st_mode & 0o777) == "0o700"
    assert oct(workspace_root.stat().st_mode & 0o777) == "0o700"
    assert oct(soul_path.stat().st_mode & 0o777) == "0o644"
    assert oct(env_path.stat().st_mode & 0o777) == "0o600"


def test_stage_product_runtime_reuses_existing_runtime_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime()

    first = stage_product_runtime(_runtime_user())
    second = stage_product_runtime(_runtime_user())

    assert second.auth_token == first.auth_token
    assert second.runtime_port == first.runtime_port
    assert second.session_id == first.session_id


def test_stage_product_runtime_uses_custom_soul_template(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime()
    template_path = tmp_path / "custom-soul.md"
    template_path.write_text("Custom runtime identity", encoding="utf-8")

    from hermes_cli.product_config import load_product_config, save_product_config

    config = load_product_config()
    config["product"]["agent"]["soul_template_path"] = str(template_path)
    save_product_config(config)

    record = stage_product_runtime(_runtime_user())
    soul_path = Path(record.hermes_home) / "SOUL.md"
    soul_text = soul_path.read_text(encoding="utf-8")
    assert "Custom runtime identity" in soul_text
    _assert_runtime_capability_overlay(soul_text)


def test_stage_product_runtime_includes_product_brand_in_default_soul(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime()

    from hermes_cli.product_config import load_product_config, save_product_config

    config = load_product_config()
    config["product"]["brand"]["name"] = "Atlas Core"
    save_product_config(config)

    record = stage_product_runtime(_runtime_user())
    soul_text = (Path(record.hermes_home) / "SOUL.md").read_text(encoding="utf-8")

    _assert_default_runtime_soul_intro(soul_text, "Atlas Core")
    assert "Atlas Core Runtime Identity" in soul_text


def test_get_product_runtime_session_proxies_runtime(monkeypatch):
    record = ProductRuntimeRecord(
        user_id="admin",
        runtime_key="admin-deadbeef0000",
        display_name="Admin",
        session_id="product_admin_123",
        container_name="runtime-admin",
        runtime="runsc",
        runtime_port=18091,
        runtime_root="/tmp/runtime",
        hermes_home="/tmp/runtime/hermes",
        workspace_root="/tmp/workspace",
        env_file="/tmp/runtime/runtime.env",
        manifest_file="/tmp/runtime/launch-spec.json",
        auth_token="runtime-token",
        status="running",
    )

    monkeypatch.setattr("hermes_cli.product_runtime.ensure_product_runtime", lambda user, config=None: record)
    seen = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "session_id": "product_admin_123",
                "messages": [{"role": "assistant", "content": "hello"}],
                "runtime_mode": "product",
                "runtime_toolsets": ["file", "terminal", "memory"],
            }

    def _fake_get(*args, **kwargs):
        seen["headers"] = kwargs.get("headers", {})
        return _Response()

    monkeypatch.setattr("hermes_cli.product_runtime_container.httpx.get", _fake_get)

    payload = get_product_runtime_session(_runtime_user())
    assert payload["session_id"] == "product_admin_123"
    assert payload["messages"][0]["content"] == "hello"
    assert seen["headers"]["X-Hermes-Product-Runtime-Token"] == "runtime-token"


def test_normalize_runtime_session_payload_requires_current_shape():
    with pytest.raises(RuntimeError, match="runtime_mode"):
        _normalize_runtime_session_payload(
            {
                "session_id": "product_admin_123",
                "messages": [],
                "runtime_profile": "admin",
                "runtime_toolset": "memory",
            }
        )


def test_wait_for_runtime_health_retries_until_ok(monkeypatch):
    record = ProductRuntimeRecord(
        user_id="admin",
        runtime_key="admin-deadbeef0000",
        display_name="Admin",
        session_id="product_admin_123",
        container_name="runtime-admin",
        runtime="runsc",
        runtime_port=18091,
        runtime_root="/tmp/runtime",
        hermes_home="/tmp/runtime/hermes",
        workspace_root="/tmp/workspace",
        env_file="/tmp/runtime/runtime.env",
        manifest_file="/tmp/runtime/launch-spec.json",
        auth_token="runtime-token",
        status="running",
    )
    calls = {"count": 0}

    class _Response:
        def __init__(self, status="ok"):
            self._status = status

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": self._status}

    def _fake_get(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("connection refused")
        return _Response()

    monkeypatch.setattr("hermes_cli.product_runtime_container.httpx.get", _fake_get)
    monkeypatch.setattr("hermes_cli.product_runtime_container.time.sleep", lambda *_args, **_kwargs: None)

    _wait_for_runtime_health(record, timeout_seconds=2.0, interval_seconds=0.01)

    assert calls["count"] == 3


def test_runtime_model_base_url_rewrites_loopback_for_container_access():
    config = {"runtime": {"host_access_host": "host.docker.internal"}}

    rewritten = _resolve_runtime_model_base_url(config, "http://127.0.0.1:8080/v1")

    assert rewritten == "http://host.docker.internal:8080/v1"


def test_runtime_model_base_url_keeps_non_loopback_hosts():
    config = {"runtime": {"host_access_host": "host.docker.internal"}}

    rewritten = _resolve_runtime_model_base_url(config, "https://llm.example.internal/v1")

    assert rewritten == "https://llm.example.internal/v1"


def test_stage_product_runtime_writes_container_reachable_model_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime()

    from hermes_cli.product_config import load_product_config, save_product_config

    config = load_product_config()
    config["runtime"]["host_access_host"] = "host.docker.internal"
    save_product_config(config)

    record = stage_product_runtime(_runtime_user())
    env_text = Path(record.env_file).read_text(encoding="utf-8")

    assert f"HERMES_WRITE_SAFE_ROOT={_RUNTIME_WORKSPACE_PATH}" in env_text
    assert f"TERMINAL_CWD={_RUNTIME_WORKSPACE_PATH}" in env_text
    assert f"TMPDIR={_RUNTIME_WORKSPACE_PATH}/.tmp" in env_text
    assert f"TEMP={_RUNTIME_WORKSPACE_PATH}/.tmp" in env_text
    assert f"TMP={_RUNTIME_WORKSPACE_PATH}/.tmp" in env_text
    assert "PYTHONPATH=/app" in env_text
    assert "HERMES_PRODUCT_PROVIDER=custom" in env_text
    assert "TIRITH_FAIL_OPEN=false" in env_text
    assert "OPENAI_BASE_URL=http://host.docker.internal:8080/v1" in env_text


def test_stage_product_runtime_writes_runtime_context_override_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime()
    config = load_config()
    config["model"]["context_length"] = 32768
    save_config(config)

    record = stage_product_runtime(_runtime_user())

    runtime_config = _runtime_config_path(load_product_config(), "user-1")
    assert runtime_config.exists()
    payload = yaml.safe_load(runtime_config.read_text(encoding="utf-8"))
    assert payload["model"]["default"] == "qwen3.5-9b-local"
    assert payload["model"]["base_url"] == "http://host.docker.internal:8080/v1"
    assert payload["model"]["provider"] == "custom"
    assert payload["model"]["context_length"] == 32768
    assert Path(record.env_file).exists()


def test_docker_run_command_adds_host_gateway_mapping():
    config = {
        "runtime": {
            "internal_port": 8091,
            "image": "hermes-product-local:dev",
            "host_access_host": "host.docker.internal",
        }
    }
    record = ProductRuntimeRecord(
        user_id="admin",
        runtime_key="admin-deadbeef0000",
        display_name="Admin",
        session_id="product_admin_123",
        container_name="runtime-admin",
        runtime="runc",
        runtime_port=18091,
        runtime_root="/tmp/runtime",
        hermes_home="/tmp/runtime/hermes",
        workspace_root="/tmp/workspace",
        env_file="/tmp/runtime/runtime.env",
        manifest_file="/tmp/runtime/launch-spec.json",
        auth_token="runtime-token",
        status="running",
    )

    command = _docker_run_command(record, config)

    assert "--network" in command
    assert "hermes-product-runtime" in command
    assert "--add-host" in command
    assert "--workdir" in command
    assert _RUNTIME_WORKSPACE_PATH in command
    assert "host.docker.internal:host-gateway" in command
    assert "type=bind,src=/tmp/runtime/hermes,dst=/srv/hermes" in command
    assert "type=bind,src=/tmp/runtime/hermes/SOUL.md,dst=/srv/hermes/SOUL.md,readonly" in command
    assert f"type=bind,src=/tmp/workspace,dst={_RUNTIME_WORKSPACE_PATH}" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "no-new-privileges" in command
    assert "/tmp:ro,noexec,nosuid,size=64m" in command
    assert "/var/tmp:ro,noexec,nosuid,size=32m" in command


@pytest.mark.skipif(os.name == "nt", reason="UID/GID mapping is POSIX-specific")
def test_runtime_container_user_uses_workspace_owner(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = ProductRuntimeRecord(
        user_id="admin",
        runtime_key="admin-deadbeef0000",
        display_name="Admin",
        session_id="product_admin_123",
        container_name="runtime-admin",
        runtime="runc",
        runtime_port=18091,
        runtime_root=str(tmp_path / "runtime"),
        hermes_home=str(tmp_path / "runtime" / "hermes"),
        workspace_root=str(workspace),
        env_file=str(tmp_path / "runtime" / "runtime.env"),
        manifest_file=str(tmp_path / "runtime" / "launch-spec.json"),
        auth_token="runtime-token",
        status="running",
    )

    assert _runtime_container_user(record) == f"{workspace.stat().st_uid}:{workspace.stat().st_gid}"


def test_docker_run_command_adds_user_mapping_when_available(monkeypatch):
    config = {
        "runtime": {
            "internal_port": 8091,
            "image": "hermes-product-local:dev",
            "host_access_host": "host.docker.internal",
        }
    }
    record = ProductRuntimeRecord(
        user_id="admin",
        runtime_key="admin-deadbeef0000",
        display_name="Admin",
        session_id="product_admin_123",
        container_name="runtime-admin",
        runtime="runc",
        runtime_port=18091,
        runtime_root="/tmp/runtime",
        hermes_home="/tmp/runtime/hermes",
        workspace_root="/tmp/workspace",
        env_file="/tmp/runtime/runtime.env",
        manifest_file="/tmp/runtime/launch-spec.json",
        auth_token="runtime-token",
        status="running",
    )
    monkeypatch.setattr("hermes_cli.product_runtime_container.runtime_container_user", lambda _record: "1000:1000")

    command = _docker_run_command(record, config)

    assert "--user" in command
    assert "1000:1000" in command


def test_docker_run_command_mounts_runtime_config_read_only_when_present(tmp_path):
    config = {
        "runtime": {
            "internal_port": 8091,
            "image": "hermes-product-local:dev",
            "host_access_host": "host.docker.internal",
        }
    }
    hermes_home = tmp_path / "runtime" / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("soul\n", encoding="utf-8")
    (hermes_home / "config.yaml").write_text("model:\n  default: test\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / "runtime" / "runtime.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("", encoding="utf-8")
    record = ProductRuntimeRecord(
        user_id="admin",
        runtime_key="admin-deadbeef0000",
        display_name="Admin",
        session_id="product_admin_123",
        container_name="runtime-admin",
        runtime="runc",
        runtime_port=18091,
        runtime_root=str(tmp_path / "runtime"),
        hermes_home=str(hermes_home),
        workspace_root=str(workspace),
        env_file=str(env_file),
        manifest_file=str(tmp_path / "runtime" / "launch-spec.json"),
        auth_token="runtime-token",
        status="running",
    )

    command = _docker_run_command(record, config)

    assert (
        f"type=bind,src={hermes_home.as_posix()}/config.yaml,dst=/srv/hermes/config.yaml,readonly"
        in command
    )


def test_stage_product_runtime_requires_ready_hermes_model_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime()
    monkeypatch.setattr(
        "hermes_cli.product_runtime_staging.resolve_runtime_provider",
        lambda requested=None: {"provider": "custom", "base_url": "", "api_key": "", "api_mode": "chat_completions"},
    )

    with pytest.raises(RuntimeError, match="hermes setup model"):
        stage_product_runtime(_runtime_user())


def test_resolve_runtime_launch_settings_uses_saved_custom_provider_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime("https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    config = load_config()
    config["custom_providers"] = [
        {
            "name": "Dashscope-intl.aliyuncs.com",
            "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "api_key": "dashscope-test-key",
            "model": "qwen3.5-27b",
        }
    ]
    save_config(config)
    save_env_value("OPENAI_API_KEY", "")

    settings = _resolve_runtime_launch_settings(load_product_config())

    assert settings.base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert settings.api_key == "dashscope-test-key"


def test_stage_product_runtime_migrates_legacy_username_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime()

    from hermes_cli.product_config import load_product_config

    config = load_product_config()
    legacy_root = tmp_path / "product" / "users" / "admin-8c6976e5b541"
    runtime_root = legacy_root / "runtime"
    hermes_home = runtime_root / "hermes"
    workspace_root = legacy_root / "workspace"
    hermes_home.mkdir(parents=True)
    workspace_root.mkdir(parents=True)
    (workspace_root / "notes.txt").write_text("hello", encoding="utf-8")
    env_file = runtime_root / "runtime.env"
    env_file.write_text("HERMES_PRODUCT_MODEL=qwen3.5-9b-local\n", encoding="utf-8")
    manifest_file = runtime_root / "launch-spec.json"
    manifest_file.write_text(
        ProductRuntimeRecord(
            user_id="admin",
            runtime_key="admin-8c6976e5b541",
            display_name="Admin User",
            session_id="legacy-session",
            container_name="hermes-product-runtime-admin-8c6976e5b541",
            runtime="runsc",
            runtime_port=18091,
            runtime_root=str(runtime_root),
            hermes_home=str(hermes_home),
            workspace_root=str(workspace_root),
            env_file=str(env_file),
            manifest_file=str(manifest_file),
            auth_token="legacy-token",
            status="staged",
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    monkeypatch.setattr("hermes_cli.product_runtime_container.remove_container_if_exists", lambda *_args, **_kwargs: None)

    record = stage_product_runtime(_runtime_user(), config=config)

    assert record.user_id == "user-1"
    assert record.session_id == "legacy-session"
    assert record.auth_token == "legacy-token"
    assert record.runtime_port == 18091
    assert Path(record.workspace_root, "notes.txt").read_text(encoding="utf-8") == "hello"
    assert not legacy_root.exists()
    assert load_runtime_record("user-1", config=config) is not None


def test_stage_product_runtime_does_not_rewrite_soul_when_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_hermes_runtime()

    first = stage_product_runtime(_runtime_user())
    soul_path = Path(first.hermes_home) / "SOUL.md"
    before_mtime = soul_path.stat().st_mtime_ns

    second = stage_product_runtime(_runtime_user())
    after_mtime = soul_path.stat().st_mtime_ns

    assert second.user_id == "user-1"
    assert after_mtime == before_mtime


def test_running_container_matches_record_detects_stale_runtime_env(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    env_file = runtime_root / "runtime.env"
    env_file.write_text(
        "\n".join(
            [
                "HERMES_PRODUCT_PROVIDER=custom",
                "HERMES_PRODUCT_MODEL=qwen3.5-9b-local",
                "OPENAI_BASE_URL=http://host.docker.internal:11437/v1",
                "OPENAI_API_KEY=product-runtime",
                "HERMES_PRODUCT_TOOLSETS=file,terminal,memory",
                "HERMES_PRODUCT_API_MODE=chat_completions",
                "HERMES_PRODUCT_RUNTIME_MODE=product",
                "TIRITH_FAIL_OPEN=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    record = ProductRuntimeRecord(
        user_id="admin",
        runtime_key="admin-deadbeef0000",
        display_name="Admin",
        session_id="product_admin_123",
        container_name="runtime-admin",
        runtime="runc",
        runtime_port=18091,
        runtime_root=str(runtime_root),
        hermes_home=str(runtime_root / "hermes"),
        workspace_root=str(tmp_path / "workspace"),
        env_file=str(env_file),
        manifest_file=str(runtime_root / "launch-spec.json"),
        auth_token="runtime-token",
        status="running",
    )
    stale_container = {
        "Config": {
            "Env": [
                "HERMES_PRODUCT_PROVIDER=openrouter",
                "HERMES_PRODUCT_MODEL=anthropic/claude-opus-4.6",
                "OPENAI_BASE_URL=https://openrouter.ai/api/v1",
                "OPENAI_API_KEY=product-runtime",
                "HERMES_PRODUCT_TOOLSETS=file,terminal,memory",
                "HERMES_PRODUCT_API_MODE=chat_completions",
                "HERMES_PRODUCT_RUNTIME_MODE=product",
                "TIRITH_FAIL_OPEN=true",
            ]
        }
    }

    assert _running_container_matches_record(record, stale_container) is False


def test_write_runtime_env_file_rejects_newlines(tmp_path):
    env_path = tmp_path / "runtime.env"

    with pytest.raises(RuntimeError, match="unsupported newline or NUL characters"):
        _write_runtime_env_file(
            env_path,
            {
                "OPENAI_API_KEY": "line-one\nline-two",
                "HERMES_PRODUCT_MODEL": "qwen3.5-9b-local",
            },
        )


def test_write_runtime_env_file_rejects_oversized_values(tmp_path):
    env_path = tmp_path / "runtime.env"

    with pytest.raises(RuntimeError, match="longer than 8192"):
        _write_runtime_env_file(
            env_path,
            {
                "OPENAI_API_KEY": "x" * 8193,
                "HERMES_PRODUCT_MODEL": "qwen3.5-9b-local",
            },
        )
