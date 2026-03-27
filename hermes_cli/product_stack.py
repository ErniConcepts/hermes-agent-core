"""Bundled product service generation for the hermes-core distribution."""

from __future__ import annotations

import ipaddress
import hashlib
import json
import logging
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any, Dict

import httpx

from hermes_cli.config import _secure_dir, _secure_file, get_env_value, save_env_value_secure
from hermes_cli.product_oidc import (
    ProductOIDCClientSettings,
    discover_product_oidc_provider_metadata,
    load_product_oidc_client_settings,
)
from hermes_cli.product_config import (
    ensure_product_home,
    get_product_storage_root,
    load_product_config,
    save_product_config,
)
from utils import atomic_json_write, atomic_yaml_write


POCKET_ID_IMAGE = "ghcr.io/pocket-id/pocket-id:v2"
_READY_TIMEOUT_SECONDS = 45.0
logger = logging.getLogger(__name__)


def get_product_services_root() -> Path:
    return get_product_storage_root() / "services"


def get_pocket_id_service_root() -> Path:
    return get_product_services_root() / "pocket-id"


def get_pocket_id_data_root() -> Path:
    return get_pocket_id_service_root() / "data"


def get_product_bootstrap_root() -> Path:
    return get_product_storage_root() / "bootstrap"


def get_first_admin_enrollment_state_path() -> Path:
    return get_product_bootstrap_root() / "first_admin_enrollment.json"


def get_tailnet_activation_state_path() -> Path:
    return get_product_bootstrap_root() / "tailnet_activation.json"


def get_tailnet_bridge_tokens_path() -> Path:
    return get_product_bootstrap_root() / "tailnet_bridge_tokens.json"


def get_pocket_id_compose_path() -> Path:
    return get_pocket_id_service_root() / "compose.yaml"


def get_pocket_id_env_path() -> Path:
    return get_pocket_id_service_root() / ".env"


def _secure_tree(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        _secure_dir(path)


def _permission_error_message(path: Path) -> str:
    return (
        f"Permission denied while writing {path}. "
        "This usually means files in ~/.hermes/product are owned by root from a previous sudo run. "
        "Fix ownership and rerun install: "
        "sudo chown -R \"$USER:$USER\" ~/.hermes/product ~/.hermes/product.yaml ~/.hermes/.env"
    )


def _public_host(config: Dict[str, Any]) -> str:
    host = str(config.get("network", {}).get("public_host", "")).strip()
    if not host:
        raise ValueError("product network.public_host must be configured")
    return host


def _url_scheme(config: Dict[str, Any]) -> str:
    network = config.get("network", {})
    configured = str(network.get("url_scheme", "")).strip().lower()
    if configured:
        if configured not in {"http", "https"}:
            raise ValueError("product network.url_scheme must be http or https")
        return configured
    return "http"


def _validate_public_host(host: str) -> None:
    candidate = (host or "").strip()
    if not candidate:
        raise ValueError("product network.public_host must not be empty")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
        raise ValueError("product network.public_host must not contain control characters")
    if any(ch.isspace() for ch in candidate):
        raise ValueError("product network.public_host must not contain whitespace")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return
    raise ValueError(
        "product network.public_host must be a hostname or domain, not a raw IP address"
    )


def _tailscale_config(config: Dict[str, Any]) -> Dict[str, Any]:
    network = config.get("network", {})
    tailscale = network.get("tailscale", {})
    return tailscale if isinstance(tailscale, dict) else {}


def _tailscale_enabled(config: Dict[str, Any]) -> bool:
    return bool(_tailscale_config(config).get("enabled", False))


def _required_tailnet_value(config: Dict[str, Any], key: str) -> str:
    value = str(_tailscale_config(config).get(key, "")).strip().lower()
    if not value:
        raise ValueError(f"product network.tailscale.{key} must be configured when Tailscale is enabled")
    return value


def _tailscale_host(config: Dict[str, Any]) -> str:
    device_name = _required_tailnet_value(config, "device_name")
    tailnet_name = _required_tailnet_value(config, "tailnet_name")
    return f"{device_name}.{tailnet_name}.ts.net"


def _tailscale_https_port(config: Dict[str, Any], key: str, default: int) -> int:
    raw_value = _tailscale_config(config).get(key, default)
    try:
        port = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"product network.tailscale.{key} must be an integer") from exc
    if port <= 0:
        raise ValueError(f"product network.tailscale.{key} must be positive")
    return port


