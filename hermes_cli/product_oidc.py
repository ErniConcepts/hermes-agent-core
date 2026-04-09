"""Provider-neutral OIDC helpers for the hermes-core product layer."""

from __future__ import annotations

import base64
import hashlib
import secrets
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from hermes_cli.config import get_env_value
from hermes_cli.product_config import load_product_config

_OIDC_METADATA_CACHE_TTL_SECONDS = 600.0
_OIDC_METADATA_CACHE: dict[str, tuple[float, "ProductOIDCProviderMetadata"]] = {}


@dataclass(frozen=True)
class ProductOIDCClientSettings:
    issuer_url: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class ProductOIDCProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str | None = None
    end_session_endpoint: str | None = None
    jwks_uri: str | None = None
    registration_endpoint: str | None = None


def discover_product_oidc_provider_metadata_by_issuer(
    issuer_url: str,
    *,
    client: httpx.Client | None = None,
) -> ProductOIDCProviderMetadata:
    cache_key = _required_string(str(issuer_url).rstrip("/"), "auth.issuer_url")
    if client is None:
        cached = _OIDC_METADATA_CACHE.get(cache_key)
        if cached is not None:
            expires_at, metadata = cached
            if expires_at > time.monotonic():
                return metadata
            _OIDC_METADATA_CACHE.pop(cache_key, None)
    well_known_url = f"{cache_key}/.well-known/openid-configuration"
    owns_client = client is None
    http_client = client or httpx.Client(timeout=10.0)
    try:
        try:
            response = http_client.get(well_known_url)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError:
            payload = _try_local_tsidp_metadata(cache_key)
            if payload is None:
                raise
    finally:
        if owns_client:
            http_client.close()

    issuer = _required_string(str(payload.get("issuer", cache_key)), "oidc issuer")
    authorization_endpoint = _required_string(
        str(payload.get("authorization_endpoint", "")),
        "authorization_endpoint",
    )
    token_endpoint = _required_string(
        str(payload.get("token_endpoint", "")),
        "token_endpoint",
    )
    userinfo_endpoint = str(payload.get("userinfo_endpoint", "")).strip() or None
    end_session_endpoint = (
        str(payload.get("end_session_endpoint", "")).strip()
        or str(payload.get("end_session_endpoint_uri", "")).strip()
        or None
    )
    jwks_uri = str(payload.get("jwks_uri", "")).strip() or None
    registration_endpoint = str(payload.get("registration_endpoint", "")).strip() or None
    metadata = ProductOIDCProviderMetadata(
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        userinfo_endpoint=userinfo_endpoint,
        end_session_endpoint=end_session_endpoint,
        jwks_uri=jwks_uri,
        registration_endpoint=registration_endpoint,
    )
    if client is None:
        _OIDC_METADATA_CACHE[cache_key] = (time.monotonic() + _OIDC_METADATA_CACHE_TTL_SECONDS, metadata)
    return metadata


