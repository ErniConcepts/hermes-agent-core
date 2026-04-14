from types import SimpleNamespace

from starlette.testclient import TestClient

from hermes_cli.product_oidc import ProductOIDCClientSettings, ProductOIDCProviderMetadata
from hermes_cli.product_users import create_product_user_with_signup, deactivate_product_user
from hermes_cli.product_runtime import _workspace_root


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
        "bootstrap": {},
        "network": {
            "app_port": 8086,
            "trusted_proxy_ips": ["127.0.0.1", "::1"],
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


def _wsl_browser_product_config():
    config = _product_config()
    config["auth"]["issuer_url"] = "https://idp-2.tail5fd7a5.ts.net"
    config["network"]["tailscale"]["device_name"] = "wsl-device"
    config["network"]["tailscale"]["app_device_name"] = "windows-device"
    config["network"]["tailscale"]["app_command_path"] = "/mnt/c/Program Files/Tailscale/tailscale.exe"
    config["network"]["tailscale"]["browser_host_mode"] = "windows_tailscale"
    return config


def _wsl_browser_urls():
    urls = _urls()
    urls["app_base_url"] = "https://windows-device.tail5fd7a5.ts.net"
    urls["oidc_callback_url"] = "https://windows-device.tail5fd7a5.ts.net/api/auth/oidc/callback"
    urls["tailnet_host"] = "windows-device.tail5fd7a5.ts.net"
    urls["tailnet_app_base_url"] = "https://windows-device.tail5fd7a5.ts.net"
    urls["issuer_url"] = "https://idp-2.tail5fd7a5.ts.net"
    urls["tailnet_issuer_url"] = "https://idp-2.tail5fd7a5.ts.net"
    return urls


def _configure_app(monkeypatch, tmp_path, claims):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET", "oidc-secret")
    monkeypatch.setattr("hermes_cli.product_app_support.enforce_product_auth_rate_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.product_app_support.load_product_config", _product_config)
    monkeypatch.setattr("hermes_cli.product_app_support.resolve_product_urls", lambda config=None: _urls())
    monkeypatch.setattr("hermes_cli.product_app_support.load_product_oidc_client_settings", lambda config=None: _oidc_settings())
    monkeypatch.setattr("hermes_cli.product_app_support.discover_product_oidc_provider_metadata", lambda settings: _oidc_metadata())
    monkeypatch.setattr(
        "hermes_cli.product_app_support.exchange_product_oidc_code",
        lambda settings, metadata, code, verifier: {"access_token": "token", "id_token": "id-token"},
    )
    monkeypatch.setattr("hermes_cli.product_app_support.validate_product_oidc_id_token", lambda *args, **kwargs: claims)
    monkeypatch.setattr("hermes_cli.product_app_support.fetch_product_oidc_userinfo", lambda *args, **kwargs: claims)
    monkeypatch.setattr(
        "hermes_cli.product_app_support.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "https://idp.tail5fd7a5.ts.net/authorize?state=state-123",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app_support.load_first_admin_enrollment_state",
        lambda: {
            "bootstrap_token": "bootstrap-token-123",
            "bootstrap_url": "https://device.tail5fd7a5.ts.net/bootstrap/bootstrap-token-123",
            "first_admin_login_seen": False,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app_support.mark_first_admin_bootstrap_completed",
        lambda tailscale_login=None: {
            "tailscale_login": tailscale_login,
            "first_admin_login_seen": True,
        },
    )


def test_product_app_login_proxies_tsidp_authorize_for_wsl_browser_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET", "oidc-secret")
    monkeypatch.setattr("hermes_cli.product_app_support.enforce_product_auth_rate_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.product_app_support.load_product_config", _wsl_browser_product_config)
    monkeypatch.setattr("hermes_cli.product_app_support.resolve_product_urls", lambda config=None: _wsl_browser_urls())
    monkeypatch.setattr(
        "hermes_cli.product_app_support.load_product_oidc_client_settings",
        lambda config=None: ProductOIDCClientSettings(
            issuer_url="https://idp-2.tail5fd7a5.ts.net",
            client_id="hermes-core",
            client_secret="secret",
            redirect_uri="https://windows-device.tail5fd7a5.ts.net/api/auth/oidc/callback",
            scopes=("openid", "profile", "email"),
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.product_app_support.discover_product_oidc_provider_metadata",
        lambda settings: ProductOIDCProviderMetadata(
            issuer="https://idp-2.tail5fd7a5.ts.net",
            authorization_endpoint="https://idp-2.tail5fd7a5.ts.net/authorize",
            token_endpoint="https://idp-2.tail5fd7a5.ts.net/token",
            userinfo_endpoint="https://idp-2.tail5fd7a5.ts.net/userinfo",
            jwks_uri="https://idp-2.tail5fd7a5.ts.net/jwks",
        ),
    )
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://windows-device.tail5fd7a5.ts.net")
    response = client.get("/api/auth/login", follow_redirects=False)

    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith("https://windows-device.tail5fd7a5.ts.net/_hermes/tsidp/authorize?")
    assert "redirect_uri=https%3A%2F%2Fwindows-device.tail5fd7a5.ts.net%2Fapi%2Fauth%2Foidc%2Fcallback" in location


def test_tsidp_browser_proxy_rewrites_relative_redirects(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app_support.resolve_product_urls", lambda config=None: _wsl_browser_urls())
    from hermes_cli.product_app_support import _rewrite_tsidp_browser_location

    assert (
        _rewrite_tsidp_browser_location("/login", _wsl_browser_product_config())
        == "https://windows-device.tail5fd7a5.ts.net/_hermes/tsidp/login"
    )


def test_tsidp_browser_proxy_strips_upstream_cookie_domain():
    from hermes_cli.product_app_support import _rewrite_tsidp_set_cookie

    assert (
        _rewrite_tsidp_set_cookie("sid=1; Path=/; Domain=idp-2.tail5fd7a5.ts.net; HttpOnly")
        == "sid=1; Path=/; HttpOnly"
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


def test_product_app_invite_claim_page_includes_pending_ui(tmp_path, monkeypatch):
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
    assert "Claiming..." in response.text
    assert "Claiming account..." in response.text
    assert "isClaimingInvite" in response.text
    assert "buttonSpin" in response.text


def test_product_app_chat_error_keeps_user_message_visible(tmp_path, monkeypatch):
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
    assert "fallbackConversation" in response.text
    assert "Your message was not saved." in response.text


def test_product_app_chat_script_tracks_reasoning_and_visible_answer_separately(tmp_path, monkeypatch):
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
    assert "name==='delta'||name==='answer'" in response.text
    assert "pending.answerText" in response.text
    assert "pending-answer" in response.text


def test_product_app_uses_configured_branding_and_no_brand_dot(tmp_path, monkeypatch):
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

    def _branded_config():
        config = _product_config()
        config["product"]["brand"]["name"] = "Atlas Core"
        return config

    monkeypatch.setattr("hermes_cli.product_app_support.load_product_config", _branded_config)
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")
    response = client.get("/")

    assert response.status_code == 200
    assert "<title>Atlas Core</title>" in response.text
    assert '<span id="brandName">Atlas Core</span>' in response.text
    assert "brand-mark" not in response.text


def test_product_app_bootstraps_first_admin_from_bootstrap_link(tmp_path, monkeypatch):
    claims = {
        "sub": "ts-admin-sub",
        "email": "admin@example.com",
        "preferred_username": "admin@example.com",
        "name": "Admin Example",
    }
    _configure_app(monkeypatch, tmp_path, claims)
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")
    start = client.get("/bootstrap/bootstrap-token-123", follow_redirects=False)
    assert start.status_code == 303
    login = client.get(start.headers["location"], follow_redirects=False)
    assert login.status_code == 307

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
    client.get("/bootstrap/bootstrap-token-123", follow_redirects=False)
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)

    stranger_claims = {
        "sub": "ts-other-sub",
        "email": "other@example.com",
        "preferred_username": "other@example.com",
        "name": "Other User",
    }
    monkeypatch.setattr("hermes_cli.product_app_support.validate_product_oidc_id_token", lambda *args, **kwargs: stranger_claims)
    monkeypatch.setattr("hermes_cli.product_app_support.fetch_product_oidc_userinfo", lambda *args, **kwargs: stranger_claims)

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
    client.get("/bootstrap/bootstrap-token-123", follow_redirects=False)
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)
    client.post(
        "/api/auth/logout",
        headers={"Origin": "https://device.tail5fd7a5.ts.net", "X-Hermes-CSRF-Token": client.get("/api/auth/session").json()["csrf_token"]},
    )
    invite = create_product_user_with_signup(display_name="Bob Example").signup
    invited_claims = {
        "sub": "ts-user-sub",
        "email": "bob@example.com",
        "preferred_username": "bob@example.com",
        "name": "Bob Example",
    }
    monkeypatch.setattr("hermes_cli.product_app_support.validate_product_oidc_id_token", lambda *args, **kwargs: invited_claims)
    monkeypatch.setattr("hermes_cli.product_app_support.fetch_product_oidc_userinfo", lambda *args, **kwargs: invited_claims)

    start = client.get(f"/invite/{invite.token}", follow_redirects=False)
    assert start.status_code == 303
    client.get(start.headers["location"], follow_redirects=False)
    response = client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)

    assert response.status_code == 303
    session = client.get("/api/auth/session").json()
    assert session["authenticated"] is False
    assert session["pending_invite_claim"] is True
    assert session["pending_invite_display_name"] == "Bob Example"
    assert session["detected_tailscale_login"] == "bob@example.com"

    claimed = client.post(
        "/api/auth/invite/claim",
        headers={"Origin": "https://device.tail5fd7a5.ts.net", "X-Hermes-CSRF-Token": session["csrf_token"]},
    )
    assert claimed.status_code == 200
    assert claimed.json()["authenticated"] is True
    assert claimed.json()["user"]["tailscale_login"] == "bob@example.com"


def test_product_app_invite_rejects_existing_user_identity(tmp_path, monkeypatch):
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
    client.get("/bootstrap/bootstrap-token-123", follow_redirects=False)
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)
    invite = create_product_user_with_signup(display_name="Bob Example").signup

    start = client.get(f"/invite/{invite.token}", follow_redirects=False)
    assert start.status_code == 303
    login = client.get(start.headers["location"], follow_redirects=False)
    assert login.status_code == 307
    response = client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)

    assert response.status_code == 303
    session = client.get("/api/auth/session").json()
    assert session["authenticated"] is False
    assert session["pending_invite_claim"] is False
    assert session["detected_tailscale_login"] == "admin@example.com"
    assert session["notice"] == "This Tailscale account already belongs to an existing Hermes Core user. Use a different Tailscale account to claim this invite."


