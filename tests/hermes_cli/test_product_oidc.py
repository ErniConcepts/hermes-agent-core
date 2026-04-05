from urllib.parse import parse_qs, urlparse

import httpx

from hermes_cli.product_config import load_product_config
from hermes_cli.product_oidc import (
    clear_product_oidc_provider_metadata_cache,
    create_oidc_login_request,
    create_pkce_challenge,
    discover_product_oidc_provider_metadata,
    discover_product_oidc_provider_metadata_by_issuer,
    exchange_product_oidc_code,
    load_product_oidc_client_settings,
)


def test_load_product_oidc_client_settings_reads_product_config_and_secret(monkeypatch):
    clear_product_oidc_provider_metadata_cache()
    config = load_product_config()
    config["network"]["tailscale"]["enabled"] = True
    config["network"]["tailscale"]["tailnet_name"] = "corpnet"
    config["network"]["tailscale"]["device_name"] = "hermes-box"
    config["network"]["tailscale"]["idp_hostname"] = "idp"
    config["auth"]["issuer_url"] = "https://idp.corpnet.ts.net"
    config["auth"]["client_id"] = "hermes-core"

    monkeypatch.setattr("hermes_cli.product_oidc.get_env_value", lambda key: "oidc-secret")

    settings = load_product_oidc_client_settings(config)

    assert settings.issuer_url == "https://idp.corpnet.ts.net"
    assert settings.client_id == "hermes-core"
    assert settings.client_secret == "oidc-secret"
    assert settings.redirect_uri == "https://hermes-box.corpnet.ts.net/api/auth/oidc/callback"
    assert settings.scopes == ("openid", "profile", "email")


def test_load_product_oidc_client_settings_uses_tailnet_callback_when_enabled(monkeypatch):
    clear_product_oidc_provider_metadata_cache()
    config = load_product_config()
    config["network"]["tailscale"]["enabled"] = True
    config["network"]["tailscale"]["tailnet_name"] = "corpnet"
    config["network"]["tailscale"]["device_name"] = "hermes-box"
    config["network"]["tailscale"]["app_https_port"] = 443
    config["network"]["tailscale"]["auth_https_port"] = 4444
    config["auth"]["issuer_url"] = "https://hermes-box.corpnet.ts.net:4444"
    config["auth"]["client_id"] = "hermes-core"

    monkeypatch.setattr("hermes_cli.product_oidc.get_env_value", lambda key: "oidc-secret")

    settings = load_product_oidc_client_settings(config)

    assert settings.redirect_uri == "https://hermes-box.corpnet.ts.net/api/auth/oidc/callback"


