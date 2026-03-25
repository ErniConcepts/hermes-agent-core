from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from pydantic import BaseModel
import yaml

from hermes_cli.config import _secure_dir, _secure_file, ensure_hermes_home, get_hermes_home
from hermes_cli.product_config import (
    load_product_config,
    resolve_hermes_model_config,
    resolve_hermes_runtime_toolsets,
    runtime_host_access_host,
)
from hermes_cli.product_identity import render_product_soul
from hermes_cli.runtime_provider import format_runtime_provider_error, resolve_runtime_provider

logger = logging.getLogger(__name__)
_RUNTIME_HEALTH_TTL_SECONDS = 10.0
_RUNTIME_HEALTH_CACHE: dict[str, float] = {}
_RUNTIME_WORKSPACE_PATH = "/workspace"


def _secure_runtime_dir(path: Path) -> None:
    try:
        path.chmod(0o777)
    except (OSError, NotImplementedError):
        pass


def _secure_runtime_file(path: Path) -> None:
    try:
        if path.exists():
            path.chmod(0o644)
    except (OSError, NotImplementedError):
        pass

class ProductRuntimeRecord(BaseModel):
    user_id: str
    runtime_key: str | None = None
    display_name: str | None = None
    session_id: str
    container_name: str
    runtime: str
    runtime_port: int
    runtime_root: str
    hermes_home: str
    workspace_root: str
    env_file: str
    manifest_file: str
    auth_token: str | None = None
    status: str = "staged"


class ProductRuntimeSession(BaseModel):
    session_id: str
    messages: list[dict[str, Any]]
    runtime_mode: str
    runtime_toolsets: list[str]


class ProductRuntimeTurnRequest(BaseModel):
    user_message: str


class ProductRuntimeEvent(BaseModel):
    event: str
    payload: dict[str, Any]


def _user_id(user: dict[str, Any]) -> str:
    username = str(user.get("preferred_username") or user.get("sub") or "").strip()
    if not username:
        raise ValueError("Signed-in user is missing a usable username")
    return username


