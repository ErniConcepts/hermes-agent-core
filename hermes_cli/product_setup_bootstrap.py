from __future__ import annotations

import re
import sys

from hermes_cli.config import get_config_path, get_env_path, get_env_value, get_hermes_home, save_env_value_secure
from hermes_cli.product_config import load_product_config, save_product_config
from hermes_cli.product_install import ensure_product_app_service_started, product_install_root, validate_product_host_prereqs
from hermes_cli.product_stack import (
    bootstrap_first_admin_enrollment,
    ensure_product_stack_started,
    first_admin_bootstrap_completed,
    initialize_product_stack,
    load_first_admin_enrollment_state,
    resolve_product_urls,
)
from hermes_cli.setup import print_header, print_info, print_success, print_warning, prompt

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_prompt_text(value: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", value or "")
    cleaned = _CONTROL_CHAR_RE.sub("", cleaned)
    return cleaned.strip()


def configure_tsidp_client_credentials() -> None:
    product_config = load_product_config()
    product_name = str(product_config.get("product", {}).get("brand", {}).get("name", "Hermes Core")).strip() or "Hermes Core"
    auth = product_config.setdefault("auth", {})
    urls = resolve_product_urls(product_config)
    current_client_id = str(auth.get("client_id", "")).strip() or "hermes-core"
    client_secret_ref = str(auth.get("client_secret_ref", "")).strip()
    existing_client_secret = str(get_env_value(client_secret_ref) or "").strip()
    print()
    print_header("tsidp Client")
    print_info("The bundled tsidp service is running.")
    print_info(f"Open the tsidp URL below and create or update the {product_name} OIDC client.")
    print_info(f"  tsidp URL:      {urls['issuer_url']}")
    print_info(f"  Redirect URI:   {urls['oidc_callback_url']}")
    print_info(f"  Suggested name: {product_name}")
    print_info("  Scopes:         openid profile email")
    print_info("Paste the client id and client secret here after saving the client in tsidp.")
    if current_client_id:
        print_info("Press Enter to keep the current saved client id.")
    if existing_client_secret:
        print_info("Press Enter to keep the current saved client secret.")
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
        if existing_client_secret:
            break
        print_warning("tsidp OIDC client secret must not be empty.")
    save_product_config(product_config)


def complete_first_admin_bootstrap(state: dict[str, object]) -> dict[str, object]:
    current_state = dict(state)
    if first_admin_bootstrap_completed(current_state):
        return current_state

    while True:
        print()
        print_header("Open Bootstrap Link")
        print_info("Open the one-time bootstrap URL below in your browser.")
        print_info("Sign in with Tailscale there to create the first admin account.")
        print_info(f"  Bootstrap URL: {current_state['setup_url']}")
        prompt("Press Enter after the bootstrap link shows you as signed in")
        refreshed_state = load_first_admin_enrollment_state() or current_state
        if first_admin_bootstrap_completed(refreshed_state):
            claimed_login = str(refreshed_state.get("tailscale_login", "")).strip()
            if claimed_login:
                print_success(f"First admin bootstrap completed for {claimed_login}.")
            else:
                print_success("First admin bootstrap completed.")
            return refreshed_state
        print_warning("Bootstrap is not complete yet. Finish the sign-in flow, then try again.")


def print_product_setup_summary() -> None:
    product_config = load_product_config()
    product_name = str(product_config.get("product", {}).get("brand", {}).get("name", "Hermes Core")).strip() or "Hermes Core"
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
    print_info(f"Product title:          {product_name}")
    print_info(f"Tailnet app URL:         {urls['app_base_url']}")
    print_info(f"Tailnet OIDC issuer:     {urls['issuer_url']}")
    print_info(f"Tailnet policy:          {policy_status}")
    if first_admin_bootstrap_completed(enrollment_state):
        print_info("First admin bootstrap:   completed")
        claimed_login = str(enrollment_state.get("tailscale_login", "")).strip()
        if claimed_login:
            print_info(f"First admin account:     {claimed_login}")
    else:
        if enrollment_state and bool(enrollment_state.get("first_admin_login_seen", False)):
            print_info("First admin bootstrap:   needs repair")
        else:
            print_info("First admin bootstrap:   pending")
        print_info(f"First admin sign-in URL: {enrollment_state.get('bootstrap_url') or urls['app_base_url']}")
    print_info(f"SOUL template:           {soul_template}")
    print_info(f"Workspace cap:           {workspace_limit_mb / 1024:.1f} GB per user")
    print_info("Hermes agent setup:")
    print_info("  Model/provider:        hermes setup model")
    print_info("  Tools:                 hermes setup tools")
    print_info("  Agent defaults:        hermes setup agent")
    print()
    print_header("Next Steps")
    print_info(f"{product_name} is now installed and reachable on your tailnet.")
    print_info("To finish the full Hermes setup, prepare these items first:")
    print_info("  a model endpoint and model name")
    print_info("  any API keys your chosen model provider needs")
    print_info("  any optional tool provider keys you want to use if you later broaden the default toolset")
    print_info("Then continue with these commands:")
    print_info("  hermes setup model")
    print_info("    Configure the model/provider Hermes will use for chats and agents.")
    print_info("  hermes setup tools")
    print_info("    Optional: broaden the default file/terminal/memory toolset with extra providers or capabilities.")
    print_info("  hermes setup agent")
    print_info("    Adjust default agent behavior and other runtime preferences.")
    print_info("After that, open the Tailnet app URL above and sign in with Tailscale.")


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


def reload_product_app_service() -> None:
    ensure_product_app_service_started(load_product_config())


def start_product_stack(force_new_bootstrap: bool = False) -> None:
    ensure_product_stack_started()
    configure_tsidp_client_credentials()
    state = bootstrap_first_admin_enrollment(force_new=force_new_bootstrap)
    product_config = load_product_config()
    ensure_product_app_service_started(product_config)
    urls = resolve_product_urls(product_config)
    print_info("Bundled tsidp service is configured.")
    if first_admin_bootstrap_completed(state):
        print_info("  First admin bootstrap: already completed")
        claimed_login = str(state.get("tailscale_login", "")).strip()
        if claimed_login:
            print_info(f"  First admin account:   {claimed_login}")
    else:
        if bool(state.get("first_admin_login_seen", False)):
            print_info("  First admin bootstrap: repaired and ready to be claimed again")
        else:
            print_info("  First admin bootstrap: one-time link required")
        print_info(f"  Bootstrap URL:         {state['setup_url']}")
    print_info(f"  Auth mode:            {state['auth_mode']}")
    print_info(f"  App URL:              {urls['app_base_url']}")
    print_info(f"  OIDC client:          {state['oidc_client_id']}")
    if not first_admin_bootstrap_completed(state):
        state = complete_first_admin_bootstrap(state)


def run_bootstrap_section(force_new_bootstrap: bool = False) -> None:
    try:
        validate_product_host_prereqs()
        initialize_product_stack()
        start_product_stack(force_new_bootstrap=force_new_bootstrap)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
