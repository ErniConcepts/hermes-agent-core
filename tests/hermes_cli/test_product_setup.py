from pathlib import Path

from hermes_cli.product_config import load_product_config
from hermes_cli.product_setup import (
    _configure_tsidp_client_credentials,
    setup_product_bootstrap_identity,
    setup_product_tailscale,
)


def test_setup_product_bootstrap_identity_saves_first_admin_login(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompts = iter(["admin@example.com"])

    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: next(prompts))

    setup_product_bootstrap_identity()

    config = load_product_config()
    assert config["bootstrap"]["first_admin_tailscale_login"] == "admin@example.com"


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
    prompts = iter(["tail5fd7a5", "laptop", "idp", "443", "tskey-auth-kv"])
    saved = {}

    monkeypatch.setattr(
        "hermes_cli.product_setup._detect_tailscale_identity",
        lambda command_path: ("laptop", "tail5fd7a5"),
    )
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(
        "hermes_cli.product_setup.save_env_value_secure",
        lambda key, value: saved.setdefault(key, value),
    )

    setup_product_tailscale()

    config = load_product_config()
    assert config["network"]["tailscale"]["tailnet_name"] == "tail5fd7a5"
    assert config["network"]["tailscale"]["device_name"] == "laptop"
    assert saved["HERMES_PRODUCT_TAILSCALE_AUTH_KEY"] == "tskey-auth-kv"
