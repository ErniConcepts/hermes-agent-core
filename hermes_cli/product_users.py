from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field

from hermes_cli.config import _secure_dir, _secure_file
from hermes_cli.product_config import get_product_storage_root, load_product_config
from hermes_cli.product_stack import resolve_product_urls
from utils import atomic_json_write

logger = logging.getLogger(__name__)
_DEFAULT_INVITE_TOKEN_TTL = 7 * 24 * 60 * 60


class ProductUser(BaseModel):
    id: str
    username: str
    display_name: str
    email: str | None = None
    email_is_placeholder: bool = False
    is_admin: bool = False
    disabled: bool = False
    tailscale_subject: str
    tailscale_login: str
    created_at: int = Field(default_factory=lambda: int(time.time()))


class ProductSignupToken(BaseModel):
    token: str
    signup_url: str
    ttl_seconds: int
    usage_limit: int = 1
    tailscale_login: str = ""


class ProductCreatedUser(BaseModel):
    user: ProductUser | None = None
    signup: ProductSignupToken


class ProductInviteRecord(BaseModel):
    invite_id: str
    token: str
    signup_url: str
    tailscale_login: str = ""
    display_name: str
    created_at: int
    expires_at: int
    status: str = "pending"
    claimed_by_user_id: str | None = None


def _users_state_path() -> Path:
    return get_product_storage_root() / "bootstrap" / "users.json"


def _invites_state_path() -> Path:
    return get_product_storage_root() / "bootstrap" / "signup_invites.json"


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else []


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(path.parent)
    atomic_json_write(path, rows)
    _secure_file(path)


def _normalize_tailscale_login(value: str | None) -> str:
    return str(value or "").strip().lower()


def _normalize_display_name(display_name: str | None, tailscale_login: str) -> str:
    return str(display_name or "").strip() or tailscale_login.split("@", 1)[0] or "User"


def _username_from_tailscale_login(tailscale_login: str) -> str:
    normalized = _normalize_tailscale_login(tailscale_login)
    base = normalized.split("@", 1)[0]
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in base).strip("._-")
    return cleaned or f"user-{secrets.token_hex(4)}"


def _load_users() -> list[ProductUser]:
    users: list[ProductUser] = []
    for row in _read_rows(_users_state_path()):
        try:
            users.append(ProductUser.model_validate(row))
        except Exception:
            continue
    return users


def _save_users(users: list[ProductUser]) -> None:
    _write_rows(_users_state_path(), [user.model_dump(mode="json") for user in users])


def _load_invites() -> list[ProductInviteRecord]:
    invites: list[ProductInviteRecord] = []
    for row in _read_rows(_invites_state_path()):
        try:
            invites.append(ProductInviteRecord.model_validate(row))
        except Exception:
            continue
    return invites


def _save_invites(invites: list[ProductInviteRecord]) -> None:
    _write_rows(_invites_state_path(), [invite.model_dump(mode="json") for invite in invites])


def _current_urls(config: dict[str, Any] | None = None) -> dict[str, str]:
    return resolve_product_urls(config or load_product_config())


def _invite_signup_url(token: str, config: dict[str, Any] | None = None) -> str:
    app_base_url = str(_current_urls(config).get("app_base_url", "")).rstrip("/")
    return f"{app_base_url}/invite/{quote(token)}"


def list_product_users(config: dict[str, Any] | None = None) -> list[ProductUser]:
    users = _load_users()
    return sorted(users, key=lambda item: (item.disabled, item.username.lower()))


def get_product_user_by_id(user_id: str, config: dict[str, Any] | None = None) -> ProductUser | None:
    target = str(user_id or "").strip()
    if not target:
        return None
    for user in _load_users():
        if user.id == target:
            return user
    return None


def get_product_user_by_tailscale_subject(subject: str) -> ProductUser | None:
    target = str(subject or "").strip()
    if not target:
        return None
    for user in _load_users():
        if user.tailscale_subject == target:
            return user
    return None


def get_product_user_by_tailscale_login(tailscale_login: str) -> ProductUser | None:
    target = _normalize_tailscale_login(tailscale_login)
    if not target:
        return None
    for user in _load_users():
        if _normalize_tailscale_login(user.tailscale_login) == target:
            return user
    return None


def _save_user(user: ProductUser) -> ProductUser:
    users = [item for item in _load_users() if item.id != user.id]
    users.append(user)
    _save_users(users)
    return user


