from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from hermes_cli.config import get_env_path
from hermes_cli.product_config import ensure_product_home, get_product_config_path, get_product_storage_root
from hermes_cli.product_stack import (
    get_pocket_id_compose_path,
    get_product_services_root,
    load_product_config,
)
from utils import atomic_json_write


DOCKER_DAEMON_CONFIG_PATH = Path("/etc/docker/daemon.json")
RUNSC_RUNTIME_NAME = "runsc"
RUNSC_RUNTIME_CONFIG = {
    "path": "runsc",
    "runtimeArgs": ["--network=host"],
}
PRODUCT_SECRET_KEYS = [
    "HERMES_PRODUCT_OIDC_CLIENT_SECRET",
    "HERMES_POCKET_ID_STATIC_API_KEY",
    "HERMES_POCKET_ID_ENCRYPTION_KEY",
]
PRODUCT_APP_SERVICE_NAME = "hermes-core-product-app.service"
PRODUCT_APP_SERVICE_PATH = Path("/etc/systemd/system") / PRODUCT_APP_SERVICE_NAME


def _is_linux() -> bool:
    return os.name != "nt" and os.uname().sysname.lower() == "linux"


def _run(
    command: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    sudo: bool = False,
) -> subprocess.CompletedProcess[str]:
    if sudo and _is_linux() and getattr(os, "geteuid", lambda: 1)() != 0:
        command = ["sudo", *command]
    return subprocess.run(
        command,
        check=check,
        capture_output=capture_output,
        text=True,
    )


def _project_root() -> Path:
    return Path(__file__).parent.parent.resolve()


def _systemd_available() -> bool:
    return _run(["systemctl", "--version"], check=False).returncode == 0


def _product_service_identity() -> tuple[str, str]:
    import pwd

    if not _is_linux():
        raise RuntimeError("Product app service management is only supported on Linux")
    if getattr(os, "geteuid", lambda: 1)() == 0:
        sudo_user = str(os.environ.get("SUDO_USER", "")).strip()
        if not sudo_user or sudo_user == "root":
            raise RuntimeError("Could not determine the non-root user for the product app service")
        account = pwd.getpwnam(sudo_user)
        return account.pw_name, account.pw_dir
    account = pwd.getpwuid(os.geteuid())
    if account.pw_name == "root":
        raise RuntimeError("Refusing to install the product app service as root")
    return account.pw_name, account.pw_dir


