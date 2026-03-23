from argparse import Namespace
from unittest.mock import patch

import os
import pytest
import yaml
from pathlib import Path

from hermes_cli.product_config import load_product_config
from hermes_cli.product_setup import (
    run_product_setup_wizard,
    setup_product_identity,
    setup_product_network,
    setup_product_storage,
    setup_product_tailscale,
)


def _make_product_args(**overrides):
    return Namespace(
        non_interactive=overrides.get("non_interactive", False),
        section=overrides.get("section", None),
    )


def test_product_setup_model_section_syncs_model_route(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def _fake_model_setup(config):
        config["model"] = {
            "provider": "custom",
            "base_url": "http://127.0.0.1:8080/v1",
            "default": "qwen3.5-9b-local",
            "api_mode": "chat_completions",
        }

    with (
        patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True),
        patch("hermes_cli.product_setup.setup_model_provider", side_effect=_fake_model_setup),
    ):
        run_product_setup_wizard(_make_product_args(section="model"))

    product_config = load_product_config()
    assert product_config["models"]["default_route"] == {
        "provider": "custom",
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "qwen3.5-9b-local",
        "api_mode": "chat_completions",
        "context_length": None,
    }


def test_product_setup_tools_section_syncs_cli_toolsets(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def _fake_tools_setup(config, first_install=False):
        config["platform_toolsets"] = {"cli": ["web", "browser", "memory", "missing-toolset"]}

    with (
        patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True),
        patch("hermes_cli.product_setup.get_available_toolsets", return_value={"web": {}, "browser": {}, "memory": {}}),
        patch("hermes_cli.product_setup.setup_tools", side_effect=_fake_tools_setup),
    ):
        run_product_setup_wizard(_make_product_args(section="tools"))

    product_config = load_product_config()
    assert product_config["tools"]["hermes_toolsets"] == ["web", "browser", "memory"]


def test_product_setup_model_section_does_not_write_generic_hermes_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def _fake_model_setup(config):
        config["model"] = {
            "provider": "custom",
            "base_url": "http://127.0.0.1:8080/v1",
            "default": "qwen3.5-9b-local",
            "api_mode": "chat_completions",
        }

    with (
        patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True),
        patch("hermes_cli.product_setup.setup_model_provider", side_effect=_fake_model_setup),
    ):
        run_product_setup_wizard(_make_product_args(section="model"))

    assert not (tmp_path / "config.yaml").exists()


def test_product_setup_model_section_seeds_isolated_config_from_product_route(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    product_config = load_product_config()
    product_config["models"]["default_route"] = {
        "provider": "custom",
        "base_url": "http://product-route.local:8080/v1",
        "model": "qwen3.5-9b-local",
        "context_length": 32768,
    }
    product_config["tools"]["hermes_toolsets"] = ["memory", "file"]
    from hermes_cli.product_config import save_product_config

    save_product_config(product_config)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")

    def _fake_model_setup(config):
        seeded_home = Path(os.environ["HERMES_HOME"])
        seeded = yaml.safe_load((seeded_home / "config.yaml").read_text(encoding="utf-8"))
        assert seeded["model"]["provider"] == "custom"
        assert seeded["model"]["base_url"] == "http://product-route.local:8080/v1"
        assert seeded["model"]["default"] == "qwen3.5-9b-local"
        assert seeded["platform_toolsets"]["cli"] == ["memory", "file"]
        config["model"] = {
            "provider": "custom",
            "base_url": "http://product-route.local:8080/v1",
            "default": "qwen3.5-9b-local",
        }

    with (
        patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True),
        patch("hermes_cli.product_setup.setup_model_provider", side_effect=_fake_model_setup),
    ):
        run_product_setup_wizard(_make_product_args(section="model"))

    assert not (tmp_path / "config.yaml").exists()
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=test-key\n"