def _format_https_url(host: str, port: int) -> str:
    if port == 443:
        return f"https://{host}"
    return f"https://{host}:{port}"


def _tailnet_bootstrap_complete(config: Dict[str, Any] | None = None) -> bool:
    product_config = config or load_product_config()
    if not _tailscale_enabled(product_config):
        return False
    state = load_first_admin_enrollment_state() or {}
    return bool(state.get("first_admin_login_seen", False))


def load_tailnet_activation_state() -> Dict[str, Any] | None:
    state_path = get_tailnet_activation_state_path()
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def _tailnet_activation_status(config: Dict[str, Any] | None = None) -> str:
    product_config = config or load_product_config()
    if not _tailscale_enabled(product_config):
        return "disabled"
    state = load_tailnet_activation_state() or {}
    status = str(state.get("status", "")).strip().lower()
    if status in {"inactive", "pending", "active"}:
        return status
    return "inactive"


def _tailnet_activation_complete(config: Dict[str, Any] | None = None) -> bool:
    return _tailnet_activation_status(config) == "active"


def _save_tailnet_activation_state(state: Dict[str, Any]) -> Dict[str, Any]:
    state_path = get_tailnet_activation_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(state_path.parent)
    atomic_json_write(state_path, state)
    _secure_file(state_path)
    return state


def enable_tailnet_activation() -> Dict[str, Any]:
    product_config = load_product_config()
    if not _tailscale_enabled(product_config):
        raise RuntimeError("Tailscale is not configured for this product install")
    state = {
        "status": "active",
        "activated_at": int(time.time()),
    }
    _save_tailnet_activation_state(state)
    ensure_product_tailnet_started(product_config, include_app=True, include_auth=False)
    return state


def _format_tailscale_reset_error(exc: subprocess.CalledProcessError, *, command: list[str]) -> str:
    detail = (exc.stderr or exc.stdout or "").strip()
    command_text = " ".join(command)
    message = f"Failed to disable Tailscale HTTPS exposure with: {command_text}"
    if detail:
        message = f"{message}\n{detail}"
    return message


def ensure_product_tailnet_stopped(config: Dict[str, Any] | None = None) -> list[subprocess.CompletedProcess[str]]:
    product_config = config or load_product_config()
    if not _tailscale_enabled(product_config):
        return []
    command = [_tailscale_command_path(product_config), "serve", "reset"]
    try:
        return [subprocess.run(command, check=True, capture_output=True, text=True)]
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(_format_tailscale_reset_error(exc, command=command)) from exc


def disable_tailnet_activation() -> Dict[str, Any]:
    product_config = load_product_config()
    state = {
        "status": "inactive",
        "activated_at": None,
    }
    _save_tailnet_activation_state(state)
    ensure_product_tailnet_stopped(product_config)
    return state


def _load_tailnet_bridge_tokens() -> list[Dict[str, Any]]:
    state_path = get_tailnet_bridge_tokens_path()
    if not state_path.exists():
        return []
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else []


def _save_tailnet_bridge_tokens(tokens: list[Dict[str, Any]]) -> None:
    state_path = get_tailnet_bridge_tokens_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(state_path.parent)
    atomic_json_write(state_path, tokens)
    _secure_file(state_path)


def _tailnet_bridge_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_tailnet_bridge_token(user_id: str, *, target_origin: str) -> Dict[str, Any]:
    if not user_id:
        raise ValueError("Tailnet bridge tokens require a user id")
    now = int(time.time())
    token = secrets.token_urlsafe(32)
    token_hash = _tailnet_bridge_token_hash(token)
    tokens = _load_tailnet_bridge_tokens()
    tokens.append(
        {
            "token_hash": token_hash,
            "user_id": user_id,
            "target_origin": target_origin,
            "created_at": now,
            "expires_at": now + 600,
            "used_at": None,
        }
    )
    _save_tailnet_bridge_tokens(tokens)
    return {"token": token, "expires_at": now + 600}


