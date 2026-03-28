#!/usr/bin/env python3
"""Product CLI entrypoint for the hermes-core distribution."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli.config import get_hermes_home
from hermes_cli.env_loader import load_hermes_dotenv

load_hermes_dotenv(project_env=PROJECT_ROOT / ".env")

os.environ.setdefault("MSWEA_GLOBAL_CONFIG_DIR", str(get_hermes_home()))
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")


def cmd_setup(args: argparse.Namespace) -> None:
    from hermes_cli.product_setup import run_product_setup_wizard

    run_product_setup_wizard(args)


def cmd_install(args: argparse.Namespace) -> None:
    from hermes_cli.product_install import run_product_install

    run_product_install(args)


def cmd_uninstall(args: argparse.Namespace) -> None:
    from hermes_cli.product_install import run_product_uninstall

    run_product_uninstall(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-core",
        description="Hermes Core product CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "    hermes-core install\n"
            "    hermes-core setup\n"
            "    hermes-core setup bootstrap\n"
            "    hermes setup model\n"
            "    hermes-core uninstall --yes\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    setup_parser = subparsers.add_parser(
        "setup",
        help="Interactive setup wizard for the hermes-core product layer",
        description="Configure the Tailnet-only Hermes Core product layer backed by tsidp",
    )
    setup_parser.add_argument(
        "section",
        nargs="?",
        choices=["tailscale", "identity", "storage", "bootstrap"],
        default=None,
        help="Run a specific product setup section instead of the full product wizard",
    )
    setup_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Non-interactive mode (prints guidance instead of running prompts)",
    )
    setup_parser.set_defaults(func=cmd_setup)

    install_parser = subparsers.add_parser(
        "install",
        help="Prepare a Linux host for the hermes-core product and run product setup",
        description="Configure Linux host prerequisites such as Docker runsc registration, then run product setup",
    )
    install_parser.add_argument(
        "--skip-setup",
        action="store_true",
        help="Prepare the Linux host only and skip the interactive product setup wizard",
    )
    install_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Pass non-interactive mode through to product setup when it runs",
    )
    install_parser.set_defaults(func=cmd_install)

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Remove hermes-core product traces from this machine",
        description="Stop product services, remove product data, and optionally revert installer-managed host runtime registration",
    )
    uninstall_parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not prompt for confirmation",
    )
    uninstall_parser.set_defaults(func=cmd_uninstall)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        raise SystemExit(1)
    try:
        args.func(args)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
