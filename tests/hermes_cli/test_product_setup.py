from pathlib import Path

from hermes_cli.product_config import load_product_config
from hermes_cli.product_setup import _configure_tsidp_client_credentials, setup_product_bootstrap_identity, setup_product_tailscale


def test_setup_product_bootstrap_identity_does_not_require_manual_login_value(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    setup_product_bootstrap_identity()

    config = load_product_config()
    assert config["bootstrap"]["first_admin_tailscale_login"] == ""


def test_configure_tsidp_client_credentials_saves_client_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompts = iter(["hermes-core", "secret-123"])
    saved = {}

    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(
        "hermes_cli.product_setup.save_env_value_secure",
        lambda key, value: saved.setdefault(key, value),
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup.resolve_product_urls",
        lambda config=None: {
            "issuer_url": "https://idp.tail5fd7a5.ts.net",
            "oidc_callback_url": "https://device.tail5fd7a5.ts.net/api/auth/oidc/callback",
        },
    )

    _configure_tsidp_client_credentials()

    config = load_product_config()
    assert config["auth"]["client_id"] == "hermes-core"
    assert saved["HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET"] == "secret-123"


def test_setup_product_tailscale_requires_auth_key_and_saves_detected_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompts = iter(["idp", "tskey-auth-kv", "tskey-api-kv"])
    saved = {}

    monkeypatch.setattr(
        "hermes_cli.product_setup._detect_tailscale_identity",
        lambda command_path: {
            "device_name": "laptop",
            "tailnet_name": "tail5fd7a5",
            "api_tailnet_name": "example.github",
        },
    )
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(
        "hermes_cli.product_setup.save_env_value_secure",
        lambda key, value: saved.setdefault(key, value),
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup.ensure_tsidp_policy",
        lambda config=None: {"changed": False, "backup_path": "", "tailnet": "example.github"},
    )

    setup_product_tailscale()

    config = load_product_config()
    assert config["network"]["tailscale"]["tailnet_name"] == "tail5fd7a5"
    assert config["network"]["tailscale"]["device_name"] == "laptop"
    assert config["network"]["tailscale"]["api_tailnet_name"] == "example.github"
    assert saved["HERMES_PRODUCT_TAILSCALE_AUTH_KEY"] == "tskey-auth-kv"
    assert saved["HERMES_PRODUCT_TAILSCALE_API_TOKEN"] == "tskey-api-kv"


def test_setup_product_tailscale_keeps_existing_secrets_on_blank_input(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_AUTH_KEY", "saved-auth")
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_API_TOKEN", "saved-api")
    prompts = iter(["idp", "", ""])
    saved = {}

    monkeypatch.setattr(
        "hermes_cli.product_setup._detect_tailscale_identity",
        lambda command_path: {
            "device_name": "laptop",
            "tailnet_name": "tail5fd7a5",
            "api_tailnet_name": "example.github",
        },
    )
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(
        "hermes_cli.product_setup.save_env_value_secure",
        lambda key, value: saved.setdefault(key, value),
    )
    monkeypatch.setattr(
        "hermes_cli.product_setup.ensure_tsidp_policy",
        lambda config=None: {"changed": False, "backup_path": "", "tailnet": "example.github"},
    )

    setup_product_tailscale()

    assert saved == {}
