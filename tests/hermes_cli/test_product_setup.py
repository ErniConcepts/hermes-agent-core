from pathlib import Path
import json
from argparse import Namespace
import pytest

from hermes_cli.product_config import load_product_config, save_product_config
from hermes_cli.product_setup_tailscale import detect_tailscale_identity
from hermes_cli.product_setup_bootstrap import _pending_bootstrap_url, print_product_setup_summary
from hermes_cli.product_setup import (
    _configure_tsidp_client_credentials,
    complete_first_admin_bootstrap,
    setup_product_branding,
    setup_product_bootstrap_identity,
    setup_product_runtime_backend,
    setup_product_tailscale,
)
from hermes_cli.product_stack import first_admin_bootstrap_completed


def test_setup_product_branding_saves_custom_product_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_setup_sections.prompt", lambda *args, **kwargs: "Atlas Core")

    setup_product_branding()

    config = load_product_config()
    assert config["product"]["brand"]["name"] == "Atlas Core"


def test_setup_product_branding_uses_default_name_for_blank_input(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_setup_sections.prompt", lambda *args, **kwargs: "")

    setup_product_branding()

    config = load_product_config()
    assert config["product"]["brand"]["name"] == "Hermes Core"


def test_run_product_setup_branding_section_restarts_app_service_without_full_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls: list[str] = []

    monkeypatch.setattr("hermes_cli.product_setup.is_interactive_stdin", lambda: True)
    monkeypatch.setattr("hermes_cli.product_setup.setup_product_branding", lambda: calls.append("branding"))
    monkeypatch.setattr("hermes_cli.product_setup._reload_product_app_service", lambda: calls.append("reload"))
    monkeypatch.setattr("hermes_cli.product_setup._print_product_setup_summary", lambda: calls.append("summary"))

    run_args = Namespace(section="branding", non_interactive=False, from_install=False)
    from hermes_cli.product_setup import run_product_setup_wizard

    run_product_setup_wizard(run_args)

    assert calls == ["branding", "reload"]


def test_run_product_setup_full_wizard_restarts_app_service_after_branding(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls: list[str] = []

    monkeypatch.setattr("hermes_cli.product_setup.is_interactive_stdin", lambda: True)
    monkeypatch.setattr("hermes_cli.product_setup.setup_product_tailscale", lambda: calls.append("tailscale"))
    monkeypatch.setattr("hermes_cli.product_setup.setup_product_bootstrap_identity", lambda: False)
    monkeypatch.setattr(
        "hermes_cli.product_setup._run_bootstrap_section",
        lambda force_new_bootstrap=False: calls.append(f"bootstrap:{force_new_bootstrap}"),
    )
    monkeypatch.setattr("hermes_cli.product_setup.setup_product_branding", lambda: calls.append("branding"))
    monkeypatch.setattr("hermes_cli.product_setup.setup_product_identity", lambda: calls.append("identity"))
    monkeypatch.setattr("hermes_cli.product_setup.setup_product_runtime_backend", lambda: calls.append("runtime"))
    monkeypatch.setattr("hermes_cli.product_setup.setup_product_storage", lambda: calls.append("storage"))
    monkeypatch.setattr("hermes_cli.product_setup._reload_product_app_service", lambda: calls.append("reload"))
    monkeypatch.setattr("hermes_cli.product_setup._print_product_setup_summary", lambda: calls.append("summary"))

    run_args = Namespace(section=None, non_interactive=False, from_install=False)
    from hermes_cli.product_setup import run_product_setup_wizard

    run_product_setup_wizard(run_args)

    assert calls == [
        "tailscale",
        "bootstrap:False",
        "branding",
        "identity",
        "runtime",
        "storage",
        "reload",
        "summary",
    ]


def test_setup_product_runtime_backend_saves_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    choices = iter([2, 0])
    monkeypatch.setattr("hermes_cli.product_setup_sections.prompt_choice", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr("hermes_cli.product_setup_sections._managed_tool_call_parsers", lambda: ["hermes", "qwen", "qwen3_coder"])

    setup_product_runtime_backend()

    config = load_product_config()
    assert config["runtime"]["backend_policy"] == "managed"
    assert config["runtime"]["tool_call_parser"] == "hermes"


def test_setup_product_runtime_backend_skips_parser_for_standard_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_setup_sections.prompt_choice", lambda *args, **kwargs: 1)
    called = {"value": False}

    def _list_parsers():
        called["value"] = True
        return ["hermes", "qwen"]

    monkeypatch.setattr("hermes_cli.product_setup_sections._managed_tool_call_parsers", _list_parsers)

    setup_product_runtime_backend()

    config = load_product_config()
    assert config["runtime"]["backend_policy"] == "standard"
    assert config["runtime"]["tool_call_parser"] == "hermes"
    assert called["value"] is False




def test_setup_product_bootstrap_identity_does_not_require_manual_login_value(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    force_new = setup_product_bootstrap_identity()

    config = load_product_config()
    assert config["bootstrap"]["first_admin_display_name"] == "Administrator"
    assert force_new is True


def test_setup_product_bootstrap_identity_allows_keeping_existing_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_setup_sections.first_admin_bootstrap_completed", lambda state=None: True)
    monkeypatch.setattr(
        "hermes_cli.product_setup_sections.load_first_admin_enrollment_state",
        lambda: {"tailscale_login": "admin@example.com", "first_admin_login_seen": True},
    )
    monkeypatch.setattr("hermes_cli.product_setup_sections.prompt_choice", lambda *args, **kwargs: 0)

    force_new = setup_product_bootstrap_identity()

    assert force_new is False


def test_setup_product_bootstrap_identity_can_create_new_admin_link(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_setup_sections.first_admin_bootstrap_completed", lambda state=None: True)
    monkeypatch.setattr(
        "hermes_cli.product_setup_sections.load_first_admin_enrollment_state",
        lambda: {"tailscale_login": "admin@example.com", "first_admin_login_seen": True},
    )
    monkeypatch.setattr("hermes_cli.product_setup_sections.prompt_choice", lambda *args, **kwargs: 1)

    force_new = setup_product_bootstrap_identity()

    assert force_new is True


def test_configure_tsidp_client_credentials_saves_client_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompts = iter(["hermes-core", "secret-123"])
    saved = {}

    monkeypatch.setattr("hermes_cli.product_setup_bootstrap.prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.bootstrap_product_tailscale_oidc_client",
        lambda config=None: (_ for _ in ()).throw(RuntimeError("auto registration unavailable")),
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.save_env_value_secure",
        lambda key, value: saved.setdefault(key, value),
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.resolve_product_urls",
        lambda config=None: {
            "issuer_url": "https://idp.tail5fd7a5.ts.net",
            "oidc_callback_url": "https://device.tail5fd7a5.ts.net/api/auth/oidc/callback",
        },
    )

    _configure_tsidp_client_credentials()

    config = load_product_config()
    assert config["auth"]["client_id"] == "hermes-core"
    assert saved["HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET"] == "secret-123"


def test_configure_tsidp_client_credentials_keeps_existing_secret_on_blank_input(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET", "saved-secret")
    prompts = iter(["hermes-core", ""])
    saved = {}

    monkeypatch.setattr("hermes_cli.product_setup_bootstrap.prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.bootstrap_product_tailscale_oidc_client",
        lambda config=None: (_ for _ in ()).throw(RuntimeError("auto registration unavailable")),
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.save_env_value_secure",
        lambda key, value: saved.setdefault(key, value),
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.resolve_product_urls",
        lambda config=None: {
            "issuer_url": "https://idp.tail5fd7a5.ts.net",
            "oidc_callback_url": "https://device.tail5fd7a5.ts.net/api/auth/oidc/callback",
        },
    )

    _configure_tsidp_client_credentials()

    config = load_product_config()
    assert config["auth"]["client_id"] == "hermes-core"
    assert saved == {}


def test_configure_tsidp_client_credentials_auto_registers_when_available(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.bootstrap_product_tailscale_oidc_client",
        lambda config=None: {"created": True, "client_id": "auto-client"},
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.resolve_product_urls",
        lambda config=None: {
            "issuer_url": "https://idp.tail5fd7a5.ts.net",
            "oidc_callback_url": "https://device.tail5fd7a5.ts.net/api/auth/oidc/callback",
        },
    )

    _configure_tsidp_client_credentials()


def test_setup_product_tailscale_requires_auth_key_and_saves_detected_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompts = iter(["tskey-auth-kv", "tskey-api-kv"])
    saved = {}

    monkeypatch.setattr(
        "hermes_cli.product_setup_tailscale.detect_tailscale_identity",
        lambda command_path: {
            "device_name": "laptop",
            "tailnet_name": "tail5fd7a5",
            "api_tailnet_name": "example.github",
            "command_path": "tailscale",
        },
    )
    monkeypatch.setattr("hermes_cli.product_setup_tailscale.prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(
        "hermes_cli.product_setup_tailscale.save_env_value_secure",
        lambda key, value: saved.setdefault(key, value),
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup_tailscale.ensure_tsidp_policy",
        lambda config=None: {"changed": False, "backup_path": "", "tailnet": "example.github"},
    )

    setup_product_tailscale()

    config = load_product_config()
    assert config["network"]["tailscale"]["tailnet_name"] == "tail5fd7a5"
    assert config["network"]["tailscale"]["device_name"] == "laptop"
    assert config["network"]["tailscale"]["api_tailnet_name"] == "example.github"
    assert config["network"]["tailscale"]["idp_hostname"] == "idp"
    assert config["network"]["tailscale"]["command_path"] == "tailscale"
    assert saved["HERMES_PRODUCT_TAILSCALE_AUTH_KEY"] == "tskey-auth-kv"
    assert saved["HERMES_PRODUCT_TAILSCALE_API_TOKEN"] == "tskey-api-kv"


def test_setup_product_tailscale_keeps_existing_secrets_on_blank_input(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_AUTH_KEY", "saved-auth")
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_API_TOKEN", "saved-api")
    prompts = iter(["", ""])
    saved = {}

    monkeypatch.setattr(
        "hermes_cli.product_setup_tailscale.detect_tailscale_identity",
        lambda command_path: {
            "device_name": "laptop",
            "tailnet_name": "tail5fd7a5",
            "api_tailnet_name": "example.github",
            "command_path": "tailscale",
        },
    )
    monkeypatch.setattr("hermes_cli.product_setup_tailscale.prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(
        "hermes_cli.product_setup_tailscale.save_env_value_secure",
        lambda key, value: saved.setdefault(key, value),
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup_tailscale.ensure_tsidp_policy",
        lambda config=None: {"changed": False, "backup_path": "", "tailnet": "example.github"},
    )

    setup_product_tailscale()

    assert saved == {}
    config = load_product_config()
    assert config["network"]["tailscale"]["idp_hostname"] == "idp"
    assert config["network"]["tailscale"]["command_path"] == "tailscale"


def test_setup_product_tailscale_uses_windows_browser_endpoint_in_wsl(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    prompts = iter(["tskey-auth-kv", "tskey-api-kv"])
    seen: dict[str, str] = {}
    config = load_product_config()
    config["network"]["tailscale"]["command_path"] = "/mnt/c/Program Files/Tailscale/tailscale.exe"
    save_product_config(config)

    def fake_detect(command_path):
        seen["command_path"] = command_path
        return {
            "device_name": "laptop",
            "tailnet_name": "tail5fd7a5",
            "api_tailnet_name": "example.github",
            "command_path": command_path,
        }

    monkeypatch.setattr("hermes_cli.product_setup_tailscale.detect_tailscale_identity", fake_detect)
    monkeypatch.setattr(
        "hermes_cli.product_setup_tailscale._detect_wsl_browser_tailscale_identity",
        lambda saved_command_path: {
            "device_name": "windows-laptop",
            "tailnet_name": "tail5fd7a5",
            "api_tailnet_name": "example.github",
            "command_path": saved_command_path,
        },
    )
    monkeypatch.setattr("hermes_cli.product_setup_tailscale.prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr("hermes_cli.product_setup_tailscale.save_env_value_secure", lambda key, value: None)
    monkeypatch.setattr(
        "hermes_cli.product_setup_tailscale.ensure_tsidp_policy",
        lambda config=None: {"changed": False, "backup_path": "", "tailnet": "example.github"},
    )

    setup_product_tailscale()

    assert seen["command_path"] == "tailscale"
    config = load_product_config()
    assert config["network"]["tailscale"]["command_path"] == "tailscale"
    assert config["network"]["tailscale"]["app_command_path"] == "/mnt/c/Program Files/Tailscale/tailscale.exe"
    assert config["network"]["tailscale"]["app_device_name"] == "windows-laptop"
    assert config["network"]["tailscale"]["browser_host_mode"] == "windows_tailscale"


def test_detect_tailscale_identity_does_not_fall_back_to_windows_tailscale_in_wsl(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    calls: list[str] = []

    linux_status = {
        "Self": {"HostName": "LaptopJannis", "DNSName": ""},
        "CurrentTailnet": None,
        "MagicDNSSuffix": "",
    }

    def fake_run(args, check, capture_output, text):
        command = args[0]
        calls.append(command)

        class Result:
            stdout = json.dumps(linux_status)

        return Result()

    monkeypatch.setattr("hermes_cli.product_setup_tailscale.subprocess.run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        detect_tailscale_identity("tailscale")

    assert calls == ["tailscale"]
    assert "CurrentTailnet" in str(exc_info.value)


def test_detect_tailscale_identity_rejects_explicit_windows_tailscale_in_wsl(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")

    with pytest.raises(RuntimeError) as exc_info:
        detect_tailscale_identity("/mnt/c/Program Files/Tailscale/tailscale.exe")

    message = str(exc_info.value)
    assert "must use the Tailscale daemon inside WSL" in message
    assert "sudo tailscale up" in message


def test_setup_product_tailscale_reports_missing_tailscale_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.product_setup_tailscale.detect_tailscale_identity",
        lambda command_path: (_ for _ in ()).throw(RuntimeError("Tailscale CLI not found: tailscale")),
    )

    with pytest.raises(RuntimeError) as exc_info:
        setup_product_tailscale()

    message = str(exc_info.value)
    assert "Tailscale must be installed and connected" in message
    assert "rerun `hermes-core setup`" in message


def test_first_admin_bootstrap_completed_requires_active_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    bootstrap_root = Path(tmp_path) / "product" / "bootstrap"
    bootstrap_root.mkdir(parents=True, exist_ok=True)
    (bootstrap_root / "first_admin_enrollment.json").write_text(
        json.dumps(
            {
                "first_admin_login_seen": True,
                "bootstrap_token": "",
                "bootstrap_url": "https://device.tail5fd7a5.ts.net",
            }
        ),
        encoding="utf-8",
    )

    assert first_admin_bootstrap_completed() is False


def test_complete_first_admin_bootstrap_prints_link_without_waiting(monkeypatch):
    prompts: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.prompt",
        lambda question, default=None, password=False: prompts.append((question, default)) or "",
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.first_admin_bootstrap_completed",
        lambda state=None: bool(state and state.get("first_admin_login_seen", False)),
    )

    final_state = complete_first_admin_bootstrap(
        {
            "setup_url": "https://device.tail5fd7a5.ts.net/bootstrap/token-1",
            "first_admin_login_seen": False,
        }
    )

    assert final_state["setup_url"] == "https://device.tail5fd7a5.ts.net/bootstrap/token-1"
    assert prompts == []


def test_pending_bootstrap_url_uses_current_app_url_for_saved_token():
    url = _pending_bootstrap_url(
        {
            "bootstrap_token": "token-1",
            "bootstrap_url": "https://old-device.tail5fd7a5.ts.net/bootstrap/token-1",
        },
        {"app_base_url": "https://new-device.tail5fd7a5.ts.net"},
    )

    assert url == "https://new-device.tail5fd7a5.ts.net/bootstrap/token-1"

def test_product_setup_summary_starts_next_steps_with_bootstrap_link(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.resolve_product_urls",
        lambda config=None: {
            "app_base_url": "https://device.tail5fd7a5.ts.net",
            "issuer_url": "https://idp.tail5fd7a5.ts.net",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup_bootstrap.load_first_admin_enrollment_state",
        lambda: {
            "bootstrap_token": "token-1",
            "bootstrap_url": "https://old-device.tail5fd7a5.ts.net/bootstrap/token-1",
            "first_admin_login_seen": False,
        },
    )
    monkeypatch.setattr("hermes_cli.product_setup_bootstrap.first_admin_bootstrap_completed", lambda state=None: False)

    print_product_setup_summary()

    output = capsys.readouterr().out
    next_steps = output.split("Next Steps", 1)[1]
    assert "First, create the first admin account:" in next_steps
    assert "Open bootstrap URL: https://device.tail5fd7a5.ts.net/bootstrap/token-1" in next_steps
    assert next_steps.index("Open bootstrap URL") < next_steps.index("hermes setup model")