def _render_product_app_service_unit(config: dict[str, Any] | None = None) -> str:
    product_config = config or load_product_config()
    app_port = int(product_config.get("network", {}).get("app_port", 8086))
    run_as_user, home_dir = _product_service_identity()
    hermes_home = str(PurePosixPath(home_dir) / ".hermes")
    return "\n".join(
        [
            "[Unit]",
            "Description=Hermes Core Product App",
            "After=network-online.target docker.service",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"User={run_as_user}",
            f"WorkingDirectory={_project_root()}",
            f"Environment=HOME={home_dir}",
            f"Environment=HERMES_HOME={hermes_home}",
            (
                "ExecStart="
                f"{sys.executable} -m uvicorn hermes_cli.product_app:create_product_app "
                f"--factory --host 127.0.0.1 --port {app_port}"
            ),
            "Restart=always",
            "RestartSec=3",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def _write_product_app_service_unit(config: dict[str, Any] | None = None) -> None:
    rendered = _render_product_app_service_unit(config)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(rendered)
        temp_path = Path(handle.name)
    try:
        if getattr(os, "geteuid", lambda: 1)() == 0:
            shutil.copyfile(temp_path, PRODUCT_APP_SERVICE_PATH)
        else:
            _run(["cp", str(temp_path), str(PRODUCT_APP_SERVICE_PATH)], sudo=True)
    finally:
        temp_path.unlink(missing_ok=True)


def ensure_product_app_service_started(config: dict[str, Any] | None = None) -> None:
    if not _is_linux():
        return
    if not _systemd_available():
        raise RuntimeError("systemd is required to manage the Hermes Core product app service")
    _write_product_app_service_unit(config)
    _run(["systemctl", "daemon-reload"], sudo=True)
    _run(["systemctl", "enable", PRODUCT_APP_SERVICE_NAME], sudo=True)
    active = _run(["systemctl", "is-active", PRODUCT_APP_SERVICE_NAME], check=False, sudo=True)
    action = "restart" if active.returncode == 0 else "start"
    _run(["systemctl", action, PRODUCT_APP_SERVICE_NAME], sudo=True)


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


def _product_install_state() -> dict[str, Any]:
    state = load_product_install_state()
    if not isinstance(state, dict):
        return {}
    return state


def _docker_compose_available() -> bool:
    result = _run(["docker", "compose", "version"], check=False)
    return result.returncode == 0


def _docker_available() -> bool:
    result = _run(["docker", "info"], check=False)
    return result.returncode == 0


def _runsc_available() -> bool:
    result = _run(["runsc", "--version"], check=False)
    return result.returncode == 0


def _docker_runtimes() -> dict[str, Any]:
    result = _run(["docker", "info", "--format", "{{json .Runtimes}}"], check=False)
    if result.returncode != 0:
        return {}
    try:
        return json.loads((result.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}


def _runsc_registered() -> bool:
    return RUNSC_RUNTIME_NAME in _docker_runtimes()


def _load_docker_daemon_config() -> tuple[dict[str, Any], bool]:
    if DOCKER_DAEMON_CONFIG_PATH.exists():
        try:
            return json.loads(DOCKER_DAEMON_CONFIG_PATH.read_text(encoding="utf-8")), True
        except PermissionError:
            result = _run(["cat", str(DOCKER_DAEMON_CONFIG_PATH)], sudo=True)
            return json.loads(result.stdout), True
    return {}, False


def _write_docker_daemon_config(config: dict[str, Any]) -> None:
    rendered = json.dumps(config, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(rendered)
        temp_path = Path(handle.name)
    try:
        if getattr(os, "geteuid", lambda: 1)() == 0:
            shutil.copyfile(temp_path, DOCKER_DAEMON_CONFIG_PATH)
        else:
            _run(["cp", str(temp_path), str(DOCKER_DAEMON_CONFIG_PATH)], sudo=True)
    finally:
        temp_path.unlink(missing_ok=True)


def _restart_docker_service() -> None:
    _run(["systemctl", "restart", "docker"], sudo=True)


def ensure_runsc_registered_with_docker() -> bool:
    config, _exists = _load_docker_daemon_config()
    runtimes = config.setdefault("runtimes", {})
    existing = runtimes.get(RUNSC_RUNTIME_NAME)
    if existing == RUNSC_RUNTIME_CONFIG and _runsc_registered():
        return False
    runtimes[RUNSC_RUNTIME_NAME] = dict(RUNSC_RUNTIME_CONFIG)
    _write_docker_daemon_config(config)
    _restart_docker_service()
    if not _runsc_registered():
        raise RuntimeError("Docker still does not report the runsc runtime after configuration")
    return True


def validate_product_host_prereqs() -> None:
    if not _is_linux():
        raise RuntimeError("hermes-core product host prerequisites are only supported on Linux")
    if not _docker_available():
        raise RuntimeError("Docker is not available or the daemon is not running")
    if not _docker_compose_available():
        raise RuntimeError("docker compose is not available")
    if not _runsc_available():
        raise RuntimeError("runsc is not installed on this machine")
    if not _runsc_registered():
        raise RuntimeError("Docker does not have the runsc runtime registered")


def _remove_env_keys(keys: list[str]) -> None:
    env_path = get_env_path()
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    filtered: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            filtered.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in keys:
            continue
        filtered.append(line)
    rendered = "\n".join(filtered).rstrip() + "\n" if filtered else ""
    env_path.write_text(rendered, encoding="utf-8")


def _remove_runtime_containers() -> None:
    result = _run(
        ["docker", "ps", "-aq", "--filter", "label=ch.hermes.product.role=runtime"],
        check=False,
    )
    container_ids = [item.strip() for item in (result.stdout or "").splitlines() if item.strip()]
    for container_id in container_ids:
        _run(["docker", "rm", "-f", container_id], check=False)


def _remove_pocket_id_stack() -> None:
    compose_path = get_pocket_id_compose_path()
    if compose_path.exists():
        _run(
            ["docker", "compose", "-f", str(compose_path), "down", "-v", "--remove-orphans"],
            check=False,
        )
    _run(["docker", "rm", "-f", "hermes-pocket-id"], check=False)


def _remove_runsc_registration_if_managed() -> bool:
    state = _product_install_state()
    if not state.get("managed_runsc_registration"):
        return False
    if not _is_linux():
        return False
    config, exists = _load_docker_daemon_config()
    if not exists:
        return False
    runtimes = config.get("runtimes", {})
    if runtimes.get(RUNSC_RUNTIME_NAME) != RUNSC_RUNTIME_CONFIG:
        return False
    runtimes.pop(RUNSC_RUNTIME_NAME, None)
    if not runtimes:
        config.pop("runtimes", None)
    _write_docker_daemon_config(config)
    _restart_docker_service()
    state["managed_runsc_registration"] = False
    save_product_install_state(state)
    return True


def perform_product_cleanup() -> dict[str, bool]:
    removed_runsc_registration = False
    if _docker_available():
        _remove_pocket_id_stack()
        _remove_runtime_containers()
    if _is_linux() and _systemd_available():
        _run(["systemctl", "disable", "--now", PRODUCT_APP_SERVICE_NAME], check=False, sudo=True)
        if PRODUCT_APP_SERVICE_PATH.exists():
            if getattr(os, "geteuid", lambda: 1)() == 0:
                PRODUCT_APP_SERVICE_PATH.unlink(missing_ok=True)
            else:
                _run(["rm", "-f", str(PRODUCT_APP_SERVICE_PATH)], check=False, sudo=True)
        _run(["systemctl", "daemon-reload"], check=False, sudo=True)
    removed_runsc_registration = _remove_runsc_registration_if_managed()
    shutil.rmtree(get_product_services_root(), ignore_errors=True)
    shutil.rmtree(get_product_storage_root(), ignore_errors=True)
    get_product_config_path().unlink(missing_ok=True)
    _remove_env_keys(PRODUCT_SECRET_KEYS)
    return {
        "removed_runsc_registration": removed_runsc_registration,
    }


def run_product_install(args: Any) -> None:
    from hermes_cli.product_setup import run_product_setup_wizard

    if not _is_linux():
        raise SystemExit("hermes-core install currently supports Linux host setup only")
    if not _docker_available():
        raise SystemExit("Docker is not available or the daemon is not running")
    if not _docker_compose_available():
        raise SystemExit("docker compose is not available")
    if not _runsc_available():
        raise SystemExit("runsc is not installed; install it before running hermes-core install")

    changed = ensure_runsc_registered_with_docker()
    state = _product_install_state()
    state["managed_runsc_registration"] = bool(changed or state.get("managed_runsc_registration"))
    save_product_install_state(state)
    ensure_product_app_service_started(load_product_config())

    if getattr(args, "skip_setup", False):
        return

    validate_product_host_prereqs()
    run_product_setup_wizard(args)


def run_product_uninstall(args: Any) -> None:
    confirmed = bool(getattr(args, "yes", False))
    if not confirmed:
        try:
            answer = input("Remove Hermes Core product traces from this machine? Type 'yes' to confirm: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if answer != "yes":
            print("Product uninstall cancelled.")
            return
    result = perform_product_cleanup()
    print("Removed Hermes Core product data and services.")
    if result["removed_runsc_registration"]:
        print("Removed installer-managed Docker runsc registration.")
