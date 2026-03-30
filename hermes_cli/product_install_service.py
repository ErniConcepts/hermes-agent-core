from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any


def product_app_service_path(hooks: Any) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / hooks.PRODUCT_APP_SERVICE_NAME


def product_install_root(hooks: Any) -> Path:
    configured = str(os.environ.get("HERMES_CORE_INSTALL_DIR", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (hooks.get_hermes_home() / hooks.DEFAULT_INSTALL_DIR_NAME).resolve()


def product_runtime_dockerfile(hooks: Any) -> Path:
    return hooks.product_install_root() / "Dockerfile.product"


def service_bind_host_and_home(hooks: Any, config: dict[str, Any] | None = None) -> tuple[str, str, str]:
    product_config = config or hooks.load_product_config()
    bind_host = str(product_config.get("network", {}).get("bind_host", "0.0.0.0")).strip() or "0.0.0.0"
    return bind_host, str(hooks.get_hermes_home()), str(hooks.product_install_root())


def render_product_service_unit(
    hooks: Any,
    spec: Any,
    *,
    bind_host: str,
    hermes_home: str,
    install_root: str,
) -> str:
    _run_as_user, home_dir = hooks._product_service_identity()
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
                f"'{hooks.sys.executable} -m uvicorn {spec.module}:{spec.factory} "
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


def render_product_app_service_unit(hooks: Any, config: dict[str, Any] | None = None) -> str:
    product_config = config or hooks.load_product_config()
    bind_host, hermes_home, install_root = hooks._service_bind_host_and_home(product_config)
    spec = hooks.ProductServiceUnitSpec(
        description="Hermes Core Product App",
        module="hermes_cli.product_app",
        factory="create_product_app",
        port=int(product_config.get("network", {}).get("app_port", 8086)),
    )
    return hooks._render_product_service_unit(spec, bind_host=bind_host, hermes_home=hermes_home, install_root=install_root)


def write_product_app_service_unit(hooks: Any, config: dict[str, Any] | None = None) -> None:
    service_path = hooks._product_app_service_path()
    service_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = hooks._render_product_app_service_unit(config)
    service_path.write_text(rendered, encoding="utf-8")


def ensure_product_app_service_started(hooks: Any, config: dict[str, Any] | None = None) -> None:
    if not hooks._is_linux():
        return
    if not hooks._systemd_available():
        raise RuntimeError("systemd is required to manage the Hermes Core product app service")
    hooks._write_product_app_service_unit(config)
    hooks._run(["systemctl", "--user", "daemon-reload"])
    hooks._run(["systemctl", "--user", "enable", hooks.PRODUCT_APP_SERVICE_NAME])
    active = hooks._run(["systemctl", "--user", "is-active", hooks.PRODUCT_APP_SERVICE_NAME], check=False)
    action = "restart" if active.returncode == 0 else "start"
    hooks._run(["systemctl", "--user", action, hooks.PRODUCT_APP_SERVICE_NAME])


def get_product_install_state_path(hooks: Any) -> Path:
    return hooks.get_product_storage_root() / "install_state.json"


def load_product_install_state(hooks: Any) -> dict[str, Any]:
    path = hooks.get_product_install_state_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_product_install_state(hooks: Any, state: dict[str, Any]) -> None:
    hooks.ensure_product_home()
    hooks.atomic_json_write(hooks.get_product_install_state_path(), state)


def product_install_state(hooks: Any) -> dict[str, Any]:
    state = hooks.load_product_install_state()
    if not isinstance(state, dict):
        return {}
    return state


def product_build_context_ignored(hooks: Any, relative_path: PurePosixPath, *, is_dir: bool) -> bool:
    relative = relative_path.as_posix()
    if not relative or relative == ".":
        return False
    for pattern in hooks.PRODUCT_DOCKER_BUILD_IGNORE_PATTERNS:
        if hooks.fnmatch(relative, pattern):
            return True
        if is_dir and hooks.fnmatch(f"{relative}/", f"{pattern.rstrip('/')}/"):
            return True
    return False

