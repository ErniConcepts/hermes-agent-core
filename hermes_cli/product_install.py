from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
import subprocess
import sys
import tempfile
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import grp
except ModuleNotFoundError:  # pragma: no cover - Windows
    grp = None

from hermes_cli.config import get_env_path, get_hermes_home
from hermes_cli.product_config import (
    ensure_product_home,
    get_product_config_path,
    get_product_storage_root,
)
from hermes_cli.product_stack import (
    get_pocket_id_compose_path,
    get_product_services_root,
    load_product_config,
)
from utils import atomic_json_write


DEFAULT_INSTALL_DIR_NAME = "hermes-core"
DOCKER_DAEMON_CONFIG_PATH = Path("/etc/docker/daemon.json")
RUNSC_RUNTIME_NAME = "runsc"
RUNSC_RUNTIME_CONFIG = {
    "path": "runsc",
    "runtimeArgs": ["--network=host"],
}
PRODUCT_SECRET_KEYS = [
    "HERMES_PRODUCT_OIDC_CLIENT_SECRET",
    "HERMES_PRODUCT_SESSION_SECRET",
    "HERMES_POCKET_ID_STATIC_API_KEY",
    "HERMES_POCKET_ID_ENCRYPTION_KEY",
]
PRODUCT_APP_SERVICE_NAME = "hermes-core-product-app.service"
PRODUCT_AUTH_PROXY_SERVICE_NAME = "hermes-core-product-auth-proxy.service"
PRODUCT_RUNTIME_IMAGE_TAG = "hermes-core-product-runtime:local"
DOCKER_GROUP_RELOGIN_EXIT_CODE = 42
APT_INSTALL_PACKAGES = [
    "docker.io",
    "runsc",
]
APT_DOCKER_COMPOSE_PACKAGE_CANDIDATES = [
    "docker-compose-v2",
    "docker-compose-plugin",
    "docker-compose",
]
PRODUCT_DOCKER_BUILD_IGNORE_PATTERNS = (
    ".git",
    ".git/**",
    ".venv",
    ".venv/**",
    ".pytest-*",
    ".pytest-*/**",
    ".tmp-*",
    ".tmp-*/**",
    ".tmp-pytest",
    ".tmp-pytest/**",
    "artifacts",
    "artifacts/**",
    ".noncode_files.txt",
    "Dockerfile.product-local",
)
@dataclass(frozen=True)
class ProductServiceUnitSpec:
    description: str
    module: str
    factory: str
    port: int


def _is_linux() -> bool:
    return os.name != "nt" and os.uname().sysname.lower() == "linux"