def bootstrap_first_admin_user(
    *,
    tailscale_subject: str,
    tailscale_login: str,
    display_name: str | None = None,
) -> ProductUser:
    normalized_login = _normalize_tailscale_login(tailscale_login)
    if not normalized_login:
        raise ValueError("First admin Tailscale login must not be empty")
    existing = list_product_users()
    if any(user.is_admin and not user.disabled for user in existing):
        raise ValueError("An active admin already exists")
    user = ProductUser(
        id=f"user-{secrets.token_hex(8)}",
        username=_username_from_tailscale_login(normalized_login),
        display_name=_normalize_display_name(display_name, normalized_login),
        email=normalized_login if "@" in normalized_login else None,
        is_admin=True,
        disabled=False,
        tailscale_subject=str(tailscale_subject or "").strip(),
        tailscale_login=normalized_login,
    )
    return _save_user(user)


def deactivate_product_user(user_id: str, config: dict[str, Any] | None = None) -> ProductUser:
    user = get_product_user_by_id(user_id, config=config)
    if user is None:
        raise ValueError("User not found")
    updated = user.model_copy(update={"disabled": True})
    return _save_user(updated)


def _active_invites() -> list[ProductInviteRecord]:
    now = int(time.time())
    changed = False
    invites = _load_invites()
    for invite in invites:
        if invite.status == "pending" and invite.expires_at <= now:
            invite.status = "expired"
            changed = True
    if changed:
        _save_invites(invites)
    return invites


def list_pending_product_signup_invites(config: dict[str, Any] | None = None) -> list[ProductInviteRecord]:
    return sorted(
        [item for item in _active_invites() if item.status == "pending"],
        key=lambda item: item.created_at,
        reverse=True,
    )


def create_product_user_with_signup(
    username: str | None = None,
    display_name: str | None = None,
    *,
    email: str | None = None,
    config: dict[str, Any] | None = None,
) -> ProductCreatedUser:
    tailscale_login = _normalize_tailscale_login(email or username)
    if tailscale_login and get_product_user_by_tailscale_login(tailscale_login):
        raise ValueError("This Tailscale account already has a product user")
    if tailscale_login and any(inv.status == "pending" and _normalize_tailscale_login(inv.tailscale_login) == tailscale_login for inv in _active_invites()):
        raise ValueError("This Tailscale account already has a pending invite")
    token = secrets.token_urlsafe(24)
    created_at = int(time.time())
    invite_label = _normalize_display_name(display_name, tailscale_login or "user")
    invite = ProductInviteRecord(
        invite_id=f"invite-{secrets.token_hex(8)}",
        token=token,
        signup_url=_invite_signup_url(token, config=config),
        tailscale_login=tailscale_login,
        display_name=invite_label,
        created_at=created_at,
        expires_at=created_at + _DEFAULT_INVITE_TOKEN_TTL,
    )
    invites = _load_invites()
    invites.append(invite)
    _save_invites(invites)
    return ProductCreatedUser(
        user=None,
        signup=ProductSignupToken(
            token=token,
            signup_url=invite.signup_url,
            ttl_seconds=_DEFAULT_INVITE_TOKEN_TTL,
            tailscale_login=tailscale_login,
        ),
    )


def claim_product_user_from_invite(
    *,
    token: str,
    tailscale_subject: str,
    tailscale_login: str,
    display_name: str | None = None,
) -> ProductUser:
    candidate_token = str(token or "").strip()
    normalized_login = _normalize_tailscale_login(tailscale_login)
    if not candidate_token:
        raise ValueError("Invite token is required")
    if not normalized_login:
        raise ValueError("Tailscale login is required")
    if get_product_user_by_tailscale_subject(tailscale_subject) or get_product_user_by_tailscale_login(normalized_login):
        raise ValueError("This Tailscale account already has a product user")
    invites = _active_invites()
    match: ProductInviteRecord | None = None
    for invite in invites:
        if invite.token == candidate_token:
            match = invite
            break
    if match is None or match.status != "pending":
        raise ValueError("Invite is invalid or expired")
    if match.tailscale_login and _normalize_tailscale_login(match.tailscale_login) != normalized_login:
        raise ValueError("Invite is not valid for this Tailscale identity")
    user = ProductUser(
        id=f"user-{secrets.token_hex(8)}",
        username=_username_from_tailscale_login(normalized_login),
        display_name=_normalize_display_name(display_name or match.display_name, normalized_login),
        email=normalized_login if "@" in normalized_login else None,
        is_admin=False,
        disabled=False,
        tailscale_subject=str(tailscale_subject or "").strip(),
        tailscale_login=normalized_login,
    )
    _save_user(user)
    match.status = "claimed"
    match.claimed_by_user_id = user.id
    match.tailscale_login = normalized_login
    _save_invites(invites)
    return user
