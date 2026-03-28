from starlette.testclient import TestClient

from hermes_cli.product_oidc import ProductOIDCClientSettings, ProductOIDCProviderMetadata
from hermes_cli.product_users import create_product_user_with_signup


def _product_config():
    return {
        "product": {"brand": {"name": "Hermes Core"}},
        "auth": {
            "provider": "tsidp",
            "issuer_url": "https://idp.tail5fd7a5.ts.net",
            "client_id": "hermes-core",
            "client_secret_ref": "HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET",
            "session_secret_ref": "HERMES_PRODUCT_SESSION_SECRET",
        },
        "bootstrap": {"first_admin_tailscale_login": "admin@example.com"},
        "network": {
            "app_port": 8086,
            "tailscale": {
                "enabled": True,
                "tailnet_name": "tail5fd7a5",
                "device_name": "device",
                "idp_hostname": "idp",
                "app_https_port": 443,
            },
        },
    }


def _urls():
    return {
        "app_base_url": "https://device.tail5fd7a5.ts.net",
        "issuer_url": "https://idp.tail5fd7a5.ts.net",
        "oidc_callback_url": "https://device.tail5fd7a5.ts.net/api/auth/oidc/callback",
        "tailnet_host": "device.tail5fd7a5.ts.net",
        "tailnet_app_base_url": "https://device.tail5fd7a5.ts.net",
        "tailnet_issuer_url": "https://idp.tail5fd7a5.ts.net",
        "tailnet_activation_status": "active",
        "tailnet_active": True,
        "local_app_base_url": "http://127.0.0.1:8086",
    }


def _oidc_settings():
    return ProductOIDCClientSettings(
        issuer_url="https://idp.tail5fd7a5.ts.net",
        client_id="hermes-core",
        client_secret="secret",
        redirect_uri="https://device.tail5fd7a5.ts.net/api/auth/oidc/callback",
        scopes=("openid", "profile", "email"),
    )


def _oidc_metadata():
    return ProductOIDCProviderMetadata(
        issuer="https://idp.tail5fd7a5.ts.net",
        authorization_endpoint="https://idp.tail5fd7a5.ts.net/authorize",
        token_endpoint="https://idp.tail5fd7a5.ts.net/token",
        userinfo_endpoint="https://idp.tail5fd7a5.ts.net/userinfo",
        jwks_uri="https://idp.tail5fd7a5.ts.net/jwks",
    )


def _configure_app(monkeypatch, tmp_path, claims):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET", "oidc-secret")
    monkeypatch.setattr("hermes_cli.product_app.load_product_config", _product_config)
    monkeypatch.setattr("hermes_cli.product_app.resolve_product_urls", lambda config=None: _urls())
    monkeypatch.setattr("hermes_cli.product_app.load_product_oidc_client_settings", lambda config=None: _oidc_settings())
    monkeypatch.setattr("hermes_cli.product_app.discover_product_oidc_provider_metadata", lambda settings: _oidc_metadata())
    monkeypatch.setattr(
        "hermes_cli.product_app.exchange_product_oidc_code",
        lambda settings, metadata, code, verifier: {"access_token": "token", "id_token": "id-token"},
    )
    monkeypatch.setattr("hermes_cli.product_app.validate_product_oidc_id_token", lambda *args, **kwargs: claims)
    monkeypatch.setattr("hermes_cli.product_app.fetch_product_oidc_userinfo", lambda *args, **kwargs: claims)
    monkeypatch.setattr(
        "hermes_cli.product_app.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "https://idp.tail5fd7a5.ts.net/authorize?state=state-123",
        },
    )


def test_product_app_signed_out_page_uses_tailscale_login(tmp_path, monkeypatch):
    _configure_app(
        monkeypatch,
        tmp_path,
        {
            "sub": "ts-sub",
            "email": "admin@example.com",
            "preferred_username": "admin@example.com",
            "name": "Admin Example",
        },
    )
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")
    response = client.get("/")

    assert response.status_code == 200
    assert "Sign in with Tailscale" in response.text
    assert "Pocket ID" not in response.text


def test_product_app_bootstraps_first_admin_from_matching_tailscale_identity(tmp_path, monkeypatch):
    claims = {
        "sub": "ts-admin-sub",
        "email": "admin@example.com",
        "preferred_username": "admin@example.com",
        "name": "Admin Example",
    }
    _configure_app(monkeypatch, tmp_path, claims)
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")
    start = client.get("/api/auth/login", follow_redirects=False)
    assert start.status_code == 307

    response = client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)

    assert response.status_code == 303
    session = client.get("/api/auth/session").json()
    assert session["authenticated"] is True
    assert session["user"]["is_admin"] is True
    assert session["user"]["tailscale_login"] == "admin@example.com"


