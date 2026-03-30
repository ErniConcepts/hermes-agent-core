from __future__ import annotations

from pathlib import Path

from hermes_cli.config import _secure_dir
from hermes_cli.product_config import get_product_storage_root


def get_product_services_root() -> Path:
    return get_product_storage_root() / "services"


def get_tsidp_service_root() -> Path:
    return get_product_services_root() / "tsidp"


def get_tsidp_data_root() -> Path:
    return get_tsidp_service_root() / "data"


def get_product_bootstrap_root() -> Path:
    return get_product_storage_root() / "bootstrap"


def get_first_admin_enrollment_state_path() -> Path:
    return get_product_bootstrap_root() / "first_admin_enrollment.json"


def get_tsidp_compose_path() -> Path:
    return get_tsidp_service_root() / "compose.yaml"


def get_tsidp_env_path() -> Path:
    return get_tsidp_service_root() / ".env"


def secure_tree(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        _secure_dir(path)


def permission_error_message(path: Path) -> str:
    return (
        f"Permission denied while writing {path}. "
        "This usually means files in ~/.hermes/product are owned by root from a previous sudo run. "
        "Fix ownership and rerun install: "
        "sudo chown -R \"$USER:$USER\" ~/.hermes/product ~/.hermes/product.yaml ~/.hermes/.env"
    )
