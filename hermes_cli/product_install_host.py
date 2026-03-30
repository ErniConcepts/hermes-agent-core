from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


def linux_distro_id(hooks: Any) -> str:
    if not hooks._is_linux():
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


def apt_supported_linux(hooks: Any) -> bool:
    return hooks._linux_distro_id() in {"ubuntu", "debian"}


def systemd_available(hooks: Any) -> bool:
    if shutil.which("systemctl") is None:
        return False
    return hooks._run(["systemctl", "--version"], check=False).returncode == 0


def product_service_identity(hooks: Any) -> tuple[str, str]:
    import pwd

    if not hooks._is_linux():
        raise RuntimeError("Product app service management is only supported on Linux")
    if getattr(hooks.os, "geteuid", lambda: 1)() == 0:
        sudo_user = str(hooks.os.environ.get("SUDO_USER", "")).strip()
        if not sudo_user or sudo_user == "root":
            raise RuntimeError("Could not determine the non-root user for the product app service")
        account = pwd.getpwnam(sudo_user)
        return account.pw_name, account.pw_dir
    account = pwd.getpwuid(hooks.os.geteuid())
    if account.pw_name == "root":
        raise RuntimeError("Refusing to install the product app service as root")
    return account.pw_name, account.pw_dir


def current_user_name(hooks: Any) -> str:
    import pwd

    if getattr(hooks.os, "geteuid", lambda: 1)() == 0:
        sudo_user = str(hooks.os.environ.get("SUDO_USER", "")).strip()
        if sudo_user and sudo_user != "root":
            return sudo_user
    return pwd.getpwuid(hooks.os.getuid()).pw_name


def user_in_group(hooks: Any, group_name: str, user_name: str | None = None) -> bool:
    if hooks.grp is None:
        return False
    name = user_name or hooks._current_user_name()
    try:
        group_info = hooks.grp.getgrnam(group_name)
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


def docker_compose_available(hooks: Any) -> bool:
    if shutil.which("docker") is None:
        return False
    result = hooks._run(["docker", "compose", "version"], check=False)
    return result.returncode == 0


def docker_available(hooks: Any) -> bool:
    if shutil.which("docker") is None:
        return False
    result = hooks._run(["docker", "info"], check=False)
    return result.returncode == 0


def docker_readiness_probe(hooks: Any) -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "Docker CLI is not installed."

    probe = hooks._run(["docker", "info", "--format", "{{json .Runtimes}}"], check=False)
    if probe.returncode == 0:
        runtimes = hooks._docker_runtimes()
        if not hooks._runsc_runtime_matches(runtimes.get(hooks.RUNSC_RUNTIME_NAME)):
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
    if hooks._systemd_available():
        docker_state = (hooks._run(["systemctl", "is-active", "docker"], check=False).stdout or "").strip().lower()
        socket_state = (hooks._run(["systemctl", "is-active", "docker.socket"], check=False).stdout or "").strip().lower()
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


def runsc_available(hooks: Any) -> bool:
    if shutil.which("runsc") is None:
        return False
    result = hooks._run(["runsc", "--version"], check=False)
    return result.returncode == 0


