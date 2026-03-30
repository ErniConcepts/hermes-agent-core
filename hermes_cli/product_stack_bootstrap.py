from __future__ import annotations

import json
import secrets
import subprocess
import time
from typing import Any


def tsidp_service_config(hooks: Any, config: dict[str, Any]) -> dict[str, Any]:
    services_cfg = config.setdefault("services", {}).setdefault("tsidp", {})
    services_cfg["mode"] = str(services_cfg.get("mode", "docker")).strip() or "docker"
    services_cfg["container_name"] = str(services_cfg.get("container_name", "hermes-tsidp")).strip() or "hermes-tsidp"
    services_cfg["image"] = str(services_cfg.get("image", hooks.TSIDP_IMAGE)).strip() or hooks.TSIDP_IMAGE
    auth_key_ref = str(services_cfg.get("auth_key_ref", "HERMES_PRODUCT_TAILSCALE_AUTH_KEY")).strip()
    if not auth_key_ref:
        raise ValueError("services.tsidp.auth_key_ref must be configured")
    services_cfg["auth_key_ref"] = auth_key_ref
    advertise_tags = services_cfg.get("advertise_tags", ["tag:tsidp"])
    if not isinstance(advertise_tags, list) or not advertise_tags:
        advertise_tags = ["tag:tsidp"]
    services_cfg["advertise_tags"] = [str(item).strip() for item in advertise_tags if str(item).strip()]
    return services_cfg


def tsidp_auth_key(hooks: Any, config: dict[str, Any]) -> str:
    env_key = str(hooks._tsidp_service_config(config).get("auth_key_ref", "")).strip()
    return str(hooks.get_env_value(env_key) or "").strip()


def build_tsidp_env_file(hooks: Any, config: dict[str, Any]) -> str:
    services_cfg = hooks._tsidp_service_config(config)
    lines = [
        "TAILSCALE_USE_WIP_CODE=1",
        f"TS_HOSTNAME={hooks._tsidp_hostname(config)}",
        "TS_STATE_DIR=/data",
        "TSIDP_LOCAL_PORT=8080",
        "TSIDP_ENABLE_STS=1",
    ]
    auth_key = hooks._tsidp_auth_key(config)
    if auth_key:
        lines.append(f"TS_AUTHKEY={auth_key}")
    advertise_tags = services_cfg.get("advertise_tags", [])
    if advertise_tags:
        lines.append(f"TS_ADVERTISE_TAGS={','.join(str(item) for item in advertise_tags)}")
    lines.append("")
    return "\n".join(lines)


