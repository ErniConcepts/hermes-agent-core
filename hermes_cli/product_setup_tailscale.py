from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from hermes_cli.config import get_env_value, save_env_value_secure
from hermes_cli.product_config import load_product_config, save_product_config
from hermes_cli.product_stack import resolve_product_urls
from hermes_cli.product_tailscale_api import ensure_tsidp_policy
from hermes_cli.setup import print_header, print_info, print_success, print_warning, prompt

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_TAILSCALE_COMMAND = "/mnt/c/Program Files/Tailscale/tailscale.exe"


def _sanitize_prompt_text(value: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", value or "")
    cleaned = _CONTROL_CHAR_RE.sub("", cleaned)
    return cleaned.strip()


def _candidate_tailscale_commands(command_path: str) -> list[str]:
    normalized = str(command_path or "tailscale").strip() or "tailscale"
    return [normalized]


def _running_in_wsl() -> bool:
    if "WSL_DISTRO_NAME" in os.environ:
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _is_windows_tailscale_command(command_path: str) -> bool:
    normalized = str(command_path or "").strip().replace("\\", "/").lower()
    return normalized.endswith("/tailscale/tailscale.exe") or normalized.endswith("tailscale.exe")


def _default_windows_tailscale_command() -> str | None:
    return _WINDOWS_TAILSCALE_COMMAND if Path(_WINDOWS_TAILSCALE_COMMAND).exists() else None


def _load_tailscale_status(command_path: str) -> dict:
    try:
        result = subprocess.run([command_path, "status", "--json"], check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Tailscale CLI not found: {command_path}. Install Tailscale before running setup.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"Failed to query Tailscale identity with '{command_path} status --json'"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from exc
    return json.loads(result.stdout or "{}")


def _parse_tailscale_identity(data: dict) -> dict[str, str]:
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
    return {"device_name": device_name, "tailnet_name": tailnet_name, "api_tailnet_name": api_tailnet_name}


def detect_tailscale_identity(command_path: str) -> dict[str, str]:
    errors: list[str] = []
    for candidate in _candidate_tailscale_commands(command_path):
        try:
            if _running_in_wsl() and _is_windows_tailscale_command(candidate):
                raise RuntimeError(
                    "Hermes Core product setup from WSL must use the Tailscale daemon inside WSL, "
                    "not the Windows Tailscale client. Start Tailscale in WSL with `sudo tailscale up`, "
                    "then rerun `hermes-core setup`."
                )
            detected = _parse_tailscale_identity(_load_tailscale_status(candidate))
            detected["command_path"] = candidate
            return detected
        except RuntimeError as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError(errors[-1])
    raise RuntimeError("Tailscale setup could not find a usable Tailscale CLI")


def _detect_wsl_browser_tailscale_identity(saved_command_path: str) -> dict[str, str] | None:
    if not _running_in_wsl():
        return None

    candidates: list[str] = []
    if _is_windows_tailscale_command(saved_command_path):
        candidates.append(str(saved_command_path).strip())
    default_windows_command = _default_windows_tailscale_command()
    if default_windows_command and default_windows_command not in candidates:
        candidates.append(default_windows_command)

    for candidate in candidates:
        try:
            detected = _parse_tailscale_identity(_load_tailscale_status(candidate))
        except RuntimeError:
            continue
        detected["command_path"] = candidate
        return detected
    return None


def setup_product_tailscale() -> None:
    product_config = load_product_config()
    tailscale = product_config.setdefault("network", {}).setdefault("tailscale", {})
    services = product_config.setdefault("services", {}).setdefault("tsidp", {})
    saved_command_path = str(tailscale.get("command_path", "tailscale")).strip() or "tailscale"
    command_path = saved_command_path

    print_header("Tailscale")
    print_info("Checking the current Tailscale node and tailnet...")
    if _running_in_wsl() and _is_windows_tailscale_command(command_path):
        print_warning(
            "Using the WSL Tailscale daemon for services and the Windows Tailscale client for the browser app URL."
        )
        command_path = "tailscale"

    try:
        detected = detect_tailscale_identity(command_path)
    except RuntimeError as exc:
        detail = str(exc).strip()
        guidance = [
            "Tailscale must be installed and connected before Hermes Core setup can continue.",
            "Install Tailscale, sign this machine into your tailnet, then rerun `hermes-core setup`.",
        ]
        if "WSL_DISTRO_NAME" in os.environ:
            guidance.append(
                "If you are running setup from WSL, start and sign in the WSL Tailscale daemon with `sudo tailscale up`. "
                "The Windows Tailscale client can be used as the browser-facing app endpoint after WSL Tailscale is connected."
            )
        if detail:
            guidance.append(f"Detection detail: {detail}")
        raise RuntimeError("\n".join(guidance)) from exc
    tailscale["enabled"] = True
    tailscale["tailnet_name"] = detected["tailnet_name"]
    tailscale["device_name"] = detected["device_name"]
    tailscale["api_tailnet_name"] = detected["api_tailnet_name"]
    tailscale["idp_hostname"] = "idp"
    tailscale["command_path"] = str(detected.get("command_path") or command_path).strip() or command_path
    tailscale["app_https_port"] = int(tailscale.get("app_https_port", 443) or 443)
    browser_detected = _detect_wsl_browser_tailscale_identity(saved_command_path)
    if browser_detected and browser_detected["tailnet_name"] == detected["tailnet_name"]:
        tailscale["app_device_name"] = browser_detected["device_name"]
        tailscale["app_command_path"] = browser_detected["command_path"]
        tailscale["browser_host_mode"] = "windows_tailscale"
    else:
        tailscale.pop("app_device_name", None)
        tailscale.pop("app_command_path", None)
        tailscale.pop("browser_host_mode", None)
    print_info(f"  Detected Tailnet device:     {detected['device_name']}")
    print_info(f"  Detected MagicDNS suffix:    {detected['tailnet_name']}")
    print_info(f"  Detected policy tailnet key: {detected['api_tailnet_name']}")
    if browser_detected and browser_detected["tailnet_name"] == detected["tailnet_name"]:
        print_info(f"  Browser-facing app device:   {browser_detected['device_name']}")
    elif browser_detected:
        print_warning(
            "Windows Tailscale is connected to a different tailnet, so the WSL Tailscale device will host the app URL."
        )

    auth_key_ref = str(services.get("auth_key_ref", "")).strip()
    api_token_ref = str(services.get("api_token_ref", "")).strip()
    existing_auth_key = str(get_env_value(auth_key_ref) or "").strip()
    existing_api_token = str(get_env_value(api_token_ref) or "").strip()
    print_info("Next, save a Tailscale auth key for the bundled tsidp service.")
    print_info("Create one in the Tailscale admin console under Keys > Generate auth key.")
    print_info("This key lets the bundled tsidp identity provider join your tailnet.")
    if existing_auth_key:
        print_info("Press Enter to keep the current saved auth key.")
    while True:
        auth_key = _sanitize_prompt_text(prompt("Tailscale auth key", ""))
        if auth_key:
            save_env_value_secure(auth_key_ref, auth_key)
            break
        if existing_auth_key:
            break
        print_warning("A Tailscale auth key is required for the bundled tsidp service.")

    print_info("Next, save a Tailscale API token for automatic policy setup.")
    print_info("Create one in the Tailscale admin console under Settings > Keys > API access tokens.")
    print_info("This token lets setup patch your tailnet policy so tsidp login and admin UI work.")
    if existing_api_token:
        print_info("Press Enter to keep the current saved API token.")
    while True:
        api_token = _sanitize_prompt_text(prompt("Tailscale API token", ""))
        if api_token:
            save_env_value_secure(api_token_ref, api_token)
            break
        if existing_api_token:
            break
        print_warning("A Tailscale API token is required so setup can patch tailnet policy automatically.")

    save_product_config(product_config)
    print_info("Applying the required tailnet policy for tsidp...")
    try:
        policy_status = ensure_tsidp_policy(product_config)
    except RuntimeError:
        raise
    urls = resolve_product_urls(product_config)
    print_success("Tailnet policy is ready for tsidp.")
    if policy_status["changed"]:
        print_info(f"  Policy backup:     {policy_status['backup_path']}")
    print_info(f"  Policy tailnet:    {policy_status['tailnet']}")
    print_info(f"  Tailnet app URL:  {urls['app_base_url']}")
    print_info(f"  Tailnet OIDC URL: {urls['issuer_url']}")