def docker_runtimes(hooks: Any) -> dict[str, Any]:
    if shutil.which("docker") is None:
        return {}
    result = hooks._run(["docker", "info", "--format", "{{json .Runtimes}}"], check=False)
    if result.returncode != 0:
        return {}
    try:
        return json.loads((result.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}


def runsc_runtime_matches(hooks: Any, config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    path_value = str(config.get("path", "")).strip()
    runtime_args = config.get("runtimeArgs")
    if not path_value or PurePosixPath(path_value).name != hooks.RUNSC_RUNTIME_NAME:
        return False
    return runtime_args == hooks.RUNSC_RUNTIME_CONFIG["runtimeArgs"]


def runsc_registered(hooks: Any) -> bool:
    return hooks._runsc_runtime_matches(hooks._docker_runtimes().get(hooks.RUNSC_RUNTIME_NAME))


def load_docker_daemon_config(hooks: Any) -> tuple[dict[str, Any], bool]:
    if hooks.DOCKER_DAEMON_CONFIG_PATH.exists():
        try:
            return json.loads(hooks.DOCKER_DAEMON_CONFIG_PATH.read_text(encoding="utf-8")), True
        except PermissionError:
            result = hooks._run(["cat", str(hooks.DOCKER_DAEMON_CONFIG_PATH)], sudo=True)
            return json.loads(result.stdout), True
    return {}, False


def write_docker_daemon_config(hooks: Any, config: dict[str, Any]) -> None:
    rendered = json.dumps(config, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(rendered)
        temp_path = Path(handle.name)
    try:
        if getattr(hooks.os, "geteuid", lambda: 1)() == 0:
            shutil.copyfile(temp_path, hooks.DOCKER_DAEMON_CONFIG_PATH)
        else:
            hooks._run(["cp", str(temp_path), str(hooks.DOCKER_DAEMON_CONFIG_PATH)], sudo=True)
    finally:
        temp_path.unlink(missing_ok=True)


def restart_docker_service(hooks: Any) -> None:
    if not hooks._systemd_available():
        return
    hooks._run(["systemctl", "stop", "docker", "docker.socket"], check=False, sudo=True)
    hooks._run(["systemctl", "reset-failed", "docker", "docker.socket"], check=False, sudo=True)
    hooks._run(["systemctl", "start", "docker.socket"], sudo=True)
    hooks._run(["systemctl", "start", "docker"], sudo=True)


def start_and_enable_docker_service(hooks: Any) -> None:
    if not hooks._systemd_available():
        return
    hooks._run(["systemctl", "enable", "--now", "docker.socket"], sudo=True)
    hooks._run(["systemctl", "enable", "--now", "docker"], sudo=True)


def docker_service_active(hooks: Any) -> bool:
    if not hooks._systemd_available():
        return False
    return hooks._run(["systemctl", "is-active", "docker"], check=False).returncode == 0


def apt_install(hooks: Any, packages: list[str]) -> None:
    env_prefix = ["env", "DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=a"]
    hooks._run([*env_prefix, "apt-get", "update", "-qq"], sudo=True)
    hooks._run([*env_prefix, "apt-get", "install", "-y", *packages], sudo=True)


def apt_package_available(hooks: Any, package_name: str) -> bool:
    result = hooks._run(["apt-cache", "policy", package_name], check=False)
    if result.returncode != 0:
        return False
    stdout = (result.stdout or "").strip().lower()
    if not stdout:
        return False
    return "candidate: (none)" not in stdout


def linux_host_prereq_packages(hooks: Any) -> list[str]:
    packages = list(hooks.APT_INSTALL_PACKAGES)
    for package_name in hooks.APT_DOCKER_COMPOSE_PACKAGE_CANDIDATES:
        if hooks._apt_package_available(package_name):
            packages.append(package_name)
            return packages
    raise RuntimeError(
        "Could not find a Docker Compose package on this Linux host. "
        "Expected one of: docker-compose-v2, docker-compose-plugin, docker-compose."
    )


def ensure_linux_product_host_prereqs(hooks: Any) -> dict[str, bool]:
    if not hooks._is_linux():
        return {"installed_packages": False, "added_docker_group_membership": False}
    if not hooks._apt_supported_linux():
        raise RuntimeError("Automatic host prerequisite installation is currently supported only on Ubuntu/Debian")

    installed_packages = False
    if not hooks._docker_available() or not hooks._docker_compose_available() or not hooks._runsc_available():
        hooks._apt_install(hooks._linux_host_prereq_packages())
        installed_packages = True

    if installed_packages or not hooks._docker_service_active():
        hooks._start_and_enable_docker_service()

    added_docker_group_membership = False
    current_user = hooks._current_user_name()
    if not hooks._user_in_group("docker", current_user):
        hooks._run(["usermod", "-aG", "docker", current_user], sudo=True)
        added_docker_group_membership = True

    return {
        "installed_packages": installed_packages,
        "added_docker_group_membership": added_docker_group_membership,
    }


def ensure_runsc_registered_with_docker(hooks: Any) -> bool:
    config, _exists = hooks._load_docker_daemon_config()
    runtimes = config.setdefault("runtimes", {})
    existing = runtimes.get(hooks.RUNSC_RUNTIME_NAME)
    if hooks._runsc_runtime_matches(existing) and hooks._runsc_registered():
        return False
    runtimes[hooks.RUNSC_RUNTIME_NAME] = dict(hooks.RUNSC_RUNTIME_CONFIG)
    hooks._write_docker_daemon_config(config)
    hooks._restart_docker_service()
    if not hooks._runsc_registered():
        raise RuntimeError("Docker still does not report the runsc runtime after configuration")
    return True


def validate_product_host_prereqs(hooks: Any) -> None:
    if not hooks._is_linux():
        raise RuntimeError("hermes-core product host prerequisites are only supported on Linux")
    docker_ready, docker_message = hooks._docker_readiness_probe()
    if not docker_ready:
        raise RuntimeError(docker_message)
    if not hooks._docker_compose_available():
        raise RuntimeError("docker compose is not available")
    if not hooks._runsc_available():
        raise RuntimeError("runsc is not installed on this machine")

