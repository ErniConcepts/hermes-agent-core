from hermes_cli.product_users import (
    create_product_user_with_signup,
    create_product_signup_token,
    create_product_user,
    deactivate_product_user,
    get_product_user_by_id,
    list_active_product_signup_tokens,
    list_product_users,
)


class DummyResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)
        self.content = b"{}"

    def json(self):
        return self._payload


class DummyClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        status_code, payload = self.responses[(method, path)]
        return DummyResponse(status_code, payload)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)


def test_list_product_users_filters_internal_and_placeholder_email(monkeypatch):
    client = DummyClient(
        {
            (
                "GET",
                "/api/users",
            ): (
                200,
                {
                    "data": [
                        {
                            "id": "internal",
                            "username": "static-api-user-abc",
                            "email": None,
                            "emailVerified": False,
                            "firstName": "Static",
                            "lastName": "API",
                            "displayName": "Static API",
                            "isAdmin": True,
                            "locale": None,
                            "customClaims": [],
                            "userGroups": [],
                            "ldapId": None,
                            "disabled": False,
                        },
                        {
                            "id": "user-1",
                            "username": "maria",
                            "email": "maria@users.local.invalid",
                            "emailVerified": False,
                            "firstName": "Maria",
                            "lastName": "User",
                            "displayName": "Maria User",
                            "isAdmin": False,
                            "locale": None,
                            "customClaims": [],
                            "userGroups": [],
                            "ldapId": None,
                            "disabled": False,
                        },
                    ]
                },
            )
        }
    )
    monkeypatch.setattr("hermes_cli.product_users._client", lambda config=None: client)

    users = list_product_users()

    assert len(users) == 1
    assert users[0].username == "maria"
    assert users[0].email is None
    assert users[0].email_is_placeholder is True


def test_create_product_user_uses_placeholder_email_when_missing(monkeypatch):
    client = DummyClient(
        {
            ("POST", "/api/users"): (
                200,
                {
                    "id": "user-1",
                    "username": "maria",
                    "email": "maria@users.local.invalid",
                    "emailVerified": False,
                    "firstName": "Maria",
                    "lastName": "Example",
                    "displayName": "Maria Example",
                    "isAdmin": False,
                    "locale": None,
                    "customClaims": [],
                    "userGroups": [],
                    "ldapId": None,
                    "disabled": False,
                },
            )
        }
    )
    monkeypatch.setattr("hermes_cli.product_users._client", lambda config=None: client)

    user = create_product_user("maria", "Maria Example")

    assert user.username == "maria"
    assert user.email is None
    payload = client.calls[0][2]["json"]
    assert payload["email"] == "maria@users.local.invalid"