def test_product_app_admin_creates_generic_invite_link(tmp_path, monkeypatch):
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
        "hermes_cli.product_app_admin_routes._require_admin_user",
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
    monkeypatch.setattr("hermes_cli.product_app_admin_routes._require_csrf", lambda request: None)
    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")

    response = client.post(
        "/api/admin/users",
        json={"display_name": "Alice Example"},
    )

    assert response.status_code == 200
    assert response.json()["signup"]["signup_url"].startswith("https://device.tail5fd7a5.ts.net/invite/")


def test_product_app_invalidates_disabled_session_without_waiting_for_refresh_ttl(tmp_path, monkeypatch):
    claims = {
        "sub": "ts-admin-sub",
        "email": "admin@example.com",
        "preferred_username": "admin@example.com",
        "name": "Admin Example",
    }
    _configure_app(monkeypatch, tmp_path, claims)
    monkeypatch.setattr("hermes_cli.product_users.resolve_product_urls", lambda config=None: _urls())
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")
    client.get("/bootstrap/bootstrap-token-123", follow_redirects=False)
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)
    client.post(
        "/api/auth/logout",
        headers={"Origin": "https://device.tail5fd7a5.ts.net", "X-Hermes-CSRF-Token": client.get("/api/auth/session").json()["csrf_token"]},
    )
    invite = create_product_user_with_signup(display_name="Bob Example").signup
    invited_claims = {
        "sub": "ts-user-sub",
        "email": "bob@example.com",
        "preferred_username": "bob@example.com",
        "name": "Bob Example",
    }
    monkeypatch.setattr("hermes_cli.product_app_support.validate_product_oidc_id_token", lambda *args, **kwargs: invited_claims)
    monkeypatch.setattr("hermes_cli.product_app_support.fetch_product_oidc_userinfo", lambda *args, **kwargs: invited_claims)

    start = client.get(f"/invite/{invite.token}", follow_redirects=False)
    client.get(start.headers["location"], follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)
    session = client.get("/api/auth/session").json()
    claimed = client.post(
        "/api/auth/invite/claim",
        headers={"Origin": "https://device.tail5fd7a5.ts.net", "X-Hermes-CSRF-Token": session["csrf_token"]},
    )
    claimed_user_id = claimed.json()["user"]["id"]

    deactivate_product_user(claimed_user_id)
    post_disable = client.get("/api/auth/session").json()

    assert post_disable["authenticated"] is False


