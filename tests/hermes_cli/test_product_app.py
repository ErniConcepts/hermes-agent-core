import pytest
from fastapi.testclient import TestClient

from hermes_cli.product_app import create_product_app


def _csrf_headers(client):
    payload = client.get("/api/auth/session").json()
    return {
        "X-Hermes-CSRF-Token": payload["csrf_token"],
        "Origin": "http://officebox.local:8086",
    }


@pytest.fixture(autouse=True)
def _clear_auth_rate_limits():
    from hermes_cli import product_app

    product_app._AUTH_RATE_LIMITS.clear()
    yield
    product_app._AUTH_RATE_LIMITS.clear()


def test_product_app_index_shows_login_link_when_signed_out(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"product": {"brand": {"name": "Hermes Core"}}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    client = TestClient(create_product_app())
    response = client.get("/")

    assert response.status_code == 200
    assert "Sign in with Pocket ID" in response.text
    assert "Loading your workspace." in response.text
    assert "Your Agent" in response.text
    assert "User Management" in response.text
    assert 'id="workspaceUploadForm"' in response.text
    assert 'id="workspaceUsageBar"' in response.text
    assert 'id="sessionCard"' not in response.text
    assert 'id="chatForm"' in response.text


def test_product_app_index_escapes_product_name(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"product": {"brand": {"name": '<script>alert("x")</script>'}}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "https://officebox.local:8086", "issuer_url": "https://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    client = TestClient(create_product_app())
    response = client.get("/")

    assert response.status_code == 200
    assert '<script>alert("x")</script>' not in response.text
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in response.text


def test_session_secret_prefers_dedicated_product_secret(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"auth": {"session_secret_ref": "HERMES_PRODUCT_SESSION_SECRET"}},
    )
    monkeypatch.setattr("hermes_cli.product_app.get_env_value", lambda key: "dedicated-secret")

    assert __import__("hermes_cli.product_app", fromlist=["_session_secret"])._session_secret() == "dedicated-secret"


def test_product_app_uses_secure_session_cookie_for_https_urls(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app.load_product_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "https://officebox.local:8086", "issuer_url": "https://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    client = TestClient(create_product_app())
    response = client.get("/api/auth/session")

    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_product_app_configures_explicit_session_max_age(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app.load_product_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    client = TestClient(create_product_app())
    response = client.get("/api/auth/session")

    assert response.status_code == 200
    assert "max-age=43200" in response.headers["set-cookie"].lower()


def test_product_app_healthz_reports_auth_provider(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {
            "auth": {"provider": "pocket-id", "issuer_url": "http://officebox.local:1411"},
            "network": {"public_host": "officebox.local", "app_port": 8086, "pocket_id_port": 1411},
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {
            "app_base_url": "http://officebox.local:8086",
            "issuer_url": "http://officebox.local:1411",
        },
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    client = TestClient(create_product_app())
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "auth_provider": "pocket-id",
        "issuer_url": "http://officebox.local:1411",
        "app_base_url": "http://officebox.local:8086",
    }


def test_product_app_login_redirects_and_stores_pending_pkce(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app.load_product_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_oidc_client_settings",
        lambda config=None: object(),
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.discover_product_oidc_provider_metadata",
        lambda settings: object(),
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "http://officebox.local:1411/authorize?client_id=hermes-core",
        },
    )

    client = TestClient(create_product_app())
    response = client.get("/api/auth/login", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "http://officebox.local:1411/authorize?client_id=hermes-core"

    session = client.get("/api/auth/session")
    payload = session.json()
    assert payload["authenticated"] is False
    assert payload["user"] is None
    assert payload["csrf_token"]


def test_product_app_login_rate_limits_after_repeated_attempts(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app.load_product_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr("hermes_cli.product_app.load_product_oidc_client_settings", lambda config=None: object())
    monkeypatch.setattr("hermes_cli.product_app.discover_product_oidc_provider_metadata", lambda settings: object())
    monkeypatch.setattr(
        "hermes_cli.product_app.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "http://officebox.local:1411/authorize?client_id=hermes-core",
        },
    )

    client = TestClient(create_product_app())
    for _ in range(10):
        response = client.get("/api/auth/login", follow_redirects=False)
        assert response.status_code == 307
    blocked = client.get("/api/auth/login", follow_redirects=False)

    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many authentication requests"}


def test_product_app_allows_tailnet_bridge_path_when_enabled(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app.load_product_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {
            "app_base_url": "http://localhost:8086",
            "issuer_url": "http://localhost:1411",
            "local_app_base_url": "http://localhost:8086",
            "local_issuer_url": "http://localhost:1411",
            "tailnet_host": "laptopjannis.tail5fd7a5.ts.net",
            "tailnet_app_base_url": "https://laptopjannis.tail5fd7a5.ts.net",
            "tailnet_issuer_url": "https://laptopjannis.tail5fd7a5.ts.net:4444",
            "tailnet_activation_status": "active",
            "tailnet_active": True,
        },
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr(
        "hermes_cli.product_app.consume_tailnet_bridge_token",
        lambda token, target_origin: {"user_id": "user-1"} if token == "good-token" else None,
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_user_by_id",
        lambda user_id: type(
            "U",
            (),
            {"id": user_id, "username": "admin", "display_name": "Admin", "email": "admin@example.com", "is_admin": True, "disabled": False},
        )(),
    )

    client = TestClient(create_product_app())
    response = client.get(
        "https://laptopjannis.tail5fd7a5.ts.net/auth/bridge?token=good-token",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "https://laptopjannis.tail5fd7a5.ts.net"


def test_product_app_active_tailnet_request_is_allowed(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app.load_product_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {
            "app_base_url": "http://localhost:8086",
            "issuer_url": "http://localhost:1411",
            "local_app_base_url": "http://localhost:8086",
            "local_issuer_url": "http://localhost:1411",
            "tailnet_host": "laptopjannis.tail5fd7a5.ts.net",
            "tailnet_app_base_url": "https://laptopjannis.tail5fd7a5.ts.net",
            "tailnet_issuer_url": "https://laptopjannis.tail5fd7a5.ts.net:4444",
            "tailnet_activation_status": "active",
            "tailnet_active": True,
        },
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    client = TestClient(create_product_app())
    response = client.get("https://laptopjannis.tail5fd7a5.ts.net/", follow_redirects=False)

    assert response.status_code == 200
    assert "Sign in with Pocket ID" in response.text


def test_product_app_exposes_admin_network_state(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app._require_admin_user", lambda request: {"sub": "user-1", "is_admin": True})
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {
            "app_base_url": "http://officebox.local:8086",
            "issuer_url": "http://officebox.local:1411",
            "local_app_base_url": "http://officebox.local:8086",
            "local_issuer_url": "http://officebox.local:1411",
            "tailnet_host": "hermes-box.corpnet.ts.net",
            "tailnet_app_base_url": "https://hermes-box.corpnet.ts.net",
            "tailnet_issuer_url": "https://hermes-box.corpnet.ts.net:4444",
            "tailnet_activation_status": "inactive",
            "tailnet_active": False,
        },
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    client = TestClient(create_product_app())
    response = client.get("/api/admin/network")

    assert response.status_code == 200
    assert response.json()["activation_status"] == "inactive"
    assert response.json()["tailnet_app_base_url"] == "https://hermes-box.corpnet.ts.net"


def test_product_app_admin_enable_tailnet_returns_network_state(monkeypatch):
    seen = []
    monkeypatch.setattr("hermes_cli.product_app._require_admin_user", lambda request: {"sub": "user-1", "is_admin": True})
    monkeypatch.setattr("hermes_cli.product_app._require_csrf", lambda request: None)
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {
            "app_base_url": "http://officebox.local:8086",
            "issuer_url": "http://officebox.local:1411",
            "local_app_base_url": "http://officebox.local:8086",
            "local_issuer_url": "http://officebox.local:1411",
            "tailnet_host": "hermes-box.corpnet.ts.net",
            "tailnet_app_base_url": "https://hermes-box.corpnet.ts.net",
            "tailnet_issuer_url": "https://hermes-box.corpnet.ts.net:4444",
            "tailnet_activation_status": "active",
            "tailnet_active": True,
        },
    )
    monkeypatch.setattr("hermes_cli.product_app.enable_tailnet_activation", lambda: seen.append("enabled"))
    client = TestClient(create_product_app())
    response = client.post(
        "/api/admin/network/tailscale/enable",
        json={},
    )

    assert response.status_code == 200
    assert response.json()["activation_status"] == "active"
    assert seen == ["enabled"]


def test_product_app_login_short_circuits_when_already_authenticated(monkeypatch):
    _patch_admin_session(monkeypatch)

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.get("/api/auth/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "http://officebox.local:8086"


def test_product_app_callback_establishes_session(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"bootstrap": {"first_admin_username": "admin"}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr("hermes_cli.product_app.load_product_oidc_client_settings", lambda config=None: object())
    monkeypatch.setattr("hermes_cli.product_app.discover_product_oidc_provider_metadata", lambda settings: object())
    monkeypatch.setattr(
        "hermes_cli.product_app.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "http://officebox.local:1411/authorize?client_id=hermes-core",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.exchange_product_oidc_code",
        lambda settings, metadata, code, verifier: {"access_token": "access-token"},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.fetch_product_oidc_userinfo",
        lambda access_token, metadata: {
            "sub": "user-1",
            "email": "admin@example.com",
            "name": "Admin User",
            "preferred_username": "admin",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_user_by_id",
        lambda user_id: type(
            "U",
            (),
            {
                "id": user_id,
                "username": "admin",
                "display_name": "Admin User",
                "email": "admin@example.com",
                "is_admin": True,
                "disabled": False,
            },
        )(),
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.mark_first_admin_bootstrap_completed",
        lambda: seen.append("marked"),
    )

    client = TestClient(create_product_app())
    client.get("/api/auth/login", follow_redirects=False)
    callback = client.get("/api/auth/oidc/callback?code=auth-code&state=state-123", follow_redirects=False)

    assert callback.status_code == 303
    assert callback.headers["location"] == "http://officebox.local:8086"

    session = client.get("/api/auth/session")
    assert session.status_code == 200
    payload = session.json()
    assert payload["authenticated"] is True
    assert payload["csrf_token"]
    assert payload["user"] == {
        "id": "user-1",
        "sub": "user-1",
        "email": "admin@example.com",
        "name": "Admin User",
        "preferred_username": "admin",
        "email_verified": True,
        "is_admin": True,
    }
    assert seen == ["marked", "marked"]


def test_product_app_account_bridge_returns_tailnet_app_redirect(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app._require_product_user", lambda request: {"sub": "user-1", "is_admin": False})
    monkeypatch.setattr("hermes_cli.product_app._require_csrf", lambda request: None)
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr("hermes_cli.product_app.create_tailnet_bridge_token", lambda user_id, target_origin: {"token": "abc"})
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {
            "app_base_url": "http://officebox.local:8086",
            "issuer_url": "http://officebox.local:1411",
            "local_app_base_url": "http://officebox.local:8086",
            "local_issuer_url": "http://officebox.local:1411",
            "tailnet_host": "hermes-box.corpnet.ts.net",
            "tailnet_app_base_url": "https://hermes-box.corpnet.ts.net",
            "tailnet_issuer_url": "https://hermes-box.corpnet.ts.net:4444",
            "tailnet_activation_status": "active",
            "tailnet_active": True,
        },
    )
    client = TestClient(create_product_app())
    response = client.post("/api/account/network/tailscale/bridge", json={})

    assert response.status_code == 200
    assert response.json() == {"redirect_url": "https://hermes-box.corpnet.ts.net/auth/bridge?token=abc"}


def test_product_app_auto_logs_in_from_bound_tailnet_identity(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app.load_product_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {
            "app_base_url": "http://localhost:8086",
            "issuer_url": "http://localhost:1411",
            "local_app_base_url": "http://localhost:8086",
            "local_issuer_url": "http://localhost:1411",
            "tailnet_host": "laptopjannis.tail5fd7a5.ts.net",
            "tailnet_app_base_url": "https://laptopjannis.tail5fd7a5.ts.net",
            "tailnet_issuer_url": "https://laptopjannis.tail5fd7a5.ts.net:4444",
            "tailnet_activation_status": "active",
            "tailnet_active": True,
        },
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr("hermes_cli.product_app.get_user_id_for_tailnet_login", lambda login: "user-1" if login == "alice@example.com" else None)
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_user_by_id",
        lambda user_id: type(
            "U",
            (),
            {"id": user_id, "username": "alice", "display_name": "Alice", "email": "alice@example.com", "is_admin": False, "disabled": False},
        )(),
    )

    client = TestClient(create_product_app())
    response = client.get(
        "https://laptopjannis.tail5fd7a5.ts.net/api/auth/session",
        headers={"Tailscale-User-Login": "alice@example.com"},
    )

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert response.json()["user"]["sub"] == "user-1"


def test_product_app_blocks_setup_on_proxy_after_bootstrap_completion(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"network": {"pocket_id_port": 1411}, "services": {"pocket_id": {"upstream_port": 19141}}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr(
        "hermes_cli.product_app.load_first_admin_enrollment_state",
        lambda: {"first_admin_login_seen": True},
    )

    client = TestClient(create_product_app())
    response = client.get("/__pocket_id_proxy/setup")

    assert response.status_code == 404


def test_product_app_proxies_pocket_id_paths_when_not_completed(monkeypatch):
    class _AsyncUpstreamResponse:
        def __init__(self):
            self.content = b'{"ok":true}'
            self.status_code = 200
            self.headers = {"content-type": "application/json"}

    class _AsyncClientStub:
        def __init__(self, *args, **kwargs):
            self.seen = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            assert method == "GET"
            assert url == "http://127.0.0.1:19141/.well-known/openid-configuration"
            return _AsyncUpstreamResponse()

    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"network": {"pocket_id_port": 1411}, "services": {"pocket_id": {"upstream_port": 19141}}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr(
        "hermes_cli.product_app.load_first_admin_enrollment_state",
        lambda: {"first_admin_login_seen": False},
    )
    monkeypatch.setattr("hermes_cli.product_app.httpx.AsyncClient", _AsyncClientStub)

    client = TestClient(create_product_app())
    response = client.get("/__pocket_id_proxy/.well-known/openid-configuration")

    assert response.status_code == 200
    assert response.json() == {"ok": True}



def test_product_app_callback_rejects_state_mismatch(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"bootstrap": {"first_admin_username": "admin"}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr("hermes_cli.product_app.load_product_oidc_client_settings", lambda config=None: object())
    monkeypatch.setattr("hermes_cli.product_app.discover_product_oidc_provider_metadata", lambda settings: object())
    monkeypatch.setattr(
        "hermes_cli.product_app.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "http://officebox.local:1411/authorize?client_id=hermes-core",
        },
    )

    client = TestClient(create_product_app())
    client.get("/api/auth/login", follow_redirects=False)
    response = client.get("/api/auth/oidc/callback?code=auth-code&state=wrong-state", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "http://officebox.local:8086"


def test_product_app_callback_without_pending_state_redirects_home(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"bootstrap": {"first_admin_username": "admin"}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    client = TestClient(create_product_app())
    response = client.get("/api/auth/oidc/callback?code=auth-code&state=state-123", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "http://officebox.local:8086"


def test_product_app_logout_clears_session(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"bootstrap": {"first_admin_username": "admin"}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr("hermes_cli.product_app.load_product_oidc_client_settings", lambda config=None: object())
    monkeypatch.setattr("hermes_cli.product_app.discover_product_oidc_provider_metadata", lambda settings: object())
    monkeypatch.setattr(
        "hermes_cli.product_app.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "http://officebox.local:1411/authorize?client_id=hermes-core",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.exchange_product_oidc_code",
        lambda settings, metadata, code, verifier: {"access_token": "access-token"},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.fetch_product_oidc_userinfo",
        lambda access_token, metadata: {
            "sub": "user-1",
            "email": "admin@example.com",
            "name": "Admin User",
            "preferred_username": "admin",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_user_by_id",
        lambda user_id: type(
            "U",
            (),
            {
                "id": user_id,
                "username": "admin",
                "display_name": "Admin User",
                "email": "admin@example.com",
                "is_admin": True,
                "disabled": False,
            },
        )(),
    )

    client = TestClient(create_product_app())
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=auth-code&state=state-123", follow_redirects=False)

    response = client.post("/api/auth/logout", headers=_csrf_headers(client))
    assert response.status_code == 200
    assert response.json()["authenticated"] is False
    assert response.json()["user"] is None
    assert response.json()["csrf_token"]
    session_payload = client.get("/api/auth/session").json()
    assert session_payload["authenticated"] is False
    assert session_payload["user"] is None
    assert session_payload["csrf_token"]


def test_product_app_chat_session_requires_auth(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"product": {"brand": {"name": "Hermes Core"}}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    client = TestClient(create_product_app())
    response = client.get("/api/chat/session")

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


def test_product_app_chat_session_returns_payload(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"bootstrap": {"first_admin_username": "admin"}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr("hermes_cli.product_app.load_product_oidc_client_settings", lambda config=None: object())
    monkeypatch.setattr("hermes_cli.product_app.discover_product_oidc_provider_metadata", lambda settings: object())
    monkeypatch.setattr(
        "hermes_cli.product_app.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "http://officebox.local:1411/authorize?client_id=hermes-core",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.exchange_product_oidc_code",
        lambda settings, metadata, code, verifier: {"access_token": "access-token"},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.fetch_product_oidc_userinfo",
        lambda access_token, metadata: {
            "sub": "user-1",
            "email": "admin@example.com",
            "name": "Admin User",
            "preferred_username": "admin",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_runtime_session",
        lambda user: {
            "session_id": "product_admin_123",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ],
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_user_by_id",
        lambda user_id: type(
            "U",
            (),
            {
                "id": user_id,
                "username": "admin",
                "display_name": "Admin User",
                "email": "admin@example.com",
                "disabled": False,
            },
        )(),
    )

    client = TestClient(create_product_app())
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=auth-code&state=state-123", follow_redirects=False)

    response = client.get("/api/chat/session")

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "product_admin_123",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ],
    }


def test_product_app_chat_session_returns_503_when_runtime_unavailable(monkeypatch):
    _patch_admin_session(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_runtime_session",
        lambda user: (_ for _ in ()).throw(RuntimeError("runtime warming up")),
    )

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.get("/api/chat/session")

    assert response.status_code == 503
    assert response.json() == {"detail": "runtime warming up"}


def test_product_app_chat_stream_returns_sse(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"bootstrap": {"first_admin_username": "admin"}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr("hermes_cli.product_app.load_product_oidc_client_settings", lambda config=None: object())
    monkeypatch.setattr("hermes_cli.product_app.discover_product_oidc_provider_metadata", lambda settings: object())
    monkeypatch.setattr(
        "hermes_cli.product_app.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "http://officebox.local:1411/authorize?client_id=hermes-core",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.exchange_product_oidc_code",
        lambda settings, metadata, code, verifier: {"access_token": "access-token"},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.fetch_product_oidc_userinfo",
        lambda access_token, metadata: {
            "sub": "user-1",
            "email": "admin@example.com",
            "name": "Admin User",
            "preferred_username": "admin",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.stream_product_runtime_turn",
        lambda user, message: iter(
            [
                'event: start\ndata: {"session_id": "product_admin_123"}\n\n',
                'event: final\ndata: {"session_id": "product_admin_123", "final_response": "done", "messages": []}\n\n',
            ]
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_user_by_id",
        lambda user_id: type(
            "U",
            (),
            {
                "id": user_id,
                "username": "admin",
                "display_name": "Admin User",
                "email": "admin@example.com",
                "disabled": False,
            },
        )(),
    )

    client = TestClient(create_product_app())
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=auth-code&state=state-123", follow_redirects=False)

    response = client.post("/api/chat/turn/stream", json={"user_message": "hello"}, headers=_csrf_headers(client))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'event: start' in response.text
    assert '"final_response": "done"' in response.text


def test_product_app_workspace_requires_auth(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"product": {"brand": {"name": "Hermes Core"}}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    client = TestClient(create_product_app())

    response = client.get("/api/workspace")

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


def test_product_app_workspace_returns_state(monkeypatch):
    _patch_admin_session(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.product_app.get_workspace_state",
        lambda user, path="": {
            "current_path": path,
            "entries": [{"name": "reports", "path": "reports", "kind": "folder", "size_bytes": 0}],
            "used_bytes": 1024,
            "limit_bytes": 2048,
        },
    )

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.get("/api/workspace?path=reports")

    assert response.status_code == 200
    assert response.json()["current_path"] == "reports"
    assert response.json()["entries"][0]["kind"] == "folder"


def test_product_app_workspace_create_folder(monkeypatch):
    _patch_admin_session(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.product_app.create_workspace_folder",
        lambda user, parent_path, folder_name: {
            "current_path": parent_path,
            "entries": [{"name": folder_name, "path": folder_name, "kind": "folder", "size_bytes": 0}],
            "used_bytes": 0,
            "limit_bytes": 10,
        },
    )

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.post("/api/workspace/folders", json={"path": "", "name": "reports"}, headers=_csrf_headers(client))

    assert response.status_code == 200
    assert response.json()["entries"][0]["name"] == "reports"


def test_product_app_workspace_create_folder_requires_csrf(monkeypatch):
    _patch_admin_session(monkeypatch)

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.post(
        "/api/workspace/folders",
        json={"path": "", "name": "reports"},
        headers={"Origin": "http://officebox.local:8086"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "CSRF validation failed"}


def test_product_app_workspace_create_folder_blocks_cross_origin(monkeypatch):
    _patch_admin_session(monkeypatch)

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.post(
        "/api/workspace/folders",
        json={"path": "", "name": "reports"},
        headers={**_csrf_headers(client), "Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-origin request blocked"}


def test_product_app_workspace_create_folder_requires_origin(monkeypatch):
    _patch_admin_session(monkeypatch)

    client = TestClient(create_product_app())
    _login_admin(client)
    payload = client.get("/api/auth/session").json()

    response = client.post(
        "/api/workspace/folders",
        json={"path": "", "name": "reports"},
        headers={"X-Hermes-CSRF-Token": payload["csrf_token"]},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Missing request origin"}


def test_product_app_workspace_upload_file(monkeypatch):
    _patch_admin_session(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.product_app.store_workspace_file",
        lambda user, parent_path, filename, content: {
            "current_path": parent_path,
            "entries": [{"name": filename, "path": filename, "kind": "file", "size_bytes": len(content)}],
            "used_bytes": len(content),
            "limit_bytes": 1024,
        },
    )

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.post(
        "/api/workspace/files",
        data={"path": ""},
        files=[("files", ("hello.txt", b"hello", "text/plain"))],
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["entries"][0]["name"] == "hello.txt"
    assert response.json()["used_bytes"] == 5


def test_product_app_workspace_delete_path(monkeypatch):
    _patch_admin_session(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.product_app.delete_workspace_path",
        lambda user, path: {
            "current_path": "",
            "entries": [],
            "used_bytes": 0,
            "limit_bytes": 1024,
        },
    )

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.post(
        "/api/workspace/delete",
        json={"path": "reports/hello.txt"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["entries"] == []


def test_product_app_proxy_strips_forwarded_headers(monkeypatch):
    seen: dict[str, object] = {}

    class _AsyncUpstreamResponse:
        def __init__(self):
            self.content = b'{"ok":true}'
            self.status_code = 200
            self.headers = {"content-type": "application/json"}

    class _AsyncClientStub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            seen["headers"] = headers or {}
            return _AsyncUpstreamResponse()

    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"network": {"pocket_id_port": 1411}, "services": {"pocket_id": {"upstream_port": 19141}}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr(
        "hermes_cli.product_app.load_first_admin_enrollment_state",
        lambda: {"first_admin_login_seen": False},
    )
    monkeypatch.setattr("hermes_cli.product_app.httpx.AsyncClient", lambda *args, **kwargs: _AsyncClientStub())

    client = TestClient(create_product_app())
    response = client.get(
        "/__pocket_id_proxy/.well-known/openid-configuration",
        headers={
            "X-Forwarded-For": "203.0.113.1",
            "X-Forwarded-Proto": "https",
            "Forwarded": "for=203.0.113.1;proto=https",
        },
    )

    assert response.status_code == 200
    forwarded_headers = {str(key).lower(): value for key, value in dict(seen["headers"]).items()}
    assert "x-forwarded-for" not in forwarded_headers
    assert "x-forwarded-proto" not in forwarded_headers
    assert "forwarded" not in forwarded_headers


def test_product_app_index_shows_session_details_when_signed_in(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {
            "product": {"brand": {"name": "Hermes Core"}},
            "bootstrap": {"first_admin_username": "admin"},
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr("hermes_cli.product_app.load_product_oidc_client_settings", lambda config=None: object())
    monkeypatch.setattr("hermes_cli.product_app.discover_product_oidc_provider_metadata", lambda settings: object())
    monkeypatch.setattr(
        "hermes_cli.product_app.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "http://officebox.local:1411/authorize?client_id=hermes-core",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.exchange_product_oidc_code",
        lambda settings, metadata, code, verifier: {"access_token": "access-token"},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.fetch_product_oidc_userinfo",
        lambda access_token, metadata: {
            "sub": "user-1",
            "email": "admin@example.com",
            "name": "Admin User",
            "preferred_username": "admin",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_runtime_session",
        lambda user: {"session_id": "product_admin_123", "messages": []},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_user_by_id",
        lambda user_id: None if user_id == "missing" else type(
            "U",
            (),
            {
                "id": "user-1",
                "username": "admin",
                "display_name": "Admin User",
                "email": "admin@example.com",
                "is_admin": True,
                "disabled": False,
            },
        )(),
    )

    client = TestClient(create_product_app())
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=auth-code&state=state-123", follow_redirects=False)
    response = client.get("/")

    assert response.status_code == 200
    session = client.get("/api/auth/session")
    assert session.json()["user"]["is_admin"] is True
    assert "Hermes Core" in response.text
    assert "Shared Files" in response.text
    assert 'id="sessionCard"' not in response.text
    assert "Create signup link" in response.text


def _login_admin(client):
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=auth-code&state=state-123", follow_redirects=False)


def _patch_admin_session(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"bootstrap": {"first_admin_username": "admin"}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr("hermes_cli.product_app.load_product_oidc_client_settings", lambda config=None: object())
    monkeypatch.setattr("hermes_cli.product_app.discover_product_oidc_provider_metadata", lambda settings: object())
    monkeypatch.setattr(
        "hermes_cli.product_app.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "http://officebox.local:1411/authorize?client_id=hermes-core",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.exchange_product_oidc_code",
        lambda settings, metadata, code, verifier: {"access_token": "access-token"},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.fetch_product_oidc_userinfo",
        lambda access_token, metadata: {
            "sub": "user-1",
            "email": "admin@example.com",
            "name": "Admin User",
            "preferred_username": "admin",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_user_by_id",
        lambda user_id: type(
            "U",
            (),
            {
                "id": user_id,
                "username": "admin",
                "display_name": "Admin User",
                "email": "admin@example.com",
                "is_admin": True,
                "disabled": False,
            },
        )(),
    )


def test_product_app_admin_users_requires_admin(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_app.load_product_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")

    client = TestClient(create_product_app())

    response = client.get("/api/admin/users")
    assert response.status_code == 401


def test_product_app_admin_users_returns_users(monkeypatch):
    _patch_admin_session(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.product_app.list_product_users",
        lambda: [
            type(
                "U",
                (),
                {
                    "id": "user-2",
                    "username": "maria",
                    "display_name": "Maria Example",
                    "email": None,
                    "is_admin": False,
                    "disabled": False,
                },
            )()
        ],
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.list_pending_product_signup_invites",
        lambda: [
            type(
                "I",
                (),
                {
                    "invite_id": "invite-signup-1",
                    "token": "signup-1",
                    "signup_url": "http://officebox.local:1411/st/signup-1",
                    "status": "pending",
                    "created_at": 1,
                    "expires_at": 2,
                },
            )()
        ],
    )

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.get("/api/admin/users")

    assert response.status_code == 200
    assert response.json()["users"][0]["type"] == "user"
    assert response.json()["users"][0]["username"] == "maria"
    assert response.json()["users"][1]["type"] == "invite"
    assert response.json()["users"][1]["status"] == "No signup"


def test_product_app_admin_create_user(monkeypatch):
    _patch_admin_session(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.product_app.create_product_user_with_signup",
        lambda username, display_name, email=None: {
            "user": None,
            "signup": {
                "token": "signup-123",
                "signup_url": "http://officebox.local:1411/st/signup-123",
                "ttl_seconds": 604800,
                "usage_limit": 1,
            },
        },
    )
    seen = []
    monkeypatch.setattr(
        "hermes_cli.product_app.register_product_signup_invite",
        lambda signup: seen.append(signup["token"] if isinstance(signup, dict) else signup.token),
    )

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.post(
        "/api/admin/users",
        json={},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["user"] is None
    assert response.json()["signup"]["signup_url"].endswith("/st/signup-123")
    assert seen == ["signup-123"]


def test_product_app_admin_deactivate_user_deletes_runtime(monkeypatch):
    _patch_admin_session(monkeypatch)
    deleted = []
    monkeypatch.setattr(
        "hermes_cli.product_app.deactivate_product_user",
        lambda user_id: {
            "id": user_id,
            "username": "maria",
            "display_name": "Maria Example",
            "email": None,
            "email_is_placeholder": True,
            "is_admin": False,
            "disabled": True,
        },
    )
    monkeypatch.setattr("hermes_cli.product_app.delete_product_runtime", lambda user_id: deleted.append(user_id))

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.post("/api/admin/users/user-2/deactivate", headers=_csrf_headers(client))

    assert response.status_code == 200
    assert response.json()["disabled"] is True
    assert deleted == ["user-2"]


def test_product_app_session_clears_when_provider_user_is_disabled(monkeypatch):
    _patch_admin_session(monkeypatch)
    monkeypatch.setattr("hermes_cli.product_app._SESSION_REFRESH_TTL_SECONDS", 0)
    monkeypatch.setattr("hermes_cli.product_app.get_product_user_by_id", lambda user_id: None)

    client = TestClient(create_product_app())
    _login_admin(client)

    response = client.get("/api/auth/session")

    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is False
    assert payload["user"] is None
    assert payload["csrf_token"]


def test_product_app_does_not_grant_admin_from_username_alone(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_app.load_product_config",
        lambda: {"bootstrap": {"first_admin_username": "admin"}},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.resolve_product_urls",
        lambda config: {"app_base_url": "http://officebox.local:8086", "issuer_url": "http://officebox.local:1411"},
    )
    monkeypatch.setattr("hermes_cli.product_app._session_secret", lambda: "test-secret")
    monkeypatch.setattr("hermes_cli.product_app.load_product_oidc_client_settings", lambda config=None: object())
    monkeypatch.setattr("hermes_cli.product_app.discover_product_oidc_provider_metadata", lambda settings: object())
    monkeypatch.setattr(
        "hermes_cli.product_app.create_oidc_login_request",
        lambda settings, metadata: {
            "state": "state-123",
            "nonce": "nonce-123",
            "verifier": "verifier-123",
            "authorization_url": "http://officebox.local:1411/authorize?client_id=hermes-core",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.exchange_product_oidc_code",
        lambda settings, metadata, code, verifier: {"access_token": "access-token"},
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.fetch_product_oidc_userinfo",
        lambda access_token, metadata: {
            "sub": "user-1",
            "email": "admin@example.com",
            "name": "Admin User",
            "preferred_username": "admin",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.product_app.get_product_user_by_id",
        lambda user_id: type(
            "U",
            (),
            {
                "id": user_id,
                "username": "admin",
                "display_name": "Admin User",
                "email": "admin@example.com",
                "is_admin": False,
                "disabled": False,
            },
        )(),
    )

    client = TestClient(create_product_app())
    client.get("/api/auth/login", follow_redirects=False)
    client.get("/api/auth/oidc/callback?code=auth-code&state=state-123", follow_redirects=False)

    payload = client.get("/api/auth/session").json()

    assert payload["authenticated"] is True
    assert payload["user"]["preferred_username"] == "admin"
    assert payload["user"]["is_admin"] is False
