from hermes_cli.product_users import (
    bootstrap_first_admin_user,
    claim_product_user_from_invite,
    create_product_user_with_signup,
    get_product_user_by_tailscale_login,
    list_pending_product_signup_invites,
    list_product_users,
)


def test_create_product_user_with_signup_creates_pending_invite(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.product_users.resolve_product_urls",
        lambda config=None: {"app_base_url": "https://device.tail5fd7a5.ts.net"},
    )

    created = create_product_user_with_signup(display_name="Alice Example")

    assert created.user is None
    assert created.signup.signup_url.startswith("https://device.tail5fd7a5.ts.net/invite/")
    pending = list_pending_product_signup_invites()
    assert len(pending) == 1
    assert pending[0].tailscale_login == ""


def test_claim_product_user_from_invite_creates_bound_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.product_users.resolve_product_urls",
        lambda config=None: {"app_base_url": "https://device.tail5fd7a5.ts.net"},
    )
    created = create_product_user_with_signup(display_name="Alice Example")

    user = claim_product_user_from_invite(
        token=created.signup.token,
        tailscale_subject="ts-sub-1",
        tailscale_login="alice@example.com",
        display_name="Alice Example",
    )

    assert user.tailscale_subject == "ts-sub-1"
    assert get_product_user_by_tailscale_login("alice@example.com").id == user.id
    assert list_pending_product_signup_invites() == []


def test_bootstrap_first_admin_user_creates_single_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    admin = bootstrap_first_admin_user(
        tailscale_subject="ts-admin",
        tailscale_login="admin@example.com",
        display_name="Admin",
    )

    users = list_product_users()
    assert len(users) == 1
    assert users[0].is_admin is True
    assert admin.tailscale_login == "admin@example.com"