def _run(
    command: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    sudo: bool = False,
) -> subprocess.CompletedProcess[str]:
    using_sudo = False
    if sudo and _is_linux() and getattr(os, "geteuid", lambda: 1)() != 0:
        command = ["sudo", *command]
        using_sudo = True
    try:
        return subprocess.run(
            command,
            check=check,
            capture_output=capture_output,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip().lower()
        if using_sudo and (
            "a password is required" in stderr
            or "no tty present" in stderr
            or "terminal is required" in stderr
        ):
            raise RuntimeError(
                "Host prerequisite installation needs sudo in an interactive local shell. "
                "Rerun 'hermes-core install' directly on the Linux device and enter your sudo password when prompted."
            ) from exc
        raise


def _product_app_service_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / PRODUCT_APP_SERVICE_NAME


def _product_auth_proxy_service_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / PRODUCT_AUTH_PROXY_SERVICE_NAME


def product_install_root() -> Path:
    configured = str(os.environ.get("HERMES_CORE_INSTALL_DIR", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (get_hermes_home() / DEFAULT_INSTALL_DIR_NAME).resolve()


def product_runtime_dockerfile() -> Path:
    return product_install_root() / "Dockerfile.product"


def _linux_distro_id() -> str:
    if not _is_linux():
        return ""
    os_release = Path("/etc/os-release")
    if not os_release.exists():
        return ""
    values: dict[str, str] = {}
    for line in os_release.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key.strip().lower()] = raw.strip().strip('"').lower()
    return values.get("id", "")


def _apt_supported_linux() -> bool:
    return _linux_distro_id() in {"ubuntu", "debian"}


def _systemd_available() -> bool:
    if shutil.which("systemctl") is None:
        return False
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


def _current_user_name() -> str:
    import pwd

    if getattr(os, "geteuid", lambda: 1)() == 0:
        sudo_user = str(os.environ.get("SUDO_USER", "")).strip()
        if sudo_user and sudo_user != "root":
            return sudo_user
    return pwd.getpwuid(os.getuid()).pw_name


def _user_in_group(group_name: str, user_name: str | None = None) -> bool:
    if grp is None:
        return False
    name = user_name or _current_user_name()
    try:
        group_info = grp.getgrnam(group_name)
    except KeyError:
        return False
    if name in group_info.gr_mem:
        return True
    try:
        import pwd

        primary_gid = pwd.getpwnam(name).pw_gid
    except KeyError:
        return False
    return primary_gid == group_info.gr_gid


def _service_bind_host_and_home(config: dict[str, Any] | None = None) -> tuple[str, str, str]:
    product_config = config or load_product_config()
    bind_host = str(product_config.get("network", {}).get("bind_host", "0.0.0.0")).strip() or "0.0.0.0"
    return bind_host, str(get_hermes_home()), str(product_install_root())


def _render_product_service_unit(spec: ProductServiceUnitSpec, *, bind_host: str, hermes_home: str, install_root: str) -> str:
    _run_as_user, home_dir = _product_service_identity()
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


def _render_product_app_service_unit(config: dict[str, Any] | None = None) -> str:
    product_config = config or load_product_config()
    bind_host, hermes_home, install_root = _service_bind_host_and_home(product_config)
    spec = ProductServiceUnitSpec(
        description="Hermes Core Product App",
        module="hermes_cli.product_app",
        factory="create_product_app",
        port=int(product_config.get("network", {}).get("app_port", 8086)),
    )
    return _render_product_service_unit(spec, bind_host=bind_host, hermes_home=hermes_home, install_root=install_root)


def _render_product_auth_proxy_service_unit(config: dict[str, Any] | None = None) -> str:
    product_config = config or load_product_config()
    bind_host, hermes_home, install_root = _service_bind_host_and_home(product_config)
    spec = ProductServiceUnitSpec(
        description="Hermes Core Product Auth Proxy",
        module="hermes_cli.product_app",
        factory="create_product_auth_proxy_app",
        port=int(product_config.get("network", {}).get("pocket_id_port", 1411)),
    )
    return _render_product_service_unit(spec, bind_host=bind_host, hermes_home=hermes_home, install_root=install_root)


def _write_product_app_service_unit(config: dict[str, Any] | None = None) -> None:
    service_path = _product_app_service_path()
    service_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _render_product_app_service_unit(config)
    service_path.write_text(rendered, encoding="utf-8")


def _write_product_auth_proxy_service_unit(config: dict[str, Any] | None = None) -> None:
    service_path = _product_auth_proxy_service_path()
    service_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _render_product_auth_proxy_service_unit(config)
    service_path.write_text(rendered, encoding="utf-8")


def ensure_product_app_service_started(config: dict[str, Any] | None = None) -> None:
    if not _is_linux():
        return
    if not _systemd_available():
        raise RuntimeError("systemd is required to manage the Hermes Core product app service")
    _write_product_app_service_unit(config)
    _write_product_auth_proxy_service_unit(config)
    _run(["systemctl", "--user", "daemon-reload"])
    for service_name in (PRODUCT_APP_SERVICE_NAME, PRODUCT_AUTH_PROXY_SERVICE_NAME):
        _run(["systemctl", "--user", "enable", service_name])
        active = _run(["systemctl", "--user", "is-active", service_name], check=False)
        action = "restart" if active.returncode == 0 else "start"
        _run(["systemctl", "--user", action, service_name])


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


def _product_build_context_ignored(relative_path: PurePosixPath, *, is_dir: bool) -> bool:
    relative = relative_path.as_posix()
    if not relative or relative == ".":
        return False
    for pattern in PRODUCT_DOCKER_BUILD_IGNORE_PATTERNS:
        if fnmatch(relative, pattern):
            return True
        if is_dir and fnmatch(f"{relative}/", f"{pattern.rstrip('/')}/"):
            return True
    return False


def _stage_product_build_context(source_root: Path, destination_root: Path) -> Path:
    destination_root.mkdir(parents=True, exist_ok=True)
    pending: list[tuple[Path, Path]] = [(source_root, destination_root)]
    while pending:
        current_source, current_destination = pending.pop()
        try:
            entries = list(os.scandir(current_source))
        except OSError:
            continue
        for entry in entries:
            source_path = Path(entry.path)
            try:
                relative = source_path.relative_to(source_root)
            except ValueError:
                continue
            relative_posix = PurePosixPath(relative.as_posix())
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if _product_build_context_ignored(relative_posix, is_dir=is_dir):
                continue
            destination_path = destination_root / relative
            if is_dir:
                destination_path.mkdir(parents=True, exist_ok=True)
                pending.append((source_path, destination_path))
                continue
            try:
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination_path)
            except OSError:
                continue
    return destination_root


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = _run(["docker", "compose", "version"], check=False)
    return result.returncode == 0


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = _run(["docker", "info"], check=False)
    return result.returncode == 0


def _docker_readiness_probe() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "Docker CLI is not installed."

    probe = _run(["docker", "info", "--format", "{{json .Runtimes}}"], check=False)
    if probe.returncode == 0:
        runtimes = _docker_runtimes()
        if not _runsc_runtime_matches(runtimes.get(RUNSC_RUNTIME_NAME)):
            return False, "Docker is reachable, but the runsc runtime is not registered."
        return True, ""

    detail = " ".join(part.strip() for part in (probe.stderr, probe.stdout) if part and part.strip()).strip()
    detail_lower = detail.lower()
    if any(token in detail_lower for token in ("permission denied", "got permission denied", "docker.sock")):
        return (
            False,
            "Docker is installed, but this user shell cannot access the Docker daemon yet. "
            "Run 'newgrp docker' or start a new login shell, verify with 'docker info', then rerun 'hermes-core install'.",
        )
    if _systemd_available():
        docker_state = (_run(["systemctl", "is-active", "docker"], check=False).stdout or "").strip().lower()
        socket_state = (_run(["systemctl", "is-active", "docker.socket"], check=False).stdout or "").strip().lower()
        failed_units: list[str] = []
        if docker_state == "failed":
            failed_units.append("docker")
        if socket_state == "failed":
            failed_units.append("docker.socket")
        if failed_units:
            joined = " and ".join(failed_units)
            return (
                False,
                f"Docker is installed, but {joined} is unhealthy. "
                "Run 'sudo systemctl reset-failed docker docker.socket', "
                "'sudo systemctl start docker.socket docker', verify with 'docker info', then rerun 'hermes-core install'.",
            )
    if detail:
        return False, f"Docker is not ready for Hermes Core install: {detail}"
    return False, "Docker is not ready for Hermes Core install. Verify with 'docker info' and rerun 'hermes-core install'."


def _runsc_available() -> bool:
    if shutil.which("runsc") is None:
        return False
    result = _run(["runsc", "--version"], check=False)
    return result.returncode == 0


def _docker_runtimes() -> dict[str, Any]:
    if shutil.which("docker") is None:
        return {}
    result = _run(["docker", "info", "--format", "{{json .Runtimes}}"], check=False)
    if result.returncode != 0:
        return {}
    try:
        return json.loads((result.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}


def _runsc_runtime_matches(config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    path_value = str(config.get("path", "")).strip()
    runtime_args = config.get("runtimeArgs")
    if not path_value or PurePosixPath(path_value).name != RUNSC_RUNTIME_NAME:
        return False
    return runtime_args == RUNSC_RUNTIME_CONFIG["runtimeArgs"]


def _runsc_registered() -> bool:
    return _runsc_runtime_matches(_docker_runtimes().get(RUNSC_RUNTIME_NAME))


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
    if not _systemd_available():
        return
    _run(["systemctl", "stop", "docker", "docker.socket"], check=False, sudo=True)
    _run(["systemctl", "reset-failed", "docker", "docker.socket"], check=False, sudo=True)
    _run(["systemctl", "start", "docker.socket"], sudo=True)
    _run(["systemctl", "start", "docker"], sudo=True)


def _start_and_enable_docker_service() -> None:
    if not _systemd_available():
        return
    _run(["systemctl", "enable", "--now", "docker.socket"], sudo=True)
    _run(["systemctl", "enable", "--now", "docker"], sudo=True)


def _docker_service_active() -> bool:
    if not _systemd_available():
        return False
    return _run(["systemctl", "is-active", "docker"], check=False).returncode == 0


def _apt_install(packages: list[str]) -> None:
    env_prefix = ["env", "DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=a"]
    _run([*env_prefix, "apt-get", "update", "-qq"], sudo=True)
    _run([*env_prefix, "apt-get", "install", "-y", *packages], sudo=True)


def _apt_package_available(package_name: str) -> bool:
    result = _run(["apt-cache", "policy", package_name], check=False)
    if result.returncode != 0:
        return False
    stdout = (result.stdout or "").strip().lower()
    if not stdout:
        return False
    return "candidate: (none)" not in stdout


def _linux_host_prereq_packages() -> list[str]:
    packages = list(APT_INSTALL_PACKAGES)
    for package_name in APT_DOCKER_COMPOSE_PACKAGE_CANDIDATES:
        if _apt_package_available(package_name):
            packages.append(package_name)
            return packages
    raise RuntimeError(
        "Could not find a Docker Compose package on this Linux host. "
        "Expected one of: docker-compose-v2, docker-compose-plugin, docker-compose."
    )


def ensure_linux_product_host_prereqs() -> dict[str, bool]:
    if not _is_linux():
        return {"installed_packages": False, "added_docker_group_membership": False}
    if not _apt_supported_linux():
        raise RuntimeError(
            "Automatic host prerequisite installation is currently supported only on Ubuntu/Debian"
        )

    installed_packages = False
    if not _docker_available() or not _docker_compose_available() or not _runsc_available():
        _apt_install(_linux_host_prereq_packages())
        installed_packages = True

    if installed_packages or not _docker_service_active():
        _start_and_enable_docker_service()

    added_docker_group_membership = False
    current_user = _current_user_name()
    if not _user_in_group("docker", current_user):
        _run(["usermod", "-aG", "docker", current_user], sudo=True)
        added_docker_group_membership = True

    return {
        "installed_packages": installed_packages,
        "added_docker_group_membership": added_docker_group_membership,
    }


def ensure_runsc_registered_with_docker() -> bool:
    config, _exists = _load_docker_daemon_config()
    runtimes = config.setdefault("runtimes", {})
    existing = runtimes.get(RUNSC_RUNTIME_NAME)
    if _runsc_runtime_matches(existing) and _runsc_registered():
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
    docker_ready, docker_message = _docker_readiness_probe()
    if not docker_ready:
        raise RuntimeError(docker_message)
    if not _docker_compose_available():
        raise RuntimeError("docker compose is not available")
    if not _runsc_available():
        raise RuntimeError("runsc is not installed on this machine")


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


def _remove_runtime_image() -> None:
    _run(["docker", "rmi", "-f", PRODUCT_RUNTIME_IMAGE_TAG], check=False)


def _remove_pocket_id_stack() -> None:
    compose_path = get_pocket_id_compose_path()
    if compose_path.exists():
        _run(
            ["docker", "compose", "-f", str(compose_path), "down", "-v", "--remove-orphans"],
            check=False,
        )
    _run(["docker", "rm", "-f", "hermes-pocket-id"], check=False)


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return
    except PermissionError:
        pass
    if not (_is_linux() and path.is_absolute()):
        raise
    command = ["rm", "-rf" if path.is_dir() and not path.is_symlink() else "-f", str(path)]
    _run(command, sudo=True)


def _remove_install_tree_and_launchers() -> None:
    for launcher_path in (
        Path.home() / ".local" / "bin" / "hermes",
        Path.home() / ".local" / "bin" / "hermes-core",
    ):
        _remove_path(launcher_path)
    _remove_path(product_install_root())


def _remove_product_user_services() -> None:
    if not (_is_linux() and _systemd_available()):
        return
    _run(["systemctl", "--user", "disable", "--now", PRODUCT_APP_SERVICE_NAME], check=False)
    _run(["systemctl", "--user", "disable", "--now", PRODUCT_AUTH_PROXY_SERVICE_NAME], check=False)
    service_path = _product_app_service_path()
    if service_path.exists():
        service_path.unlink(missing_ok=True)
    auth_proxy_path = _product_auth_proxy_service_path()
    if auth_proxy_path.exists():
        auth_proxy_path.unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"], check=False)


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
    if not _runsc_runtime_matches(runtimes.get(RUNSC_RUNTIME_NAME)):
        return False
    runtimes.pop(RUNSC_RUNTIME_NAME, None)
    if not runtimes:
        config.pop("runtimes", None)
    _write_docker_daemon_config(config)
    _restart_docker_service()
    state["managed_runsc_registration"] = False
    save_product_install_state(state)
    return True


def build_product_runtime_image() -> None:
    dockerfile_path = product_runtime_dockerfile()
    project_root = product_install_root()
    if not dockerfile_path.exists():
        raise RuntimeError(f"Product runtime Dockerfile not found: {dockerfile_path}")
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-product-build-") as build_root:
            staged_root = _stage_product_build_context(project_root, Path(build_root) / "context")
            staged_dockerfile = staged_root / dockerfile_path.name
            _run(
                [
                    "docker",
                    "build",
                    "-t",
                    PRODUCT_RUNTIME_IMAGE_TAG,
                    "-f",
                    str(staged_dockerfile),
                    str(staged_root),
                ],
                capture_output=False,
            )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Failed to build the local Hermes Core product runtime image") from exc


def perform_product_cleanup() -> dict[str, bool]:
    removed_runsc_registration = False
    if _docker_available():
        _remove_pocket_id_stack()
        _remove_runtime_containers()
        _remove_runtime_image()
    _remove_product_user_services()
    removed_runsc_registration = _remove_runsc_registration_if_managed()
    _remove_path(get_product_services_root())
    _remove_path(get_product_storage_root())
    get_product_config_path().unlink(missing_ok=True)
    _remove_env_keys(PRODUCT_SECRET_KEYS)
    _remove_install_tree_and_launchers()
    return {
        "removed_runsc_registration": removed_runsc_registration,
    }


def run_product_install(args: Any) -> None:
    from hermes_cli.product_setup import run_product_setup_wizard

    if not _is_linux():
        raise SystemExit("hermes-core install currently supports Linux host setup only")
    try:
        prereq_state = ensure_linux_product_host_prereqs()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    docker_ready, docker_message = _docker_readiness_probe()
    if prereq_state.get("added_docker_group_membership") and not docker_ready:
        print(
            "Added your user to the docker group. Run 'newgrp docker' or start a new login shell, then rerun 'hermes-core install'."
        )
        raise SystemExit(DOCKER_GROUP_RELOGIN_EXIT_CODE)
    if not _docker_compose_available():
        raise SystemExit("docker compose is not available")

    if not _runsc_available():
        raise SystemExit("runsc is not installed on this machine")
    try:
        changed = ensure_runsc_registered_with_docker()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    docker_ready, docker_message = _docker_readiness_probe()
    if not docker_ready:
        raise SystemExit(docker_message)
    state = _product_install_state()
    state["managed_runsc_registration"] = bool(changed or state.get("managed_runsc_registration"))
    save_product_install_state(state)
    build_product_runtime_image()

    if getattr(args, "skip_setup", False):
        return

    validate_product_host_prereqs()
    setattr(args, "from_install", True)
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
