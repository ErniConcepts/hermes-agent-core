"""Product-first setup flow for the hermes-core distribution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Any
from pathlib import Path

import yaml

from hermes_cli.config import ensure_hermes_home, get_config_path, get_env_path, get_hermes_home
from hermes_cli.product_config import initialize_product_config_file, load_product_config, save_product_config
from hermes_cli.product_stack import (
    bootstrap_first_admin_enrollment,
    ensure_product_stack_started,
    initialize_product_stack,
    resolve_product_urls,
)
from hermes_cli.product_install import ensure_product_app_service_started, validate_product_host_prereqs
from model_tools import get_available_toolsets
from toolsets import validate_toolset
from hermes_cli.setup import (
    PROJECT_ROOT,
    Colors,
    color,
    get_env_path,
    get_env_value,
    get_config_path,
    is_interactive_stdin,
    print_error,
    print_header,
    print_info,
    print_noninteractive_setup_guidance,
    print_success,
    print_warning,
    prompt,
    prompt_choice,
    setup_model_provider,
    setup_tools,
)


PRODUCT_SETUP_SECTIONS = [
    ("network", "Product Network"),
    ("tailscale", "Tailscale"),
    ("identity", "Agent Identity"),
    ("storage", "Workspace Storage"),
    ("model", "Model & Provider"),
    ("tools", "Tools"),
    ("bootstrap", "Pocket ID & First Admin"),
]

DEFAULT_PRODUCT_TOOLSETS = ["memory", "session_search"]


def _detect_tailscale_identity(command_path: str) -> tuple[str, str]:
    result = subprocess.run(
        [command_path, "status", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
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
        public_host = (prompt("Public host", current_public_host) or current_public_host).strip()
        try:
            _validate_public_host(public_host)
        except ValueError as exc:
            print_warning(str(exc))
            continue
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

    raw_enabled = (prompt("Enable Tailscale exposure (yes/no)", current_enabled) or current_enabled).strip().lower()
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
        chosen_tailnet = (prompt("Tailnet name", current_tailnet or tailnet_name) or current_tailnet or tailnet_name).strip().lower()
        if chosen_tailnet:
            tailscale["tailnet_name"] = chosen_tailnet
            break
        print_warning("Tailnet name must not be empty when Tailscale is enabled.")

    while True:
        chosen_device = (prompt("Tailscale device name", current_device or device_name) or current_device or device_name).strip().lower()
        if chosen_device:
            tailscale["device_name"] = chosen_device
            break
        print_warning("Tailscale device name must not be empty when Tailscale is enabled.")

    while True:
        try:
            app_port = int((prompt("Tailnet HTTPS port for app", current_app_port) or current_app_port).strip())
            auth_port = int((prompt("Tailnet HTTPS port for Pocket ID", current_auth_port) or current_auth_port).strip())
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
        raw_value = (prompt("SOUL.md template path", current_path) or current_path).strip()
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
        raw_value = (prompt("Per-user workspace limit (GB)", default_gb) or default_gb).strip()
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

def _sync_model_route_from_temp_config(temp_config: dict[str, Any]) -> None:
    model_cfg = temp_config.get("model")
    if isinstance(model_cfg, str):
        model_cfg = {"default": model_cfg}
    if not isinstance(model_cfg, dict):
        raise RuntimeError("Product model setup did not return a valid model configuration")

    provider = str(model_cfg.get("provider") or "").strip()
    model_name = str(model_cfg.get("default") or "").strip()
    if not provider or not model_name:
        raise RuntimeError("Product model setup requires both provider and model")

    product_config = load_product_config()
    route = product_config.setdefault("models", {}).setdefault("default_route", {})
    route["provider"] = provider
    route["model"] = model_name

    api_mode = str(model_cfg.get("api_mode") or "").strip()
    if api_mode:
        route["api_mode"] = api_mode
    else:
        route.pop("api_mode", None)

    base_url = str(model_cfg.get("base_url") or "").strip()
    if provider == "custom":
        base_url = base_url or str(get_env_value("OPENAI_BASE_URL") or "").strip()
        if not base_url:
            raise RuntimeError("Custom product model routes require a base URL")
    if base_url:
        route["base_url"] = base_url.rstrip("/")
    else:
        route.pop("base_url", None)

    save_product_config(product_config)


def _seed_product_model_setup_config(product_config: dict[str, Any]) -> dict[str, Any]:
    route = product_config.get("models", {}).get("default_route", {})
    toolsets = product_config.get("tools", {}).get("hermes_toolsets", []) or list(DEFAULT_PRODUCT_TOOLSETS)
    seeded_model: dict[str, Any] = {
        "provider": str(route.get("provider", "custom")).strip() or "custom",
        "default": str(route.get("model", "")).strip() or "qwen3.5-9b-local",
    }
    base_url = str(route.get("base_url", "")).strip()
    if base_url:
        seeded_model["base_url"] = base_url
    api_mode = str(route.get("api_mode", "")).strip()
    if api_mode:
        seeded_model["api_mode"] = api_mode
    return {
        "model": seeded_model,
        "platform_toolsets": {"cli": [str(toolset).strip() for toolset in toolsets if str(toolset).strip()]},
    }


@contextmanager
def _isolated_product_setup_home() -> Any:
    product_config = load_product_config()
    real_home = get_hermes_home()
    real_env_path = get_env_path()
    real_auth_path = real_home / "auth.json"
    original_home = os.environ.get("HERMES_HOME")
    with tempfile.TemporaryDirectory(prefix="hermes-core-product-setup-") as temp_dir:
        temp_home = Path(temp_dir)
        try:
            os.environ["HERMES_HOME"] = str(temp_home)
            ensure_hermes_home()
            if real_env_path.exists():
                shutil.copyfile(real_env_path, get_env_path())
            seeded_config = _seed_product_model_setup_config(product_config)
            get_config_path().write_text(yaml.safe_dump(seeded_config, sort_keys=False), encoding="utf-8")
            yield temp_home
            temp_env_path = get_env_path()
            if temp_env_path.exists():
                real_home.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(temp_env_path, real_env_path)
            temp_auth_path = temp_home / "auth.json"
            if temp_auth_path.exists():
                real_home.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(temp_auth_path, real_auth_path)
        finally:
            if original_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = original_home


def _sync_toolsets_from_temp_config(temp_config: dict[str, Any]) -> None:
    platform_toolsets = temp_config.get("platform_toolsets", {})
    cli_toolsets = platform_toolsets.get("cli") if isinstance(platform_toolsets, dict) else None
    if not isinstance(cli_toolsets, list):
        raise RuntimeError("Product tool setup did not return a valid CLI toolset selection")

    normalized = [str(toolset).strip() for toolset in cli_toolsets if str(toolset).strip()]
    available_toolsets = set(get_available_toolsets().keys())
    filtered = [toolset for toolset in normalized if validate_toolset(toolset) and toolset in available_toolsets]
    dropped = [toolset for toolset in normalized if toolset not in filtered]
    if dropped:
        print_warning(
            "Ignoring unavailable or unknown toolsets in product config sync: "
            + ", ".join(dropped)
        )
    if not filtered:
        raise RuntimeError("Product tools setup requires at least one valid Hermes toolset")
    product_config = load_product_config()
    product_config.setdefault("tools", {})["hermes_toolsets"] = filtered
    save_product_config(product_config)


def _print_product_setup_summary() -> None:
    product_config = load_product_config()
    hermes_home = get_hermes_home()
    urls = resolve_product_urls(product_config)
    soul_template = (
        str(product_config.get("product", {}).get("agent", {}).get("soul_template_path", "")).strip()
        or "(bundled default)"
    )
    workspace_limit_mb = int(product_config.get("storage", {}).get("user_workspace_limit_mb", 2048))

    print()
    print_header("Product Setup Summary")
    print_info(f"Hermes config:  {get_config_path()}")
    print_info(f"Secrets file:   {get_env_path()}")
    print_info(f"Product config: {hermes_home / 'product.yaml'}")
    print_info(f"Data folder:    {hermes_home}")
    print_info(f"Install dir:    {PROJECT_ROOT}")
    print_info(f"Canonical app URL:       {urls['app_base_url']}")
    print_info(f"Canonical Pocket ID URL: {urls['issuer_url']}")
    if urls.get("local_app_base_url"):
        print_info(f"Local debug URL:        {urls['local_app_base_url']}")
    if urls.get("local_issuer_url"):
        print_info(f"Local auth debug URL:   {urls['local_issuer_url']}")
    print_info(f"SOUL template:  {soul_template}")
    print_info(f"Workspace cap:  {workspace_limit_mb / 1024:.1f} GB per user")


def _start_product_stack() -> None:
    ensure_product_stack_started()
    state = bootstrap_first_admin_enrollment()
    ensure_product_app_service_started(load_product_config())
    print_info("Bundled Pocket ID service is up.")
    print_info(f"  First admin: {state['username']}")
    if state["email"]:
        print_info(f"  First admin email: {state['email']}")
    print_info(f"  Auth mode: {state['auth_mode']}")
    print_info(f"  First admin setup URL: {state['setup_url']}")
    print_info(f"  OIDC client: {state['oidc_client_id']}")


def _run_model_section() -> None:
    existing_route = dict(load_product_config().get("models", {}).get("default_route", {}))
    temp_config: dict[str, Any] = _seed_product_model_setup_config(load_product_config())
    with _isolated_product_setup_home() as temp_home:
        setup_model_provider(temp_config)
        disk_config = yaml.safe_load((temp_home / "config.yaml").read_text(encoding="utf-8")) or {}
        if isinstance(disk_config, dict) and isinstance(disk_config.get("model"), (dict, str)):
            merged_config = dict(disk_config)
            if isinstance(disk_config.get("model"), dict) and isinstance(temp_config.get("model"), dict):
                merged_model = dict(disk_config["model"])
                merged_model.update(temp_config["model"])
                merged_config["model"] = merged_model
            elif isinstance(temp_config.get("model"), (dict, str)):
                merged_config["model"] = temp_config["model"]
            temp_config = merged_config
        elif not isinstance(temp_config.get("model"), (dict, str)):
            temp_config.update(disk_config if isinstance(disk_config, dict) else {})
    try:
        _sync_model_route_from_temp_config(temp_config)
    except RuntimeError as exc:
        if "valid model configuration" not in str(exc):
            raise
        provider = str(existing_route.get("provider", "")).strip()
        model_name = str(existing_route.get("model", "")).strip()
        if not provider or not model_name:
            raise
        print_info("  Kept current product model route.")


def _run_tools_section() -> None:
    temp_config: dict[str, Any] = {}
    product_config = load_product_config()
    selected_toolsets = product_config.get("tools", {}).get("hermes_toolsets", [])
    normalized = [str(toolset).strip() for toolset in selected_toolsets if str(toolset).strip()]
    temp_config.setdefault("platform_toolsets", {})["cli"] = normalized or list(DEFAULT_PRODUCT_TOOLSETS)
    setup_tools(temp_config, first_install=False)
    _sync_toolsets_from_temp_config(temp_config)


def _run_bootstrap_section() -> None:
    try:
        validate_product_host_prereqs()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    initialize_product_stack()
    _start_product_stack()


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
        elif section == "model":
            _run_model_section()
        elif section == "tools":
            _run_tools_section()
        elif section == "bootstrap":
            _run_bootstrap_section()
        else:
            print_error(f"Unknown product setup section: {section}")
            print_info(f"Available sections: {', '.join(key for key, _ in PRODUCT_SETUP_SECTIONS)}")
            return
        _print_product_setup_summary()
        return

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
    print_info("It leaves the generic 'hermes setup' flow untouched.")
    print()

    setup_product_network()
    setup_product_tailscale()
    setup_product_identity()
    setup_product_storage()
    _run_model_section()
    _run_tools_section()
    _run_bootstrap_section()
    _print_product_setup_summary()
    print()
    print_success("Product setup complete!")
