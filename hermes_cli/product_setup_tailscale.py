from __future__ import annotations

import json
import subprocess
from typing import Any


def detect_tailscale_identity(command_path: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            [command_path, "status", "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Tailscale CLI not found: {command_path}. Install Tailscale before running setup."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"Failed to query Tailscale identity with '{command_path} status --json'"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from exc

    payload = result.stdout or "{}"
    data = json.loads(payload)
    self_payload = data.get("Self", {}) if isinstance(data, dict) else {}
    tailnet_payload = data.get("CurrentTailnet", {}) if isinstance(data, dict) else {}
    if not isinstance(self_payload, dict):
        raise RuntimeError("Tailscale status payload did not contain Self")
    if not isinstance(tailnet_payload, dict):
        raise RuntimeError("Tailscale status payload did not contain CurrentTailnet")
    tailnet_name = str(data.get("MagicDNSSuffix", "")).strip().rstrip(".").lower()
    api_tailnet_name = str(tailnet_payload.get("Name", "")).strip()
    dns_name = str(self_payload.get("DNSName", "")).strip().rstrip(".").lower()
    if not tailnet_name or not dns_name or not api_tailnet_name:
        raise RuntimeError("Tailscale is not connected to a tailnet with MagicDNS enabled")
    if tailnet_name.endswith(".ts.net"):
        tailnet_name = tailnet_name.removesuffix(".ts.net")
    if not dns_name.endswith(".ts.net"):
        raise RuntimeError("Unexpected Tailscale DNS name; expected a .ts.net host")
    device_name = dns_name.split(".", 1)[0]
    if not device_name or not tailnet_name:
        raise RuntimeError("Could not derive Tailnet device name and tailnet name")
    return {
        "device_name": device_name,
        "tailnet_name": tailnet_name,
        "api_tailnet_name": api_tailnet_name,
    }


def setup_product_tailscale(hooks: Any) -> None:
    product_config = hooks.load_product_config()
    tailscale = product_config.setdefault("network", {}).setdefault("tailscale", {})
    services = product_config.setdefault("services", {}).setdefault("tsidp", {})
    current_idp_hostname = str(tailscale.get("idp_hostname", "idp")).strip() or "idp"
    command_path = str(tailscale.get("command_path", "tailscale")).strip() or "tailscale"

    hooks.print_header("Tailscale")
    hooks.print_info("This branch exposes Hermes Core only through your tailnet.")
    hooks.print_info("Setup will:")
    hooks.print_info("  1. Verify the current Tailscale node and tailnet")
    hooks.print_info("  2. Save a tsidp auth key and Tailscale API token")
    hooks.print_info("  3. Patch tailnet policy so tsidp login and admin UI work")
    hooks.print_info("  4. Continue into tsidp startup and first-admin bootstrap")

    detected = hooks._detect_tailscale_identity(command_path)
    tailscale["enabled"] = True
    tailscale["tailnet_name"] = detected["tailnet_name"]
    tailscale["device_name"] = detected["device_name"]
    tailscale["api_tailnet_name"] = detected["api_tailnet_name"]
    tailscale["app_https_port"] = int(tailscale.get("app_https_port", 443) or 443)
    hooks.print_info(f"  Detected Tailnet device:     {detected['device_name']}")
    hooks.print_info(f"  Detected MagicDNS suffix:    {detected['tailnet_name']}")
    hooks.print_info(f"  Detected policy tailnet key: {detected['api_tailnet_name']}")

    while True:
        chosen_idp_hostname = hooks._sanitize_prompt_text(
            hooks.prompt("tsidp hostname", current_idp_hostname) or current_idp_hostname
        ).lower()
        if chosen_idp_hostname:
            tailscale["idp_hostname"] = chosen_idp_hostname
            break
        hooks.print_warning("tsidp hostname must not be empty.")

    auth_key_ref = str(services.get("auth_key_ref", "")).strip()
    api_token_ref = str(services.get("api_token_ref", "")).strip()
    existing_auth_key = str(hooks.get_env_value(auth_key_ref) or "").strip()
    existing_api_token = str(hooks.get_env_value(api_token_ref) or "").strip()
    if existing_auth_key:
        hooks.print_info("  Tailscale auth key: already saved; press Enter to keep it.")
    if existing_api_token:
        hooks.print_info("  Tailscale API token: already saved; press Enter to keep it.")
    while True:
        auth_key = hooks._sanitize_prompt_text(hooks.prompt("Tailscale auth key", ""))
        if auth_key:
            hooks.save_env_value_secure(auth_key_ref, auth_key)
            break
        if existing_auth_key:
            break
        hooks.print_warning("A Tailscale auth key is required for the bundled tsidp service.")

    while True:
        api_token = hooks._sanitize_prompt_text(hooks.prompt("Tailscale API token", ""))
        if api_token:
            hooks.save_env_value_secure(api_token_ref, api_token)
            break
        if existing_api_token:
            break
        hooks.print_warning("A Tailscale API token is required so setup can patch tailnet policy automatically.")

    tailscale["command_path"] = command_path
    hooks.save_product_config(product_config)
    policy_status = hooks.ensure_tsidp_policy(product_config)
    urls = hooks.resolve_product_urls(product_config)
    hooks.print_success("Tailnet policy is ready for tsidp.")
    if policy_status["changed"]:
        hooks.print_info(f"  Policy backup:     {policy_status['backup_path']}")
    hooks.print_info(f"  Policy tailnet:    {policy_status['tailnet']}")
    hooks.print_info(f"  Tailnet app URL:  {urls['app_base_url']}")
    hooks.print_info(f"  Tailnet OIDC URL: {urls['issuer_url']}")