def _runtime_key(user_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", user_id).strip("._-")
    if not normalized:
        normalized = "user"
    normalized = normalized[:48]
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
    return f"{normalized}-{digest}"


def product_runtime_session_id(user_id: str) -> str:
    runtime_key = _runtime_key(user_id).replace("-", "_")
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
    return f"product_{runtime_key}_{digest}"


def _product_storage_root(config: dict[str, Any]) -> Path:
    return get_hermes_home() / str(config.get("storage", {}).get("root", "product"))


def _product_users_root(config: dict[str, Any]) -> Path:
    return get_hermes_home() / str(config.get("storage", {}).get("users_root", "product/users"))


def _runtime_root(config: dict[str, Any], user_id: str) -> Path:
    return _product_users_root(config) / _runtime_key(user_id) / "runtime"


def _workspace_root(config: dict[str, Any], user_id: str) -> Path:
    return _product_users_root(config) / _runtime_key(user_id) / "workspace"


def _hermes_home(config: dict[str, Any], user_id: str) -> Path:
    return _runtime_root(config, user_id) / "hermes"


def _manifest_path(config: dict[str, Any], user_id: str) -> Path:
    return _runtime_root(config, user_id) / "launch-spec.json"


def _env_path(config: dict[str, Any], user_id: str) -> Path:
    return _runtime_root(config, user_id) / "runtime.env"


def _runtime_config_path(config: dict[str, Any], user_id: str) -> Path:
    return _hermes_home(config, user_id) / "config.yaml"


def _runtime_toolsets(config: dict[str, Any]) -> list[str]:
    _ = config
    try:
        return resolve_hermes_runtime_toolsets()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def _runtime_port_range(config: dict[str, Any]) -> tuple[int, int]:
    runtime_config = config.get("runtime", {})
    start = int(runtime_config.get("host_port_start", 18091))
    end = int(runtime_config.get("host_port_end", 18150))
    return start, end


def _runtime_image(config: dict[str, Any]) -> str:
    runtime_config = config.get("runtime", {})
    image = str(runtime_config.get("image", "")).strip()
    if not image:
        raise RuntimeError("product runtime.image must be configured")
    return image


def _runtime_binary(config: dict[str, Any]) -> str:
    runtime_config = config.get("runtime", {})
    runtime = str(runtime_config.get("isolation_runtime", "")).strip()
    if not runtime:
        raise RuntimeError("product runtime.isolation_runtime must be configured")
    return runtime


def _runtime_internal_port(config: dict[str, Any]) -> int:
    runtime_port = config.get("runtime", {}).get("internal_port")
    if runtime_port is None:
        raise RuntimeError("product runtime.internal_port must be configured")
    return int(runtime_port)


def _resolve_runtime_model_base_url(config: dict[str, Any], base_url: str) -> str:
    normalized = str(base_url or "").strip()
    if not normalized:
        return normalized
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        return normalized
    hostname = (parsed.hostname or "").strip().lower()
    if hostname not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        return normalized.rstrip("/")
    replacement_host = runtime_host_access_host(config)
    netloc = replacement_host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    rewritten = parsed._replace(netloc=netloc)
    return urlunparse(rewritten).rstrip("/")


def _resolve_runtime_port(config: dict[str, Any], user_id: str) -> int:
    existing = load_runtime_record(user_id, config=config)
    if existing is not None:
        return existing.runtime_port
    used_ports: set[int] = set()
    users_root = _product_users_root(config)
    if users_root.exists():
        for manifest in users_root.glob("*/runtime/launch-spec.json"):
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                used_ports.add(int(payload["runtime_port"]))
            except Exception:
                continue
    start, end = _runtime_port_range(config)
    for port in range(start, end + 1):
        if port not in used_ports:
            return port
    raise RuntimeError("No runtime ports are available in the configured product range")


def load_runtime_record(user_id: str, *, config: dict[str, Any] | None = None) -> ProductRuntimeRecord | None:
    product_config = config or load_product_config()
    manifest_path = _manifest_path(product_config, user_id)
    if not manifest_path.exists():
        return None
    return ProductRuntimeRecord.model_validate_json(manifest_path.read_text(encoding="utf-8"))


def _write_runtime_record(record: ProductRuntimeRecord) -> None:
    manifest_path = Path(record.manifest_file)
    manifest_path.write_text(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _secure_file(manifest_path)


def _write_runtime_cli_config(config: dict[str, Any], user_id: str, *, base_url: str, model: str) -> None:
    _ = config
    model_cfg = resolve_hermes_model_config()
    config_path = _runtime_config_path(config, user_id)
    context_length = model_cfg.get("context_length")
    try:
        normalized_context_length = int(context_length) if context_length is not None else None
    except (TypeError, ValueError):
        normalized_context_length = None

    if normalized_context_length is None or normalized_context_length <= 0:
        if config_path.exists():
            config_path.unlink()
        return

    runtime_config = {
        "model": {
            "default": model,
            "base_url": base_url,
            "provider": str(model_cfg.get("provider") or "").strip() or "custom",
            "context_length": normalized_context_length,
        }
    }
    config_path.write_text(yaml.safe_dump(runtime_config, sort_keys=False), encoding="utf-8")
    _secure_runtime_file(config_path)


def stage_product_runtime(user: dict[str, Any], *, config: dict[str, Any] | None = None) -> ProductRuntimeRecord:
    product_config = config or load_product_config()
    ensure_hermes_home()
    user_id = _user_id(user)
    existing = load_runtime_record(user_id, config=product_config)
    runtime_root = _runtime_root(product_config, user_id)
    hermes_home = _hermes_home(product_config, user_id)
    workspace_root = _workspace_root(product_config, user_id)
    for path in (
        _product_storage_root(product_config),
        _product_users_root(product_config),
        runtime_root,
        hermes_home,
        hermes_home / "memories",
        workspace_root,
    ):
        path.mkdir(parents=True, exist_ok=True)
        _secure_runtime_dir(path)

    soul_path = hermes_home / "SOUL.md"
    soul_path.write_text(render_product_soul(product_config), encoding="utf-8")
    _secure_runtime_file(soul_path)

    try:
        model_cfg = resolve_hermes_model_config()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        route = resolve_runtime_provider(requested=str(model_cfg.get("provider") or "").strip() or None)
    except Exception as exc:
        detail = format_runtime_provider_error(exc).strip()
        message = "Hermes model/provider is not ready. Run 'hermes setup model'."
        if detail:
            message = f"{message} {detail}"
        raise RuntimeError(message) from exc

    model = str(model_cfg.get("default") or "").strip()
    configured_provider = str(model_cfg.get("provider") or "").strip().lower()
    provider = str(route.get("provider") or configured_provider or "").strip().lower() or "custom"
    if configured_provider == "custom":
        provider = "custom"
    base_url = str(route.get("base_url") or "").strip()
    api_mode = str(route.get("api_mode") or model_cfg.get("api_mode") or "chat_completions").strip() or "chat_completions"
    api_key = str(route.get("api_key") or "").strip() or "product-runtime"
    if not model:
        raise RuntimeError("Hermes model.default must be configured. Run 'hermes setup model'.")
    if not base_url:
        raise RuntimeError("Hermes runtime base URL is not available. Run 'hermes setup model'.")
    base_url = _resolve_runtime_model_base_url(product_config, base_url)
    _write_runtime_cli_config(product_config, user_id, base_url=base_url, model=model)
    session_id = existing.session_id if existing is not None else product_runtime_session_id(user_id)
    runtime_port = existing.runtime_port if existing is not None else _resolve_runtime_port(product_config, user_id)
    runtime_key = existing.runtime_key if existing is not None and existing.runtime_key else _runtime_key(user_id)
    container_name = existing.container_name if existing is not None else f"hermes-product-runtime-{runtime_key}"
    toolsets = _runtime_toolsets(product_config)
    auth_token = existing.auth_token if existing is not None and existing.auth_token else secrets.token_urlsafe(32)

    env = {
        "HERMES_HOME": "/srv/hermes",
        "HERMES_WRITE_SAFE_ROOT": _RUNTIME_WORKSPACE_PATH,
        "TERMINAL_CWD": _RUNTIME_WORKSPACE_PATH,
        "OPENAI_BASE_URL": base_url,
        "OPENAI_API_KEY": api_key,
        "HERMES_PRODUCT_RUNTIME_MODE": "product",
        "HERMES_RUNTIME_HOST": "0.0.0.0",
        "HERMES_RUNTIME_PORT": str(_runtime_internal_port(product_config)),
        "HERMES_PRODUCT_SESSION_ID": session_id,
        "HERMES_PRODUCT_TOOLSETS": ",".join(toolsets),
        "HERMES_PRODUCT_PROVIDER": provider,
        "HERMES_PRODUCT_API_MODE": api_mode,
        "HERMES_PRODUCT_MODEL": model,
        "HERMES_PRODUCT_RUNTIME_TOKEN": auth_token,
    }
    env_path = _env_path(product_config, user_id)
    env_path.write_text("".join(f"{key}={value}\n" for key, value in sorted(env.items())), encoding="utf-8")
    _secure_file(env_path)

    record = ProductRuntimeRecord(
        user_id=user_id,
        runtime_key=runtime_key,
        display_name=str(user.get("name") or user.get("preferred_username") or "").strip() or None,
        session_id=session_id,
        container_name=container_name,
        runtime=_runtime_binary(product_config),
        runtime_port=runtime_port,
        runtime_root=str(runtime_root),
        hermes_home=str(hermes_home),
        workspace_root=str(workspace_root),
        env_file=str(env_path),
        manifest_file=str(_manifest_path(product_config, user_id)),
        auth_token=auth_token,
        status="staged",
    )
    _write_runtime_record(record)
    return record


def _docker_run_command(record: ProductRuntimeRecord, config: dict[str, Any]) -> list[str]:
    internal_port = _runtime_internal_port(config)
    hermes_home = Path(record.hermes_home)
    mounts = [
        f"type=bind,src={hermes_home.as_posix()},dst=/srv/hermes",
        f"type=bind,src={(hermes_home / 'SOUL.md').as_posix()},dst=/srv/hermes/SOUL.md,readonly",
        f"type=bind,src={Path(record.workspace_root).as_posix()},dst={_RUNTIME_WORKSPACE_PATH}",
    ]
    runtime_config = hermes_home / "config.yaml"
    if runtime_config.exists():
        mounts.insert(
            2,
            f"type=bind,src={runtime_config.as_posix()},dst=/srv/hermes/config.yaml,readonly",
        )
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
        "/tmp:rw,noexec,nosuid,size=64m",
        "--tmpfs",
        "/var/tmp:rw,noexec,nosuid,size=32m",
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
        _runtime_image(config),
        "python",
        "-m",
        "hermes_cli.product_runtime_service",
    ]
    label_index = command.index("--label")
    mount_args: list[str] = []
    for mount in mounts:
        mount_args.extend(["--mount", mount])
    command[label_index:label_index] = mount_args
    return command


def _docker_inspect_state(container_name: str) -> dict[str, Any] | None:
    result = subprocess.run(
        ["docker", "inspect", container_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    payload = json.loads(result.stdout)
    if not payload:
        return None
    return payload[0]


def _container_env_map(container_state: dict[str, Any] | None) -> dict[str, str]:
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


def _runtime_launch_env(record: ProductRuntimeRecord) -> dict[str, str]:
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


def _running_container_matches_record(record: ProductRuntimeRecord, container_state: dict[str, Any] | None) -> bool:
    container_env = _container_env_map(container_state)
    expected_env = _runtime_launch_env(record)
    if not container_env or not expected_env:
        return False
    keys_to_match = {
        "HERMES_PRODUCT_PROVIDER",
        "HERMES_PRODUCT_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "HERMES_PRODUCT_TOOLSETS",
        "HERMES_PRODUCT_API_MODE",
        "HERMES_PRODUCT_RUNTIME_MODE",
    }
    for key in keys_to_match:
        if container_env.get(key, "") != expected_env.get(key, ""):
            return False
    return True


def _remove_container_if_exists(container_name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        text=True,
        check=False,
    )


def _wait_for_runtime_health(
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


def ensure_product_runtime(user: dict[str, Any], *, config: dict[str, Any] | None = None) -> ProductRuntimeRecord:
    started = time.perf_counter()
    product_config = config or load_product_config()
    record = stage_product_runtime(user, config=product_config)
    container_state = _docker_inspect_state(record.container_name)
    if container_state and bool(container_state.get("State", {}).get("Running")):
        if not _running_container_matches_record(record, container_state):
            _remove_container_if_exists(record.container_name)
            container_state = None
            _RUNTIME_HEALTH_CACHE.pop(record.container_name, None)
        else:
            running = ProductRuntimeRecord(**{**record.model_dump(), "status": "running"})
            last_healthy_at = _RUNTIME_HEALTH_CACHE.get(running.container_name, 0.0)
            if time.monotonic() - last_healthy_at > _RUNTIME_HEALTH_TTL_SECONDS:
                _wait_for_runtime_health(running)
            logger.info(
                "product_runtime ensure for %s reused running container in %.0fms",
                running.container_name,
                (time.perf_counter() - started) * 1000,
            )
            return running

    _remove_container_if_exists(record.container_name)
    result = subprocess.run(
        _docker_run_command(record, product_config),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "docker run failed")
    running = ProductRuntimeRecord(**{**record.model_dump(), "status": "running"})
    _wait_for_runtime_health(running)
    logger.info(
        "product_runtime ensure for %s started container in %.0fms",
        running.container_name,
        (time.perf_counter() - started) * 1000,
    )
    return running


def runtime_base_url(record: ProductRuntimeRecord) -> str:
    return f"http://127.0.0.1:{record.runtime_port}"


def _normalize_runtime_session_payload(payload: dict[str, Any]) -> dict[str, Any]:
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


def get_product_runtime_session(user: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    record = ensure_product_runtime(user, config=config)
    response = httpx.get(
        f"{runtime_base_url(record)}/runtime/session",
        timeout=10.0,
        headers={"X-Hermes-Product-Runtime-Token": record.auth_token},
    )
    response.raise_for_status()
    payload = _normalize_runtime_session_payload(response.json())
    logger.info(
        "product_runtime session fetch for %s completed in %.0fms",
        record.container_name,
        (time.perf_counter() - started) * 1000,
    )
    return ProductRuntimeSession.model_validate(payload).model_dump(mode="json")


def stream_product_runtime_turn(
    user: dict[str, Any],
    user_message: str,
    *,
    config: dict[str, Any] | None = None,
) -> Iterator[str]:
    message = user_message.strip()
    if not message:
        raise ValueError("User message must not be empty")
    record = ensure_product_runtime(user, config=config)
    with httpx.Client(timeout=120.0) as client:
        with client.stream(
            "POST",
            f"{runtime_base_url(record)}/runtime/turn/stream",
            json=ProductRuntimeTurnRequest(user_message=message).model_dump(),
            headers={
                "Accept": "text/event-stream",
                "X-Hermes-Product-Runtime-Token": record.auth_token,
            },
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_text():
                yield chunk


def delete_product_runtime(user_id: str, *, config: dict[str, Any] | None = None) -> None:
    product_config = config or load_product_config()
    record = load_runtime_record(user_id, config=product_config)
    if record is not None:
        _remove_container_if_exists(record.container_name)
        runtime_root = Path(record.runtime_root)
        if runtime_root.exists():
            shutil.rmtree(runtime_root)