def test_product_setup_model_section_reads_back_isolated_config_when_wizard_keeps_current(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    product_config = load_product_config()
    product_config["models"]["default_route"] = {
        "provider": "custom",
        "base_url": "http://product-route.local:8080/v1",
        "model": "qwen3.5-9b-local",
        "context_length": 32768,
    }
    from hermes_cli.product_config import save_product_config

    save_product_config(product_config)

    def _fake_model_setup(config):
        seeded_home = Path(os.environ["HERMES_HOME"])
        seeded = yaml.safe_load((seeded_home / "config.yaml").read_text(encoding="utf-8"))
        seeded["model"]["api_mode"] = "chat_completions"
        (seeded_home / "config.yaml").write_text(yaml.safe_dump(seeded, sort_keys=False), encoding="utf-8")

    with (
        patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True),
        patch("hermes_cli.product_setup.setup_model_provider", side_effect=_fake_model_setup),
    ):
        run_product_setup_wizard(_make_product_args(section="model"))

    reloaded = load_product_config()
    assert reloaded["models"]["default_route"]["provider"] == "custom"
    assert reloaded["models"]["default_route"]["base_url"] == "http://product-route.local:8080/v1"
    assert reloaded["models"]["default_route"]["model"] == "qwen3.5-9b-local"
    assert reloaded["models"]["default_route"]["api_mode"] == "chat_completions"


def test_product_setup_network_section_updates_public_host(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: "officebox.local")

    setup_product_network()

    product_config = load_product_config()
    assert product_config["network"]["public_host"] == "officebox.local"


def test_product_setup_identity_section_updates_soul_template_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    template_path = tmp_path / "custom-soul.md"
    template_path.write_text("custom soul", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: str(template_path))

    setup_product_identity()

    product_config = load_product_config()
    assert product_config["product"]["agent"]["soul_template_path"] == str(template_path.resolve())


def test_product_setup_storage_section_updates_workspace_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: "5")

    setup_product_storage()

    product_config = load_product_config()
    assert product_config["storage"]["user_workspace_limit_mb"] == 5120


def test_product_setup_tailscale_section_updates_tailnet_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    answers = iter(["yes", "", "", "443", "4444"])
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(
        "hermes_cli.product_setup.subprocess.run",
        lambda *args, **kwargs: type(
            "_Result",
            (),
            {
                "stdout": '{"Self":{"DNSName":"hermes-box.corpnet.ts.net."},"MagicDNSSuffix":"corpnet.ts.net"}',
            },
        )(),
    )

    setup_product_tailscale()

    product_config = load_product_config()
    assert product_config["network"]["tailscale"] == {
        "enabled": True,
        "tailnet_name": "corpnet",
        "device_name": "hermes-box",
        "app_https_port": 443,
        "auth_https_port": 4444,
        "command_path": "tailscale",
    }


def test_start_product_stack_ensures_linux_product_app_service(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "hermes_cli.product_setup.ensure_product_app_service_started",
        lambda config=None: seen.append("service"),
    )
    monkeypatch.setattr("hermes_cli.product_setup.ensure_product_stack_started", lambda: seen.append("stack"))
    monkeypatch.setattr(
        "hermes_cli.product_setup.bootstrap_first_admin_enrollment",
        lambda: seen.append("bootstrap")
        or {
            "username": "admin",
            "display_name": "Administrator",
            "email": "",
            "auth_mode": "passkey",
            "setup_url": "https://example.ts.net:8443/setup",
            "oidc_client_id": "hermes-core",
        },
    )

    from hermes_cli.product_setup import _start_product_stack

    _start_product_stack()

    assert seen == ["stack", "bootstrap", "service"]


def test_product_setup_noninteractive_prints_guidance(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with patch("hermes_cli.product_setup.is_interactive_stdin", return_value=False):
        run_product_setup_wizard(_make_product_args())

    out = capsys.readouterr().out
    assert "hermes config set model.provider custom" in out


def test_product_setup_bootstrap_section_validates_host_prereqs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with (
        patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True),
        patch("hermes_cli.product_setup.validate_product_host_prereqs") as mock_validate,
        patch("hermes_cli.product_setup.initialize_product_stack"),
        patch("hermes_cli.product_setup._start_product_stack"),
    ):
        run_product_setup_wizard(_make_product_args(section="bootstrap"))

    mock_validate.assert_called_once()


def test_product_setup_bootstrap_section_exits_cleanly_on_prereq_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with (
        patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True),
        patch("hermes_cli.product_setup.validate_product_host_prereqs", side_effect=RuntimeError("Docker is not available")),
    ):
        with patch("hermes_cli.product_setup.initialize_product_stack") as mock_init:
            with patch("hermes_cli.product_setup._start_product_stack") as mock_start:
                with pytest.raises(SystemExit, match="Docker is not available"):
                    run_product_setup_wizard(_make_product_args(section="bootstrap"))

    mock_init.assert_not_called()
    mock_start.assert_not_called()
