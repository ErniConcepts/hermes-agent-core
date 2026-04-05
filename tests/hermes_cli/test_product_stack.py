from pathlib import Path

import yaml
import httpx

from hermes_cli.product_config import load_product_config
from hermes_cli.product_stack_bootstrap import tailscale_oidc_registration_payload
from hermes_cli.product_stack import (
    _build_tsidp_compose_spec,
    _build_tsidp_env_file,
    bootstrap_first_admin_enrollment,
    bootstrap_product_tailscale_oidc_client,
    get_tsidp_compose_path,
    get_tsidp_env_path,
    initialize_product_stack,
    resolve_product_urls,
    sync_running_tsidp_issuer_url,
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


def test_resolve_product_urls_prefers_configured_issuer_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = _config()
    config["auth"]["issuer_url"] = "https://idp-1.tail5fd7a5.ts.net"

    urls = resolve_product_urls(config)

    assert urls["issuer_url"] == "https://idp-1.tail5fd7a5.ts.net"


def test_sync_running_tsidp_issuer_url_updates_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_AUTH_KEY", "tskey-auth-kv")
    saved: list[str] = []
    monkeypatch.setattr(
        "hermes_cli.product_stack_bootstrap.running_tsidp_issuer_url",
        lambda config: "https://idp-1.tail5fd7a5.ts.net",
    )
    monkeypatch.setattr(
        "hermes_cli.product_stack_bootstrap.save_product_config",
        lambda config: saved.append(config["auth"]["issuer_url"]),
    )

    config = initialize_product_stack(_config())
    updated = sync_running_tsidp_issuer_url(config)

    assert updated["auth"]["issuer_url"] == "https://idp-1.tail5fd7a5.ts.net"
    assert saved[-1] == "https://idp-1.tail5fd7a5.ts.net"


def test_initialize_product_stack_writes_tsidp_env_and_compose(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_AUTH_KEY", "tskey-auth-kv")
    monkeypatch.setattr("hermes_cli.product_stack_bootstrap.host_default_route_mtu", lambda: 1280)

    config = initialize_product_stack(_config())

    assert config["auth"]["provider"] == "tsidp"
    env_text = get_tsidp_env_path().read_text(encoding="utf-8")
    assert "TS_AUTHKEY=tskey-auth-kv" in env_text
    compose = yaml.safe_load(get_tsidp_compose_path().read_text(encoding="utf-8"))
    assert compose["services"]["tsidp"]["container_name"] == "hermes-tsidp"
    assert compose["networks"]["default"]["driver_opts"]["com.docker.network.driver.mtu"] == "1280"


def test_build_tsidp_env_file_uses_current_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_AUTH_KEY", "tskey-auth-kv")

    rendered = _build_tsidp_env_file(_config())

    assert "TAILSCALE_USE_WIP_CODE=1" in rendered
    assert "TSIDP_LOCAL_PORT=8080" in rendered
    assert "TS_AUTHKEY=tskey-auth-kv" in rendered


def test_build_tsidp_compose_spec_inherits_host_route_mtu(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_stack_bootstrap.host_default_route_mtu", lambda: 1280)
    initialize_product_stack(_config())

    compose = _build_tsidp_compose_spec(_config())

    assert compose["networks"]["default"]["driver_opts"]["com.docker.network.driver.mtu"] == "1280"

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


def test_tailscale_oidc_registration_payload_uses_current_oidc_callback(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    payload = tailscale_oidc_registration_payload(_config())

    assert payload["client_name"] == "Hermes Core"
    assert payload["redirect_uris"] == ["https://device.tail5fd7a5.ts.net/api/auth/oidc/callback"]
    assert payload["scope"] == "openid profile email"


def test_bootstrap_product_tailscale_oidc_client_registers_and_saves_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_AUTH_KEY", "tskey-auth-kv")
    saved: dict[str, str] = {}
    monkeypatch.setattr("hermes_cli.product_stack_bootstrap.ensure_product_stack_started", lambda config=None: None)
    monkeypatch.setattr("hermes_cli.product_stack_bootstrap.wait_for_tsidp_ready", lambda config, timeout: None)
    monkeypatch.setattr(
        "hermes_cli.product_stack_bootstrap.save_env_value_secure",
        lambda key, value: saved.setdefault(key, value),
    )

    class _FakeClient:
        def __init__(self, timeout=15.0):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json=None, headers=None):
            assert url == "https://idp.tail5fd7a5.ts.net/register"
            assert json["redirect_uris"] == ["https://device.tail5fd7a5.ts.net/api/auth/oidc/callback"]
            return httpx.Response(
                201,
                json={"client_id": "auto-client", "client_secret": "auto-secret"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        "hermes_cli.product_stack_bootstrap.discover_product_oidc_provider_metadata_by_issuer",
        lambda issuer_url: type("Metadata", (), {"registration_endpoint": "https://idp.tail5fd7a5.ts.net/register"})(),
    )
    monkeypatch.setattr("hermes_cli.product_stack_bootstrap.httpx.Client", _FakeClient)

    result = bootstrap_product_tailscale_oidc_client(_config())

    assert result["created"] is True
    assert result["client_id"] == "auto-client"
    assert saved["HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET"] == "auto-secret"
    updated = load_product_config()
    assert updated["auth"]["client_id"] == "auto-client"


def test_bootstrap_product_tailscale_oidc_client_keeps_existing_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_AUTH_KEY", "tskey-auth-kv")
    monkeypatch.setenv("HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET", "saved-secret")
    config = _config()
    config["auth"]["client_id"] = "saved-client"
    monkeypatch.setattr("hermes_cli.product_stack_bootstrap.ensure_product_stack_started", lambda config=None: None)
    monkeypatch.setattr("hermes_cli.product_stack_bootstrap.wait_for_tsidp_ready", lambda config, timeout: None)
    monkeypatch.setattr(
        "hermes_cli.product_stack_bootstrap.discover_product_oidc_provider_metadata",
        lambda settings: type("Metadata", (), {"registration_endpoint": "https://idp.tail5fd7a5.ts.net/register"})(),
    )

    result = bootstrap_product_tailscale_oidc_client(config)

    assert result["created"] is False
    assert result["client_id"] == "saved-client"
