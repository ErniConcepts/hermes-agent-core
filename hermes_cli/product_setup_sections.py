from __future__ import annotations

import re
from pathlib import Path

from hermes_cli.product_config import load_product_config, save_product_config
from hermes_cli.setup import print_header, print_info, print_warning, prompt

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_prompt_text(value: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", value or "")
    cleaned = _CONTROL_CHAR_RE.sub("", cleaned)
    return cleaned.strip()


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
    bootstrap.setdefault("first_admin_display_name", "Administrator")
    save_product_config(product_config)
    print_header("Tailnet Auth")
    print_info("Setup will create a one-time bootstrap link for the first admin.")
    print_info("Open that link, sign in with Tailscale, and the first authenticated account becomes admin.")
