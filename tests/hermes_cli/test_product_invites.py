from hermes_cli.product_invites import (
    list_pending_product_signup_invites,
    register_product_signup_invite,
)


def test_register_product_signup_invite_creates_pending_entry(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1000)

    invite = register_product_signup_invite(
        type(
            "S",
            (),
            {
                "token": "signup-1",
                "signup_url": "http://localhost:1411/st/signup-1",
                "ttl_seconds": 3600,
                "usage_limit": 1,
            },
        )()
    )

    assert invite.invite_id == "invite-signup-1"
    assert invite.status == "pending"
    assert invite.expires_at == 4600


def test_list_pending_product_signup_invites_hides_used_and_expired(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 2000)
    register_product_signup_invite(
        type(
            "S",
            (),
            {
                "token": "signup-active",
                "signup_url": "http://localhost:1411/st/signup-active",
                "ttl_seconds": 3600,
                "usage_limit": 1,
            },
        )()
    )
    register_product_signup_invite(
        type(
            "S",
            (),
            {
                "token": "signup-used",
                "signup_url": "http://localhost:1411/st/signup-used",
                "ttl_seconds": 3600,
                "usage_limit": 1,
            },
        )()
    )
    monkeypatch.setattr("hermes_cli.product_invites.list_active_product_signup_tokens", lambda config=None: {"signup-active"})

    pending = list_pending_product_signup_invites()

    assert [item.token for item in pending] == ["signup-active"]


def test_list_pending_product_signup_invites_hides_expired(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1000)
    register_product_signup_invite(
        type(
            "S",
            (),
            {
                "token": "signup-expired",
                "signup_url": "http://localhost:1411/st/signup-expired",
                "ttl_seconds": 10,
                "usage_limit": 1,
            },
        )()
    )
    monkeypatch.setattr("time.time", lambda: 2000)
    monkeypatch.setattr("hermes_cli.product_invites.list_active_product_signup_tokens", lambda config=None: {"signup-expired"})

    pending = list_pending_product_signup_invites()

    assert pending == []


def test_list_pending_product_signup_invites_keeps_pending_when_token_api_unavailable(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1000)
    register_product_signup_invite(
        type(
            "S",
            (),
            {
                "token": "signup-api-down",
                "signup_url": "http://localhost:1411/st/signup-api-down",
                "ttl_seconds": 3600,
                "usage_limit": 1,
            },
        )()
    )
    monkeypatch.setattr(
        "hermes_cli.product_invites.list_active_product_signup_tokens",
        lambda config=None: (_ for _ in ()).throw(RuntimeError("api unavailable")),
    )

    pending = list_pending_product_signup_invites()

    assert [item.token for item in pending] == ["signup-api-down"]
