from __future__ import annotations

import json
import secrets
import subprocess
import time
from typing import Any

import httpx

from hermes_cli.config import _secure_file, get_env_value, save_env_value_secure
from hermes_cli.product_config import ensure_product_home, load_product_config, save_product_config
from hermes_cli.product_oidc import (
    discover_product_oidc_provider_metadata,
    discover_product_oidc_provider_metadata_by_issuer,
    load_product_oidc_client_settings,
)
from hermes_cli.product_stack_paths import (
    get_first_admin_enrollment_state_path,
    get_product_bootstrap_root,
    get_product_services_root,
    get_tsidp_compose_path,
    get_tsidp_data_root,
    get_tsidp_env_path,
    get_tsidp_service_root,
    permission_error_message,
    secure_tree,
)
from hermes_cli.product_stack_tailscale import (
    resolve_product_urls,
    tailscale_enabled,
    tsidp_hostname,
    wait_for_tsidp_ready,
    ensure_product_tailnet_started,
)
from utils import atomic_json_write, atomic_yaml_write


TSIDP_IMAGE = "ghcr.io/tailscale/tsidp:latest"
READY_TIMEOUT_SECONDS = 45.0
TSIDP_WELL_KNOWN_PATH = "/.well-known/openid-configuration"


def tsidp_service_config(config: dict[str, Any]) -> dict[str, Any]:
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


def tsidp_auth_key(config: dict[str, Any]) -> str:
    env_key = str(tsidp_service_config(config).get("auth_key_ref", "")).strip()
    return str(get_env_value(env_key) or "").strip()


def build_tsidp_env_file(config: dict[str, Any]) -> str:
    services_cfg = tsidp_service_config(config)
    lines = [
        "TAILSCALE_USE_WIP_CODE=1",
        f"TS_HOSTNAME={tsidp_hostname(config)}",
        "TS_STATE_DIR=/data",
        "TSIDP_LOCAL_PORT=8080",
        "TSIDP_ENABLE_STS=1",
    ]
    auth_key = tsidp_auth_key(config)
    if auth_key:
        lines.append(f"TS_AUTHKEY={auth_key}")
    advertise_tags = services_cfg.get("advertise_tags", [])
    if advertise_tags:
        lines.append(f"TS_ADVERTISE_TAGS={','.join(str(item) for item in advertise_tags)}")
    lines.append("")
    return "\n".join(lines)


