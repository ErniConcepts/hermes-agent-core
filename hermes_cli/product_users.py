from __future__ import annotations

import logging
import time
import re
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from hermes_cli.product_stack import _api_headers, _ensure_signup_mode_with_token, resolve_product_urls
from hermes_cli.product_config import load_product_config

logger = logging.getLogger(__name__)
_READ_TIMEOUT_SECONDS = 5.0
_WRITE_TIMEOUT_SECONDS = 10.0
_CLIENT_CACHE: dict[tuple[str, str, float], httpx.Client] = {}

_PLACEHOLDER_EMAIL_DOMAIN = "users.local.invalid"
_DEFAULT_SIGNUP_TOKEN_TTL = 7 * 24 * 60 * 60
_DEFAULT_SIGNUP_TOKEN_USAGE_LIMIT = 1
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._@-]*[A-Za-z0-9])?$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ProductUser(BaseModel):
    id: str
    username: str
    display_name: str
    email: str | None = None
    email_is_placeholder: bool = False
    is_admin: bool = False
    disabled: bool = False


class ProductSignupToken(BaseModel):
    token: str
    signup_url: str
    ttl_seconds: int
    usage_limit: int


class ProductCreatedUser(BaseModel):
    user: ProductUser | None = None
    signup: ProductSignupToken


class PocketIdUserRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    username: str
    email: str | None = None
    email_verified: bool = Field(default=False, alias="emailVerified")
    first_name: str = Field(default="", alias="firstName")
    last_name: str = Field(default="", alias="lastName")
    display_name: str = Field(default="", alias="displayName")
    is_admin: bool = Field(default=False, alias="isAdmin")
    disabled: bool = False
    locale: str | None = None
    custom_claims: list[Any] = Field(default_factory=list, alias="customClaims")
    user_groups: list[Any] = Field(default_factory=list, alias="userGroups")
    ldap_id: str | None = Field(default=None, alias="ldapId")


def _client(config: dict[str, Any] | None = None) -> httpx.Client:
    product_config = config or load_product_config()
    base_url = resolve_product_urls(product_config)["issuer_url"]
    headers = _api_headers(product_config)
    timeout = _WRITE_TIMEOUT_SECONDS
    cache_key = (base_url, headers.get("X-API-Key", ""), timeout)
    client = _CLIENT_CACHE.get(cache_key)
    if client is None:
        client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
        _CLIENT_CACHE[cache_key] = client
    return client


def _request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    expected_status: int | tuple[int, ...],
    **kwargs: Any,
) -> dict[str, Any]:
    started = time.perf_counter()
    response = client.request(method, path, **kwargs)
    expected = (
        expected_status
        if isinstance(expected_status, tuple)
        else (expected_status,)
    )
    if response.status_code not in expected:
        raise RuntimeError(f"{method} {path} failed with {response.status_code}: {response.text}")
    logger.info(
        "product_users request %s %s completed in %.0fms",
        method,
        path,
        (time.perf_counter() - started) * 1000,
    )
    return response.json() if response.content else {}


def _is_internal_user(record: PocketIdUserRecord) -> bool:
    return record.username.startswith("static-api-user-")


def _placeholder_email(username: str) -> str:
    return f"{username}@{_PLACEHOLDER_EMAIL_DOMAIN}"


def _normalize_email(email: str | None) -> tuple[str | None, bool]:
    normalized = (email or "").strip()
    if not normalized:
        return None, False
    if normalized.endswith(f"@{_PLACEHOLDER_EMAIL_DOMAIN}"):
        return None, True
    return normalized, False


def _normalize_user(record: PocketIdUserRecord) -> ProductUser:
    email, email_is_placeholder = _normalize_email(record.email)
    display_name = record.display_name.strip() or record.username
    return ProductUser(
        id=record.id,
        username=record.username,
        display_name=display_name,
        email=email,
        email_is_placeholder=email_is_placeholder,
        is_admin=record.is_admin,
        disabled=record.disabled,
    )


