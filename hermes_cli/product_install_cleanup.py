from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


def stage_product_build_context(hooks: Any, source_root: Path, destination_root: Path) -> Path:
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
            if hooks._product_build_context_ignored(relative_posix, is_dir=is_dir):
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


def remove_env_keys(hooks: Any, keys: list[str]) -> None:
    env_path = hooks.get_env_path()
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


def remove_runtime_containers(hooks: Any) -> None:
    result = hooks._run(["docker", "ps", "-aq", "--filter", "label=ch.hermes.product.role=runtime"], check=False)
    container_ids = [item.strip() for item in (result.stdout or "").splitlines() if item.strip()]
    for container_id in container_ids:
        hooks._run(["docker", "rm", "-f", container_id], check=False)


def remove_runtime_image(hooks: Any) -> None:
    hooks._run(["docker", "rmi", "-f", hooks.PRODUCT_RUNTIME_IMAGE_TAG], check=False)


def remove_tsidp_stack(hooks: Any) -> None:
    compose_path = hooks.get_tsidp_compose_path()
    if compose_path.exists():
        hooks._run(["docker", "compose", "-f", str(compose_path), "down", "-v", "--remove-orphans"], check=False)
    hooks._run(["docker", "rm", "-f", "hermes-tsidp"], check=False)


def remove_path(hooks: Any, path: Path) -> None:
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
    if not (hooks._is_linux() and path.is_absolute()):
        raise
    command = ["rm", "-rf" if path.is_dir() and not path.is_symlink() else "-f", str(path)]
    hooks._run(command, sudo=True)


def remove_install_tree_and_launchers(hooks: Any) -> None:
    for launcher_path in (
        Path.home() / ".local" / "bin" / "hermes",
        Path.home() / ".local" / "bin" / "hermes-core",
    ):
        hooks._remove_path(launcher_path)
    hooks._remove_path(hooks.product_install_root())


def remove_product_user_services(hooks: Any) -> None:
    if not (hooks._is_linux() and hooks._systemd_available()):
        return
    hooks._run(["systemctl", "--user", "disable", "--now", hooks.PRODUCT_APP_SERVICE_NAME], check=False)
    service_path = hooks._product_app_service_path()
    if service_path.exists():
        service_path.unlink(missing_ok=True)
    hooks._run(["systemctl", "--user", "daemon-reload"], check=False)


def remove_runsc_registration_if_managed(hooks: Any) -> bool:
    state = hooks._product_install_state()
    if not state.get("managed_runsc_registration"):
        return False
    if not hooks._is_linux():
        return False
    config, exists = hooks._load_docker_daemon_config()
    if not exists:
        return False
    runtimes = config.get("runtimes", {})
    if not hooks._runsc_runtime_matches(runtimes.get(hooks.RUNSC_RUNTIME_NAME)):
        return False
    runtimes.pop(hooks.RUNSC_RUNTIME_NAME, None)
    if not runtimes:
        config.pop("runtimes", None)
    hooks._write_docker_daemon_config(config)
    hooks._restart_docker_service()
    state["managed_runsc_registration"] = False
    hooks.save_product_install_state(state)
    return True


def build_product_runtime_image(hooks: Any) -> None:
    dockerfile_path = hooks.product_runtime_dockerfile()
    project_root = hooks.product_install_root()
    if not dockerfile_path.exists():
        raise RuntimeError(f"Product runtime Dockerfile not found: {dockerfile_path}")
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-product-build-") as build_root:
            staged_root = hooks._stage_product_build_context(project_root, Path(build_root) / "context")
            staged_dockerfile = staged_root / dockerfile_path.name
            hooks._run(
                [
                    "docker",
                    "build",
                    "-t",
                    hooks.PRODUCT_RUNTIME_IMAGE_TAG,
                    "-f",
                    str(staged_dockerfile),
                    str(staged_root),
                ],
                capture_output=False,
            )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Failed to build the local Hermes Core product runtime image") from exc


def perform_product_cleanup(hooks: Any) -> dict[str, bool]:
    removed_runsc_registration = False
    if hooks._docker_available():
        hooks._remove_tsidp_stack()
        hooks._remove_runtime_containers()
        hooks._remove_runtime_image()
    hooks._remove_product_user_services()
    removed_runsc_registration = hooks._remove_runsc_registration_if_managed()
    hooks._remove_path(hooks.get_product_services_root())
    hooks._remove_path(hooks.get_product_storage_root())
    hooks.get_product_config_path().unlink(missing_ok=True)
    hooks._remove_env_keys(hooks.PRODUCT_SECRET_KEYS)
    hooks._remove_install_tree_and_launchers()
    return {"removed_runsc_registration": removed_runsc_registration}

