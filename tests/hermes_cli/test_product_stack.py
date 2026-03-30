from pathlib import Path

import yaml

from hermes_cli.product_config import load_product_config
from hermes_cli.product_stack import (
    _build_tsidp_compose_spec,
    _build_tsidp_env_file,
    bootstrap_first_admin_enrollment,
    get_tsidp_compose_path,
    get_tsidp_env_path,
    initialize_product_stack,
    resolve_product_urls,
)


def _config():
    config = load_product_config()
    config["auth"]["client_id"] = "hermes-core"
    config["auth"]["issuer_url"] = "https://idp.tail5fd7a5.ts.net"
    config["network"]["tailscale"]["enabled"] = True
    config["network"]["tailscale"]["tailnet_name"] = "tail5fd7a5"
    config["network"]["tailscale"]["device_name"] = "device"
    config["network"]["tailscale"]["idp_hostname"] = "idp"
    config["network"]["tailscale"]["app_https_port"] = 443
    return config


def test_resolve_product_urls_returns_tailnet_only_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    urls = resolve_product_urls(_config())

    assert urls["app_base_url"] == "https://device.tail5fd7a5.ts.net"
    assert urls["issuer_url"] == "https://idp.tail5fd7a5.ts.net"


def test_initialize_product_stack_writes_tsidp_env_and_compose(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_AUTH_KEY", "tskey-auth-kv")

    config = initialize_product_stack(_config())

    assert config["auth"]["provider"] == "tsidp"
    env_text = get_tsidp_env_path().read_text(encoding="utf-8")
    assert "TS_AUTHKEY=tskey-auth-kv" in env_text
    compose = yaml.safe_load(get_tsidp_compose_path().read_text(encoding="utf-8"))
    assert compose["services"]["tsidp"]["container_name"] == "hermes-tsidp"


def test_build_tsidp_env_file_uses_current_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_AUTH_KEY", "tskey-auth-kv")

    rendered = _build_tsidp_env_file(_config())

    assert "TAILSCALE_USE_WIP_CODE=1" in rendered
    assert "TSIDP_LOCAL_PORT=8080" in rendered
    assert "TS_AUTHKEY=tskey-auth-kv" in rendered

def test_bootstrap_first_admin_enrollment_creates_one_time_link(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_AUTH_KEY", "tskey-auth-kv")
    monkeypatch.setattr(
        "hermes_cli.product_stack_bootstrap.bootstrap_product_oidc_client",
        lambda config=None: {"client_id": "hermes-core"},
    )

    state = bootstrap_first_admin_enrollment(_config())

    assert state["auth_mode"] == "tsidp"
    assert state["bootstrap_token"]
    assert state["bootstrap_url"].startswith("https://device.tail5fd7a5.ts.net/bootstrap/")
