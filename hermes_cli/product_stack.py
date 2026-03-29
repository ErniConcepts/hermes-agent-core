"""Bundled product service generation for the hermes-core distribution."""

from __future__ import annotations

import ipaddress
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


TSIDP_IMAGE = "ghcr.io/tailscale/tsidp:latest"
_READY_TIMEOUT_SECONDS = 45.0
logger = logging.getLogger(__name__)


def get_product_services_root() -> Path:
    return get_product_storage_root() / "services"


def get_tsidp_service_root() -> Path:
    return get_product_services_root() / "tsidp"


def get_tsidp_data_root() -> Path:
    return get_tsidp_service_root() / "data"


def get_product_bootstrap_root() -> Path:
    return get_product_storage_root() / "bootstrap"


def get_first_admin_enrollment_state_path() -> Path:
    return get_product_bootstrap_root() / "first_admin_enrollment.json"


def get_tsidp_compose_path() -> Path:
    return get_tsidp_service_root() / "compose.yaml"


def get_tsidp_env_path() -> Path:
    return get_tsidp_service_root() / ".env"


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


def _tsidp_hostname(config: Dict[str, Any]) -> str:
    value = str(_tailscale_config(config).get("idp_hostname", "idp")).strip().lower()
    if not value:
        raise ValueError("product network.tailscale.idp_hostname must not be empty")
    return value


def _tsidp_host(config: Dict[str, Any]) -> str:
    return f"{_tsidp_hostname(config)}.{_required_tailnet_value(config, 'tailnet_name')}.ts.net"


def _tsidp_issuer_url(config: Dict[str, Any]) -> str:
    return f"https://{_tsidp_host(config)}"


def _format_https_url(host: str, port: int) -> str:
    if port == 443:
        return f"https://{host}"
    return f"https://{host}:{port}"


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
def resolve_product_urls(config: Dict[str, Any] | None = None) -> Dict[str, str]:
    product_config = config or load_product_config()
    if not _tailscale_enabled(product_config):
        raise ValueError("Tailscale must be enabled for this product install")
    network = product_config.get("network", {})
    app_port = int(network.get("app_port", 8086))
    tailnet_host = _tailscale_host(product_config)
    app_https_port = _tailscale_https_port(product_config, "app_https_port", 443)
    tailnet_app_base_url = _format_https_url(tailnet_host, app_https_port)
    tailnet_issuer_url = _tsidp_issuer_url(product_config)
    return {
        "public_host": tailnet_host,
        "url_scheme": "https",
        "app_base_url": tailnet_app_base_url,
        "issuer_url": tailnet_issuer_url,
        "oidc_callback_url": f"{tailnet_app_base_url}/api/auth/oidc/callback",
        "tailnet_host": tailnet_host,
        "tailnet_app_base_url": tailnet_app_base_url,
        "tailnet_issuer_url": tailnet_issuer_url,
        "local_app_base_url": f"http://127.0.0.1:{app_port}",
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
) -> list[subprocess.CompletedProcess[str]]:
    product_config = config or load_product_config()
    if not _tailscale_enabled(product_config):
        return []

    network = product_config.get("network", {})
    app_port = int(network.get("app_port", 8086))
    app_https_port = _tailscale_https_port(product_config, "app_https_port", 443)

    commands: list[list[str]] = []
    if include_app:
        commands.append(
            _tailscale_serve_command(
                product_config,
                https_port=app_https_port,
                target_url=f"http://127.0.0.1:{app_port}",
            )
        )
    results: list[subprocess.CompletedProcess[str]] = []
    for command in commands:
        try:
            results.append(subprocess.run(command, check=True, capture_output=True, text=True))
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(_format_tailscale_serve_error(exc, command=command)) from exc
    return results


def _tsidp_service_config(config: Dict[str, Any]) -> Dict[str, Any]:
    services_cfg = config.setdefault("services", {}).setdefault("tsidp", {})
    services_cfg["mode"] = str(services_cfg.get("mode", "docker")).strip() or "docker"
    services_cfg["container_name"] = str(services_cfg.get("container_name", "hermes-tsidp")).strip() or "hermes-tsidp"
    services_cfg["image"] = str(services_cfg.get("image", TSIDP_IMAGE)).strip() or TSIDP_IMAGE
    auth_key_ref = str(services_cfg.get("auth_key_ref", "HERMES_PRODUCT_TAILSCALE_AUTH_KEY")).strip()
    if not auth_key_ref:
        raise ValueError("services.tsidp.auth_key_ref must be configured")
    services_cfg["auth_key_ref"] = auth_key_ref
    advertise_tags = services_cfg.get("advertise_tags", ["tag:tsidp"])
    if not isinstance(advertise_tags, list) or not advertise_tags:
        advertise_tags = ["tag:tsidp"]
    services_cfg["advertise_tags"] = [str(item).strip() for item in advertise_tags if str(item).strip()]
    return services_cfg