def _split_display_name(display_name: str, username: str) -> tuple[str, str]:
    normalized = display_name.strip() or username
    parts = normalized.split(maxsplit=1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else "user"
    return first_name[:50], last_name[:50]


def _validate_username(username: str) -> str:
    normalized = username.strip()
    if not normalized:
        raise ValueError("Username must not be empty")
    if not _USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Username may use letters, numbers, underscores, dots, hyphens, and @, and must start and end with a letter or number"
        )
    return normalized


def _validate_optional_email(email: str | None) -> str | None:
    normalized = (email or "").strip()
    if not normalized:
        return None
    if not _EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("Email must be a valid email address")
    return normalized


def list_product_users(config: dict[str, Any] | None = None) -> list[ProductUser]:
    client = _client(config)
    previous_timeout = getattr(client, "timeout", None)
    if hasattr(client, "timeout"):
        client.timeout = httpx.Timeout(_READ_TIMEOUT_SECONDS)
    try:
        payload = _request_json(client, "GET", "/api/users", expected_status=200)
    finally:
        if hasattr(client, "timeout"):
            client.timeout = previous_timeout
    records = [
        PocketIdUserRecord.model_validate(item)
        for item in payload.get("data", [])
    ]
    visible = [_normalize_user(item) for item in records if not _is_internal_user(item)]
    return sorted(visible, key=lambda item: (item.disabled, item.username.lower()))


