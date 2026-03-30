"""Product-first setup flow for the hermes-core Tailnet-only distribution."""

from __future__ import annotations

import os
import re
import socket
import sys
from ipaddress import ip_address
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
from hermes_cli.product_setup_bootstrap import (
    clear_terminal_screen as _clear_terminal_screen_impl,
    configure_tsidp_client_credentials as _configure_tsidp_client_credentials_impl,
    print_install_handoff as _print_install_handoff_impl,
    print_product_setup_summary as _print_product_setup_summary_impl,
    run_bootstrap_section as _run_bootstrap_section_impl,
    start_product_stack as _start_product_stack_impl,
)
from hermes_cli.product_setup_sections import (
    setup_product_bootstrap_identity as _setup_product_bootstrap_identity_impl,
    setup_product_identity as _setup_product_identity_impl,
    setup_product_storage as _setup_product_storage_impl,
)
from hermes_cli.product_setup_tailscale import (
    detect_tailscale_identity as _detect_tailscale_identity_impl,
    setup_product_tailscale as _setup_product_tailscale_impl,
)
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
    return _detect_tailscale_identity_impl(command_path)


def setup_product_tailscale() -> None:
    _setup_product_tailscale_impl(sys.modules[__name__])


def setup_product_identity() -> None:
    _setup_product_identity_impl(sys.modules[__name__])


def setup_product_storage() -> None:
    _setup_product_storage_impl(sys.modules[__name__])


def setup_product_bootstrap_identity() -> None:
    _setup_product_bootstrap_identity_impl(sys.modules[__name__])


def _configure_tsidp_client_credentials() -> None:
    _configure_tsidp_client_credentials_impl(sys.modules[__name__])


def _print_product_setup_summary() -> None:
    _print_product_setup_summary_impl(sys.modules[__name__])


def _clear_terminal_screen() -> None:
    _clear_terminal_screen_impl()


def _print_install_handoff() -> None:
    _print_install_handoff_impl(sys.modules[__name__])


def _start_product_stack() -> None:
    _start_product_stack_impl(sys.modules[__name__])


def _run_bootstrap_section() -> None:
    _run_bootstrap_section_impl(sys.modules[__name__])


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
