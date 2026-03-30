from __future__ import annotations

import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from hermes_cli.config import get_hermes_home
from hermes_cli.product_config import ensure_product_home, get_product_storage_root
from utils import atomic_json_write


def product_app_service_path(service_name: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / service_name


def product_install_root(default_install_dir_name: str) -> Path:
    configured = str(os.environ.get("HERMES_CORE_INSTALL_DIR", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (get_hermes_home() / default_install_dir_name).resolve()


def product_runtime_dockerfile(default_install_dir_name: str) -> Path:
    return product_install_root(default_install_dir_name) / "Dockerfile.product"


def service_bind_host_and_home(load_product_config_fn: Any, default_install_dir_name: str, config: dict[str, Any] | None = None) -> tuple[str, str, str]:
    product_config = config or load_product_config_fn()
    bind_host = str(product_config.get("network", {}).get("bind_host", "0.0.0.0")).strip() or "0.0.0.0"
    return bind_host, str(get_hermes_home()), str(product_install_root(default_install_dir_name))


def render_product_service_unit(
    product_service_identity_fn: Any,
    spec: Any,
    *,
    bind_host: str,
    hermes_home: str,
    install_root: str,
) -> str:
    _run_as_user, home_dir = product_service_identity_fn()
    return "\n".join(
        [
            "[Unit]",
            f"Description={spec.description}",
            "After=network-online.target docker.service",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={home_dir}",
            f"Environment=HOME={home_dir}",
            f"Environment=HERMES_HOME={hermes_home}",
            f"Environment=HERMES_CORE_INSTALL_DIR={install_root}",
            (
                "ExecStart="
                "/usr/bin/sg docker -c "
                f"'{sys.executable} -m uvicorn {spec.module}:{spec.factory} "
                f"--factory --host {bind_host} --port {spec.port}'"
            ),
            "Restart=always",
            "RestartSec=3",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def render_product_app_service_unit(
    *,
    load_product_config_fn: Any,
    service_bind_host_and_home_fn: Any,
    render_product_service_unit_fn: Any,
    product_service_unit_spec_cls: Any,
    default_install_dir_name: str,
    config: dict[str, Any] | None = None,
) -> str:
    product_config = config or load_product_config_fn()
    bind_host, hermes_home, install_root = service_bind_host_and_home_fn(load_product_config_fn, default_install_dir_name, product_config)
    spec = product_service_unit_spec_cls(
        description="Hermes Core Product App",
        module="hermes_cli.product_app",
        factory="create_product_app",
        port=int(product_config.get("network", {}).get("app_port", 8086)),
    )
    return render_product_service_unit_fn(spec, bind_host=bind_host, hermes_home=hermes_home, install_root=install_root)


def write_product_app_service_unit(
    *,
    product_app_service_path_fn: Any,
    render_product_app_service_unit_fn: Any,
    service_name: str,
    config: dict[str, Any] | None = None,
) -> None:
    service_path = product_app_service_path_fn(service_name)
    service_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_product_app_service_unit_fn(config)
    service_path.write_text(rendered, encoding="utf-8")


def ensure_product_app_service_started(
    *,
    is_linux_fn: Any,
    systemd_available_fn: Any,
    write_product_app_service_unit_fn: Any,
    run_fn: Any,
    service_name: str,
    config: dict[str, Any] | None = None,
) -> None:
    if not is_linux_fn():
        return
    if not systemd_available_fn():
        raise RuntimeError("systemd is required to manage the Hermes Core product app service")
    write_product_app_service_unit_fn(config)
    run_fn(["systemctl", "--user", "daemon-reload"])
    run_fn(["systemctl", "--user", "enable", service_name])
    active = run_fn(["systemctl", "--user", "is-active", service_name], check=False)
    action = "restart" if active.returncode == 0 else "start"
    run_fn(["systemctl", "--user", action, service_name])


def get_product_install_state_path() -> Path:
    return get_product_storage_root() / "install_state.json"


def load_product_install_state() -> dict[str, Any]:
    path = get_product_install_state_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_product_install_state(state: dict[str, Any]) -> None:
    ensure_product_home()
    atomic_json_write(get_product_install_state_path(), state)


def product_install_state() -> dict[str, Any]:
    state = load_product_install_state()
    if not isinstance(state, dict):
        return {}
    return state


def product_build_context_ignored(patterns: tuple[str, ...], relative_path: PurePosixPath, *, is_dir: bool) -> bool:
    from fnmatch import fnmatch

    relative = relative_path.as_posix()
    if not relative or relative == ".":
        return False
    for pattern in patterns:
        if fnmatch(relative, pattern):
            return True
        if is_dir and fnmatch(f"{relative}/", f"{pattern.rstrip('/')}/"):
            return True
    return False
