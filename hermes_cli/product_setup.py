"""Product-first setup flow for the hermes-core Tailnet-only distribution."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import json
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from hermes_cli.config import (
    ensure_hermes_home,
    get_config_path,
    get_env_value,
    get_env_path,
    get_hermes_home,
    save_env_value_secure,
)
from hermes_cli.product_config import initialize_product_config_file, load_product_config, save_product_config
from hermes_cli.product_install import ensure_product_app_service_started, product_install_root, validate_product_host_prereqs
from hermes_cli.product_stack import (
    bootstrap_first_admin_enrollment,
    ensure_product_stack_started,
    initialize_product_stack,
    load_first_admin_enrollment_state,
    resolve_product_urls,
)
from hermes_cli.product_tailscale_api import ensure_tsidp_policy
from hermes_cli.setup import (
    Colors,
    color,
    is_interactive_stdin,
    print_header,
    print_info,
    print_noninteractive_setup_guidance,
    print_success,
    print_warning,
    prompt,
)


PRODUCT_SETUP_SECTIONS = [
    ("tailscale", "Tailscale"),
    ("bootstrap", "Tailnet Auth & First Admin"),
    ("identity", "Agent Identity"),
    ("storage", "Workspace Storage"),
]
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_prompt_text(value: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", value or "")
    cleaned = _CONTROL_CHAR_RE.sub("", cleaned)
    return cleaned.strip()


def _local_interface_addresses() -> set[str]:
    addresses = {"127.0.0.1", "::1"}
    if os.name != "nt":
        result = subprocess.run(
            ["hostname", "-I"],
            check=False,
            capture_output=True,
            text=True,
        )
        for token in (result.stdout or "").split():
            try:
                addresses.add(str(ip_address(token.strip())))
            except ValueError:
                continue
    for candidate in {socket.gethostname(), socket.getfqdn(), "localhost"}:
        if not candidate:
            continue
        try:
            infos = socket.getaddrinfo(candidate, None, proto=socket.IPPROTO_TCP)
        except OSError:
            continue
        for info in infos:
            try:
                addresses.add(str(ip_address(info[4][0])))
            except (ValueError, IndexError, TypeError):
                continue
    return addresses


def _detect_tailscale_identity(command_path: str) -> dict[str, str]:
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


def setup_product_tailscale() -> None:
    product_config = load_product_config()
    tailscale = product_config.setdefault("network", {}).setdefault("tailscale", {})
    services = product_config.setdefault("services", {}).setdefault("tsidp", {})
    current_idp_hostname = str(tailscale.get("idp_hostname", "idp")).strip() or "idp"
    command_path = str(tailscale.get("command_path", "tailscale")).strip() or "tailscale"

    print_header("Tailscale")
    print_info("This branch exposes Hermes Core only through your tailnet.")
    print_info("Setup will:")
    print_info("  1. Verify the current Tailscale node and tailnet")
    print_info("  2. Save a tsidp auth key and Tailscale API token")
    print_info("  3. Patch tailnet policy so tsidp login and admin UI work")
    print_info("  4. Continue into tsidp startup and first-admin bootstrap")

    detected = _detect_tailscale_identity(command_path)
    tailscale["enabled"] = True
    tailscale["tailnet_name"] = detected["tailnet_name"]
    tailscale["device_name"] = detected["device_name"]
    tailscale["api_tailnet_name"] = detected["api_tailnet_name"]
    tailscale["app_https_port"] = int(tailscale.get("app_https_port", 443) or 443)
    print_info(f"  Detected Tailnet device:     {detected['device_name']}")
    print_info(f"  Detected MagicDNS suffix:    {detected['tailnet_name']}")
    print_info(f"  Detected policy tailnet key: {detected['api_tailnet_name']}")

    while True:
        chosen_idp_hostname = _sanitize_prompt_text(
            prompt("tsidp hostname", current_idp_hostname) or current_idp_hostname
        ).lower()
        if chosen_idp_hostname:
            tailscale["idp_hostname"] = chosen_idp_hostname
            break
        print_warning("tsidp hostname must not be empty.")

    auth_key_ref = str(services.get("auth_key_ref", "")).strip()
    api_token_ref = str(services.get("api_token_ref", "")).strip()
    existing_auth_key = str(get_env_value(auth_key_ref) or "").strip()
    existing_api_token = str(get_env_value(api_token_ref) or "").strip()
    if existing_auth_key:
        print_info("  Tailscale auth key: already saved; press Enter to keep it.")
    if existing_api_token:
        print_info("  Tailscale API token: already saved; press Enter to keep it.")
    while True:
        auth_key = _sanitize_prompt_text(prompt("Tailscale auth key", ""))
        if auth_key:
            save_env_value_secure(auth_key_ref, auth_key)
            break
        if existing_auth_key:
            break
        print_warning("A Tailscale auth key is required for the bundled tsidp service.")

    while True:
        api_token = _sanitize_prompt_text(prompt("Tailscale API token", ""))
        if api_token:
            save_env_value_secure(api_token_ref, api_token)
            break
        if existing_api_token:
            break
        print_warning("A Tailscale API token is required so setup can patch tailnet policy automatically.")

    tailscale["command_path"] = command_path
    save_product_config(product_config)
    policy_status = ensure_tsidp_policy(product_config)
    urls = resolve_product_urls(product_config)
    print_success("Tailnet policy is ready for tsidp.")
    if policy_status["changed"]:
        print_info(f"  Policy backup:     {policy_status['backup_path']}")
    print_info(f"  Policy tailnet:    {policy_status['tailnet']}")
    print_info(f"  Tailnet app URL:  {urls['app_base_url']}")
    print_info(f"  Tailnet OIDC URL: {urls['issuer_url']}")


def setup_product_identity() -> None:
    product_config = load_product_config()
    current_path = str(product_config.get("product", {}).get("agent", {}).get("soul_template_path", "")).strip()

    print_header("Agent Identity")
    print_info("Choose an optional markdown file to use as the runtime SOUL.md template.")
    print_info("Leave this blank to use the bundled default Hermes Core identity.")

    while True:
        raw_value = _sanitize_prompt_text(prompt("SOUL.md template path", current_path) or current_path)
        if not raw_value:
            product_config.setdefault("product", {}).setdefault("agent", {})["soul_template_path"] = ""
            save_product_config(product_config)
            print_info("  Using bundled default SOUL.md template.")
            return
        candidate = Path(raw_value).expanduser().resolve()
        if not candidate.exists():
            print_warning(f"Template not found: {candidate}")
            continue
        if not candidate.is_file():
            print_warning(f"Template path is not a file: {candidate}")
            continue
        product_config.setdefault("product", {}).setdefault("agent", {})["soul_template_path"] = str(candidate)
        save_product_config(product_config)
        print_info(f"  Runtime SOUL.md will be rendered from: {candidate}")
        return


def setup_product_storage() -> None:
    product_config = load_product_config()
    current_limit_mb = int(product_config.get("storage", {}).get("user_workspace_limit_mb", 2048))
    default_gb = f"{current_limit_mb / 1024:.1f}".rstrip("0").rstrip(".")

    print_header("Workspace Storage")
    print_info("Choose the per-user storage limit for uploaded files and folders.")
    print_info("Files are written directly into the live-mounted runtime workspace.")

    while True:
        raw_value = _sanitize_prompt_text(prompt("Per-user workspace limit (GB)", default_gb) or default_gb)
        try:
            limit_gb = float(raw_value)
        except ValueError:
            print_warning("Please enter a number like 2, 5, or 10.")
            continue
        if limit_gb <= 0:
            print_warning("Workspace storage limit must be greater than zero.")
            continue
        limit_mb = max(1, round(limit_gb * 1024))
        product_config.setdefault("storage", {})["user_workspace_limit_mb"] = limit_mb
        save_product_config(product_config)
        print_info(f"  Per-user workspace limit: {limit_mb / 1024:.1f} GB")
        return


def setup_product_bootstrap_identity() -> None:
    print_header("Tailnet Auth")
    print_info("Setup will create a one-time bootstrap link for the first admin.")
    print_info("Open that link, sign in with Tailscale, and the first authenticated account becomes admin.")


def _configure_tsidp_client_credentials() -> None:
    product_config = load_product_config()
    auth = product_config.setdefault("auth", {})
    urls = resolve_product_urls(product_config)
    current_client_id = str(auth.get("client_id", "")).strip() or "hermes-core"
    client_secret_ref = str(auth.get("client_secret_ref", "")).strip()

    print()
    print_header("tsidp Client")
    print_info("The bundled tsidp service is running.")
    print_info("Next steps:")
    print_info("  1. Open the tsidp URL below")
    print_info("  2. Create a client named Hermes Core")
    print_info("  3. Use the redirect URI shown below")
    print_info("  4. Paste the client id and client secret back here")
    print_info(f"  tsidp URL:      {urls['issuer_url']}")
    print_info(f"  Redirect URI:   {urls['oidc_callback_url']}")
    print_info("  Suggested name: Hermes Core")
    print_info("  Scopes:         openid profile email")

    while True:
        client_id = _sanitize_prompt_text(prompt("tsidp OIDC client id", current_client_id) or current_client_id)
        if client_id:
            auth["client_id"] = client_id
            break
        print_warning("tsidp OIDC client id must not be empty.")

    while True:
        client_secret = _sanitize_prompt_text(prompt("tsidp OIDC client secret", ""))
        if client_secret:
            save_env_value_secure(client_secret_ref, client_secret)
            break
        print_warning("tsidp OIDC client secret must not be empty.")

    save_product_config(product_config)


def _print_product_setup_summary() -> None:
    product_config = load_product_config()
    hermes_home = get_hermes_home()
    urls = resolve_product_urls(product_config)
    enrollment_state = load_first_admin_enrollment_state() or {}
    soul_template = str(product_config.get("product", {}).get("agent", {}).get("soul_template_path", "")).strip() or "(bundled default)"
    workspace_limit_mb = int(product_config.get("storage", {}).get("user_workspace_limit_mb", 2048))
    tailscale_cfg = product_config.get("network", {}).get("tailscale", {})
    policy_status = "configured" if str(tailscale_cfg.get("api_tailnet_name", "")).strip() else "not configured yet"

    print()
    print_header("Product Setup Summary")
    print_info(f"Hermes config:  {get_config_path()}")
    print_info(f"Secrets file:   {get_env_path()}")
    print_info(f"Product config: {hermes_home / 'product.yaml'}")
    print_info(f"Data folder:    {hermes_home}")
    print_info(f"Install dir:    {product_install_root()}")
    print_info(f"Tailnet app URL:         {urls['app_base_url']}")
    print_info(f"Tailnet OIDC issuer:     {urls['issuer_url']}")
    print_info(f"Tailnet policy:          {policy_status}")
    print_info(f"Local debug URL:         {urls['local_app_base_url']}")
    if bool(enrollment_state.get("first_admin_login_seen", False)):
        print_info("First admin bootstrap:   completed")
        claimed_login = str(enrollment_state.get("tailscale_login", "")).strip()
        if claimed_login:
            print_info(f"First admin account:     {claimed_login}")
    else:
        print_info("First admin bootstrap:   pending")
        print_info(f"First admin sign-in URL: {enrollment_state.get('bootstrap_url') or urls['app_base_url']}")
    print_info(f"SOUL template:           {soul_template}")
    print_info(f"Workspace cap:           {workspace_limit_mb / 1024:.1f} GB per user")
    print_info("Hermes agent setup:")
    print_info("  Model/provider:        hermes setup model")
    print_info("  Tools:                 hermes setup tools")
    print_info("  Gateway/messaging:     hermes setup gateway")
    print_info("  Agent defaults:        hermes setup agent")


def _clear_terminal_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def _print_install_handoff() -> None:
    _clear_terminal_screen()
    print()
    print_header("Hermes Core Install")
    print_info("Host prerequisites are ready.")
    print_info("Starting the Tailnet-only product setup wizard...")
    print()


def _start_product_stack() -> None:
    ensure_product_stack_started()
    _configure_tsidp_client_credentials()
    state = bootstrap_first_admin_enrollment()
    ensure_product_app_service_started(load_product_config())
    print_info("Bundled tsidp service is configured.")
    print_info("  First admin bootstrap: one-time link required")
    print_info(f"  Auth mode:            {state['auth_mode']}")
    print_info(f"  App URL:              {state['setup_url']}")
    print_info(f"  OIDC client:          {state['oidc_client_id']}")


def _run_bootstrap_section() -> None:
    try:
        validate_product_host_prereqs()
        initialize_product_stack()
        _start_product_stack()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def run_product_setup_wizard(args: Any) -> None:
    ensure_hermes_home()
    initialize_product_config_file()

    non_interactive = getattr(args, "non_interactive", False)
    if not non_interactive and not is_interactive_stdin():
        non_interactive = True
    if non_interactive:
        print_noninteractive_setup_guidance("Running in a non-interactive environment (no TTY detected).")
        return

    section = getattr(args, "section", None)
    if section:
        if section == "tailscale":
            setup_product_tailscale()
        elif section == "identity":
            setup_product_identity()
        elif section == "storage":
            setup_product_storage()
        elif section == "bootstrap":
            setup_product_bootstrap_identity()
            _run_bootstrap_section()
        else:
            raise SystemExit(f"Unknown product setup section: {section}")
        _print_product_setup_summary()
        return

    if getattr(args, "from_install", False):
        _print_install_handoff()

    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.MAGENTA))
    print(color("│        Hermes Core Tailnet-only Setup Wizard           │", Colors.MAGENTA))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.MAGENTA))
    print()
    print_info("This configures the supplier-curated Tailnet-only Hermes Core product distribution.")
    print_info("All product access is authenticated through tsidp on your tailnet.")
    print_info("Setup will guide you through the full auth path in order.")
    print()

    setup_product_tailscale()
    setup_product_bootstrap_identity()
    _run_bootstrap_section()
    setup_product_identity()
    setup_product_storage()
    _print_product_setup_summary()
    print()
    print_success("Product setup complete!")