def _tsidp_auth_key(config: Dict[str, Any]) -> str:
    env_key = str(_tsidp_service_config(config).get("auth_key_ref", "")).strip()
    return str(get_env_value(env_key) or "").strip()


def _build_tsidp_env_file(config: Dict[str, Any]) -> str:
    services_cfg = _tsidp_service_config(config)
    lines = [
        "TAILSCALE_USE_WIP_CODE=1",
        f"TS_HOSTNAME={_tsidp_hostname(config)}",
        "TS_STATE_DIR=/data",
        "TSIDP_LOCAL_PORT=8080",
        "TSIDP_ENABLE_STS=1",
    ]
    auth_key = _tsidp_auth_key(config)
    if auth_key:
        lines.append(f"TS_AUTHKEY={auth_key}")
    advertise_tags = services_cfg.get("advertise_tags", [])
    if advertise_tags:
        lines.append(f"TS_ADVERTISE_TAGS={','.join(str(item) for item in advertise_tags)}")
    lines.append("")
    return "\n".join(lines)


def _build_tsidp_compose_spec(config: Dict[str, Any]) -> Dict[str, Any]:
    services_cfg = _tsidp_service_config(config)
    data_root_path = get_tsidp_data_root()
    data_root = data_root_path.as_posix()
    owner = data_root_path.stat()
    service: Dict[str, Any] = {
        "image": str(services_cfg["image"]),
        "container_name": str(services_cfg["container_name"]),
        "restart": "unless-stopped",
        "env_file": [get_tsidp_env_path().as_posix()],
        "volumes": [f"{data_root}:/data"],
        "user": f"{owner.st_uid}:{owner.st_gid}",
        "healthcheck": {
            "test": ["CMD", "wget", "-qO-", "http://127.0.0.1:8080/.well-known/openid-configuration"],
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "10s",
        },
    }
    return {"services": {"tsidp": service}}


def ensure_product_tsidp_started(config: Dict[str, Any] | None = None) -> subprocess.CompletedProcess[str] | None:
    product_config = config or load_product_config()
    if not _tailscale_enabled(product_config):
        return None
    compose_path = get_tsidp_compose_path()
    command = ["docker", "compose", "-f", str(compose_path), "up", "-d", "--wait", "--force-recreate"]
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"Failed to start tsidp with docker compose ({compose_path})"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from exc


def _wait_for_tsidp_ready(config: Dict[str, Any], timeout_seconds: float = _READY_TIMEOUT_SECONDS) -> None:
    health_url = _tsidp_issuer_url(config).rstrip("/") + "/.well-known/openid-configuration"
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(health_url, timeout=5.0)
            if response.status_code == 200:
                return
            last_error = RuntimeError(f"tsidp health endpoint returned {response.status_code}")
        except Exception as exc:
            last_error = exc
        time.sleep(1.0)
    raise RuntimeError(
        f"tsidp did not become ready at {health_url}: {last_error}. "
        "Check that Tailscale is installed, connected, MagicDNS is enabled, and tsidp is allowed on this tailnet."
    )


def _required_secret(env_key: str) -> str:
    current = (get_env_value(env_key) or "").strip()
    if current:
        return current
    generated = secrets.token_urlsafe(48)
    save_env_value_secure(env_key, generated)
    return generated


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


