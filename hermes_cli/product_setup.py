"""Product-first setup flow for the hermes-core Tailnet-only distribution."""

from __future__ import annotations

from typing import Any

from hermes_cli.config import ensure_hermes_home
from hermes_cli.product_config import initialize_product_config_file
from hermes_cli.product_setup_bootstrap import (
    clear_terminal_screen as _clear_terminal_screen,
    complete_first_admin_bootstrap,
    configure_tsidp_client_credentials as _configure_tsidp_client_credentials,
    print_install_handoff as _print_install_handoff,
    print_product_setup_summary as _print_product_setup_summary,
    reload_product_app_service as _reload_product_app_service,
    run_bootstrap_section as _run_bootstrap_section,
    start_product_stack as _start_product_stack,
)
from hermes_cli.product_setup_sections import (
    setup_product_branding,
    setup_product_bootstrap_identity,
    setup_product_identity,
    setup_product_storage,
)
from hermes_cli.product_setup_tailscale import (
    detect_tailscale_identity as _detect_tailscale_identity,
    setup_product_tailscale,
)
from hermes_cli.setup import (
    Colors,
    color,
    is_interactive_stdin,
    print_info,
    print_noninteractive_setup_guidance,
    print_success,
)


PRODUCT_SETUP_SECTIONS = [
    ("tailscale", "Tailscale"),
    ("bootstrap", "Tailnet Auth & First Admin"),
    ("branding", "Branding"),
    ("identity", "Agent Identity"),
    ("storage", "Workspace Storage"),
]


def _reload_app_after_setup() -> None:
    try:
        _reload_product_app_service()
    except RuntimeError:
        # Setup still needs to succeed before the local service exists.
        return


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
        elif section == "branding":
            setup_product_branding()
        elif section == "identity":
            setup_product_identity()
        elif section == "storage":
            setup_product_storage()
        elif section == "bootstrap":
            force_new_bootstrap = setup_product_bootstrap_identity()
            _run_bootstrap_section(force_new_bootstrap=force_new_bootstrap)
        else:
            raise SystemExit(f"Unknown product setup section: {section}")
        _reload_app_after_setup()
        _print_product_setup_summary()
        return
    if getattr(args, "from_install", False):
        _print_install_handoff()
    print()
    print(color("Hermes Core Setup", Colors.MAGENTA))
    print()
    print_info("This configures the supplier-curated Tailnet-only Hermes Core product distribution.")
    print_info("All product access is authenticated through tsidp on your tailnet.")
    print_info("Setup will guide you through the full auth path in order.")
    print()
    setup_product_tailscale()
    force_new_bootstrap = setup_product_bootstrap_identity()
    _run_bootstrap_section(force_new_bootstrap=force_new_bootstrap)
    setup_product_branding()
    setup_product_identity()
    setup_product_storage()
    _reload_app_after_setup()
    _print_product_setup_summary()
    print()
    print_success("Product setup complete!")
