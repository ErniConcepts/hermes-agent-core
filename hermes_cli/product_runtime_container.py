from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from hermes_cli.product_config import load_product_config, runtime_host_access_host
from hermes_cli.product_runtime_common import (
    ProductRuntimeRecord,
    ProductRuntimeSession,
    ProductRuntimeTurnRequest,
    _RUNTIME_ENV_MATCH_KEYS,
    _RUNTIME_HEALTH_CACHE,
    _RUNTIME_HEALTH_TTL_SECONDS,
    _RUNTIME_WORKSPACE_PATH,
)
from hermes_cli.product_runtime_staging import runtime_image, runtime_internal_port

logger = logging.getLogger(__name__)


def runtime_mounts(record: ProductRuntimeRecord) -> list[str]:
    hermes_home = Path(record.hermes_home)
    mounts = [
        f"type=bind,src={hermes_home.as_posix()},dst=/srv/hermes",
        f"type=bind,src={(hermes_home / 'SOUL.md').as_posix()},dst=/srv/hermes/SOUL.md,readonly",
        f"type=bind,src={Path(record.workspace_root).as_posix()},dst={_RUNTIME_WORKSPACE_PATH}",
    ]
    runtime_config = hermes_home / "config.yaml"
    if runtime_config.exists():
        mounts.insert(2, f"type=bind,src={runtime_config.as_posix()},dst=/srv/hermes/config.yaml,readonly")
    return mounts


def runtime_container_user(record: ProductRuntimeRecord) -> str | None:
    if os.name == "nt":
        return None
    try:
        workspace_stat = Path(record.workspace_root).stat()
        uid = int(workspace_stat.st_uid)
        gid = int(workspace_stat.st_gid)
        if uid >= 0 and gid >= 0:
            return f"{uid}:{gid}"
    except Exception:
        pass
    try:
        uid = os.getuid()
        gid = os.getgid()
        if uid >= 0 and gid >= 0:
            return f"{uid}:{gid}"
    except Exception:
        return None
    return None


def docker_run_command(record: ProductRuntimeRecord, config: dict[str, Any]) -> list[str]:
    internal_port = runtime_internal_port(config)
    mounts = runtime_mounts(record)
    user_spec = runtime_container_user(record)
    command = [
        "docker",
        "run",
        "--detach",
        "--restart",
        "unless-stopped",
        "--runtime",
        record.runtime,
        "--name",
        record.container_name,
        "--publish",
        f"127.0.0.1:{record.runtime_port}:{internal_port}",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(int(config.get("runtime", {}).get("pids_limit", 256))),
        "--tmpfs",
        "/tmp:ro,noexec,nosuid,size=64m",
        "--tmpfs",
        "/var/tmp:ro,noexec,nosuid,size=32m",
        "--env-file",
        record.env_file,
        "--workdir",
        _RUNTIME_WORKSPACE_PATH,
        "--add-host",
        f"{runtime_host_access_host(config)}:host-gateway",
        "--label",
        f"ch.hermes.product.user_id={record.user_id}",
        "--label",
        "ch.hermes.product.role=runtime",
        runtime_image(config),
        "python",
        "-m",
        "hermes_cli.product_runtime_service",
    ]
    if user_spec:
        workdir_index = command.index("--workdir")
        command[workdir_index:workdir_index] = ["--user", user_spec]
    label_index = command.index("--label")
    mount_args: list[str] = []
    for mount in mounts:
        mount_args.extend(["--mount", mount])
    command[label_index:label_index] = mount_args
    return command