def test_product_app_stop_route_proxies_runtime_interrupt(tmp_path, monkeypatch):
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
    monkeypatch.setattr(
        "hermes_cli.product_app_chat_routes._require_product_user",
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
    monkeypatch.setattr("hermes_cli.product_app_chat_routes._require_csrf", lambda request: None)
    monkeypatch.setattr("hermes_cli.product_app_chat_routes.stop_product_chat_turn", lambda *args, **kwargs: True)
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")
    response = client.post("/api/chat/turn/stop")

    assert response.status_code == 200
    assert response.json() == {"stopped": True}


def test_product_app_chat_stream_uses_transport_layer(tmp_path, monkeypatch):
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
    monkeypatch.setattr(
        "hermes_cli.product_app_chat_routes._require_product_user",
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
    monkeypatch.setattr("hermes_cli.product_app_chat_routes._require_csrf", lambda request: None)
    seen = {}

    def _stream(_user, user_message, *, config=None):
        seen["user_message"] = user_message
        yield "event: final\ndata: {}\n\n"

    monkeypatch.setattr("hermes_cli.product_app_chat_routes.stream_product_chat_turn", _stream)
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")
    response = client.post("/api/chat/turn/stream", json={"user_message": "hello"})

    assert response.status_code == 200
    assert seen["user_message"] == "hello"


def test_product_app_rejects_first_admin_without_bootstrap_link(tmp_path, monkeypatch):
    claims = {
        "sub": "ts-admin-sub",
        "email": "admin@example.com",
        "preferred_username": "admin@example.com",
        "name": "Admin Example",
    }
    _configure_app(monkeypatch, tmp_path, claims)
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")
    client.get("/api/auth/login", follow_redirects=False)
    response = client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)

    assert response.status_code == 303
    session = client.get("/api/auth/session").json()
    assert session["authenticated"] is False
    assert session["notice"] == "Open the one-time bootstrap link from setup to create the first admin."


def test_product_app_downloads_workspace_file_for_signed_in_user(tmp_path, monkeypatch):
    claims = {
        "sub": "ts-admin-sub",
        "email": "admin@example.com",
        "preferred_username": "admin@example.com",
        "name": "Admin Example",
    }
    _configure_app(monkeypatch, tmp_path, claims)
    from hermes_cli.product_app import create_product_app

    client = TestClient(create_product_app(), base_url="https://device.tail5fd7a5.ts.net")
    client.get("/bootstrap/bootstrap-token-123", follow_redirects=False)
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=ok&state=state-123", follow_redirects=False)
    session = client.get("/api/auth/session").json()
    user_id = session["user"]["sub"]
    workspace_root = _workspace_root(_product_config(), user_id)
    workspace_root.mkdir(parents=True, exist_ok=True)
    target = workspace_root / "notes.txt"
    target.write_text("tailnet download ok", encoding="utf-8")

    response = client.get("/api/workspace/download", params={"path": "notes.txt"})

    assert response.status_code == 200
    assert response.content == b"tailnet download ok"
    assert "attachment; filename=\"notes.txt\"" in response.headers["content-disposition"]


def test_product_app_healthz_minimizes_public_payload(tmp_path, monkeypatch):
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
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_product_app_session_exposes_longer_csrf_token(tmp_path, monkeypatch):
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
    session = client.get("/api/auth/session").json()

    assert len(session["csrf_token"]) >= 43


def test_client_ip_uses_forwarded_for_from_trusted_proxy(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app_support.load_product_config", _product_config)
    from hermes_cli.product_app_support import _client_ip

    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"X-Forwarded-For": "198.51.100.9, 127.0.0.1"},
    )

    assert _client_ip(request) == "198.51.100.9"