def build_tsidp_compose_spec(hooks: Any, config: dict[str, Any]) -> dict[str, Any]:
    services_cfg = hooks._tsidp_service_config(config)
    data_root_path = hooks.get_tsidp_data_root()
    data_root = data_root_path.as_posix()
    owner = data_root_path.stat()
    service: dict[str, Any] = {
        "image": str(services_cfg["image"]),
        "container_name": str(services_cfg["container_name"]),
        "restart": "unless-stopped",
        "env_file": [hooks.get_tsidp_env_path().as_posix()],
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


def ensure_product_tsidp_started(hooks: Any, config: dict[str, Any] | None = None) -> subprocess.CompletedProcess[str] | None:
    product_config = config or hooks.load_product_config()
    if not hooks._tailscale_enabled(product_config):
        return None
    compose_path = hooks.get_tsidp_compose_path()
    command = ["docker", "compose", "-f", str(compose_path), "up", "-d", "--wait", "--force-recreate"]
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"Failed to start tsidp with docker compose ({compose_path})"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from exc


def required_secret(hooks: Any, env_key: str) -> str:
    current = (hooks.get_env_value(env_key) or "").strip()
    if current:
        return current
    generated = secrets.token_urlsafe(48)
    hooks.save_env_value_secure(env_key, generated)
    return generated


def ensure_client_secret(hooks: Any, config: dict[str, Any]) -> str:
    env_key = str(config.get("auth", {}).get("client_secret_ref", "")).strip()
    if not env_key:
        raise ValueError("auth.client_secret_ref must be configured in product.yaml")
    return hooks._required_secret(env_key)


def ensure_session_secret(hooks: Any, config: dict[str, Any]) -> str:
    env_key = str(config.get("auth", {}).get("session_secret_ref", "")).strip()
    if not env_key:
        raise ValueError("auth.session_secret_ref must be configured in product.yaml")
    return hooks._required_secret(env_key)


def initialize_product_stack(hooks: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    product_config = config or hooks.load_product_config()
    if not hooks._tailscale_enabled(product_config):
        raise RuntimeError("Tailscale must be enabled for this product install")
    hooks.ensure_product_home()
    hooks._secure_tree(
        hooks.get_product_services_root(),
        hooks.get_tsidp_service_root(),
        hooks.get_tsidp_data_root(),
        hooks.get_product_bootstrap_root(),
    )

    urls = hooks.resolve_product_urls(product_config)
    product_config["network"]["public_host"] = urls["public_host"]
    product_config["auth"]["provider"] = "tsidp"
    product_config["auth"]["issuer_url"] = urls["issuer_url"]
    hooks._tsidp_service_config(product_config)
    hooks._ensure_session_secret(product_config)

    tsidp_env_path = hooks.get_tsidp_env_path()
    try:
        tsidp_env_path.write_text(hooks._build_tsidp_env_file(product_config), encoding="utf-8")
    except PermissionError as exc:
        raise RuntimeError(hooks._permission_error_message(tsidp_env_path)) from exc
    hooks._secure_file(tsidp_env_path)

    tsidp_compose_path = hooks.get_tsidp_compose_path()
    try:
        hooks.atomic_yaml_write(tsidp_compose_path, hooks._build_tsidp_compose_spec(product_config))
    except PermissionError as exc:
        raise RuntimeError(hooks._permission_error_message(tsidp_compose_path)) from exc
    hooks._secure_file(tsidp_compose_path)

    hooks.save_product_config(product_config)
    return product_config


def ensure_product_stack_started(hooks: Any, config: dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    product_config = config or hooks.initialize_product_stack()
    result = hooks.ensure_product_tsidp_started(product_config)
    hooks.ensure_product_tailnet_started(product_config, include_app=True)
    return result if result is not None else subprocess.CompletedProcess([], 0, "", "")


def bootstrap_product_oidc_client(hooks: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    product_config = hooks.initialize_product_stack(config or hooks.load_product_config())
    hooks.ensure_product_stack_started(product_config)
    hooks._wait_for_tsidp_ready(product_config, hooks._READY_TIMEOUT_SECONDS)
    settings = hooks.load_product_oidc_client_settings(product_config)
    metadata = hooks.discover_product_oidc_provider_metadata(settings)
    return {
        "client_id": settings.client_id,
        "issuer_url": settings.issuer_url,
        "callback_url": settings.redirect_uri,
        "authorization_endpoint": metadata.authorization_endpoint,
        "token_endpoint": metadata.token_endpoint,
    }


def load_product_tailscale_oidc_client_settings(hooks: Any, config: dict[str, Any] | None = None) -> Any:
    return hooks.load_product_oidc_client_settings(config or hooks.load_product_config())


def tailscale_oidc_registration_payload(hooks: Any, config: dict[str, Any]) -> dict[str, Any]:
    urls = hooks.resolve_product_urls(config)
    brand_name = str(config.get("product", {}).get("brand", {}).get("name", "Hermes Core")).strip() or "Hermes Core"
    return {
        "client_name": f"{brand_name} Tailnet",
        "redirect_uris": [f"{urls['tailnet_app_base_url'].rstrip('/')}/api/auth/tailscale/callback"],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "scope": "openid profile email",
        "token_endpoint_auth_method": "client_secret_post",
    }


def bootstrap_product_tailscale_oidc_client(hooks: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return hooks.bootstrap_product_oidc_client(config)


def load_first_admin_enrollment_state(hooks: Any) -> dict[str, Any] | None:
    state_path = hooks.get_first_admin_enrollment_state_path()
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def mark_first_admin_bootstrap_completed(hooks: Any, tailscale_login: str | None = None) -> dict[str, Any] | None:
    state = hooks.load_first_admin_enrollment_state()
    if not state:
        return None
    if bool(state.get("first_admin_login_seen")):
        return state
    state["first_admin_login_seen"] = True
    state["bootstrap_completed_at"] = int(time.time())
    state["bootstrap_token"] = ""
    state["bootstrap_url"] = hooks.resolve_product_urls(hooks.load_product_config())["app_base_url"]
    normalized_login = str(tailscale_login or "").strip().lower()
    if normalized_login:
        state["tailscale_login"] = normalized_login
    state_path = hooks.get_first_admin_enrollment_state_path()
    hooks.atomic_json_write(state_path, state)
    hooks._secure_file(state_path)
    return state


def bootstrap_first_admin_enrollment(hooks: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    product_config = hooks.initialize_product_stack(config or hooks.load_product_config())
    oidc_state = hooks.bootstrap_product_oidc_client(product_config)
    existing_state = hooks.load_first_admin_enrollment_state()
    existing_login = str(existing_state.get("tailscale_login", "")).strip().lower() if existing_state else ""
    username = str(product_config.get("bootstrap", {}).get("first_admin_username", "")).strip() or "admin"
    display_name = str(product_config.get("bootstrap", {}).get("first_admin_display_name", "Administrator")).strip() or "Administrator"
    email = existing_login if "@" in existing_login else ""
    first_admin_login_seen = bool(existing_state.get("first_admin_login_seen", False)) if existing_state else False
    bootstrap_token = ""
    if not first_admin_login_seen:
        bootstrap_token = str(existing_state.get("bootstrap_token", "")).strip() if existing_state else ""
        if not bootstrap_token:
            bootstrap_token = secrets.token_urlsafe(24)
    app_base_url = hooks.resolve_product_urls(product_config)["app_base_url"]
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

    state_path = hooks.get_first_admin_enrollment_state_path()
    hooks.atomic_json_write(state_path, state)
    hooks._secure_file(state_path)
    return state