def test_discover_product_oidc_provider_metadata_uses_well_known(monkeypatch):
    clear_product_oidc_provider_metadata_cache()
    monkeypatch.setattr("hermes_cli.product_oidc.get_env_value", lambda key: "oidc-secret")

    def _handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://idp.corpnet.ts.net/.well-known/openid-configuration"
        return httpx.Response(
            200,
            json={
                "issuer": "https://idp.corpnet.ts.net",
                "authorization_endpoint": "https://idp.corpnet.ts.net/authorize",
                "token_endpoint": "https://idp.corpnet.ts.net/token",
                "userinfo_endpoint": "https://idp.corpnet.ts.net/userinfo",
                "jwks_uri": "https://idp.corpnet.ts.net/jwks",
                "registration_endpoint": "https://idp.corpnet.ts.net/register",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    settings = load_product_oidc_client_settings(
        {
            "auth": {
                "issuer_url": "https://idp.corpnet.ts.net",
                "client_id": "hermes-core",
                "client_secret_ref": "HERMES_PRODUCT_OIDC_CLIENT_SECRET",
            },
            "network": {
                "app_port": 8086,
                "tailscale": {
                    "enabled": True,
                    "tailnet_name": "corpnet",
                    "device_name": "hermes-box",
                    "idp_hostname": "idp",
                    "app_https_port": 443,
                },
            },
        }
    )

    metadata = discover_product_oidc_provider_metadata(settings, client=client)

    assert metadata.authorization_endpoint == "https://idp.corpnet.ts.net/authorize"
    assert metadata.token_endpoint == "https://idp.corpnet.ts.net/token"
    assert metadata.userinfo_endpoint == "https://idp.corpnet.ts.net/userinfo"
    assert metadata.jwks_uri == "https://idp.corpnet.ts.net/jwks"
    assert metadata.registration_endpoint == "https://idp.corpnet.ts.net/register"


def test_discover_product_oidc_provider_metadata_by_issuer_uses_registration_endpoint():
    clear_product_oidc_provider_metadata_cache()

    def _handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://idp.corpnet.ts.net/.well-known/openid-configuration"
        return httpx.Response(
            200,
            json={
                "issuer": "https://idp.corpnet.ts.net",
                "authorization_endpoint": "https://idp.corpnet.ts.net/authorize",
                "token_endpoint": "https://idp.corpnet.ts.net/token",
                "registration_endpoint": "https://idp.corpnet.ts.net/register",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(_handler))

    metadata = discover_product_oidc_provider_metadata_by_issuer(
        "https://idp.corpnet.ts.net",
        client=client,
    )

    assert metadata.registration_endpoint == "https://idp.corpnet.ts.net/register"


def test_create_oidc_login_request_uses_pkce_and_standard_scopes(monkeypatch):
    clear_product_oidc_provider_metadata_cache()
    monkeypatch.setattr("hermes_cli.product_oidc.secrets.token_urlsafe", lambda _n=0: "fixed-token")
    monkeypatch.setattr("hermes_cli.product_oidc.get_env_value", lambda key: "oidc-secret")
    settings = load_product_oidc_client_settings(
        {
            "auth": {
                "issuer_url": "https://idp.corpnet.ts.net",
                "client_id": "hermes-core",
                "client_secret_ref": "HERMES_PRODUCT_OIDC_CLIENT_SECRET",
            },
            "network": {
                "app_port": 8086,
                "tailscale": {
                    "enabled": True,
                    "tailnet_name": "corpnet",
                    "device_name": "hermes-box",
                    "idp_hostname": "idp",
                    "app_https_port": 443,
                },
            },
        }
    )
    metadata = discover_product_oidc_provider_metadata(
        settings,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "issuer": "https://idp.corpnet.ts.net",
                        "authorization_endpoint": "https://idp.corpnet.ts.net/authorize",
                        "token_endpoint": "https://idp.corpnet.ts.net/token",
                    },
                )
            )
        ),
    )

    login = create_oidc_login_request(
        settings,
        metadata,
        state="state-123",
        nonce="nonce-123",
        verifier="verifier-123",
    )

    parsed = urlparse(login["authorization_url"])
    params = parse_qs(parsed.query)
    assert login["state"] == "state-123"
    assert login["nonce"] == "nonce-123"
    assert login["verifier"] == "verifier-123"
    assert params["client_id"] == ["hermes-core"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["openid profile email"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == [create_pkce_challenge("verifier-123")]


def test_exchange_product_oidc_code_posts_expected_token_request(monkeypatch):
    clear_product_oidc_provider_metadata_cache()
    monkeypatch.setattr("hermes_cli.product_oidc.get_env_value", lambda key: "oidc-secret")
    seen = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json={"access_token": "access", "id_token": "id-token", "token_type": "Bearer"},
        )

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    settings = load_product_oidc_client_settings(
        {
            "auth": {
                "issuer_url": "https://idp.corpnet.ts.net",
                "client_id": "hermes-core",
                "client_secret_ref": "HERMES_PRODUCT_OIDC_CLIENT_SECRET",
            },
            "network": {
                "app_port": 8086,
                "tailscale": {
                    "enabled": True,
                    "tailnet_name": "corpnet",
                    "device_name": "hermes-box",
                    "idp_hostname": "idp",
                    "app_https_port": 443,
                },
            },
        }
    )
    metadata = discover_product_oidc_provider_metadata(
        settings,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "issuer": "https://idp.corpnet.ts.net",
                        "authorization_endpoint": "https://idp.corpnet.ts.net/authorize",
                        "token_endpoint": "https://idp.corpnet.ts.net/token",
                    },
                )
            )
        ),
    )

    tokens = exchange_product_oidc_code(
        settings,
        metadata,
        code="auth-code",
        verifier="verifier-123",
        client=client,
    )

    assert seen["url"] == "https://idp.corpnet.ts.net/token"
    assert "grant_type=authorization_code" in seen["body"]
    assert "code=auth-code" in seen["body"]
    assert "code_verifier=verifier-123" in seen["body"]
    assert "client_secret=oidc-secret" in seen["body"]
    assert tokens["access_token"] == "access"


def test_discover_product_oidc_provider_metadata_caches_default_client(monkeypatch):
    clear_product_oidc_provider_metadata_cache()
    monkeypatch.setattr("hermes_cli.product_oidc.get_env_value", lambda key: "oidc-secret")
    calls = {"count": 0}

    class _FakeClient:
        def __init__(self, timeout=10.0):
            self.timeout = timeout

        def get(self, url):
            calls["count"] += 1
            return httpx.Response(
                200,
                json={
                    "issuer": "https://idp.corpnet.ts.net",
                    "authorization_endpoint": "https://idp.corpnet.ts.net/authorize",
                    "token_endpoint": "https://idp.corpnet.ts.net/token",
                },
                request=httpx.Request("GET", url),
            )

        def close(self):
            return None

    monkeypatch.setattr("hermes_cli.product_oidc.httpx.Client", _FakeClient)
    settings = load_product_oidc_client_settings(
        {
            "auth": {
                "issuer_url": "https://idp.corpnet.ts.net",
                "client_id": "hermes-core",
                "client_secret_ref": "HERMES_PRODUCT_OIDC_CLIENT_SECRET",
            },
            "network": {
                "app_port": 8086,
                "tailscale": {
                    "enabled": True,
                    "tailnet_name": "corpnet",
                    "device_name": "hermes-box",
                    "idp_hostname": "idp",
                    "app_https_port": 443,
                },
            },
        }
    )

    first = discover_product_oidc_provider_metadata(settings)
    second = discover_product_oidc_provider_metadata(settings)

    assert first.authorization_endpoint == second.authorization_endpoint
    assert calls["count"] == 1