def consume_tailnet_bridge_token(token: str, *, target_origin: str) -> Dict[str, Any] | None:
    candidate = str(token or "").strip()
    if not candidate:
        return None
    token_hash = _tailnet_bridge_token_hash(candidate)
    now = int(time.time())
    tokens = _load_tailnet_bridge_tokens()
    changed = False
    matched: Dict[str, Any] | None = None
    retained: list[Dict[str, Any]] = []
    for item in tokens:
        if not isinstance(item, dict):
            changed = True
            continue
        expires_at = int(item.get("expires_at", 0) or 0)
        used_at = item.get("used_at")
        if expires_at and expires_at <= now:
            changed = True
            continue
        if item.get("token_hash") == token_hash:
            if used_at:
                changed = True
                continue
            if str(item.get("target_origin", "")).strip() != target_origin:
                retained.append(item)
                matched = None
                continue
            item = dict(item)
            item["used_at"] = now
            matched = item
            changed = True
        retained.append(item)
    if changed:
        _save_tailnet_bridge_tokens(retained)
    return matched


def resolve_product_urls(config: Dict[str, Any] | None = None) -> Dict[str, str]:
    product_config = config or load_product_config()
    network = product_config.get("network", {})
    app_port = int(network.get("app_port", 8086))
    pocket_id_port = int(network.get("pocket_id_port", 1411))
    public_host = _public_host(product_config)
    _validate_public_host(public_host)
    scheme = _url_scheme(product_config)
    local_app_base_url = f"{scheme}://{public_host}:{app_port}"
    local_issuer_url = f"{scheme}://{public_host}:{pocket_id_port}"

    if _tailscale_enabled(product_config):
        tailnet_host = _tailscale_host(product_config)
        app_https_port = _tailscale_https_port(product_config, "app_https_port", 443)
        auth_https_port = _tailscale_https_port(product_config, "auth_https_port", 4444)
        tailnet_app_base_url = _format_https_url(tailnet_host, app_https_port)
        tailnet_issuer_url = _format_https_url(tailnet_host, auth_https_port)
        activation_status = _tailnet_activation_status(product_config)
        return {
            "public_host": public_host,
            "url_scheme": scheme,
            "app_base_url": local_app_base_url,
            "issuer_url": local_issuer_url,
            "oidc_callback_url": f"{local_app_base_url}/api/auth/oidc/callback",
            "pocket_id_setup_url": f"{local_issuer_url}/setup",
            "local_app_base_url": local_app_base_url,
            "local_issuer_url": local_issuer_url,
            "tailnet_host": tailnet_host,
            "tailnet_app_base_url": tailnet_app_base_url,
            "tailnet_issuer_url": tailnet_issuer_url,
            "tailnet_activation_status": activation_status,
            "tailnet_active": activation_status == "active",
        }

    return {
        "public_host": public_host,
        "url_scheme": scheme,
        "app_base_url": local_app_base_url,
        "issuer_url": local_issuer_url,
        "oidc_callback_url": f"{local_app_base_url}/api/auth/oidc/callback",
        "pocket_id_setup_url": f"{local_issuer_url}/setup",
    }


def _tailscale_command_path(config: Dict[str, Any]) -> str:
    configured = str(_tailscale_config(config).get("command_path", "tailscale")).strip()
    if not configured:
        raise ValueError("product network.tailscale.command_path must not be empty")
    return configured


def _tailscale_serve_command(config: Dict[str, Any], *, https_port: int, target_url: str) -> list[str]:
    return [
        _tailscale_command_path(config),
        "serve",
        "--bg",
        f"--https={https_port}",
        target_url,
    ]


def _format_tailscale_serve_error(
    exc: subprocess.CalledProcessError,
    *,
    command: list[str],
) -> str:
    detail = (exc.stderr or exc.stdout or "").strip()
    command_text = " ".join(command)
    lowered = detail.lower()
    if "serve config denied" in lowered or "set --operator" in lowered:
        return (
            "Failed to configure Tailscale HTTPS exposure because the current user is not "
            "allowed to manage 'tailscale serve'. Run this once on the host and retry:\n"
            '  sudo tailscale set --operator="$USER"\n'
            f"Then rerun the install/setup command. Failing command: {command_text}"
        )
    message = f"Failed to configure Tailscale HTTPS exposure with: {command_text}"
    if detail:
        message = f"{message}\n{detail}"
    return message


