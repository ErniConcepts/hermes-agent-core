"""Product-first setup flow for the hermes-core Tailnet-only distribution."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from hermes_cli.config import (
    ensure_hermes_home,
    get_config_path,
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
    ("identity", "Agent Identity"),
    ("storage", "Workspace Storage"),
    ("bootstrap", "Tailnet Auth & First Admin"),
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


def _detect_tailscale_identity(command_path: str) -> tuple[str, str]:
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
    data = __import__("json").loads(payload)
    self_payload = data.get("Self", {}) if isinstance(data, dict) else {}
    if not isinstance(self_payload, dict):
        raise RuntimeError("Tailscale status payload did not contain Self")
    tailnet_name = str(data.get("MagicDNSSuffix", "")).strip().rstrip(".").lower()
    dns_name = str(self_payload.get("DNSName", "")).strip().rstrip(".").lower()
    if not tailnet_name or not dns_name:
        raise RuntimeError("Tailscale is not connected to a tailnet with MagicDNS enabled")
    if tailnet_name.endswith(".ts.net"):
        tailnet_name = tailnet_name.removesuffix(".ts.net")
    if not dns_name.endswith(".ts.net"):
        raise RuntimeError("Unexpected Tailscale DNS name; expected a .ts.net host")
    device_name = dns_name.split(".", 1)[0]
    if not device_name or not tailnet_name:
        raise RuntimeError("Could not derive Tailnet device name and tailnet name")
    return device_name, tailnet_name


def setup_product_tailscale() -> None:
    product_config = load_product_config()
    tailscale = product_config.setdefault("network", {}).setdefault("tailscale", {})
    services = product_config.setdefault("services", {}).setdefault("tsidp", {})
    current_tailnet = str(tailscale.get("tailnet_name", "")).strip()
    current_device = str(tailscale.get("device_name", "")).strip()
    current_app_port = str(int(tailscale.get("app_https_port", 443)))
    current_idp_hostname = str(tailscale.get("idp_hostname", "idp")).strip() or "idp"
    command_path = str(tailscale.get("command_path", "tailscale")).strip() or "tailscale"

    print_header("Tailscale")
    print_info("This branch exposes Hermes Core only through your tailnet.")
    print_info("Setup verifies the local Tailscale node and stores an auth key for the bundled tsidp service.")

    device_name, tailnet_name = _detect_tailscale_identity(command_path)
    tailscale["enabled"] = True
    print_info(f"  Detected Tailnet device: {device_name}")
    print_info(f"  Detected Tailnet name:   {tailnet_name}")

    while True:
        chosen_tailnet = _sanitize_prompt_text(
            prompt("Tailnet name", current_tailnet or tailnet_name) or current_tailnet or tailnet_name
        ).lower()
        if chosen_tailnet:
            tailscale["tailnet_name"] = chosen_tailnet
            break
        print_warning("Tailnet name must not be empty.")

    while True:
        chosen_device = _sanitize_prompt_text(
            prompt("Tailscale device name", current_device or device_name) or current_device or device_name
        ).lower()
        if chosen_device:
            tailscale["device_name"] = chosen_device
            break
        print_warning("Tailscale device name must not be empty.")

    while True:
        chosen_idp_hostname = _sanitize_prompt_text(
            prompt("tsidp hostname", current_idp_hostname) or current_idp_hostname
        ).lower()
        if chosen_idp_hostname:
            tailscale["idp_hostname"] = chosen_idp_hostname
            break
        print_warning("tsidp hostname must not be empty.")

    while True:
        try:
            app_port = int(
                _sanitize_prompt_text(prompt("Tailnet HTTPS port for app", current_app_port) or current_app_port)
            )
        except ValueError:
            print_warning("Tailnet HTTPS port must be an integer.")
            continue
        if app_port <= 0:
            print_warning("Tailnet HTTPS port must be positive.")
            continue
        tailscale["app_https_port"] = app_port
        break

    auth_key_ref = str(services.get("auth_key_ref", "")).strip()
    while True:
        auth_key = _sanitize_prompt_text(prompt("Tailscale auth key", ""))
        if auth_key:
            save_env_value_secure(auth_key_ref, auth_key)
            break
        print_warning("A Tailscale auth key is required for the bundled tsidp service.")

    tailscale["command_path"] = command_path
    save_product_config(product_config)
    urls = resolve_product_urls(product_config)
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
    product_config = load_product_config()
    bootstrap = product_config.setdefault("bootstrap", {})
    current_first_admin = str(bootstrap.get("first_admin_tailscale_login", "")).strip()

    print_header("Tailnet Auth")
    print_info("Enter the Tailscale identity that should become the first admin.")
    print_info("The tsidp OIDC client will be created after the bundled tsidp service starts.")

    while True:
        first_admin_login = _sanitize_prompt_text(
            prompt("First admin Tailscale login", current_first_admin) or current_first_admin
        ).lower()
        if first_admin_login:
            bootstrap["first_admin_tailscale_login"] = first_admin_login
            break
        print_warning("First admin Tailscale login must not be empty.")

    save_product_config(product_config)


def _configure_tsidp_client_credentials() -> None:
    product_config = load_product_config()
    auth = product_config.setdefault("auth", {})
    urls = resolve_product_urls(product_config)
    current_client_id = str(auth.get("client_id", "")).strip() or "hermes-core"
    client_secret_ref = str(auth.get("client_secret_ref", "")).strip()

    print()
    print_header("tsidp Client")
    print_info("The bundled tsidp service is running. Create a Hermes Core client in the tsidp UI, then paste the credentials here.")
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
    first_admin_login = str(product_config.get("bootstrap", {}).get("first_admin_tailscale_login", "")).strip()

    print()
    print_header("Product Setup Summary")
    print_info(f"Hermes config:  {get_config_path()}")
    print_info(f"Secrets file:   {get_env_path()}")
    print_info(f"Product config: {hermes_home / 'product.yaml'}")
    print_info(f"Data folder:    {hermes_home}")
    print_info(f"Install dir:    {product_install_root()}")
    print_info(f"Tailnet app URL:         {urls['app_base_url']}")
    print_info(f"Tailnet OIDC issuer:     {urls['issuer_url']}")
    print_info(f"Local debug URL:         {urls['local_app_base_url']}")
    if bool(enrollment_state.get("first_admin_login_seen", False)):
        print_info("First admin bootstrap:   completed")
    else:
        print_info(f"First admin identity:    {first_admin_login or '(not set)'}")
        print_info(f"First admin sign-in URL: {urls['app_base_url']}")
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
    print_info(f"  First admin identity: {state['tailscale_login']}")
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
    print()

    setup_product_tailscale()
    setup_product_identity()
    setup_product_storage()
    setup_product_bootstrap_identity()
    _run_bootstrap_section()
    _print_product_setup_summary()
    print()
    print_success("Product setup complete!")