def get_product_user_by_id(user_id: str, config: dict[str, Any] | None = None) -> ProductUser | None:
    client = _client(config)
    previous_timeout = getattr(client, "timeout", None)
    if hasattr(client, "timeout"):
        client.timeout = httpx.Timeout(_READ_TIMEOUT_SECONDS)
    started = time.perf_counter()
    try:
        response = client.get(f"/api/users/{user_id}")
    finally:
        if hasattr(client, "timeout"):
            client.timeout = previous_timeout
    logger.info(
        "product_users request GET /api/users/%s completed in %.0fms",
        user_id,
        (time.perf_counter() - started) * 1000,
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RuntimeError(f"GET /api/users/{user_id} failed with {response.status_code}: {response.text}")
    record = PocketIdUserRecord.model_validate(response.json())
    if _is_internal_user(record):
        return None
    return _normalize_user(record)


def create_product_user(
    username: str,
    display_name: str,
    *,
    email: str | None = None,
    config: dict[str, Any] | None = None,
) -> ProductUser:
    normalized_username = _validate_username(username)
    normalized_display_name = display_name.strip() or normalized_username
    first_name, last_name = _split_display_name(normalized_display_name, normalized_username)
    normalized_email = _validate_optional_email(email) or _placeholder_email(normalized_username)
    payload = {
        "username": normalized_username,
        "firstName": first_name,
        "lastName": last_name,
        "displayName": normalized_display_name,
        "email": normalized_email,
        "emailsVerified": False,
        "isAdmin": False,
        "disabled": False,
    }
    client = _client(config)
    response = _request_json(client, "POST", "/api/users", expected_status=(200, 201), json=payload)
    return _normalize_user(PocketIdUserRecord.model_validate(response))


def deactivate_product_user(user_id: str, config: dict[str, Any] | None = None) -> ProductUser:
    client = _client(config)
    previous_timeout = getattr(client, "timeout", None)
    if hasattr(client, "timeout"):
        client.timeout = httpx.Timeout(_READ_TIMEOUT_SECONDS)
    started = time.perf_counter()
    try:
        get_response = client.get(f"/api/users/{user_id}")
    finally:
        if hasattr(client, "timeout"):
            client.timeout = previous_timeout
    logger.info(
        "product_users request GET /api/users/%s completed in %.0fms",
        user_id,
        (time.perf_counter() - started) * 1000,
    )
    if get_response.status_code == 404:
        raise ValueError("User not found")
    if get_response.status_code != 200:
        raise RuntimeError(
            f"GET /api/users/{user_id} failed with {get_response.status_code}: {get_response.text}"
        )
    record = PocketIdUserRecord.model_validate(get_response.json())
    if _is_internal_user(record):
        raise ValueError("Internal service users cannot be managed from the product UI")
    payload = {
        "username": record.username,
        "email": record.email,
        "firstName": record.first_name,
        "lastName": record.last_name,
        "displayName": record.display_name,
        "isAdmin": record.is_admin,
        "disabled": True,
        "locale": record.locale,
    }
    response = _request_json(client, "PUT", f"/api/users/{user_id}", expected_status=200, json=payload)
    return _normalize_user(PocketIdUserRecord.model_validate(response))


def create_product_signup_token(config: dict[str, Any] | None = None) -> ProductSignupToken:
    product_config = config or load_product_config()
    try:
        _ensure_signup_mode_with_token(product_config)
    except Exception as exc:  # pragma: no cover - defensive behavior for external API variance
        logger.warning("Failed to enforce Pocket ID allowUserSignups=withToken before token creation: %s", exc)
    payload = {
        "ttl": _DEFAULT_SIGNUP_TOKEN_TTL,
        "usageLimit": _DEFAULT_SIGNUP_TOKEN_USAGE_LIMIT,
        "userGroupIds": [],
    }
    client = _client(product_config)
    response = _request_json(client, "POST", "/api/signup-tokens", expected_status=(200, 201), json=payload)
    token = str(response.get("token", "")).strip()
    if not token:
        raise RuntimeError("Pocket ID did not return a signup token")
    urls = resolve_product_urls(product_config)
    public_signup_base = str(urls.get("app_base_url", "")).strip()
    if not public_signup_base:
        raise RuntimeError("Product app URL is not configured")
    return ProductSignupToken(
        token=token,
        signup_url=f"{public_signup_base.rstrip('/')}/st/{token}",
        ttl_seconds=_DEFAULT_SIGNUP_TOKEN_TTL,
        usage_limit=_DEFAULT_SIGNUP_TOKEN_USAGE_LIMIT,
    )


def list_active_product_signup_tokens(config: dict[str, Any] | None = None) -> set[str]:
    client = _client(config)
    started = time.perf_counter()
    response = client.get("/api/signup-tokens")
    if response.status_code != 200:
        raise RuntimeError(
            f"GET /api/signup-tokens failed with {response.status_code}: {response.text}"
        )
    logger.info(
        "product_users request GET /api/signup-tokens completed in %.0fms",
        (time.perf_counter() - started) * 1000,
    )
    payload = response.json() if response.content else {}
    rows: list[Any]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            rows = data
        elif isinstance(payload.get("tokens"), list):
            rows = payload.get("tokens", [])
        else:
            rows = []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    tokens: set[str] = set()
    now_epoch = int(time.time())
    for item in rows:
        if not isinstance(item, dict):
            continue
        token = str(item.get("token", "")).strip()
        if not token:
            continue
        try:
            usage_count = int(item.get("usageCount", 0))
        except (TypeError, ValueError):
            usage_count = 0
        try:
            usage_limit = int(item.get("usageLimit", 1))
        except (TypeError, ValueError):
            usage_limit = 1
        expires_at_raw = item.get("expiresAt")
        expires_at_epoch: int | None = None
        if isinstance(expires_at_raw, (int, float)):
            expires_at_epoch = int(expires_at_raw)
        elif isinstance(expires_at_raw, str):
            candidate = expires_at_raw.strip()
            if candidate:
                try:
                    expires_at_epoch = int(datetime.fromisoformat(candidate.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    expires_at_epoch = None
        if usage_limit > 0 and usage_count >= usage_limit:
            continue
        if expires_at_epoch is not None and expires_at_epoch <= now_epoch:
            continue
        tokens.add(token)
    return tokens


def create_product_user_with_signup(
    username: str | None = None,
    display_name: str | None = None,
    *,
    email: str | None = None,
    config: dict[str, Any] | None = None,
) -> ProductCreatedUser:
    if username is not None:
        _validate_username(username)
    if email is not None:
        _validate_optional_email(email)
    signup = ProductSignupToken.model_validate(create_product_signup_token(config=config))
    return ProductCreatedUser(user=None, signup=signup)