def _local_tsidp_metadata_payload(container_name: str) -> dict[str, Any]:
    command = [
        "docker",
        "exec",
        container_name,
        "wget",
        "-qO-",
        "http://127.0.0.1:8080/.well-known/openid-configuration",
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = httpx.Response(200, text=(result.stdout or "")).json()
    if not isinstance(payload, dict):
        raise RuntimeError("tsidp local metadata did not return a JSON object")
    return payload


def _try_local_tsidp_metadata(cache_key: str) -> dict[str, Any] | None:
    product_config = load_product_config()
    configured_issuer = str(product_config.get("auth", {}).get("issuer_url", "")).strip().rstrip("/")
    if not configured_issuer or configured_issuer != cache_key:
        return None
    container_name = str(
        product_config.get("services", {}).get("tsidp", {}).get("container_name", "hermes-tsidp")
    ).strip() or "hermes-tsidp"
    try:
        return _local_tsidp_metadata_payload(container_name)
    except Exception:
        return None


def _required_string(value: str, field_name: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError(f"{field_name} must not be empty")
    return candidate


def load_product_oidc_client_settings(
    config: Mapping[str, Any] | None = None,
) -> ProductOIDCClientSettings:
    from hermes_cli.product_stack import resolve_product_urls

    product_config = dict(config or load_product_config())
    auth = dict(product_config.get("auth", {}))
    urls = resolve_product_urls(product_config)

    issuer_url = _required_string(str(auth.get("issuer_url", "")), "auth.issuer_url")
    client_id = _required_string(str(auth.get("client_id", "")), "auth.client_id")
    client_secret_ref = _required_string(
        str(auth.get("client_secret_ref", "")),
        "auth.client_secret_ref",
    )
    client_secret = _required_string(
        get_env_value(client_secret_ref) or "",
        client_secret_ref,
    )
    return ProductOIDCClientSettings(
        issuer_url=issuer_url.rstrip("/"),
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=_required_string(urls["oidc_callback_url"], "oidc redirect_uri"),
        scopes=("openid", "profile", "email"),
    )


def discover_product_oidc_provider_metadata(
    settings: ProductOIDCClientSettings,
    *,
    client: httpx.Client | None = None,
) -> ProductOIDCProviderMetadata:
    return discover_product_oidc_provider_metadata_by_issuer(settings.issuer_url, client=client)


def clear_product_oidc_provider_metadata_cache() -> None:
    _OIDC_METADATA_CACHE.clear()


def create_pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def create_pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_oidc_login_request(
    settings: ProductOIDCClientSettings,
    metadata: ProductOIDCProviderMetadata,
    *,
    state: str | None = None,
    nonce: str | None = None,
    verifier: str | None = None,
    scopes: tuple[str, ...] | None = None,
) -> dict[str, str]:
    chosen_state = state or secrets.token_urlsafe(24)
    chosen_nonce = nonce or secrets.token_urlsafe(24)
    chosen_verifier = verifier or create_pkce_verifier()
    chosen_scopes = scopes or settings.scopes
    code_challenge = create_pkce_challenge(chosen_verifier)
    query = urlencode(
        {
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "response_type": "code",
            "scope": " ".join(chosen_scopes),
            "state": chosen_state,
            "nonce": chosen_nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return {
        "state": chosen_state,
        "nonce": chosen_nonce,
        "verifier": chosen_verifier,
        "authorization_url": f"{metadata.authorization_endpoint}?{query}",
    }


def exchange_product_oidc_code(
    settings: ProductOIDCClientSettings,
    metadata: ProductOIDCProviderMetadata,
    *,
    code: str,
    verifier: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    owns_client = client is None
    http_client = client or httpx.Client(timeout=10.0)
    try:
        response = http_client.post(
            metadata.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.redirect_uri,
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()
    finally:
        if owns_client:
            http_client.close()


def fetch_product_oidc_userinfo(
    access_token: str,
    metadata: ProductOIDCProviderMetadata,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    if not metadata.userinfo_endpoint:
        raise ValueError("OIDC provider metadata does not include a userinfo endpoint")

    owns_client = client is None
    http_client = client or httpx.Client(timeout=10.0)
    try:
        response = http_client.get(
            metadata.userinfo_endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http_client.close()

    if not isinstance(payload, dict):
        raise ValueError("userinfo response must be a JSON object")
    return payload


def validate_product_oidc_id_token(
    id_token: str,
    settings: ProductOIDCClientSettings,
    metadata: ProductOIDCProviderMetadata,
    *,
    nonce: str,
) -> dict[str, Any]:
    if not metadata.jwks_uri:
        raise ValueError("OIDC provider metadata does not include jwks_uri")
    signing_key = PyJWKClient(metadata.jwks_uri).get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
        audience=settings.client_id,
        issuer=metadata.issuer,
        options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
        nonce=nonce,
    )
