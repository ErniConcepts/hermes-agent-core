from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from hermes_cli.config import get_env_path
from hermes_cli.product_config import get_product_config_path, get_product_storage_root
from hermes_cli.product_install_cleanup import (
    build_product_runtime_image as _build_product_runtime_image_impl,
    perform_product_cleanup as _perform_product_cleanup_impl,
    remove_env_keys as _remove_env_keys_impl,
    remove_install_tree_and_launchers as _remove_install_tree_and_launchers_impl,
    remove_path as _remove_path_impl,
    remove_product_user_services as _remove_product_user_services_impl,
    remove_runsc_registration_if_managed as _remove_runsc_registration_if_managed_impl,
    remove_runtime_containers as _remove_runtime_containers_impl,
    remove_runtime_image as _remove_runtime_image_impl,
    remove_tsidp_stack as _remove_tsidp_stack_impl,
    stage_product_build_context as _stage_product_build_context_impl,
)
from hermes_cli.product_install_host import (
    apt_install as _apt_install_impl,
    apt_package_available as _apt_package_available_impl,
    apt_supported_linux as _apt_supported_linux_impl,
    current_user_name as _current_user_name_impl,
    docker_available as _docker_available_impl,
    docker_compose_available as _docker_compose_available_impl,
    docker_readiness_probe as _docker_readiness_probe_impl,
    docker_runtimes as _docker_runtimes_impl,
    docker_service_active as _docker_service_active_impl,
    ensure_linux_product_host_prereqs as _ensure_linux_product_host_prereqs_impl,
    ensure_runsc_registered_with_docker as _ensure_runsc_registered_with_docker_impl,
    linux_distro_id as _linux_distro_id_impl,
    linux_host_prereq_packages as _linux_host_prereq_packages_impl,
    load_docker_daemon_config as _load_docker_daemon_config_impl,
    product_service_identity as _product_service_identity_impl,
    restart_docker_service as _restart_docker_service_impl,
    runsc_available as _runsc_available_impl,
    runsc_registered as _runsc_registered_impl,
    runsc_runtime_matches as _runsc_runtime_matches_impl,
    start_and_enable_docker_service as _start_and_enable_docker_service_impl,
    systemd_available as _systemd_available_impl,
    user_in_group as _user_in_group_impl,
    validate_product_host_prereqs as _validate_product_host_prereqs_impl,
    write_docker_daemon_config as _write_docker_daemon_config_impl,
)
from hermes_cli.product_install_service import (
    ensure_product_app_service_started as _ensure_product_app_service_started_impl,
    get_product_install_state_path,
    load_product_install_state,
    product_app_service_path as _product_app_service_path_impl,
    product_build_context_ignored as _product_build_context_ignored_impl,
    product_install_root as _product_install_root_impl,
    product_runtime_dockerfile as _product_runtime_dockerfile_impl,
    render_product_app_service_unit as _render_product_app_service_unit_impl,
    save_product_install_state,
    write_product_app_service_unit as _write_product_app_service_unit_impl,
)
from hermes_cli.product_runtime_network import (
    ensure_runtime_docker_network,
    ensure_runtime_host_firewall,
    local_host_model_port,
    remove_runtime_docker_network,
    remove_runtime_host_firewall,
)
from hermes_cli.product_stack import get_product_services_root, get_tsidp_compose_path


