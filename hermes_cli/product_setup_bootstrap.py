from __future__ import annotations

import re
import sys

from hermes_cli.config import get_config_path, get_env_path, get_hermes_home, save_env_value_secure
from hermes_cli.product_config import load_product_config, save_product_config
from hermes_cli.product_install import ensure_product_app_service_started, product_install_root, validate_product_host_prereqs
from hermes_cli.product_stack import (
    bootstrap_first_admin_enrollment,
    ensure_product_stack_started,
    initialize_product_stack,
    load_first_admin_enrollment_state,
    resolve_product_urls,
)
from hermes_cli.setup import print_header, print_info, print_warning, prompt

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_prompt_text(value: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", value or "")
    cleaned = _CONTROL_CHAR_RE.sub("", cleaned)
    return cleaned.strip()


def configure_tsidp_client_credentials() -> None:
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


def print_product_setup_summary() -> None:
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


def clear_terminal_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def print_install_handoff() -> None:
    clear_terminal_screen()
    print()
    print_header("Hermes Core Install")
    print_info("Host prerequisites are ready.")
    print_info("Starting the Tailnet-only product setup wizard...")
    print()


def start_product_stack() -> None:
    ensure_product_stack_started()
    configure_tsidp_client_credentials()
    state = bootstrap_first_admin_enrollment()
    ensure_product_app_service_started(load_product_config())
    print_info("Bundled tsidp service is configured.")
    print_info("  First admin bootstrap: one-time link required")
    print_info(f"  Auth mode:            {state['auth_mode']}")
    print_info(f"  App URL:              {state['setup_url']}")
    print_info(f"  OIDC client:          {state['oidc_client_id']}")


def run_bootstrap_section() -> None:
    try:
        validate_product_host_prereqs()
        initialize_product_stack()
        start_product_stack()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