def _first_admin_bootstrap_completed() -> bool:
    state = load_first_admin_enrollment_state() or {}
    return bool(state.get("first_admin_login_seen", False))


def ensure_product_tailnet_started(
    config: Dict[str, Any] | None = None,
    *,
    include_app: bool = True,
    include_auth: bool | None = None,
) -> list[subprocess.CompletedProcess[str]]:
    product_config = config or load_product_config()
    if not _tailscale_enabled(product_config):
        return []

    network = product_config.get("network", {})
    app_port = int(network.get("app_port", 8086))
    app_https_port = _tailscale_https_port(product_config, "app_https_port", 443)
    auth_https_port = _tailscale_https_port(product_config, "auth_https_port", 4444)
    auth_target_url = f"http://127.0.0.1:{int(network.get('pocket_id_port', 1411))}"

    auth_enabled = _first_admin_bootstrap_completed() if include_auth is None else include_auth

    commands: list[list[str]] = []
    if include_app:
        commands.append(
            _tailscale_serve_command(
                product_config,
                https_port=app_https_port,
                target_url=f"http://127.0.0.1:{app_port}",
            )
        )
    if auth_enabled:
        commands.append(
            _tailscale_serve_command(
                product_config,
                https_port=auth_https_port,
                target_url=auth_target_url,
            )
        )
    results: list[subprocess.CompletedProcess[str]] = []
    for command in commands:
        try:
            results.append(subprocess.run(command, check=True, capture_output=True, text=True))
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(_format_tailscale_serve_error(exc, command=command)) from exc
    return results


def _required_secret(env_key: str) -> str:
    current = (get_env_value(env_key) or "").strip()
    if current:
        return current
    generated = secrets.token_urlsafe(48)
    save_env_value_secure(env_key, generated)
    return generated


def _pocket_id_upstream_base_url(config: Dict[str, Any]) -> str:
    services_cfg = config.get("services", {}).get("pocket_id", {})
    upstream_port = int(services_cfg.get("upstream_port", 19141))
    if upstream_port <= 0:
        raise ValueError("services.pocket_id.upstream_port must be a positive integer")
    return f"http://127.0.0.1:{upstream_port}"


def _ensure_client_secret(config: Dict[str, Any]) -> str:
    env_key = str(config.get("auth", {}).get("client_secret_ref", "")).strip()
    if not env_key:
        raise ValueError("auth.client_secret_ref must be configured in product.yaml")
    return _required_secret(env_key)


def _ensure_session_secret(config: Dict[str, Any]) -> str:
    env_key = str(config.get("auth", {}).get("session_secret_ref", "")).strip()
    if not env_key:
        raise ValueError("auth.session_secret_ref must be configured in product.yaml")
    return _required_secret(env_key)


def _ensure_static_api_key(config: Dict[str, Any]) -> str:
    env_key = str(
        config.get("services", {}).get("pocket_id", {}).get("static_api_key_ref", "")
    ).strip()
    if not env_key:
        raise ValueError("services.pocket_id.static_api_key_ref must be configured in product.yaml")
    return _required_secret(env_key)


def _ensure_encryption_key(config: Dict[str, Any]) -> str:
    env_key = str(
        config.get("services", {}).get("pocket_id", {}).get("encryption_key_ref", "")
    ).strip()
    if not env_key:
        raise ValueError("services.pocket_id.encryption_key_ref must be configured in product.yaml")
    current = (get_env_value(env_key) or "").strip()
    if current:
        return current
    generated = secrets.token_urlsafe(32)
    save_env_value_secure(env_key, generated)
    return generated


