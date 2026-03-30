from __future__ import annotations

import sys
from typing import Any


def configure_tsidp_client_credentials(hooks: Any) -> None:
    product_config = hooks.load_product_config()
    auth = product_config.setdefault("auth", {})
    urls = hooks.resolve_product_urls(product_config)
    current_client_id = str(auth.get("client_id", "")).strip() or "hermes-core"
    client_secret_ref = str(auth.get("client_secret_ref", "")).strip()

    print()
    hooks.print_header("tsidp Client")
    hooks.print_info("The bundled tsidp service is running.")
    hooks.print_info("Next steps:")
    hooks.print_info("  1. Open the tsidp URL below")
    hooks.print_info("  2. Create a client named Hermes Core")
    hooks.print_info("  3. Use the redirect URI shown below")
    hooks.print_info("  4. Paste the client id and client secret back here")
    hooks.print_info(f"  tsidp URL:      {urls['issuer_url']}")
    hooks.print_info(f"  Redirect URI:   {urls['oidc_callback_url']}")
    hooks.print_info("  Suggested name: Hermes Core")
    hooks.print_info("  Scopes:         openid profile email")

    while True:
        client_id = hooks._sanitize_prompt_text(hooks.prompt("tsidp OIDC client id", current_client_id) or current_client_id)
        if client_id:
            auth["client_id"] = client_id
            break
        hooks.print_warning("tsidp OIDC client id must not be empty.")

    while True:
        client_secret = hooks._sanitize_prompt_text(hooks.prompt("tsidp OIDC client secret", ""))
        if client_secret:
            hooks.save_env_value_secure(client_secret_ref, client_secret)
            break
        hooks.print_warning("tsidp OIDC client secret must not be empty.")

    hooks.save_product_config(product_config)


def print_product_setup_summary(hooks: Any) -> None:
    product_config = hooks.load_product_config()
    hermes_home = hooks.get_hermes_home()
    urls = hooks.resolve_product_urls(product_config)
    enrollment_state = hooks.load_first_admin_enrollment_state() or {}
    soul_template = str(product_config.get("product", {}).get("agent", {}).get("soul_template_path", "")).strip() or "(bundled default)"
    workspace_limit_mb = int(product_config.get("storage", {}).get("user_workspace_limit_mb", 2048))
    tailscale_cfg = product_config.get("network", {}).get("tailscale", {})
    policy_status = "configured" if str(tailscale_cfg.get("api_tailnet_name", "")).strip() else "not configured yet"

    print()
    hooks.print_header("Product Setup Summary")
    hooks.print_info(f"Hermes config:  {hooks.get_config_path()}")
    hooks.print_info(f"Secrets file:   {hooks.get_env_path()}")
    hooks.print_info(f"Product config: {hermes_home / 'product.yaml'}")
    hooks.print_info(f"Data folder:    {hermes_home}")
    hooks.print_info(f"Install dir:    {hooks.product_install_root()}")
    hooks.print_info(f"Tailnet app URL:         {urls['app_base_url']}")
    hooks.print_info(f"Tailnet OIDC issuer:     {urls['issuer_url']}")
    hooks.print_info(f"Tailnet policy:          {policy_status}")
    hooks.print_info(f"Local debug URL:         {urls['local_app_base_url']}")
    if bool(enrollment_state.get("first_admin_login_seen", False)):
        hooks.print_info("First admin bootstrap:   completed")
        claimed_login = str(enrollment_state.get("tailscale_login", "")).strip()
        if claimed_login:
            hooks.print_info(f"First admin account:     {claimed_login}")
    else:
        hooks.print_info("First admin bootstrap:   pending")
        hooks.print_info(f"First admin sign-in URL: {enrollment_state.get('bootstrap_url') or urls['app_base_url']}")
    hooks.print_info(f"SOUL template:           {soul_template}")
    hooks.print_info(f"Workspace cap:           {workspace_limit_mb / 1024:.1f} GB per user")
    hooks.print_info("Hermes agent setup:")
    hooks.print_info("  Model/provider:        hermes setup model")
    hooks.print_info("  Tools:                 hermes setup tools")
    hooks.print_info("  Gateway/messaging:     hermes setup gateway")
    hooks.print_info("  Agent defaults:        hermes setup agent")


def clear_terminal_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def print_install_handoff(hooks: Any) -> None:
    clear_terminal_screen()
    print()
    hooks.print_header("Hermes Core Install")
    hooks.print_info("Host prerequisites are ready.")
    hooks.print_info("Starting the Tailnet-only product setup wizard...")
    print()


def start_product_stack(hooks: Any) -> None:
    hooks.ensure_product_stack_started()
    hooks._configure_tsidp_client_credentials()
    state = hooks.bootstrap_first_admin_enrollment()
    hooks.ensure_product_app_service_started(hooks.load_product_config())
    hooks.print_info("Bundled tsidp service is configured.")
    hooks.print_info("  First admin bootstrap: one-time link required")
    hooks.print_info(f"  Auth mode:            {state['auth_mode']}")
    hooks.print_info(f"  App URL:              {state['setup_url']}")
    hooks.print_info(f"  OIDC client:          {state['oidc_client_id']}")


def run_bootstrap_section(hooks: Any) -> None:
    try:
        hooks.validate_product_host_prereqs()
        hooks.initialize_product_stack()
        hooks._start_product_stack()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
