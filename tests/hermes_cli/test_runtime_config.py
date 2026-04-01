from hermes_cli.runtime_config import build_runtime_cli_config


def test_build_runtime_cli_config_includes_model_and_session_reset() -> None:
    payload = build_runtime_cli_config(
        base_url="http://127.0.0.1:8080/v1",
        model="qwen3.5-9b-local",
        model_cfg={"provider": "custom", "context_length": 32768},
        root_config={"session_reset": {"mode": "both", "idle_minutes": 30, "at_hour": 4}},
    )

    assert payload == {
        "model": {
            "default": "qwen3.5-9b-local",
            "base_url": "http://127.0.0.1:8080/v1",
            "provider": "custom",
            "context_length": 32768,
        },
        "session_reset": {"mode": "both", "idle_minutes": 30, "at_hour": 4},
    }


def test_build_runtime_cli_config_omits_invalid_context_length() -> None:
    payload = build_runtime_cli_config(
        base_url="http://127.0.0.1:8080/v1",
        model="qwen3.5-9b-local",
        model_cfg={"provider": "custom", "context_length": "invalid"},
        root_config={},
    )

    assert payload == {}
