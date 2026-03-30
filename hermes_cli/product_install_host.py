from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import grp
except ModuleNotFoundError:  # pragma: no cover - Windows
    grp = None


def linux_distro_id(is_linux_fn: Any) -> str:
    if not is_linux_fn():
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


def apt_supported_linux(linux_distro_id_fn: Any) -> bool:
    return linux_distro_id_fn() in {"ubuntu", "debian"}


def systemd_available(run_fn: Any) -> bool:
    if shutil.which("systemctl") is None:
        return False
    return run_fn(["systemctl", "--version"], check=False).returncode == 0


def product_service_identity(is_linux_fn: Any) -> tuple[str, str]:
    import pwd

    if not is_linux_fn():
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


def current_user_name() -> str:
    import pwd

    if getattr(os, "geteuid", lambda: 1)() == 0:
        sudo_user = str(os.environ.get("SUDO_USER", "")).strip()
        if sudo_user and sudo_user != "root":
            return sudo_user
    return pwd.getpwuid(os.getuid()).pw_name


def user_in_group(group_name: str, user_name: str | None = None) -> bool:
    if grp is None:
        return False
    name = user_name or current_user_name()
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


def docker_compose_available(run_fn: Any) -> bool:
    if shutil.which("docker") is None:
        return False
    return run_fn(["docker", "compose", "version"], check=False).returncode == 0


def docker_available(run_fn: Any) -> bool:
    if shutil.which("docker") is None:
        return False
    return run_fn(["docker", "info"], check=False).returncode == 0


