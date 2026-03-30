from pathlib import Path
import json
import pytest

from hermes_cli.product_config import load_product_config
from hermes_cli.product_setup_tailscale import detect_tailscale_identity
from hermes_cli.product_setup import _configure_tsidp_client_credentials, setup_product_bootstrap_identity, setup_product_tailscale


def test_setup_product_bootstrap_identity_does_not_require_manual_login_value(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    setup_product_bootstrap_identity()

    config = load_product_config()
    assert config["bootstrap"]["first_admin_display_name"] == "Administrator"


def test_configure_tsidp_client_credentials_saves_client_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompts = iter(["hermes-core", "secret-123"])
    saved = {}

    monkeypatch.setattr("hermes_cli.product_setup_bootstrap.prompt", lambda *args, **kwargs: next(prompts))
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


def test_detect_tailscale_identity_falls_back_to_windows_tailscale_in_wsl(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr("hermes_cli.product_setup_tailscale.os.path.exists", lambda path: True)

    linux_status = {
        "Self": {"HostName": "LaptopJannis", "DNSName": ""},
        "CurrentTailnet": None,
        "MagicDNSSuffix": "",
    }
    windows_status = {
        "Self": {"HostName": "LaptopJannis", "DNSName": "laptopjannis.cheetah-vernier.ts.net."},
        "CurrentTailnet": {"Name": "jannis-cmd.github"},
        "MagicDNSSuffix": "cheetah-vernier.ts.net",
    }

    def fake_run(args, check, capture_output, text):
        command = args[0]
        payload = linux_status if command == "tailscale" else windows_status

        class Result:
            stdout = json.dumps(payload)

        return Result()

    monkeypatch.setattr("hermes_cli.product_setup_tailscale.subprocess.run", fake_run)

    detected = detect_tailscale_identity("tailscale")

    assert detected == {
        "device_name": "laptopjannis",
        "tailnet_name": "cheetah-vernier",
        "api_tailnet_name": "jannis-cmd.github",
        "command_path": "/mnt/c/Program Files/Tailscale/tailscale.exe",
    }


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