def initialize_product_stack(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    product_config = config or load_product_config()
    if not _tailscale_enabled(product_config):
        raise RuntimeError("Tailscale must be enabled for this product install")
    ensure_product_home()
    _secure_tree(
        get_product_services_root(),
        get_tsidp_service_root(),
        get_tsidp_data_root(),
        get_product_bootstrap_root(),
    )

    urls = resolve_product_urls(product_config)
    product_config["network"]["public_host"] = urls["public_host"]
    product_config["auth"]["provider"] = "tsidp"
    product_config["auth"]["issuer_url"] = urls["issuer_url"]
    _tsidp_service_config(product_config)
    _ensure_session_secret(product_config)

    tsidp_env_path = get_tsidp_env_path()
    try:
        tsidp_env_path.write_text(_build_tsidp_env_file(product_config), encoding="utf-8")
    except PermissionError as exc:
        raise RuntimeError(_permission_error_message(tsidp_env_path)) from exc
    _secure_file(tsidp_env_path)

    tsidp_compose_path = get_tsidp_compose_path()
    try:
        atomic_yaml_write(tsidp_compose_path, _build_tsidp_compose_spec(product_config))
    except PermissionError as exc:
        raise RuntimeError(_permission_error_message(tsidp_compose_path)) from exc
    _secure_file(tsidp_compose_path)

    save_product_config(product_config)
    return product_config


def ensure_product_stack_started(config: Dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    product_config = config or initialize_product_stack()
    result = ensure_product_tsidp_started(product_config)
    ensure_product_tailnet_started(product_config, include_app=True)
    return result if result is not None else subprocess.CompletedProcess([], 0, "", "")


def bootstrap_product_oidc_client(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    product_config = initialize_product_stack(config or load_product_config())
    ensure_product_stack_started(product_config)
    _wait_for_tsidp_ready(product_config)
    settings = load_product_oidc_client_settings(product_config)
    metadata = discover_product_oidc_provider_metadata(settings)
    return {
        "client_id": settings.client_id,
        "issuer_url": settings.issuer_url,
        "callback_url": settings.redirect_uri,
        "authorization_endpoint": metadata.authorization_endpoint,
        "token_endpoint": metadata.token_endpoint,
    }


def load_product_tailscale_oidc_client_settings(
    config: Dict[str, Any] | None = None,
) -> ProductOIDCClientSettings:
    return load_product_oidc_client_settings(config or load_product_config())


def _tailscale_oidc_registration_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    urls = resolve_product_urls(config)
    brand_name = str(config.get("product", {}).get("brand", {}).get("name", "Hermes Core")).strip() or "Hermes Core"
    return {
        "client_name": f"{brand_name} Tailnet",
        "redirect_uris": [f"{urls['tailnet_app_base_url'].rstrip('/')}/api/auth/tailscale/callback"],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "scope": "openid profile email",
        "token_endpoint_auth_method": "client_secret_post",
    }


def bootstrap_product_tailscale_oidc_client(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return bootstrap_product_oidc_client(config)


def load_first_admin_enrollment_state() -> Dict[str, Any] | None:
    state_path = get_first_admin_enrollment_state_path()
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def mark_first_admin_bootstrap_completed(tailscale_login: str | None = None) -> Dict[str, Any] | None:
    state = load_first_admin_enrollment_state()
    if not state:
        return None
    if bool(state.get("first_admin_login_seen")):
        return state
    state["first_admin_login_seen"] = True
    state["bootstrap_completed_at"] = int(time.time())
    state["bootstrap_token"] = ""
    state["bootstrap_url"] = resolve_product_urls(load_product_config())["app_base_url"]
    normalized_login = str(tailscale_login or "").strip().lower()
    if normalized_login:
        state["tailscale_login"] = normalized_login
    state_path = get_first_admin_enrollment_state_path()
    atomic_json_write(state_path, state)
    _secure_file(state_path)
    return state


def bootstrap_first_admin_enrollment(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    product_config = initialize_product_stack(config or load_product_config())
    oidc_state = bootstrap_product_oidc_client(product_config)
    existing_state = load_first_admin_enrollment_state()
    existing_login = str(existing_state.get("tailscale_login", "")).strip().lower() if existing_state else ""
    username = str(product_config.get("bootstrap", {}).get("first_admin_username", "")).strip() or "admin"
    display_name = str(
        product_config.get("bootstrap", {}).get("first_admin_display_name", "Administrator")
    ).strip() or "Administrator"
    email = existing_login if "@" in existing_login else ""
    first_admin_login_seen = bool(existing_state.get("first_admin_login_seen", False)) if existing_state else False
    bootstrap_token = ""
    if not first_admin_login_seen:
        bootstrap_token = str(existing_state.get("bootstrap_token", "")).strip() if existing_state else ""
        if not bootstrap_token:
            bootstrap_token = secrets.token_urlsafe(24)
    app_base_url = resolve_product_urls(product_config)["app_base_url"]
    bootstrap_url = f"{app_base_url.rstrip('/')}/bootstrap/{bootstrap_token}" if bootstrap_token else app_base_url
    state = {
        "username": username,
        "display_name": display_name,
        "email": email,
        "tailscale_login": existing_login,
        "auth_mode": "tsidp",
        "bootstrap_mode": "tailscale_oidc",
        "setup_url": bootstrap_url,
        "bootstrap_url": bootstrap_url,
        "bootstrap_token": bootstrap_token,
        "oidc_client_id": oidc_state["client_id"],
        "first_admin_login_seen": first_admin_login_seen,
        "bootstrap_completed_at": existing_state.get("bootstrap_completed_at") if existing_state else None,
    }
    if existing_state == state:
        return existing_state

    state_path = get_first_admin_enrollment_state_path()
    atomic_json_write(state_path, state)
    _secure_file(state_path)
    return state
