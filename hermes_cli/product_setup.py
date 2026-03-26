"""Product-first setup flow for the hermes-core distribution."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from hermes_cli.config import ensure_hermes_home, get_config_path, get_env_path, get_hermes_home
from hermes_cli.product_config import initialize_product_config_file, load_product_config, save_product_config
from hermes_cli.product_stack import (
    bootstrap_first_admin_enrollment,
    ensure_product_stack_started,
    initialize_product_stack,
    load_first_admin_enrollment_state,
    resolve_product_urls,
)
from hermes_cli.product_install import ensure_product_app_service_started, validate_product_host_prereqs
from hermes_cli.product_install import product_install_root
from hermes_cli.setup import (
    Colors,
    color,
    get_config_path,
    is_interactive_stdin,
    print_error,
    print_header,
    print_info,
    print_noninteractive_setup_guidance,
    print_success,
    print_warning,
    prompt,
)


PRODUCT_SETUP_SECTIONS = [
    ("network", "Product Network"),
    ("tailscale", "Tailscale"),
    ("identity", "Agent Identity"),
    ("storage", "Workspace Storage"),
    ("bootstrap", "Pocket ID & First Admin"),
]
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_prompt_text(value: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", value or "")
    cleaned = _CONTROL_CHAR_RE.sub("", cleaned)
    return cleaned.strip()


def _ensure_tcp_port_available(host: str, port: int, label: str) -> None:
    bind_host = (host or "").strip() or "0.0.0.0"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_host, port))
    except OSError as exc:
        raise RuntimeError(
            f"{label} port {port} on {bind_host} is already in use. "
            f"Free that port or change product network settings before bootstrap."
        ) from exc


def _validate_product_ports_available() -> None:
    product_config = load_product_config()
    network = product_config.get("network", {})
    bind_host = str(network.get("bind_host", "0.0.0.0")).strip() or "0.0.0.0"
    app_port = int(network.get("app_port", 8086))
    pocket_id_port = int(network.get("pocket_id_port", 1411))
    _ensure_tcp_port_available(bind_host, pocket_id_port, "Pocket ID")
    _ensure_tcp_port_available(bind_host, app_port, "Product app")


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
            f"Tailscale CLI not found: {command_path}. Install Tailscale or disable Tailscale exposure."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"Failed to query Tailscale identity with '{command_path} status --json'"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from exc
    payload = json.loads(result.stdout or "{}")
    dns_name = str(payload.get("Self", {}).get("DNSName", "")).strip().rstrip(".").lower()
    suffix = str(payload.get("MagicDNSSuffix", "")).strip().rstrip(".").lower()
    if not dns_name or not suffix:
        raise RuntimeError("Could not detect the current Tailscale device hostname")
    if not suffix.endswith(".ts.net"):
        raise RuntimeError(f"Unexpected Tailscale MagicDNS suffix: {suffix}")
    if not dns_name.endswith(f".{suffix}"):
        raise RuntimeError(f"Tailscale DNS name does not match suffix: {dns_name}")
    device_name = dns_name[: -(len(suffix) + 1)]
    tailnet_name = suffix.removesuffix(".ts.net")
    if "." in device_name:
        device_name = device_name.split(".", 1)[0]
    if not device_name or not tailnet_name:
        raise RuntimeError("Could not derive the Tailnet device name and tailnet name")
    return device_name, tailnet_name


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


def _resolve_hostname_addresses(hostname: str) -> set[str]:
    resolved: set[str] = set()
    for info in socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP):
        try:
            resolved.add(str(ip_address(info[4][0])))
        except (ValueError, IndexError, TypeError):
            continue
    return resolved


def _validate_public_host_for_this_machine(public_host: str) -> str | None:
    if public_host.lower() == "localhost":
        return None
    try:
        resolved = _resolve_hostname_addresses(public_host)
    except OSError:
        return (
            f"Host '{public_host}' does not currently resolve on this machine. "
            "LAN access will work only after DNS or mDNS points that name at this device."
        )
    if not resolved:
        return (
            f"Host '{public_host}' does not currently resolve on this machine. "
            "LAN access will work only after DNS or mDNS points that name at this device."
        )
    local_addresses = _local_interface_addresses()
    if local_addresses and resolved.isdisjoint(local_addresses):
        resolved_text = ", ".join(sorted(resolved))
        local_text = ", ".join(sorted(local_addresses))
        raise ValueError(
            f"Host '{public_host}' resolves to {resolved_text}, not this machine ({local_text}). "
            "Choose localhost or a hostname that resolves to this device."
        )
    return None


def setup_product_network() -> None:
    from hermes_cli.product_stack import _validate_public_host

    product_config = load_product_config()
    current_public_host = (
        str(product_config.get("network", {}).get("public_host", "")).strip() or "localhost"
    )

    print_header("Product Network")
    print_info("Choose the hostname users will use to reach this machine.")
    print_info("This hostname is used for local URLs when Tailscale mode is disabled.")
    print_info("When Tailscale mode is enabled, the Tailnet URL becomes the canonical auth origin.")
    print_info("Use a hostname like localhost, officebox.local, or a DNS name.")
    print_info("Raw IP addresses are not supported for the Pocket ID public host.")

    while True:
        public_host = _sanitize_prompt_text(
            prompt("Public host", current_public_host) or current_public_host
        )
        try:
            _validate_public_host(public_host)
            warning = _validate_public_host_for_this_machine(public_host)
        except ValueError as exc:
            print_warning(str(exc))
            continue
        if warning:
            print_warning(warning)
        product_config.setdefault("network", {})["public_host"] = public_host
        save_product_config(product_config)
        urls = resolve_product_urls(product_config)
        print_info(f"  App URL: {urls['app_base_url']}")
        print_info(f"  Pocket ID issuer: {urls['issuer_url']}")
        break


def setup_product_tailscale() -> None:
    product_config = load_product_config()
    tailscale = product_config.setdefault("network", {}).setdefault("tailscale", {})
    enabled = bool(tailscale.get("enabled", False))
    current_enabled = "yes" if enabled else "no"
    current_tailnet = str(tailscale.get("tailnet_name", "")).strip()
    current_device = str(tailscale.get("device_name", "")).strip()
    current_app_port = str(int(tailscale.get("app_https_port", 443)))
    current_auth_port = str(int(tailscale.get("auth_https_port", 4444)))

    print_header("Tailscale")
    print_info("Optionally expose the product app and Pocket ID through your tailnet.")
    print_info("When enabled, the Tailnet app URL becomes the only supported browser origin.")
    print_info("Local browser requests redirect to the Tailnet URL instead of running a second auth origin.")

    raw_enabled = _sanitize_prompt_text(
        prompt("Enable Tailscale exposure (yes/no)", current_enabled) or current_enabled
    ).lower()
    enabled = raw_enabled in {"y", "yes", "true", "1"}
    tailscale["enabled"] = enabled
    if not enabled:
        save_product_config(product_config)
        print_info("  Tailscale exposure disabled.")
        return

    command_path = str(tailscale.get("command_path", "tailscale")).strip() or "tailscale"
    device_name, tailnet_name = _detect_tailscale_identity(command_path)
    print_info(f"  Detected Tailnet device: {device_name}")
    print_info(f"  Detected Tailnet name:   {tailnet_name}")

    while True:
        chosen_tailnet = _sanitize_prompt_text(
            prompt("Tailnet name", current_tailnet or tailnet_name)
            or current_tailnet
            or tailnet_name
        ).lower()
        if chosen_tailnet:
            tailscale["tailnet_name"] = chosen_tailnet
            break
        print_warning("Tailnet name must not be empty when Tailscale is enabled.")

    while True:
        chosen_device = _sanitize_prompt_text(
            prompt("Tailscale device name", current_device or device_name)
            or current_device
            or device_name
        ).lower()
        if chosen_device:
            tailscale["device_name"] = chosen_device
            break
        print_warning("Tailscale device name must not be empty when Tailscale is enabled.")

    while True:
        try:
            app_port = int(
                _sanitize_prompt_text(
                    prompt("Tailnet HTTPS port for app", current_app_port) or current_app_port
                )
            )
            auth_port = int(
                _sanitize_prompt_text(
                    prompt("Tailnet HTTPS port for Pocket ID", current_auth_port) or current_auth_port
                )
            )
        except ValueError:
            print_warning("Tailscale HTTPS ports must be integers.")
            continue
        if app_port <= 0 or auth_port <= 0:
            print_warning("Tailscale HTTPS ports must be positive.")
            continue
        if app_port == auth_port:
            print_warning("App and Pocket ID must use different Tailnet HTTPS ports.")
            continue
        tailscale["app_https_port"] = app_port
        tailscale["auth_https_port"] = auth_port
        break

    save_product_config(product_config)
    urls = resolve_product_urls(product_config)
    print_info(f"  Canonical app URL:       {urls['app_base_url']}")
    print_info(f"  Canonical Pocket ID URL: {urls['issuer_url']}")
    if urls.get("local_app_base_url"):
        print_info(f"  Local debug URL:         {urls['local_app_base_url']}")


def setup_product_identity() -> None:
    product_config = load_product_config()
    current_path = (
        str(product_config.get("product", {}).get("agent", {}).get("soul_template_path", "")).strip()
    )

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


def _print_product_setup_summary() -> None:
    product_config = load_product_config()
    hermes_home = get_hermes_home()
    urls = resolve_product_urls(product_config)
    enrollment_state = load_first_admin_enrollment_state() or {}
    soul_template = (
        str(product_config.get("product", {}).get("agent", {}).get("soul_template_path", "")).strip()
        or "(bundled default)"
    )
    workspace_limit_mb = int(product_config.get("storage", {}).get("user_workspace_limit_mb", 2048))
    bind_host = str(product_config.get("network", {}).get("bind_host", "0.0.0.0")).strip() or "0.0.0.0"

    print()
    print_header("Product Setup Summary")
    print_info(f"Hermes config:  {get_config_path()}")
    print_info(f"Secrets file:   {get_env_path()}")
    print_info(f"Product config: {hermes_home / 'product.yaml'}")
    print_info(f"Data folder:    {hermes_home}")
    print_info(f"Install dir:    {product_install_root()}")
    print_info(f"Canonical app URL:       {urls['app_base_url']}")
    print_info(f"Canonical Pocket ID URL: {urls['issuer_url']}")
    if bind_host in {"127.0.0.1", "localhost"}:
        print_info(f"Service bind host:       {bind_host} (local-only)")
        print_info("LAN access URL:          disabled (set network.bind_host to 0.0.0.0)")
    else:
        print_info(f"Service bind host:       {bind_host} (LAN reachable)")
        app_port = int(product_config.get("network", {}).get("app_port", 8086))
        auth_port = int(product_config.get("network", {}).get("pocket_id_port", 1411))
        print_info(f"LAN app URL:             http://<HOST_IP>:{app_port}")
        print_info(f"LAN auth URL:            http://<HOST_IP>:{auth_port}")
    if urls.get("local_app_base_url"):
        print_info(f"Local debug URL:        {urls['local_app_base_url']}")
    if urls.get("local_issuer_url"):
        print_info(f"Local auth debug URL:   {urls['local_issuer_url']}")
    tailscale_enabled = bool(product_config.get("network", {}).get("tailscale", {}).get("enabled", False))
    bootstrap_mode = str(enrollment_state.get("bootstrap_mode", "native_setup")).strip() or "native_setup"
    first_admin_login_seen = bool(enrollment_state.get("first_admin_login_seen", False))
    setup_url = str(enrollment_state.get("setup_url", "")).strip()
    if first_admin_login_seen:
        print_info("First admin bootstrap:  completed")
        if tailscale_enabled:
            print_info("Tailnet auth exposure:  enabled")
    else:
        print_info(f"First admin bootstrap:  {bootstrap_mode}")
        if setup_url:
            print_info(f"First admin sign-up:    {setup_url}")
    if tailscale_enabled:
            print_info("Tailnet auth exposure:  pending first admin bootstrap")
            print_info("  During bootstrap, Pocket ID setup is intentionally local-only.")
            if urls.get("local_issuer_url"):
                print_info(f"  Complete bootstrap at: {urls['local_issuer_url']}/setup")
    print_info(f"SOUL template:  {soul_template}")
    print_info(f"Workspace cap:  {workspace_limit_mb / 1024:.1f} GB per user")
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
    print_info("Starting the product setup wizard...")
    print()


def _start_product_stack() -> None:
    ensure_product_stack_started()
    state = bootstrap_first_admin_enrollment()
    ensure_product_app_service_started(load_product_config())
    print_info("Bundled Pocket ID service is up.")
    print_info(f"  First admin: {state['username']}")
    if state["email"]:
        print_info(f"  First admin email: {state['email']}")
    print_info(f"  Auth mode: {state['auth_mode']}")
    print_info(f"  Bootstrap mode: {state.get('bootstrap_mode', 'native_setup')}")
    if state.get("first_admin_login_seen"):
        print_info("  First admin bootstrap already completed.")
    elif state.get("setup_url"):
        print_info(f"  First admin setup URL: {state['setup_url']}")
    print_info(f"  OIDC client: {state['oidc_client_id']}")


def _run_bootstrap_section() -> None:
    try:
        validate_product_host_prereqs()
        _validate_product_ports_available()
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
        print_noninteractive_setup_guidance(
            "Running in a non-interactive environment (no TTY detected)."
        )
        return

    section = getattr(args, "section", None)
    if section:
        if section == "network":
            setup_product_network()
        elif section == "tailscale":
            setup_product_tailscale()
        elif section == "identity":
            setup_product_identity()
        elif section == "storage":
            setup_product_storage()
        elif section == "bootstrap":
            _run_bootstrap_section()
        else:
            print_error(f"Unknown product setup section: {section}")
            print_info(f"Available sections: {', '.join(key for key, _ in PRODUCT_SETUP_SECTIONS)}")
            return
        _print_product_setup_summary()
        return

    if getattr(args, "from_install", False):
        _print_install_handoff()

    print()
    print(
        color(
            "┌─────────────────────────────────────────────────────────┐",
            Colors.MAGENTA,
        )
    )
    print(
        color(
            "│           ⚕ Hermes Core Product Setup Wizard            │",
            Colors.MAGENTA,
        )
    )
    print(
        color(
            "└─────────────────────────────────────────────────────────┘",
            Colors.MAGENTA,
        )
    )
    print()
    print_info("This configures the supplier-curated local product distribution.")
    print_info("Hermes-native agent configuration stays in the generic 'hermes setup' flow.")
    print()

    setup_product_network()
    setup_product_tailscale()
    setup_product_identity()
    setup_product_storage()
    _run_bootstrap_section()
    _print_product_setup_summary()
    print()
    print_success("Product setup complete!")