def _build_env_file(config: Dict[str, Any]) -> str:
    network = config.get("network", {})
    bind_host = str(network.get("bind_host", "")).strip()
    if not bind_host:
        raise ValueError("product network.bind_host must be configured")
    return "\n".join(
        [
            f"APP_URL={resolve_product_urls(config)['issuer_url']}",
            f"ENCRYPTION_KEY={_ensure_encryption_key(config)}",
            f"STATIC_API_KEY={_ensure_static_api_key(config)}",
            f"HOST={bind_host}",
            "PORT=1411",
            "",
        ]
    )


def _build_compose_spec(config: Dict[str, Any]) -> Dict[str, Any]:
    network = config.get("network", {})
    services_cfg = config.get("services", {}).get("pocket_id", {})
    bind_host = str(network.get("bind_host", "")).strip()
    if not bind_host:
        raise ValueError("product network.bind_host must be configured")
    upstream_port = int(services_cfg.get("upstream_port", 19141))
    if upstream_port <= 0:
        raise ValueError("services.pocket_id.upstream_port must be a positive integer")
    container_name = str(services_cfg.get("container_name", "")).strip()
    if not container_name:
        raise ValueError("services.pocket_id.container_name must be configured")
    data_root = get_pocket_id_data_root().as_posix()
    image = str(services_cfg.get("image", "")).strip()
    if not image:
        raise ValueError("services.pocket_id.image must be configured")
    service: Dict[str, Any] = {
        "image": image,
        "container_name": container_name,
        "restart": "unless-stopped",
        "env_file": [get_pocket_id_env_path().as_posix()],
        "ports": [f"127.0.0.1:{upstream_port}:1411"],
        "volumes": [f"{data_root}:/app/data"],
        "healthcheck": {
            "test": ["CMD", "/app/pocket-id", "healthcheck"],
            "interval": "90s",
            "timeout": "5s",
            "retries": 2,
            "start_period": "10s",
        },
    }
    return {"services": {"pocket-id": service}}


