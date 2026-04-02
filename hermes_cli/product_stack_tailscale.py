from __future__ import annotations

import subprocess
import time

import httpx

from hermes_cli.product_config import load_product_config


def url_scheme(config: dict[str, object]) -> str:
    network = config.get("network", {})
    configured = str(network.get("url_scheme", "")).strip().lower()
    if configured:
        if configured not in {"http", "https"}:
            raise ValueError("product network.url_scheme must be http or https")
        return configured
    return "http"


def tailscale_config(config: dict[str, object]) -> dict[str, object]:
    network = config.get("network", {})
    tailscale = network.get("tailscale", {})
    return tailscale if isinstance(tailscale, dict) else {}


def tailscale_enabled(config: dict[str, object]) -> bool:
    return bool(tailscale_config(config).get("enabled", False))


def required_tailnet_value(config: dict[str, object], key: str) -> str:
    value = str(tailscale_config(config).get(key, "")).strip().lower()
    if not value:
        raise ValueError(f"product network.tailscale.{key} must be configured when Tailscale is enabled")
    return value


def tailscale_host(config: dict[str, object]) -> str:
    return f"{required_tailnet_value(config, 'device_name')}.{required_tailnet_value(config, 'tailnet_name')}.ts.net"


def tailscale_https_port(config: dict[str, object], key: str, default: int) -> int:
    raw_value = tailscale_config(config).get(key, default)
    try:
        port = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"product network.tailscale.{key} must be an integer") from exc
    if port <= 0:
        raise ValueError(f"product network.tailscale.{key} must be positive")
    return port


def tsidp_hostname(config: dict[str, object]) -> str:
    value = str(tailscale_config(config).get("idp_hostname", "idp")).strip().lower()
    if not value:
        raise ValueError("product network.tailscale.idp_hostname must not be empty")
    return value


def tsidp_host(config: dict[str, object]) -> str:
    return f"{tsidp_hostname(config)}.{required_tailnet_value(config, 'tailnet_name')}.ts.net"


def tsidp_issuer_url(config: dict[str, object]) -> str:
    return f"https://{tsidp_host(config)}"


def configured_tsidp_issuer_url(config: dict[str, object]) -> str:
    auth = config.get("auth", {})
    if isinstance(auth, dict):
        configured = str(auth.get("issuer_url", "")).strip()
        if configured:
            return configured.rstrip("/")
    return tsidp_issuer_url(config)


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


def tailscale_command_path(config: dict[str, object]) -> str:
    configured = str(tailscale_config(config).get("command_path", "tailscale")).strip()
    if not configured:
        raise ValueError("product network.tailscale.command_path must not be empty")
    return configured


def tailscale_serve_command(config: dict[str, object], *, https_port: int, target_url: str) -> list[str]:
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


def ensure_product_tailnet_stopped(config: dict[str, object] | None = None) -> list[subprocess.CompletedProcess[str]]:
    product_config = config or load_product_config()
    if not tailscale_enabled(product_config):
        return []
    command = [tailscale_command_path(product_config), "serve", "reset"]
    try:
        return [subprocess.run(command, check=True, capture_output=True, text=True)]
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(format_tailscale_reset_error(exc, command=command)) from exc


def resolve_product_urls(config: dict[str, object] | None = None) -> dict[str, str]:
    product_config = config or load_product_config()
    if not tailscale_enabled(product_config):
        raise ValueError("Tailscale must be enabled for this product install")
    network = product_config.get("network", {})
    app_port = int(network.get("app_port", 8086))
    tailnet_host = tailscale_host(product_config)
    app_https_port = tailscale_https_port(product_config, "app_https_port", 443)
    tailnet_app_base_url = format_https_url(tailnet_host, app_https_port)
    tailnet_issuer_url = configured_tsidp_issuer_url(product_config)
    return {
        "url_scheme": "https",
        "app_base_url": tailnet_app_base_url,
        "issuer_url": tailnet_issuer_url,
        "oidc_callback_url": f"{tailnet_app_base_url}/api/auth/oidc/callback",
        "tailnet_host": tailnet_host,
        "tailnet_app_base_url": tailnet_app_base_url,
        "tailnet_issuer_url": tailnet_issuer_url,
        "local_app_base_url": f"http://127.0.0.1:{app_port}",
    }


def first_admin_bootstrap_completed() -> bool:
    from hermes_cli.product_stack_bootstrap import first_admin_bootstrap_completed as _bootstrap_completed

    return _bootstrap_completed()


def ensure_product_tailnet_started(
    config: dict[str, object] | None = None,
    *,
    include_app: bool = True,
) -> list[subprocess.CompletedProcess[str]]:
    product_config = config or load_product_config()
    if not tailscale_enabled(product_config):
        return []
    network = product_config.get("network", {})
    app_port = int(network.get("app_port", 8086))
    app_https_port = tailscale_https_port(product_config, "app_https_port", 443)
    commands: list[list[str]] = []
    if include_app:
        commands.append(
            tailscale_serve_command(product_config, https_port=app_https_port, target_url=f"http://127.0.0.1:{app_port}")
        )
    results: list[subprocess.CompletedProcess[str]] = []
    for command in commands:
        try:
            results.append(subprocess.run(command, check=True, capture_output=True, text=True))
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(format_tailscale_serve_error(exc, command=command)) from exc
    return results


def wait_for_tsidp_ready(config: dict[str, object], timeout_seconds: float) -> None:
    health_url = configured_tsidp_issuer_url(config).rstrip("/") + "/.well-known/openid-configuration"
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
