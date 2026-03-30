from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


def stage_product_build_context(product_build_context_ignored_fn: Any, source_root: Path, destination_root: Path) -> Path:
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
            if product_build_context_ignored_fn(relative_posix, is_dir=is_dir):
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


def remove_env_keys(get_env_path_fn: Any, keys: list[str]) -> None:
    env_path = get_env_path_fn()
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


def remove_runtime_containers(run_fn: Any) -> None:
    result = run_fn(["docker", "ps", "-aq", "--filter", "label=ch.hermes.product.role=runtime"], check=False)
    container_ids = [item.strip() for item in (result.stdout or "").splitlines() if item.strip()]
    for container_id in container_ids:
        run_fn(["docker", "rm", "-f", container_id], check=False)


def remove_runtime_image(run_fn: Any, runtime_image_tag: str) -> None:
    run_fn(["docker", "rmi", "-f", runtime_image_tag], check=False)


def remove_tsidp_stack(run_fn: Any, compose_path: Path) -> None:
    if compose_path.exists():
        run_fn(["docker", "compose", "-f", str(compose_path), "down", "-v", "--remove-orphans"], check=False)
    run_fn(["docker", "rm", "-f", "hermes-tsidp"], check=False)


def remove_path(is_linux_fn: Any, run_fn: Any, path: Path) -> None:
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
    if not (is_linux_fn() and path.is_absolute()):
        raise
    command = ["rm", "-rf" if path.is_dir() and not path.is_symlink() else "-f", str(path)]
    run_fn(command, sudo=True)


def remove_install_tree_and_launchers(remove_path_fn: Any, product_install_root_fn: Any) -> None:
    for launcher_path in (Path.home() / ".local" / "bin" / "hermes", Path.home() / ".local" / "bin" / "hermes-core"):
        remove_path_fn(launcher_path)
    remove_path_fn(product_install_root_fn())


def remove_product_user_services(
    *,
    is_linux_fn: Any,
    systemd_available_fn: Any,
    run_fn: Any,
    product_app_service_path_fn: Any,
    service_name: str,
) -> None:
    if not (is_linux_fn() and systemd_available_fn()):
        return
    run_fn(["systemctl", "--user", "disable", "--now", service_name], check=False)
    service_path = product_app_service_path_fn(service_name)
    if service_path.exists():
        service_path.unlink(missing_ok=True)
    run_fn(["systemctl", "--user", "daemon-reload"], check=False)


def remove_runsc_registration_if_managed(
    *,
    product_install_state_fn: Any,
    is_linux_fn: Any,
    load_docker_daemon_config_fn: Any,
    runsc_runtime_matches_fn: Any,
    write_docker_daemon_config_fn: Any,
    restart_docker_service_fn: Any,
    save_product_install_state_fn: Any,
    runtime_name: str,
) -> bool:
    state = product_install_state_fn()
    if not state.get("managed_runsc_registration"):
        return False
    if not is_linux_fn():
        return False
    config, exists = load_docker_daemon_config_fn()
    if not exists:
        return False
    runtimes = config.get("runtimes", {})
    if not runsc_runtime_matches_fn(runtimes.get(runtime_name)):
        return False
    runtimes.pop(runtime_name, None)
    if not runtimes:
        config.pop("runtimes", None)
    write_docker_daemon_config_fn(config)
    restart_docker_service_fn()
    state["managed_runsc_registration"] = False
    save_product_install_state_fn(state)
    return True


def build_product_runtime_image(
    *,
    product_runtime_dockerfile_fn: Any,
    product_install_root_fn: Any,
    stage_product_build_context_fn: Any,
    run_fn: Any,
    runtime_image_tag: str,
) -> None:
    dockerfile_path = product_runtime_dockerfile_fn()
    project_root = product_install_root_fn()
    if not dockerfile_path.exists():
        raise RuntimeError(f"Product runtime Dockerfile not found: {dockerfile_path}")
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-product-build-") as build_root:
            staged_root = stage_product_build_context_fn(project_root, Path(build_root) / "context")
            staged_dockerfile = staged_root / dockerfile_path.name
            run_fn(
                ["docker", "build", "-t", runtime_image_tag, "-f", str(staged_dockerfile), str(staged_root)],
                capture_output=False,
            )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Failed to build the local Hermes Core product runtime image") from exc


def perform_product_cleanup(
    *,
    docker_available_fn: Any,
    remove_tsidp_stack_fn: Any,
    remove_runtime_containers_fn: Any,
    remove_runtime_image_fn: Any,
    remove_product_user_services_fn: Any,
    remove_runsc_registration_if_managed_fn: Any,
    remove_path_fn: Any,
    get_product_services_root_fn: Any,
    get_product_storage_root_fn: Any,
    get_product_config_path_fn: Any,
    remove_env_keys_fn: Any,
    remove_install_tree_and_launchers_fn: Any,
    product_secret_keys: list[str],
) -> dict[str, bool]:
    removed_runsc_registration = False
    if docker_available_fn():
        remove_tsidp_stack_fn()
        remove_runtime_containers_fn()
        remove_runtime_image_fn()
    remove_product_user_services_fn()
    removed_runsc_registration = remove_runsc_registration_if_managed_fn()
    remove_path_fn(get_product_services_root_fn())
    remove_path_fn(get_product_storage_root_fn())
    get_product_config_path_fn().unlink(missing_ok=True)
    remove_env_keys_fn(product_secret_keys)
    remove_install_tree_and_launchers_fn()
    return {"removed_runsc_registration": removed_runsc_registration}