def host_default_route_mtu() -> int | None:
    try:
        route_result = subprocess.run(
            ["ip", "route", "show", "default"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    interface_name = ""
    parts = (route_result.stdout or "").strip().split()
    for index, part in enumerate(parts):
        if part == "dev" and index + 1 < len(parts):
            interface_name = parts[index + 1].strip()
            break
    if not interface_name:
        return None
    try:
        link_result = subprocess.run(
            ["ip", "link", "show", "dev", interface_name],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return None
    for token_index, token in enumerate((link_result.stdout or "").strip().split()):
        if token == "mtu" and token_index + 1 < len((link_result.stdout or "").strip().split()):
            try:
                return int((link_result.stdout or "").strip().split()[token_index + 1])
            except ValueError:
                return None
    return None


def build_tsidp_compose_spec(config: dict[str, Any]) -> dict[str, Any]:
    services_cfg = tsidp_service_config(config)
    data_root_path = get_tsidp_data_root()
    owner = data_root_path.stat()
    service: dict[str, Any] = {
        "image": str(services_cfg["image"]),
        "container_name": str(services_cfg["container_name"]),
        "restart": "unless-stopped",
        "env_file": [get_tsidp_env_path().as_posix()],
        "volumes": [f"{data_root_path.as_posix()}:/data"],
        "user": f"{owner.st_uid}:{owner.st_gid}",
        "healthcheck": {
            "test": ["CMD", "wget", "-qO-", "http://127.0.0.1:8080/.well-known/openid-configuration"],
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "10s",
        },
    }
    compose_spec: dict[str, Any] = {"services": {"tsidp": service}}
    mtu = host_default_route_mtu()
    if mtu and mtu > 0:
        compose_spec["networks"] = {"default": {"driver_opts": {"com.docker.network.driver.mtu": str(mtu)}}}
    return compose_spec


def ensure_product_tsidp_started(config: dict[str, Any] | None = None) -> subprocess.CompletedProcess[str] | None:
    product_config = config or load_product_config()
    if not tailscale_enabled(product_config):
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


def running_tsidp_issuer_url(config: dict[str, Any]) -> str:
    container_name = str(tsidp_service_config(config).get("container_name", "")).strip()
    if not container_name:
        raise RuntimeError("services.tsidp.container_name must be configured")
    command = [
        "docker",
        "exec",
        container_name,
        "wget",
        "-qO-",
        f"http://127.0.0.1:8080{TSIDP_WELL_KNOWN_PATH}",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"Failed to read tsidp issuer from running container {container_name}"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from exc
    try:
        payload = json.loads((result.stdout or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Running tsidp returned invalid well-known metadata from {container_name}") from exc
    issuer = str(payload.get("issuer", "")).strip()
    if not issuer:
        raise RuntimeError(f"Running tsidp did not report an issuer URL in {container_name}")
    return issuer.rstrip("/")


def sync_running_tsidp_issuer_url(config: dict[str, Any]) -> dict[str, Any]:
    issuer_url = running_tsidp_issuer_url(config)
    current = str(config.get("auth", {}).get("issuer_url", "")).strip().rstrip("/")
    if issuer_url == current:
        return config
    config.setdefault("auth", {})["issuer_url"] = issuer_url
    save_product_config(config)
    return config


def required_secret(env_key: str) -> str:
    current = (get_env_value(env_key) or "").strip()
    if current:
        return current
    generated = secrets.token_urlsafe(48)
    save_env_value_secure(env_key, generated)
    return generated


def ensure_client_secret(config: dict[str, Any]) -> str:
    env_key = str(config.get("auth", {}).get("client_secret_ref", "")).strip()
    if not env_key:
        raise ValueError("auth.client_secret_ref must be configured in product.yaml")
    return required_secret(env_key)


def ensure_session_secret(config: dict[str, Any]) -> str:
    env_key = str(config.get("auth", {}).get("session_secret_ref", "")).strip()
    if not env_key:
        raise ValueError("auth.session_secret_ref must be configured in product.yaml")
    return required_secret(env_key)


def initialize_product_stack(config: dict[str, Any] | None = None) -> dict[str, Any]:
    product_config = config or load_product_config()
    if not tailscale_enabled(product_config):
        raise RuntimeError("Tailscale must be enabled for this product install")
    ensure_product_home()
    secure_tree(get_product_services_root(), get_tsidp_service_root(), get_tsidp_data_root(), get_product_bootstrap_root())
    urls = resolve_product_urls(product_config)
    product_config["auth"]["provider"] = "tsidp"
    product_config["auth"]["issuer_url"] = urls["issuer_url"]
    tsidp_service_config(product_config)
    ensure_session_secret(product_config)

    tsidp_env_path = get_tsidp_env_path()
    try:
        tsidp_env_path.write_text(build_tsidp_env_file(product_config), encoding="utf-8")
    except PermissionError as exc:
        raise RuntimeError(permission_error_message(tsidp_env_path)) from exc
    _secure_file(tsidp_env_path)

    tsidp_compose_path = get_tsidp_compose_path()
    try:
        atomic_yaml_write(tsidp_compose_path, build_tsidp_compose_spec(product_config))
    except PermissionError as exc:
        raise RuntimeError(permission_error_message(tsidp_compose_path)) from exc
    _secure_file(tsidp_compose_path)

    save_product_config(product_config)
    return product_config


def ensure_product_stack_started(config: dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    product_config = config or initialize_product_stack()
    result = ensure_product_tsidp_started(product_config)
    product_config = sync_running_tsidp_issuer_url(product_config)
    ensure_product_tailnet_started(product_config, include_app=True)
    return result if result is not None else subprocess.CompletedProcess([], 0, "", "")


def bootstrap_product_oidc_client(config: dict[str, Any] | None = None) -> dict[str, Any]:
    product_config = initialize_product_stack(config or load_product_config())
    ensure_product_stack_started(product_config)
    wait_for_tsidp_ready(product_config, READY_TIMEOUT_SECONDS)
    auth = product_config.get("auth", {})
    client_id = str(auth.get("client_id", "")).strip()
    client_secret_ref = str(auth.get("client_secret_ref", "")).strip()
    client_secret = str(get_env_value(client_secret_ref) or "").strip() if client_secret_ref else ""
    if not client_id or not client_secret:
        bootstrap_product_tailscale_oidc_client(product_config)
        product_config = load_product_config()
    settings = load_product_oidc_client_settings(product_config)
    metadata = discover_product_oidc_provider_metadata(settings)
    return {
        "client_id": settings.client_id,
        "issuer_url": settings.issuer_url,
        "callback_url": settings.redirect_uri,
        "authorization_endpoint": metadata.authorization_endpoint,
        "token_endpoint": metadata.token_endpoint,
    }


def load_product_tailscale_oidc_client_settings(config: dict[str, Any] | None = None) -> Any:
    return load_product_oidc_client_settings(config or load_product_config())


def tailscale_oidc_registration_payload(config: dict[str, Any]) -> dict[str, Any]:
    urls = resolve_product_urls(config)
    brand_name = str(config.get("product", {}).get("brand", {}).get("name", "Hermes Core")).strip() or "Hermes Core"
    return {
        "client_name": brand_name,
        "redirect_uris": [urls["oidc_callback_url"]],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "scope": "openid profile email",
        "token_endpoint_auth_method": "client_secret_post",
    }


def bootstrap_product_tailscale_oidc_client(config: dict[str, Any] | None = None) -> dict[str, Any]:
    product_config = initialize_product_stack(config or load_product_config())
    ensure_product_stack_started(product_config)
    wait_for_tsidp_ready(product_config, READY_TIMEOUT_SECONDS)
    auth = product_config.setdefault("auth", {})
    client_id = str(auth.get("client_id", "")).strip()
    client_secret_ref = str(auth.get("client_secret_ref", "")).strip()
    saved_client_secret = str(get_env_value(client_secret_ref) or "").strip() if client_secret_ref else ""
    issuer_url = str(auth.get("issuer_url", "")).strip()

    if client_id and client_secret_ref and saved_client_secret:
        settings = load_product_oidc_client_settings(product_config)
        metadata = discover_product_oidc_provider_metadata(settings)
        return {
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "issuer_url": settings.issuer_url,
            "callback_url": settings.redirect_uri,
            "registration_endpoint": metadata.registration_endpoint,
            "created": False,
        }

    metadata = discover_product_oidc_provider_metadata_by_issuer(issuer_url)
    registration_endpoint = str(metadata.registration_endpoint or "").strip()
    if not registration_endpoint:
        raise RuntimeError(
            "tsidp does not expose an OIDC registration endpoint. Enable tsidp dynamic client registration or create the client manually."
        )

    payload = tailscale_oidc_registration_payload(product_config)
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                registration_endpoint,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            registration = response.json()
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail_payload = exc.response.json()
            if isinstance(detail_payload, dict):
                detail = str(detail_payload.get("error_description") or detail_payload.get("error") or "").strip()
        except ValueError:
            detail = (exc.response.text or "").strip()
        message = "Automatic tsidp OIDC client registration failed"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("Automatic tsidp OIDC client registration failed") from exc

    if not isinstance(registration, dict):
        raise RuntimeError("Automatic tsidp OIDC client registration returned an unexpected payload.")
    new_client_id = str(registration.get("client_id", "")).strip()
    new_client_secret = str(registration.get("client_secret", "")).strip()
    if not new_client_id or not new_client_secret:
        raise RuntimeError("Automatic tsidp OIDC client registration did not return a client id and secret.")
    if not client_secret_ref:
        raise RuntimeError("auth.client_secret_ref must be configured in product.yaml")

    auth["client_id"] = new_client_id
    save_env_value_secure(client_secret_ref, new_client_secret)
    save_product_config(product_config)

    return {
        "client_id": new_client_id,
        "client_secret": new_client_secret,
        "issuer_url": str(auth.get("issuer_url", "")).strip(),
        "callback_url": resolve_product_urls(product_config)["oidc_callback_url"],
        "registration_endpoint": registration_endpoint,
        "created": True,
    }


def load_first_admin_enrollment_state() -> dict[str, Any] | None:
    state_path = get_first_admin_enrollment_state_path()
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def active_admin_exists() -> bool:
    from hermes_cli.product_users import list_product_users

    return any(user.is_admin and not user.disabled for user in list_product_users())


def first_admin_bootstrap_completed(state: dict[str, Any] | None = None) -> bool:
    enrollment_state = state if state is not None else load_first_admin_enrollment_state()
    if not enrollment_state:
        return False
    return bool(enrollment_state.get("first_admin_login_seen", False)) and active_admin_exists()


def mark_first_admin_bootstrap_completed(tailscale_login: str | None = None) -> dict[str, Any] | None:
    state = load_first_admin_enrollment_state()
    if not state:
        return None
    if first_admin_bootstrap_completed(state):
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


def bootstrap_first_admin_enrollment(config: dict[str, Any] | None = None, force_new: bool = False) -> dict[str, Any]:
    product_config = initialize_product_stack(config or load_product_config())
    oidc_state = bootstrap_product_oidc_client(product_config)
    existing_state = load_first_admin_enrollment_state()
    bootstrap_completed = False if force_new else first_admin_bootstrap_completed(existing_state)
    existing_login = str(existing_state.get("tailscale_login", "")).strip().lower() if existing_state and bootstrap_completed else ""
    username = str(product_config.get("bootstrap", {}).get("first_admin_username", "")).strip() or "admin"
    display_name = str(product_config.get("bootstrap", {}).get("first_admin_display_name", "Administrator")).strip() or "Administrator"
    email = existing_login if "@" in existing_login else ""
    first_admin_login_seen = bootstrap_completed
    bootstrap_token = ""
    if not first_admin_login_seen:
        bootstrap_token = str(existing_state.get("bootstrap_token", "")).strip() if existing_state else ""
        if force_new or not bootstrap_token:
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