def docker_runtimes(run_fn: Any) -> dict[str, Any]:
    if shutil.which("docker") is None:
        return {}
    result = run_fn(["docker", "info", "--format", "{{json .Runtimes}}"], check=False)
    if result.returncode != 0:
        return {}
    try:
        return json.loads((result.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}


def runsc_runtime_matches(runtime_name: str, runtime_config: dict[str, Any], config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    path_value = str(config.get("path", "")).strip()
    runtime_args = config.get("runtimeArgs")
    if not path_value or PurePosixPath(path_value).name != runtime_name:
        return False
    return runtime_args == runtime_config["runtimeArgs"]


def runsc_registered(run_fn: Any, runtime_name: str, runtime_config: dict[str, Any]) -> bool:
    return runsc_runtime_matches(runtime_name, runtime_config, docker_runtimes(run_fn).get(runtime_name))


def docker_readiness_probe(
    *,
    run_fn: Any,
    systemd_available_fn: Any,
    docker_runtimes_fn: Any,
    runsc_runtime_matches_fn: Any,
    runtime_name: str,
) -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "Docker CLI is not installed."
    probe = run_fn(["docker", "info", "--format", "{{json .Runtimes}}"], check=False)
    if probe.returncode == 0:
        runtimes = docker_runtimes_fn()
        if not runsc_runtime_matches_fn(runtimes.get(runtime_name)):
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
    if systemd_available_fn():
        docker_state = (run_fn(["systemctl", "is-active", "docker"], check=False).stdout or "").strip().lower()
        socket_state = (run_fn(["systemctl", "is-active", "docker.socket"], check=False).stdout or "").strip().lower()
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


def runsc_available(run_fn: Any) -> bool:
    if shutil.which("runsc") is None:
        return False
    return run_fn(["runsc", "--version"], check=False).returncode == 0


def load_docker_daemon_config(run_fn: Any, daemon_path: Path) -> tuple[dict[str, Any], bool]:
    if daemon_path.exists():
        try:
            return json.loads(daemon_path.read_text(encoding="utf-8")), True
        except PermissionError:
            result = run_fn(["cat", str(daemon_path)], sudo=True)
            return json.loads(result.stdout), True
    return {}, False


def write_docker_daemon_config(run_fn: Any, daemon_path: Path, config: dict[str, Any]) -> None:
    rendered = json.dumps(config, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(rendered)
        temp_path = Path(handle.name)
    try:
        if getattr(os, "geteuid", lambda: 1)() == 0:
            shutil.copyfile(temp_path, daemon_path)
        else:
            run_fn(["cp", str(temp_path), str(daemon_path)], sudo=True)
    finally:
        temp_path.unlink(missing_ok=True)


def restart_docker_service(run_fn: Any, systemd_available_fn: Any) -> None:
    if not systemd_available_fn():
        return
    run_fn(["systemctl", "stop", "docker", "docker.socket"], check=False, sudo=True)
    run_fn(["systemctl", "reset-failed", "docker", "docker.socket"], check=False, sudo=True)
    run_fn(["systemctl", "start", "docker.socket"], sudo=True)
    run_fn(["systemctl", "start", "docker"], sudo=True)


def start_and_enable_docker_service(run_fn: Any, systemd_available_fn: Any) -> None:
    if not systemd_available_fn():
        return
    run_fn(["systemctl", "enable", "--now", "docker.socket"], sudo=True)
    run_fn(["systemctl", "enable", "--now", "docker"], sudo=True)


def docker_service_active(run_fn: Any, systemd_available_fn: Any) -> bool:
    if not systemd_available_fn():
        return False
    return run_fn(["systemctl", "is-active", "docker"], check=False).returncode == 0


def apt_install(run_fn: Any, packages: list[str]) -> None:
    env_prefix = ["env", "DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=a"]
    run_fn([*env_prefix, "apt-get", "update", "-qq"], sudo=True)
    run_fn([*env_prefix, "apt-get", "install", "-y", *packages], sudo=True)


def apt_package_available(run_fn: Any, package_name: str) -> bool:
    result = run_fn(["apt-cache", "policy", package_name], check=False)
    if result.returncode != 0:
        return False
    stdout = (result.stdout or "").strip().lower()
    if not stdout:
        return False
    return "candidate: (none)" not in stdout


def linux_host_prereq_packages(package_candidates: list[str], base_packages: list[str], apt_package_available_fn: Any) -> list[str]:
    packages = list(base_packages)
    for package_name in package_candidates:
        if apt_package_available_fn(package_name):
            packages.append(package_name)
            return packages
    raise RuntimeError(
        "Could not find a Docker Compose package on this Linux host. "
        "Expected one of: docker-compose-v2, docker-compose-plugin, docker-compose."
    )


def ensure_linux_product_host_prereqs(
    *,
    is_linux_fn: Any,
    apt_supported_linux_fn: Any,
    docker_available_fn: Any,
    docker_compose_available_fn: Any,
    runsc_available_fn: Any,
    apt_install_fn: Any,
    linux_host_prereq_packages_fn: Any,
    docker_service_active_fn: Any,
    start_and_enable_docker_service_fn: Any,
    current_user_name_fn: Any,
    user_in_group_fn: Any,
    run_fn: Any,
) -> dict[str, bool]:
    if not is_linux_fn():
        return {"installed_packages": False, "added_docker_group_membership": False}
    if not apt_supported_linux_fn():
        raise RuntimeError("Automatic host prerequisite installation is currently supported only on Ubuntu/Debian")
    installed_packages = False
    if not docker_available_fn() or not docker_compose_available_fn() or not runsc_available_fn():
        apt_install_fn(linux_host_prereq_packages_fn())
        installed_packages = True
    if installed_packages or not docker_service_active_fn():
        start_and_enable_docker_service_fn()
    added_docker_group_membership = False
    current_user = current_user_name_fn()
    if not user_in_group_fn("docker", current_user):
        run_fn(["usermod", "-aG", "docker", current_user], sudo=True)
        added_docker_group_membership = True
    return {"installed_packages": installed_packages, "added_docker_group_membership": added_docker_group_membership}


def ensure_runsc_registered_with_docker(
    *,
    load_docker_daemon_config_fn: Any,
    runsc_runtime_matches_fn: Any,
    runsc_registered_fn: Any,
    write_docker_daemon_config_fn: Any,
    restart_docker_service_fn: Any,
    runtime_name: str,
    runtime_config: dict[str, Any],
) -> bool:
    config, _exists = load_docker_daemon_config_fn()
    runtimes = config.setdefault("runtimes", {})
    existing = runtimes.get(runtime_name)
    if runsc_runtime_matches_fn(existing) and runsc_registered_fn():
        return False
    runtimes[runtime_name] = dict(runtime_config)
    write_docker_daemon_config_fn(config)
    restart_docker_service_fn()
    if not runsc_registered_fn():
        raise RuntimeError("Docker still does not report the runsc runtime after configuration")
    return True


def validate_product_host_prereqs(
    *,
    is_linux_fn: Any,
    docker_readiness_probe_fn: Any,
    docker_compose_available_fn: Any,
    runsc_available_fn: Any,
) -> None:
    if not is_linux_fn():
        raise RuntimeError("hermes-core product host prerequisites are only supported on Linux")
    docker_ready, docker_message = docker_readiness_probe_fn()
    if not docker_ready:
        raise RuntimeError(docker_message)
    if not docker_compose_available_fn():
        raise RuntimeError("docker compose is not available")
    if not runsc_available_fn():
        raise RuntimeError("runsc is not installed on this machine")