def test_create_product_user_rejects_invalid_username(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_users._client", lambda config=None: None)

    try:
        create_product_user(" test 4 ", "Test User")
    except ValueError as exc:
        assert "Username may use letters" in str(exc)
    else:
        raise AssertionError("Expected invalid username to be rejected")


def test_create_product_user_rejects_invalid_email(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_users._client", lambda config=None: None)

    try:
        create_product_user("test4", "Test User", email="not-an-email")
    except ValueError as exc:
        assert str(exc) == "Email must be a valid email address"
    else:
        raise AssertionError("Expected invalid email to be rejected")


def test_get_product_user_by_id_returns_none_for_missing(monkeypatch):
    client = DummyClient({("GET", "/api/users/user-1"): (404, {"error": "missing"})})
    monkeypatch.setattr("hermes_cli.product_users._client", lambda config=None: client)

    assert get_product_user_by_id("user-1") is None


def test_deactivate_product_user_sets_disabled(monkeypatch):
    client = DummyClient(
        {
            ("GET", "/api/users/user-1"): (
                200,
                {
                    "id": "user-1",
                    "username": "maria",
                    "email": "maria@example.com",
                    "emailVerified": False,
                    "firstName": "Maria",
                    "lastName": "Example",
                    "displayName": "Maria Example",
                    "isAdmin": False,
                    "locale": None,
                    "customClaims": [],
                    "userGroups": [],
                    "ldapId": None,
                    "disabled": False,
                },
            ),
            ("PUT", "/api/users/user-1"): (
                200,
                {
                    "id": "user-1",
                    "username": "maria",
                    "email": "maria@example.com",
                    "emailVerified": False,
                    "firstName": "Maria",
                    "lastName": "Example",
                    "displayName": "Maria Example",
                    "isAdmin": False,
                    "locale": None,
                    "customClaims": [],
                    "userGroups": [],
                    "ldapId": None,
                    "disabled": True,
                },
            ),
        }
    )
    monkeypatch.setattr("hermes_cli.product_users._client", lambda config=None: client)

    user = deactivate_product_user("user-1")

    assert user.disabled is True
    assert client.calls[1][2]["json"]["disabled"] is True


def test_create_product_signup_token_returns_full_url(monkeypatch):
    client = DummyClient({("POST", "/api/signup-tokens"): (200, {"token": "signup-123"})})
    monkeypatch.setattr("hermes_cli.product_users._client", lambda config=None: client)
    monkeypatch.setattr("hermes_cli.product_users.socket.gethostname", lambda: "laptopjannis")
    seen = []
    monkeypatch.setattr("hermes_cli.product_users._ensure_signup_mode_with_token", lambda config: seen.append(True))
    monkeypatch.setattr(
        "hermes_cli.product_users.resolve_product_urls",
        lambda config=None: {"app_base_url": "http://localhost:8086", "tailnet_active": False},
    )

    token = create_product_signup_token({})

    assert token.token == "signup-123"
    assert token.signup_url == "http://laptopjannis.local:8086/st/signup-123"
    assert seen == [True]


def test_create_product_signup_token_keeps_primary_app_url_when_tailnet_is_active(monkeypatch):
    client = DummyClient({("POST", "/api/signup-tokens"): (200, {"token": "signup-123"})})
    monkeypatch.setattr("hermes_cli.product_users._client", lambda config=None: client)
    monkeypatch.setattr("hermes_cli.product_users._ensure_signup_mode_with_token", lambda config: None)
    monkeypatch.setattr(
        "hermes_cli.product_users.resolve_product_urls",
        lambda config=None: {
            "app_base_url": "http://officebox.local:8086",
            "tailnet_app_base_url": "https://hermes-box.corpnet.ts.net",
            "tailnet_active": True,
        },
    )

    token = create_product_signup_token({})

    assert token.signup_url == "http://officebox.local:8086/st/signup-123"


def test_create_product_signup_token_uses_lan_hostname_when_public_host_is_localhost(monkeypatch):
    client = DummyClient({("POST", "/api/signup-tokens"): (200, {"token": "signup-123"})})
    monkeypatch.setattr("hermes_cli.product_users._client", lambda config=None: client)
    monkeypatch.setattr("hermes_cli.product_users._ensure_signup_mode_with_token", lambda config: None)
    monkeypatch.setattr("hermes_cli.product_users.socket.gethostname", lambda: "laptopjannis")
    monkeypatch.setattr(
        "hermes_cli.product_users.resolve_product_urls",
        lambda config=None: {
            "public_host": "localhost",
            "app_base_url": "http://localhost:8086",
            "tailnet_app_base_url": "https://hermes-box.corpnet.ts.net",
            "tailnet_active": True,
        },
    )

    token = create_product_signup_token({})

    assert token.signup_url == "http://laptopjannis.local:8086/st/signup-123"


def test_create_product_user_with_signup_combines_results(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_users.create_product_signup_token",
        lambda config=None: {
                "token": "signup-123",
                "signup_url": "http://localhost:1411/st/signup-123",
                "ttl_seconds": 604800,
                "usage_limit": 1,
            },
    )

    created = create_product_user_with_signup("maria", "Maria Example", email="maria@example.com")

    assert created.user is None
    assert created.signup.signup_url.endswith("/st/signup-123")


def test_list_active_product_signup_tokens_reads_data_rows(monkeypatch):
    client = DummyClient({("GET", "/api/signup-tokens"): (200, {"data": [{"token": "signup-1"}, {"token": "signup-2"}]})})
    monkeypatch.setattr("hermes_cli.product_users._client", lambda config=None: client)

    tokens = list_active_product_signup_tokens()

    assert tokens == {"signup-1", "signup-2"}


def test_list_active_product_signup_tokens_filters_used_and_expired(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1700000000)
    client = DummyClient(
        {
            (
                "GET",
                "/api/signup-tokens",
            ): (
                200,
                {
                    "data": [
                        {
                            "token": "signup-active",
                            "usageCount": 0,
                            "usageLimit": 1,
                            "expiresAt": "2099-01-01T00:00:00Z",
                        },
                        {
                            "token": "signup-used",
                            "usageCount": 1,
                            "usageLimit": 1,
                            "expiresAt": "2099-01-01T00:00:00Z",
                        },
                        {
                            "token": "signup-expired",
                            "usageCount": 0,
                            "usageLimit": 1,
                            "expiresAt": "2000-01-01T00:00:00Z",
                        },
                    ]
                },
            )
        }
    )
    monkeypatch.setattr("hermes_cli.product_users._client", lambda config=None: client)

    tokens = list_active_product_signup_tokens()

    assert tokens == {"signup-active"}