def initialize_product_stack(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    product_config = config or load_product_config()
    ensure_product_home()
    _secure_tree(
        get_product_services_root(),
        get_pocket_id_service_root(),
        get_pocket_id_data_root(),
        get_product_bootstrap_root(),
    )

    urls = resolve_product_urls(product_config)
    product_config["network"]["public_host"] = urls["public_host"]
    product_config["auth"]["provider"] = "pocket-id"
    product_config["auth"]["issuer_url"] = urls["issuer_url"]
    services_cfg = product_config.setdefault("services", {}).setdefault("pocket_id", {})
    services_cfg["mode"] = str(services_cfg.get("mode", "docker")).strip() or "docker"
    services_cfg["container_name"] = str(services_cfg.get("container_name", "hermes-pocket-id")).strip() or "hermes-pocket-id"
    services_cfg["image"] = str(services_cfg.get("image", POCKET_ID_IMAGE)).strip() or POCKET_ID_IMAGE
    services_cfg.pop("puid", None)
    services_cfg.pop("pgid", None)
    services_cfg.pop("user", None)

    _ensure_client_secret(product_config)
    _ensure_session_secret(product_config)

    env_path = get_pocket_id_env_path()
    try:
        env_path.write_text(_build_env_file(product_config), encoding="utf-8")
    except PermissionError as exc:
        raise RuntimeError(_permission_error_message(env_path)) from exc
    _secure_file(env_path)

    compose_path = get_pocket_id_compose_path()
    try:
        atomic_yaml_write(compose_path, _build_compose_spec(product_config))
    except PermissionError as exc:
        raise RuntimeError(_permission_error_message(compose_path)) from exc
    _secure_file(compose_path)

    save_product_config(product_config)
    return product_config


def ensure_product_stack_started(config: Dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    product_config = config or initialize_product_stack()
    compose_path = get_pocket_id_compose_path()
    command = ["docker", "compose", "-f", str(compose_path), "up", "-d", "--wait", "--force-recreate"]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"Failed to start Pocket ID with docker compose ({compose_path})"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from exc
    ensure_product_tailnet_started(product_config)
    return result


def _wait_for_pocket_id_ready(config: Dict[str, Any], timeout_seconds: float = _READY_TIMEOUT_SECONDS) -> None:
    health_url = _pocket_id_upstream_base_url(config) + "/.well-known/openid-configuration"
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(health_url, timeout=5.0)
            if response.status_code == 200:
                return
            last_error = RuntimeError(f"Pocket ID health endpoint returned {response.status_code}")
        except Exception as exc:  # pragma: no cover - exercised via retry path
            last_error = exc
        time.sleep(1.0)
    raise RuntimeError(f"Pocket ID did not become ready at {health_url}: {last_error}")


def _api_headers(config: Dict[str, Any]) -> Dict[str, str]:
    return {"X-API-Key": _ensure_static_api_key(config)}


def _oidc_client_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    urls = resolve_product_urls(config)
    brand_name = str(config.get("product", {}).get("brand", {}).get("name", "Hermes Core")).strip() or "Hermes Core"
    return {
        "id": str(config.get("auth", {}).get("client_id", "hermes-core")).strip() or "hermes-core",
        "name": brand_name,
        "callbackURLs": [urls["oidc_callback_url"]],
        "logoutCallbackURLs": [urls["app_base_url"]],
        "isPublic": False,
        "pkceEnabled": True,
        "requiresReauthentication": False,
        "credentials": {"federatedIdentities": []},
        "launchURL": urls["app_base_url"],
        "hasLogo": False,
        "hasDarkLogo": False,
        "isGroupRestricted": False,
    }


def _request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    expected_status: int,
    **kwargs: Any,
) -> Dict[str, Any]:
    response = client.request(method, url, **kwargs)
    if response.status_code != expected_status:
        raise RuntimeError(f"{method} {url} failed with {response.status_code}: {response.text}")
    return response.json() if response.content else {}


def _ensure_signup_mode_with_token(config: Dict[str, Any]) -> None:
    base_url = _pocket_id_upstream_base_url(config)
    headers = _api_headers(config)
    with httpx.Client(base_url=base_url, headers=headers, timeout=10.0) as client:
        current_response = client.get("/api/application-configuration/all")
        if current_response.status_code != 200:
            raise RuntimeError(
                f"GET {base_url}/api/application-configuration/all failed with "
                f"{current_response.status_code}: {current_response.text}"
            )
        rows = current_response.json() if current_response.content else []
        if not isinstance(rows, list):
            raise RuntimeError("Pocket ID returned invalid application configuration payload")
        payload: Dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key", "")).strip()
            if not key:
                continue
            payload[key] = str(row.get("value", ""))
        if not payload:
            raise RuntimeError("Pocket ID application configuration payload was empty")
        if payload.get("allowUserSignups") == "withToken":
            return
        payload["allowUserSignups"] = "withToken"
        update_response = client.put("/api/application-configuration", json=payload)
        if update_response.status_code != 200:
            raise RuntimeError(
                f"PUT {base_url}/api/application-configuration failed with "
                f"{update_response.status_code}: {update_response.text}"
            )


def bootstrap_product_oidc_client(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    product_config = initialize_product_stack(config or load_product_config())
    ensure_product_stack_started(product_config)
    _wait_for_pocket_id_ready(product_config)
    try:
        _ensure_signup_mode_with_token(product_config)
    except Exception as exc:  # pragma: no cover - defensive behavior for external API variance
        logger.warning("Failed to enforce Pocket ID allowUserSignups=withToken: %s", exc)

    urls = resolve_product_urls(product_config)
    client_payload = _oidc_client_payload(product_config)
    client_id = client_payload["id"]
    base_url = _pocket_id_upstream_base_url(product_config)
    headers = _api_headers(product_config)

    with httpx.Client(base_url=base_url, headers=headers, timeout=10.0) as client:
        get_response = client.get(f"/api/oidc/clients/{client_id}")
        if get_response.status_code == 404:
            _request_json(client, "POST", "/api/oidc/clients", expected_status=201, json=client_payload)
        elif get_response.status_code == 200:
            _request_json(
                client,
                "PUT",
                f"/api/oidc/clients/{client_id}",
                expected_status=200,
                json={key: value for key, value in client_payload.items() if key != "id"},
            )
        else:
            raise RuntimeError(
                f"GET {base_url}/api/oidc/clients/{client_id} failed with "
                f"{get_response.status_code}: {get_response.text}"
            )

        secret_response = _request_json(
            client,
            "POST",
            f"/api/oidc/clients/{client_id}/secret",
            expected_status=200,
        )

    client_secret = str(secret_response.get("secret", "")).strip()
    if not client_secret:
        raise RuntimeError("Pocket ID did not return an OIDC client secret")
    save_env_value_secure(str(product_config["auth"]["client_secret_ref"]), client_secret)
    settings = load_product_oidc_client_settings(product_config)
    upstream_settings = ProductOIDCClientSettings(
        issuer_url=_pocket_id_upstream_base_url(product_config),
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        redirect_uri=settings.redirect_uri,
        scopes=settings.scopes,
    )
    metadata = discover_product_oidc_provider_metadata(upstream_settings)
    return {
        "client_id": client_id,
        "issuer_url": settings.issuer_url,
        "callback_url": urls["oidc_callback_url"],
        "authorization_endpoint": metadata.authorization_endpoint,
        "token_endpoint": metadata.token_endpoint,
    }


def load_first_admin_enrollment_state() -> Dict[str, Any] | None:
    state_path = get_first_admin_enrollment_state_path()
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def _first_admin_bootstrap_mode(config: Dict[str, Any]) -> str:
    bootstrap_cfg = config.get("bootstrap", {})
    forced_mode = str(bootstrap_cfg.get("first_admin_bootstrap_mode", "")).strip().lower()
    if forced_mode in {"token", "native_setup"}:
        return forced_mode
    # Conservative default: native Pocket ID setup flow.
    # We only switch to token mode when explicitly enabled and supported.
    tokenized_supported = bool(bootstrap_cfg.get("tokenized_first_admin_supported", False))
    return "token" if tokenized_supported else "native_setup"


def mark_first_admin_bootstrap_completed() -> Dict[str, Any] | None:
    state = load_first_admin_enrollment_state()
    if not state:
        return None
    if bool(state.get("first_admin_login_seen")):
        return state
    state["first_admin_login_seen"] = True
    state["bootstrap_completed_at"] = int(time.time())
    state_path = get_first_admin_enrollment_state_path()
    atomic_json_write(state_path, state)
    _secure_file(state_path)
    product_config = load_product_config()
    save_product_config(initialize_product_stack(product_config))
    bootstrap_product_oidc_client(product_config)
    # Once first admin bootstrap is complete, expose auth on tailnet as well.
    try:
        ensure_product_tailnet_started()
    except Exception as exc:  # pragma: no cover - defensive for external tailscale variance
        logger.warning("Failed to refresh tailscale auth exposure after bootstrap completion: %s", exc)
    return state


def bootstrap_first_admin_enrollment(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    product_config = initialize_product_stack(config or load_product_config())
    oidc_state = bootstrap_product_oidc_client(product_config)
    existing_state = load_first_admin_enrollment_state()
    bootstrap_mode = _first_admin_bootstrap_mode(product_config)

    username = str(product_config.get("bootstrap", {}).get("first_admin_username", "admin")).strip() or "admin"
    display_name = str(
        product_config.get("bootstrap", {}).get("first_admin_display_name", "Administrator")
    ).strip() or "Administrator"
    email = str(product_config.get("bootstrap", {}).get("first_admin_email", "")).strip()
    state = {
        "username": username,
        "display_name": display_name,
        "email": email,
        "auth_mode": str(product_config.get("auth", {}).get("mode", "passkey")).strip() or "passkey",
        "bootstrap_mode": bootstrap_mode,
        "setup_url": resolve_product_urls(product_config)["pocket_id_setup_url"] if bootstrap_mode == "native_setup" else "",
        "oidc_client_id": oidc_state["client_id"],
        "first_admin_login_seen": bool(existing_state.get("first_admin_login_seen", False)) if existing_state else False,
        "bootstrap_completed_at": existing_state.get("bootstrap_completed_at") if existing_state else None,
    }
    if existing_state == state:
        return existing_state

    state_path = get_first_admin_enrollment_state_path()
    atomic_json_write(state_path, state)
    _secure_file(state_path)
    return state