def test_client_ip_uses_real_ip_for_trusted_proxy_when_forwarded_for_missing(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app_support.load_product_config", _product_config)
    from hermes_cli.product_app_support import _client_ip

    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"X-Real-IP": "198.51.100.10"},
    )

    assert _client_ip(request) == "198.51.100.10"


def test_client_ip_ignores_forwarded_headers_from_untrusted_peer(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app_support.load_product_config", _product_config)
    from hermes_cli.product_app_support import _client_ip

    request = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.20"),
        headers={"X-Forwarded-For": "198.51.100.11", "X-Real-IP": "198.51.100.12"},
    )

    assert _client_ip(request) == "203.0.113.20"


def test_client_ip_falls_back_to_peer_for_malformed_forwarded_headers(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app_support.load_product_config", _product_config)
    from hermes_cli.product_app_support import _client_ip

    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"X-Forwarded-For": "not-an-ip", "X-Real-IP": "still-not-an-ip"},
    )

    assert _client_ip(request) == "127.0.0.1"


def test_product_app_requires_dedicated_session_secret(tmp_path, monkeypatch):
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
    monkeypatch.delenv("HERMES_PRODUCT_SESSION_SECRET", raising=False)

    from hermes_cli.product_app import _session_secret

    try:
        _session_secret()
    except RuntimeError as exc:
        assert "HERMES_PRODUCT_SESSION_SECRET" in str(exc)
    else:
        raise AssertionError("Expected product app session secret lookup to fail closed")


def test_create_product_auth_proxy_app_compatibility_factory(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("hermes_cli.product_app.create_product_app", lambda: sentinel)

    from hermes_cli.product_app import create_product_auth_proxy_app

    assert create_product_auth_proxy_app() is sentinel
