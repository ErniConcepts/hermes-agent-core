from __future__ import annotations

from hermes_cli.product_users import (
    ProductInviteRecord,
    ProductSignupToken,
    list_pending_product_signup_invites,
)


def register_product_signup_invite(signup: ProductSignupToken) -> ProductInviteRecord:
    pending = list_pending_product_signup_invites()
    for invite in pending:
        if invite.token == signup.token:
            return invite
    raise ValueError("Invite token was not persisted")


def reconcile_product_signup_invites(config: dict | None = None) -> list[ProductInviteRecord]:
    return list_pending_product_signup_invites(config=config)