def docker_inspect_state(container_name: str) -> dict[str, Any] | None:
    result = subprocess.run(["docker", "inspect", container_name], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    payload = json.loads(result.stdout)
    if not payload:
        return None
    return payload[0]


def container_env_map(container_state: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(container_state, dict):
        return {}
    raw_env = container_state.get("Config", {}).get("Env", [])
    if not isinstance(raw_env, list):
        return {}
    env_map: dict[str, str] = {}
    for item in raw_env:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, value = item.split("=", 1)
        env_map[key] = value
    return env_map


def runtime_launch_env(record: ProductRuntimeRecord) -> dict[str, str]:
    env_path = Path(record.env_file)
    if not env_path.exists():
        return {}
    env_map: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env_map[key] = value
    return env_map


def running_container_matches_record(record: ProductRuntimeRecord, container_state: dict[str, Any] | None) -> bool:
    container_env = container_env_map(container_state)
    expected_env = runtime_launch_env(record)
    if not container_env or not expected_env:
        return False
    for key in _RUNTIME_ENV_MATCH_KEYS:
        if container_env.get(key, "") != expected_env.get(key, ""):
            return False
    return True


def remove_container_if_exists(container_name: str) -> None:
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True, check=False)


def runtime_base_url(record: ProductRuntimeRecord) -> str:
    return f"http://127.0.0.1:{record.runtime_port}"


def wait_for_runtime_health(
    record: ProductRuntimeRecord,
    *,
    timeout_seconds: float = 20.0,
    interval_seconds: float = 0.25,
) -> None:
    started = time.perf_counter()
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{runtime_base_url(record)}/healthz", timeout=2.0)
            response.raise_for_status()
            payload = response.json()
            if str(payload.get("status", "")).strip().lower() == "ok":
                _RUNTIME_HEALTH_CACHE[record.container_name] = time.monotonic()
                logger.info(
                    "product_runtime health check for %s completed in %.0fms",
                    record.container_name,
                    (time.perf_counter() - started) * 1000,
                )
                return
            last_error = "runtime health endpoint did not report ok"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(interval_seconds)
    raise RuntimeError(f"Runtime failed to become ready: {last_error or 'health check timeout'}")


def ensure_runtime_container(record: ProductRuntimeRecord, config: dict[str, Any]) -> ProductRuntimeRecord:
    started = time.perf_counter()
    container_state = docker_inspect_state(record.container_name)
    if container_state and bool(container_state.get("State", {}).get("Running")):
        if not running_container_matches_record(record, container_state):
            remove_container_if_exists(record.container_name)
            _RUNTIME_HEALTH_CACHE.pop(record.container_name, None)
        else:
            running = ProductRuntimeRecord(**{**record.model_dump(), "status": "running"})
            last_healthy_at = _RUNTIME_HEALTH_CACHE.get(running.container_name, 0.0)
            if time.monotonic() - last_healthy_at > _RUNTIME_HEALTH_TTL_SECONDS:
                wait_for_runtime_health(running)
            logger.info(
                "product_runtime ensure for %s reused running container in %.0fms",
                running.container_name,
                (time.perf_counter() - started) * 1000,
            )
            return running

    remove_container_if_exists(record.container_name)
    result = subprocess.run(docker_run_command(record, config), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "docker run failed")
    running = ProductRuntimeRecord(**{**record.model_dump(), "status": "running"})
    wait_for_runtime_health(running)
    logger.info(
        "product_runtime ensure for %s started container in %.0fms",
        running.container_name,
        (time.perf_counter() - started) * 1000,
    )
    return running


def normalize_runtime_session_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    runtime_mode = str(normalized.get("runtime_mode") or "").strip()
    if not runtime_mode:
        raise RuntimeError("Runtime session payload is missing runtime_mode")
    normalized["runtime_mode"] = runtime_mode
    runtime_toolsets = normalized.get("runtime_toolsets")
    if isinstance(runtime_toolsets, list):
        normalized["runtime_toolsets"] = [str(item).strip() for item in runtime_toolsets if str(item).strip()]
    else:
        raise RuntimeError("Runtime session payload is missing runtime_toolsets")
    return normalized


def get_product_runtime_session(record: ProductRuntimeRecord) -> dict[str, Any]:
    started = time.perf_counter()
    response = httpx.get(
        f"{runtime_base_url(record)}/runtime/session",
        timeout=10.0,
        headers={"X-Hermes-Product-Runtime-Token": record.auth_token},
    )
    response.raise_for_status()
    payload = normalize_runtime_session_payload(response.json())
    logger.info(
        "product_runtime session fetch for %s completed in %.0fms",
        record.container_name,
        (time.perf_counter() - started) * 1000,
    )
    return ProductRuntimeSession.model_validate(payload).model_dump(mode="json")


def stream_product_runtime_turn(record: ProductRuntimeRecord, user_message: str) -> Iterator[str]:
    message = user_message.strip()
    if not message:
        raise ValueError("User message must not be empty")
    with httpx.Client(timeout=120.0) as client:
        with client.stream(
            "POST",
            f"{runtime_base_url(record)}/runtime/turn/stream",
            json=ProductRuntimeTurnRequest(user_message=message).model_dump(),
            headers={"Accept": "text/event-stream", "X-Hermes-Product-Runtime-Token": record.auth_token},
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_text():
                yield chunk


def stop_product_runtime_turn(record: ProductRuntimeRecord) -> bool:
    response = httpx.post(
        f"{runtime_base_url(record)}/runtime/turn/stop",
        timeout=10.0,
        headers={"X-Hermes-Product-Runtime-Token": record.auth_token},
    )
    response.raise_for_status()
    payload = response.json()
    return bool(payload.get("stopped"))


def delete_product_runtime(user_id: str, *, config: dict[str, Any] | None = None) -> None:
    from hermes_cli.product_runtime_staging import load_runtime_record

    product_config = config or load_product_config()
    record = load_runtime_record(user_id, config=product_config)
    if record is not None:
        remove_container_if_exists(record.container_name)
        runtime_root = Path(record.runtime_root)
        if runtime_root.exists():
            shutil.rmtree(runtime_root)
