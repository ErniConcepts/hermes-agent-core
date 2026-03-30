from __future__ import annotations

import ipaddress
import subprocess
import time
from typing import Any

import httpx


def public_host(config: dict[str, Any]) -> str:
    host = str(config.get("network", {}).get("public_host", "")).strip()
    if not host:
        raise ValueError("product network.public_host must be configured")
    return host


def url_scheme(config: dict[str, Any]) -> str:
    network = config.get("network", {})
    configured = str(network.get("url_scheme", "")).strip().lower()
    if configured:
        if configured not in {"http", "https"}:
            raise ValueError("product network.url_scheme must be http or https")
        return configured
    return "http"


def validate_public_host(host: str) -> None:
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
    raise ValueError("product network.public_host must be a hostname or domain, not a raw IP address")


def tailscale_config(config: dict[str, Any]) -> dict[str, Any]:
    network = config.get("network", {})
    tailscale = network.get("tailscale", {})
    return tailscale if isinstance(tailscale, dict) else {}


def tailscale_enabled(config: dict[str, Any]) -> bool:
    return bool(tailscale_config(config).get("enabled", False))


def required_tailnet_value(config: dict[str, Any], key: str) -> str:
    value = str(tailscale_config(config).get(key, "")).strip().lower()
    if not value:
        raise ValueError(f"product network.tailscale.{key} must be configured when Tailscale is enabled")
    return value


def tailscale_host(config: dict[str, Any]) -> str:
    return f"{required_tailnet_value(config, 'device_name')}.{required_tailnet_value(config, 'tailnet_name')}.ts.net"


def tailscale_https_port(config: dict[str, Any], key: str, default: int) -> int:
    raw_value = tailscale_config(config).get(key, default)
    try:
        port = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"product network.tailscale.{key} must be an integer") from exc
    if port <= 0:
        raise ValueError(f"product network.tailscale.{key} must be positive")
    return port


def tsidp_hostname(config: dict[str, Any]) -> str:
    value = str(tailscale_config(config).get("idp_hostname", "idp")).strip().lower()
    if not value:
        raise ValueError("product network.tailscale.idp_hostname must not be empty")
    return value


def tsidp_host(config: dict[str, Any]) -> str:
    return f"{tsidp_hostname(config)}.{required_tailnet_value(config, 'tailnet_name')}.ts.net"


def tsidp_issuer_url(config: dict[str, Any]) -> str:
    return f"https://{tsidp_host(config)}"


def format_https_url(host: str, port: int) -> str:
    if port == 443:
        return f"https://{host}"
    return f"https://{host}:{port}"


def format_tailscale_reset_error(exc: subprocess.CalledProcessError, *, command: list[str]) -> str:
    detail = (exc.stderr or exc.stdout or "").strip()
    command_text = " ".join(command)
    message = f"Failed to disable Tailscale HTTPS exposure with: {command_text}"
    if detail:
        message = f"{message}\n{detail}"
    return message


def tailscale_command_path(config: dict[str, Any]) -> str:
    configured = str(tailscale_config(config).get("command_path", "tailscale")).strip()
    if not configured:
        raise ValueError("product network.tailscale.command_path must not be empty")
    return configured


def tailscale_serve_command(config: dict[str, Any], *, https_port: int, target_url: str) -> list[str]:
    return [tailscale_command_path(config), "serve", "--bg", f"--https={https_port}", target_url]


def format_tailscale_serve_error(exc: subprocess.CalledProcessError, *, command: list[str]) -> str:
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


def ensure_product_tailnet_stopped(hooks: Any, config: dict[str, Any] | None = None) -> list[subprocess.CompletedProcess[str]]:
    product_config = config or hooks.load_product_config()
    if not hooks._tailscale_enabled(product_config):
        return []
    command = [hooks._tailscale_command_path(product_config), "serve", "reset"]
    try:
        return [subprocess.run(command, check=True, capture_output=True, text=True)]
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(hooks._format_tailscale_reset_error(exc, command=command)) from exc


def resolve_product_urls(hooks: Any, config: dict[str, Any] | None = None) -> dict[str, str]:
    product_config = config or hooks.load_product_config()
    if not hooks._tailscale_enabled(product_config):
        raise ValueError("Tailscale must be enabled for this product install")
    network = product_config.get("network", {})
    app_port = int(network.get("app_port", 8086))
    tailnet_host = hooks._tailscale_host(product_config)
    app_https_port = hooks._tailscale_https_port(product_config, "app_https_port", 443)
    tailnet_app_base_url = hooks._format_https_url(tailnet_host, app_https_port)
    tailnet_issuer_url = hooks._tsidp_issuer_url(product_config)
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


def first_admin_bootstrap_completed(hooks: Any) -> bool:
    state = hooks.load_first_admin_enrollment_state() or {}
    return bool(state.get("first_admin_login_seen", False))


def ensure_product_tailnet_started(
    hooks: Any,
    config: dict[str, Any] | None = None,
    *,
    include_app: bool = True,
) -> list[subprocess.CompletedProcess[str]]:
    product_config = config or hooks.load_product_config()
    if not hooks._tailscale_enabled(product_config):
        return []

    network = product_config.get("network", {})
    app_port = int(network.get("app_port", 8086))
    app_https_port = hooks._tailscale_https_port(product_config, "app_https_port", 443)

    commands: list[list[str]] = []
    if include_app:
        commands.append(
            hooks._tailscale_serve_command(
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
            raise RuntimeError(hooks._format_tailscale_serve_error(exc, command=command)) from exc
    return results


def wait_for_tsidp_ready(hooks: Any, config: dict[str, Any], timeout_seconds: float) -> None:
    health_url = hooks._tsidp_issuer_url(config).rstrip("/") + "/.well-known/openid-configuration"
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
