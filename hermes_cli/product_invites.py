from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from hermes_cli.config import _secure_dir, _secure_file
from hermes_cli.product_config import get_product_storage_root
from hermes_cli.product_users import ProductSignupToken, list_active_product_signup_tokens
from utils import atomic_json_write


class ProductInviteRecord(BaseModel):
    invite_id: str
    token: str
    signup_url: str
    created_at: int
    expires_at: int
    status: str = "pending"


def _invites_state_path() -> Path:
    return get_product_storage_root() / "bootstrap" / "signup_invites.json"


def _load_invites() -> list[ProductInviteRecord]:
    state_path = _invites_state_path()
    if not state_path.exists():
        return []
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    invites: list[ProductInviteRecord] = []
    for item in payload:
        try:
            invites.append(ProductInviteRecord.model_validate(item))
        except Exception:
            continue
    return invites


def _save_invites(invites: list[ProductInviteRecord]) -> None:
    state_path = _invites_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(state_path.parent)
    atomic_json_write(
        state_path,
        [item.model_dump(mode="json") for item in invites],
    )
    _secure_file(state_path)


def register_product_signup_invite(signup: ProductSignupToken) -> ProductInviteRecord:
    now = int(time.time())
    invite_digest = hashlib.sha256(signup.token.encode("utf-8")).hexdigest()[:16]
    invite = ProductInviteRecord(
        invite_id=f"invite-{invite_digest}",
        token=signup.token,
        signup_url=signup.signup_url,
        created_at=now,
        expires_at=now + int(signup.ttl_seconds),
        status="pending",
    )
    invites = _load_invites()
    invites = [item for item in invites if item.token != invite.token]
    invites.append(invite)
    _save_invites(invites)
    return invite


def reconcile_product_signup_invites(config: dict[str, Any] | None = None) -> list[ProductInviteRecord]:
    invites = _load_invites()
    if not invites:
        return []
    try:
        active_tokens = list_active_product_signup_tokens(config=config)
    except Exception:
        return invites
    now = int(time.time())
    changed = False
    for invite in invites:
        if invite.status != "pending":
            continue
        if now >= invite.expires_at:
            invite.status = "expired"
            changed = True
            continue
        if invite.token not in active_tokens:
            invite.status = "used"
            changed = True
    if changed:
        _save_invites(invites)
    return invites


def list_pending_product_signup_invites(config: dict[str, Any] | None = None) -> list[ProductInviteRecord]:
    invites = reconcile_product_signup_invites(config=config)
    pending = [item for item in invites if item.status == "pending"]
    return sorted(pending, key=lambda item: item.created_at, reverse=True)