def test_product_app_rejects_uninvited_tailscale_identity_after_bootstrap(tmp_path, monkeypatch):
    admin_claims = {
        "sub": "ts-admin-sub",
        "email": "admin@example.com",
        "preferred_username": "admin@example.com",
        "name": "Admin Example",
    }
    _configure_app(monkeypatch, tmp_path, admin_claims)
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)

    stranger_claims = {
        "sub": "ts-other-sub",
        "email": "other@example.com",
        "preferred_username": "other@example.com",
        "name": "Other User",
    }
    monkeypatch.setattr("hermes_cli.product_app.validate_product_oidc_id_token", lambda *args, **kwargs: stranger_claims)
    monkeypatch.setattr("hermes_cli.product_app.fetch_product_oidc_userinfo", lambda *args, **kwargs: stranger_claims)

    client.post(
        "/api/auth/logout",
        headers={"Origin": "https://device.tail5fd7a5.ts.net", "X-Hermes-CSRF-Token": client.get("/api/auth/session").json()["csrf_token"]},
    )
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)

    session = client.get("/api/auth/session").json()
    assert session["authenticated"] is False
    assert session["notice"] == "This Tailscale account is not invited to this app."
    assert session["detected_tailscale_login"] == "other@example.com"


def test_product_app_claims_pending_invite_on_oidc_callback(tmp_path, monkeypatch):
    admin_claims = {
        "sub": "ts-admin-sub",
        "email": "admin@example.com",
        "preferred_username": "admin@example.com",
        "name": "Admin Example",
    }
    _configure_app(monkeypatch, tmp_path, admin_claims)
    monkeypatch.setattr("hermes_cli.product_users.resolve_product_urls", lambda config=None: _urls())
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)
    client.post(
        "/api/auth/logout",
        headers={"Origin": "https://device.tail5fd7a5.ts.net", "X-Hermes-CSRF-Token": client.get("/api/auth/session").json()["csrf_token"]},
    )
    invite = create_product_user_with_signup(
        username="bob@example.com",
        display_name="Bob Example",
        email="bob@example.com",
    ).signup
    invited_claims = {
        "sub": "ts-user-sub",
        "email": "bob@example.com",
        "preferred_username": "bob@example.com",
        "name": "Bob Example",
    }
    monkeypatch.setattr("hermes_cli.product_app.validate_product_oidc_id_token", lambda *args, **kwargs: invited_claims)
    monkeypatch.setattr("hermes_cli.product_app.fetch_product_oidc_userinfo", lambda *args, **kwargs: invited_claims)

    start = client.get(f"/invite/{invite.token}", follow_redirects=False)
    assert start.status_code == 303
    client.get(start.headers["location"], follow_redirects=False)
    response = client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)

    assert response.status_code == 303
    session = client.get("/api/auth/session").json()
    assert session["authenticated"] is True
    assert session["user"]["tailscale_login"] == "bob@example.com"


def test_product_app_admin_creates_invite_link_for_tailscale_login(tmp_path, monkeypatch):
    claims = {
        "sub": "ts-admin-sub",
        "email": "admin@example.com",
        "preferred_username": "admin@example.com",
        "name": "Admin Example",
    }
    _configure_app(monkeypatch, tmp_path, claims)
    monkeypatch.setattr("hermes_cli.product_users.resolve_product_urls", lambda config=None: _urls())
    from hermes_cli.product_app import create_product_app

    monkeypatch.setattr(
        "hermes_cli.product_app._require_admin_user",
        lambda request: {
            "id": "user-admin",
            "sub": "user-admin",
            "name": "Admin Example",
            "preferred_username": "admin",
            "email": "admin@example.com",
            "is_admin": True,
            "tailscale_login": "admin@example.com",
        },
    )
    monkeypatch.setattr("hermes_cli.product_app._require_csrf", lambda request: None)
    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")

    response = client.post(
        "/api/admin/users",
        json={"tailscale_login": "alice@example.com", "display_name": "Alice Example"},
    )

    assert response.status_code == 200
    assert response.json()["signup"]["signup_url"].startswith("https://device.tail5fd7a5.ts.net/invite/")
