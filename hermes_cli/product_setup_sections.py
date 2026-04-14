from __future__ import annotations

import re
from pathlib import Path

from hermes_cli.product_config import (
    load_product_config,
    runtime_backend_policy,
    save_product_config,
)
from hermes_cli.product_stack import first_admin_bootstrap_completed, load_first_admin_enrollment_state
from hermes_cli.setup import print_header, print_info, print_warning, prompt, prompt_choice

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
def _sanitize_prompt_text(value: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", value or "")
    cleaned = _CONTROL_CHAR_RE.sub("", cleaned)
    return cleaned.strip()


def setup_product_branding() -> None:
    product_config = load_product_config()
    current_name = str(product_config.get("product", {}).get("brand", {}).get("name", "Hermes Core")).strip() or "Hermes Core"
    print_header("Branding")
    print_info("Choose the product name shown in the web UI.")
    print_info("Typography and colors stay the same; only the lettering changes.")
    raw_value = _sanitize_prompt_text(prompt("Product title", current_name) or current_name)
    product_name = raw_value or "Hermes Core"
    product_config.setdefault("product", {}).setdefault("brand", {})["name"] = product_name
    save_product_config(product_config)
    print_info(f"  Product title: {product_name}")


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


def setup_product_runtime_backend() -> None:
    product_config = load_product_config()
    current_policy = runtime_backend_policy(product_config)
    print_header("Local Model Backend")
    print_info("Choose how product runtimes should handle local/custom model endpoints.")
    print_info("Use the managed backend only when the selected model endpoint exposes a compatible tool-calling API.")
    choices = [
        "Managed backend for local models (recommended)",
        "Standard backend always",
        "Managed backend always",
    ]
    default_idx = {
        "auto_local_managed": 0,
        "standard": 1,
        "managed": 2,
    }.get(current_policy, 0)
    selected = prompt_choice("Runtime backend policy:", choices, default_idx)
    backend_policy = {
        0: "auto_local_managed",
        1: "standard",
        2: "managed",
    }[selected]
    product_config.setdefault("runtime", {})["backend_policy"] = backend_policy
    save_product_config(product_config)
    print_info(f"  Runtime backend policy: {backend_policy}")


def setup_product_bootstrap_identity() -> bool:
    product_config = load_product_config()
    bootstrap = product_config.setdefault("bootstrap", {})
    bootstrap.setdefault("first_admin_display_name", "Administrator")
    save_product_config(product_config)
    enrollment_state = load_first_admin_enrollment_state() or {}
    print_header("Tailnet Auth Status")
    if first_admin_bootstrap_completed(enrollment_state):
        print_info("First admin bootstrap is already completed on this install.")
        claimed_login = str(enrollment_state.get("tailscale_login", "")).strip()
        if claimed_login:
            print_info(f"Current first admin account: {claimed_login}")
        choice = prompt_choice(
            "Choose how to continue:",
            ["Keep existing admin", "Create new bootstrap link"],
            0,
        )
        if choice == 0:
            print_info("Setup will keep the existing first admin and refresh the current auth configuration.")
            return False
        print_info("Setup will create a new one-time bootstrap link.")
        print_info("Open that link, sign in with Tailscale, and the next authenticated account becomes admin.")
        return True

    if enrollment_state and bool(enrollment_state.get("first_admin_login_seen", False)):
        print_warning("Saved bootstrap state did not match an existing admin user. Setup will repair it by generating a new bootstrap link.")
        return True
    print_info("Setup will create a one-time bootstrap link for the first admin.")
    print_info("Open that link, sign in with Tailscale, and the first authenticated account becomes admin.")
    return True