DEFAULT_INSTALL_DIR_NAME = "hermes-core"
DOCKER_DAEMON_CONFIG_PATH = Path("/etc/docker/daemon.json")
RUNSC_RUNTIME_NAME = "runsc"
RUNSC_RUNTIME_CONFIG = {"path": "runsc", "runtimeArgs": []}
PRODUCT_SECRET_KEYS = [
    "HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET",
    "HERMES_PRODUCT_SESSION_SECRET",
    "HERMES_PRODUCT_TAILSCALE_AUTH_KEY",
]
PRODUCT_APP_SERVICE_NAME = "hermes-core-product-app.service"
PRODUCT_RUNTIME_IMAGE_TAG = "hermes-core-product-runtime:local"
DOCKER_GROUP_RELOGIN_EXIT_CODE = 42
APT_INSTALL_PACKAGES = ["docker.io", "runsc"]
APT_DOCKER_COMPOSE_PACKAGE_CANDIDATES = ["docker-compose-v2", "docker-compose-plugin", "docker-compose"]
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
        return subprocess.run(command, check=check, capture_output=capture_output, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip().lower()
        if using_sudo and ("a password is required" in stderr or "no tty present" in stderr or "terminal is required" in stderr):
            raise RuntimeError(
                "Host prerequisite installation needs sudo in an interactive local shell. "
                "Rerun 'hermes-core install' directly on the Linux device and enter your sudo password when prompted."
            ) from exc
        raise


def _product_app_service_path() -> Path:
    return _product_app_service_path_impl(PRODUCT_APP_SERVICE_NAME)


def product_install_root() -> Path:
    return _product_install_root_impl(DEFAULT_INSTALL_DIR_NAME)


def product_runtime_dockerfile() -> Path:
    return _product_runtime_dockerfile_impl(DEFAULT_INSTALL_DIR_NAME)


def _linux_distro_id() -> str:
    return _linux_distro_id_impl(_is_linux)


def _apt_supported_linux() -> bool:
    return _apt_supported_linux_impl(_linux_distro_id)


def _systemd_available() -> bool:
    return _systemd_available_impl(_run)


def _product_service_identity() -> tuple[str, str]:
    return _product_service_identity_impl(_is_linux)


def _current_user_name() -> str:
    return _current_user_name_impl()


def _user_in_group(group_name: str, user_name: str | None = None) -> bool:
    return _user_in_group_impl(group_name, user_name)


def _render_product_app_service_unit(config: dict[str, Any] | None = None) -> str:
    from hermes_cli.product_stack import load_product_config

    return _render_product_app_service_unit_impl(
        load_product_config_fn=load_product_config,
        product_service_identity_fn=_product_service_identity,
        product_service_unit_spec_cls=ProductServiceUnitSpec,
        default_install_dir_name=DEFAULT_INSTALL_DIR_NAME,
        config=config,
    )


def _write_product_app_service_unit(config: dict[str, Any] | None = None) -> None:
    _write_product_app_service_unit_impl(
        product_app_service_path_fn=_product_app_service_path_impl,
        render_product_app_service_unit_fn=_render_product_app_service_unit,
        service_name=PRODUCT_APP_SERVICE_NAME,
        config=config,
    )


def ensure_product_app_service_started(config: dict[str, Any] | None = None) -> None:
    _ensure_product_app_service_started_impl(
        is_linux_fn=_is_linux,
        systemd_available_fn=_systemd_available,
        write_product_app_service_unit_fn=_write_product_app_service_unit,
        run_fn=_run,
        service_name=PRODUCT_APP_SERVICE_NAME,
        config=config,
    )


def _product_install_state() -> dict[str, Any]:
    return load_product_install_state()


def _product_build_context_ignored(relative_path: PurePosixPath, *, is_dir: bool) -> bool:
    return _product_build_context_ignored_impl(PRODUCT_DOCKER_BUILD_IGNORE_PATTERNS, relative_path, is_dir=is_dir)


def _stage_product_build_context(source_root: Path, destination_root: Path) -> Path:
    return _stage_product_build_context_impl(_product_build_context_ignored, source_root, destination_root)


def _docker_compose_available() -> bool:
    return _docker_compose_available_impl(_run)


def _docker_available() -> bool:
    return _docker_available_impl(_run)


def _docker_runtimes() -> dict[str, Any]:
    return _docker_runtimes_impl(_run)


def _runsc_runtime_matches(config: Any) -> bool:
    return _runsc_runtime_matches_impl(RUNSC_RUNTIME_NAME, RUNSC_RUNTIME_CONFIG, config)


def _docker_readiness_probe() -> tuple[bool, str]:
    return _docker_readiness_probe_impl(
        run_fn=_run,
        systemd_available_fn=_systemd_available,
        docker_runtimes_fn=_docker_runtimes,
        runsc_runtime_matches_fn=_runsc_runtime_matches,
        runtime_name=RUNSC_RUNTIME_NAME,
    )


def _runsc_available() -> bool:
    return _runsc_available_impl(_run)


def _runsc_registered() -> bool:
    return _runsc_registered_impl(_run, RUNSC_RUNTIME_NAME, RUNSC_RUNTIME_CONFIG)


def _load_docker_daemon_config() -> tuple[dict[str, Any], bool]:
    return _load_docker_daemon_config_impl(_run, DOCKER_DAEMON_CONFIG_PATH)


def _write_docker_daemon_config(config: dict[str, Any]) -> None:
    _write_docker_daemon_config_impl(_run, DOCKER_DAEMON_CONFIG_PATH, config)


def _restart_docker_service() -> None:
    _restart_docker_service_impl(_run, _systemd_available)


def _start_and_enable_docker_service() -> None:
    _start_and_enable_docker_service_impl(_run, _systemd_available)


def _docker_service_active() -> bool:
    return _docker_service_active_impl(_run, _systemd_available)


def _apt_install(packages: list[str]) -> None:
    _apt_install_impl(_run, packages)


def _apt_package_available(package_name: str) -> bool:
    return _apt_package_available_impl(_run, package_name)


def _linux_host_prereq_packages() -> list[str]:
    return _linux_host_prereq_packages_impl(APT_DOCKER_COMPOSE_PACKAGE_CANDIDATES, APT_INSTALL_PACKAGES, _apt_package_available)


def ensure_linux_product_host_prereqs() -> dict[str, bool]:
    return _ensure_linux_product_host_prereqs_impl(
        is_linux_fn=_is_linux,
        apt_supported_linux_fn=_apt_supported_linux,
        docker_available_fn=_docker_available,
        docker_compose_available_fn=_docker_compose_available,
        runsc_available_fn=_runsc_available,
        apt_install_fn=_apt_install,
        linux_host_prereq_packages_fn=_linux_host_prereq_packages,
        docker_service_active_fn=_docker_service_active,
        start_and_enable_docker_service_fn=_start_and_enable_docker_service,
        current_user_name_fn=_current_user_name,
        user_in_group_fn=_user_in_group,
        run_fn=_run,
    )


def ensure_runsc_registered_with_docker() -> bool:
    return _ensure_runsc_registered_with_docker_impl(
        load_docker_daemon_config_fn=_load_docker_daemon_config,
        runsc_runtime_matches_fn=_runsc_runtime_matches,
        runsc_registered_fn=_runsc_registered,
        write_docker_daemon_config_fn=_write_docker_daemon_config,
        restart_docker_service_fn=_restart_docker_service,
        runtime_name=RUNSC_RUNTIME_NAME,
        runtime_config=RUNSC_RUNTIME_CONFIG,
    )


def validate_product_host_prereqs() -> None:
    _validate_product_host_prereqs_impl(
        is_linux_fn=_is_linux,
        docker_readiness_probe_fn=_docker_readiness_probe,
        docker_compose_available_fn=_docker_compose_available,
        runsc_available_fn=_runsc_available,
    )


def _remove_env_keys(keys: list[str]) -> None:
    _remove_env_keys_impl(get_env_path, keys)


def _remove_runtime_containers() -> None:
    _remove_runtime_containers_impl(_run)


def _remove_runtime_image() -> None:
    _remove_runtime_image_impl(_run, PRODUCT_RUNTIME_IMAGE_TAG)


def _remove_tsidp_stack() -> None:
    _remove_tsidp_stack_impl(_run, get_tsidp_compose_path())


def _remove_path(path: Path) -> None:
    _remove_path_impl(_is_linux, _run, path)


def _remove_install_tree_and_launchers() -> None:
    _remove_install_tree_and_launchers_impl(_remove_path, product_install_root)


def _remove_product_user_services() -> None:
    _remove_product_user_services_impl(
        is_linux_fn=_is_linux,
        systemd_available_fn=_systemd_available,
        run_fn=_run,
        product_app_service_path_fn=_product_app_service_path_impl,
        service_name=PRODUCT_APP_SERVICE_NAME,
    )


def _remove_runsc_registration_if_managed() -> bool:
    return _remove_runsc_registration_if_managed_impl(
        product_install_state_fn=_product_install_state,
        is_linux_fn=_is_linux,
        load_docker_daemon_config_fn=_load_docker_daemon_config,
        runsc_runtime_matches_fn=_runsc_runtime_matches,
        write_docker_daemon_config_fn=_write_docker_daemon_config,
        restart_docker_service_fn=_restart_docker_service,
        save_product_install_state_fn=save_product_install_state,
        runtime_name=RUNSC_RUNTIME_NAME,
    )


def build_product_runtime_image() -> None:
    _build_product_runtime_image_impl(
        product_runtime_dockerfile_fn=product_runtime_dockerfile,
        product_install_root_fn=product_install_root,
        stage_product_build_context_fn=_stage_product_build_context,
        run_fn=_run,
        runtime_image_tag=PRODUCT_RUNTIME_IMAGE_TAG,
    )


def ensure_product_runtime_networking() -> dict[str, bool]:
    network_created = ensure_runtime_docker_network(_run)
    firewall_changed = ensure_runtime_host_firewall(_run, model_port=local_host_model_port())
    return {"created_network": network_created, "updated_firewall": firewall_changed}


def _record_runtime_management_state(
    *,
    managed_runsc_registration: bool | None = None,
    networking: dict[str, bool] | None = None,
) -> None:
    state = _product_install_state()
    if managed_runsc_registration is not None:
        state["managed_runsc_registration"] = bool(managed_runsc_registration or state.get("managed_runsc_registration"))
    if networking is not None:
        state["managed_runtime_network"] = bool(networking["created_network"] or state.get("managed_runtime_network"))
        state["managed_runtime_firewall"] = bool(networking["updated_firewall"] or state.get("managed_runtime_firewall"))
    save_product_install_state(state)


def perform_product_cleanup() -> dict[str, bool]:
    result = _perform_product_cleanup_impl(
        docker_available_fn=_docker_available,
        remove_tsidp_stack_fn=_remove_tsidp_stack,
        remove_runtime_containers_fn=_remove_runtime_containers,
        remove_runtime_image_fn=_remove_runtime_image,
        remove_product_user_services_fn=_remove_product_user_services,
        remove_runsc_registration_if_managed_fn=_remove_runsc_registration_if_managed,
        remove_path_fn=_remove_path,
        get_product_services_root_fn=get_product_services_root,
        get_product_storage_root_fn=get_product_storage_root,
        get_product_config_path_fn=get_product_config_path,
        remove_env_keys_fn=_remove_env_keys,
        remove_install_tree_and_launchers_fn=_remove_install_tree_and_launchers,
        product_secret_keys=PRODUCT_SECRET_KEYS,
    )
    result["removed_runtime_network"] = remove_runtime_docker_network(_run)
    result["removed_runtime_firewall"] = remove_runtime_host_firewall(_run, model_port=local_host_model_port())
    return result


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
        print("Added your user to the docker group. Run 'newgrp docker' or start a new login shell, then rerun 'hermes-core install'.")
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
    networking = ensure_product_runtime_networking()
    _record_runtime_management_state(managed_runsc_registration=changed, networking=networking)
    build_product_runtime_image()
    if getattr(args, "skip_setup", False):
        return
    validate_product_host_prereqs()
    setattr(args, "from_install", True)
    run_product_setup_wizard(args)
    _record_runtime_management_state(networking=ensure_product_runtime_networking())


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
