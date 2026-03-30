from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import yaml


def user_id(user: dict[str, Any]) -> str:
    stable_id = str(user.get("sub") or user.get("id") or "").strip()
    if not stable_id:
        raise ValueError("Signed-in user is missing a stable user identifier")
    return stable_id


def legacy_user_ids(user: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for value in (user.get("preferred_username"), user.get("username")):
        normalized = str(value or "").strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def runtime_key(user_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", user_id).strip("._-")
    if not normalized:
        normalized = "user"
    normalized = normalized[:48]
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
    return f"{normalized}-{digest}"


def product_runtime_session_id(user_id: str) -> str:
    normalized_runtime_key = runtime_key(user_id).replace("-", "_")
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
    return f"product_{normalized_runtime_key}_{digest}"


def product_storage_root(hooks: Any, config: dict[str, Any]) -> Path:
    return hooks.get_hermes_home() / str(config.get("storage", {}).get("root", "product"))


def product_users_root(hooks: Any, config: dict[str, Any]) -> Path:
    return hooks.get_hermes_home() / str(config.get("storage", {}).get("users_root", "product/users"))


def runtime_root(hooks: Any, config: dict[str, Any], user_id: str) -> Path:
    return hooks._product_users_root(config) / hooks._runtime_key(user_id) / "runtime"


def user_storage_root(hooks: Any, config: dict[str, Any], user_id: str) -> Path:
    return hooks._product_users_root(config) / hooks._runtime_key(user_id)


def workspace_root(hooks: Any, config: dict[str, Any], user_id: str) -> Path:
    return hooks._product_users_root(config) / hooks._runtime_key(user_id) / "workspace"


def hermes_home(hooks: Any, config: dict[str, Any], user_id: str) -> Path:
    return hooks._runtime_root(config, user_id) / "hermes"


def manifest_path(hooks: Any, config: dict[str, Any], user_id: str) -> Path:
    return hooks._runtime_root(config, user_id) / "launch-spec.json"


def env_path(hooks: Any, config: dict[str, Any], user_id: str) -> Path:
    return hooks._runtime_root(config, user_id) / "runtime.env"


def runtime_config_path(hooks: Any, config: dict[str, Any], user_id: str) -> Path:
    return hooks._hermes_home(config, user_id) / "config.yaml"


def runtime_toolsets(hooks: Any, config: dict[str, Any]) -> list[str]:
    _ = config
    try:
        return hooks.resolve_hermes_runtime_toolsets()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def runtime_port_range(hooks: Any, config: dict[str, Any]) -> tuple[int, int]:
    runtime_config = config.get("runtime", {})
    start = int(runtime_config.get("host_port_start", 18091))
    end = int(runtime_config.get("host_port_end", 18150))
    return start, end


def runtime_image(config: dict[str, Any]) -> str:
    runtime_config = config.get("runtime", {})
    image = str(runtime_config.get("image", "")).strip()
    if not image:
        raise RuntimeError("product runtime.image must be configured")
    return image


def runtime_binary(config: dict[str, Any]) -> str:
    runtime_config = config.get("runtime", {})
    runtime = str(runtime_config.get("isolation_runtime", "")).strip()
    if not runtime:
        raise RuntimeError("product runtime.isolation_runtime must be configured")
    return runtime


def runtime_internal_port(config: dict[str, Any]) -> int:
    runtime_port = config.get("runtime", {}).get("internal_port")
    if runtime_port is None:
        raise RuntimeError("product runtime.internal_port must be configured")
    return int(runtime_port)


def resolve_runtime_model_base_url(hooks: Any, config: dict[str, Any], base_url: str) -> str:
    normalized = str(base_url or "").strip()
    if not normalized:
        return normalized
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        return normalized
    hostname = (parsed.hostname or "").strip().lower()
    if hostname not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        return normalized.rstrip("/")
    replacement_host = hooks.runtime_host_access_host(config)
    netloc = replacement_host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    rewritten = parsed._replace(netloc=netloc)
    return urlunparse(rewritten).rstrip("/")


def resolve_runtime_port(hooks: Any, config: dict[str, Any], user_id: str) -> int:
    existing = hooks.load_runtime_record(user_id, config=config)
    if existing is not None:
        return existing.runtime_port
    used_ports: set[int] = set()
    users_root = hooks._product_users_root(config)
    if users_root.exists():
        for manifest in users_root.glob("*/runtime/launch-spec.json"):
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                used_ports.add(int(payload["runtime_port"]))
            except Exception:
                continue
    start, end = hooks._runtime_port_range(config)
    for port in range(start, end + 1):
        if port not in used_ports:
            return port
    raise RuntimeError("No runtime ports are available in the configured product range")


def write_runtime_record(hooks: Any, record: Any) -> None:
    manifest_path = Path(record.manifest_file)
    manifest_path.write_text(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hooks._secure_runtime_file(manifest_path)


def write_runtime_text_if_changed(path: Path, content: str) -> bool:
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    if existing == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def write_runtime_cli_config(hooks: Any, config: dict[str, Any], user_id: str, *, base_url: str, model: str) -> None:
    _ = config
    model_cfg = hooks.resolve_hermes_model_config()
    config_path = hooks._runtime_config_path(config, user_id)
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
    hooks._write_runtime_text_if_changed(config_path, yaml.safe_dump(runtime_config, sort_keys=False))
    hooks._secure_container_readable_file(config_path)


def resolve_runtime_api_key(hooks: Any, model_cfg: dict[str, Any]) -> str:
    direct_key = str(model_cfg.get("api_key") or model_cfg.get("api") or "").strip()
    if direct_key:
        return direct_key

    configured_provider = str(model_cfg.get("provider") or "").strip().lower()
    configured_base_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
    if configured_provider == "custom" and configured_base_url:
        custom_providers = hooks.load_product_config().get("custom_providers")
        if not isinstance(custom_providers, list):
            from hermes_cli.config import load_config

            custom_providers = load_config().get("custom_providers")
        if isinstance(custom_providers, list):
            for entry in custom_providers:
                if not isinstance(entry, dict):
                    continue
                entry_base_url = str(entry.get("base_url") or "").strip().rstrip("/")
                entry_key = str(entry.get("api_key") or "").strip()
                if entry_base_url == configured_base_url and entry_key:
                    return entry_key

    return str(hooks.get_env_value("OPENAI_API_KEY") or "").strip()


def resolve_runtime_launch_settings(hooks: Any, product_config: dict[str, Any]) -> Any:
    try:
        model_cfg = hooks.resolve_hermes_model_config()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        route = hooks.resolve_runtime_provider(requested=str(model_cfg.get("provider") or "").strip() or None)
    except Exception as exc:
        detail = hooks.format_runtime_provider_error(exc).strip()
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
    api_key = str(route.get("api_key") or "").strip()
    if not api_key:
        api_key = hooks._resolve_runtime_api_key(model_cfg)
    if not api_key:
        api_key = "product-runtime"
    if not model:
        raise RuntimeError("Hermes model.default must be configured. Run 'hermes setup model'.")
    if not base_url:
        raise RuntimeError("Hermes runtime base URL is not available. Run 'hermes setup model'.")

    return hooks.ProductRuntimeLaunchSettings(
        model=model,
        provider=provider,
        base_url=hooks._resolve_runtime_model_base_url(product_config, base_url),
        api_mode=api_mode,
        api_key=api_key,
        toolsets=hooks._runtime_toolsets(product_config),
    )


def runtime_environment(hooks: Any, settings: Any, *, session_id: str, auth_token: str, internal_port: int) -> dict[str, str]:
    return {
        "HERMES_HOME": "/srv/hermes",
        "HERMES_WRITE_SAFE_ROOT": hooks._RUNTIME_WORKSPACE_PATH,
        "TERMINAL_CWD": hooks._RUNTIME_WORKSPACE_PATH,
        "OPENAI_BASE_URL": settings.base_url,
        "OPENAI_API_KEY": settings.api_key,
        "HERMES_PRODUCT_RUNTIME_MODE": "product",
        "HERMES_RUNTIME_HOST": "0.0.0.0",
        "HERMES_RUNTIME_PORT": str(internal_port),
        "HERMES_PRODUCT_SESSION_ID": session_id,
        "HERMES_PRODUCT_TOOLSETS": ",".join(settings.toolsets),
        "HERMES_PRODUCT_PROVIDER": settings.provider,
        "HERMES_PRODUCT_API_MODE": settings.api_mode,
        "HERMES_PRODUCT_MODEL": settings.model,
        "HERMES_PRODUCT_RUNTIME_TOKEN": auth_token,
    }


def write_runtime_env_file(hooks: Any, path: Path, env: dict[str, str]) -> None:
    invalid_keys: list[str] = []
    for key, value in env.items():
        if any(char in value for char in ("\n", "\r", "\x00")):
            invalid_keys.append(key)
    if invalid_keys:
        joined = ", ".join(sorted(invalid_keys))
        raise RuntimeError(f"Runtime env contains unsupported newline or NUL characters: {joined}")
    path.write_text("".join(f"{key}={value}\n" for key, value in sorted(env.items())), encoding="utf-8")
    hooks._secure_runtime_file(path)


def migrate_legacy_runtime(hooks: Any, user: dict[str, Any], product_config: dict[str, Any], stable_user_id: str) -> Any | None:
    stable_root = hooks._user_storage_root(product_config, stable_user_id)
    if stable_root.exists():
        return None
    for legacy_user_id in hooks._legacy_user_ids(user):
        if legacy_user_id == stable_user_id:
            continue
        legacy_record = hooks.load_runtime_record(legacy_user_id, config=product_config)
        if legacy_record is None:
            continue
        legacy_root = hooks._user_storage_root(product_config, legacy_user_id)
        if not legacy_root.exists():
            continue
        hooks._remove_container_if_exists(legacy_record.container_name)
        stable_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_root), str(stable_root))
        migrated = legacy_record.model_copy(
            update={
                "user_id": stable_user_id,
                "runtime_key": hooks._runtime_key(stable_user_id),
                "container_name": f"hermes-product-runtime-{hooks._runtime_key(stable_user_id)}",
                "runtime_root": str(hooks._runtime_root(product_config, stable_user_id)),
                "hermes_home": str(hooks._hermes_home(product_config, stable_user_id)),
                "workspace_root": str(hooks._workspace_root(product_config, stable_user_id)),
                "env_file": str(hooks._env_path(product_config, stable_user_id)),
                "manifest_file": str(hooks._manifest_path(product_config, stable_user_id)),
                "display_name": str(user.get("name") or user.get("preferred_username") or "").strip() or legacy_record.display_name,
                "status": "staged",
            }
        )
        hooks._write_runtime_record(migrated)
        return migrated
    return None


def stage_product_runtime(hooks: Any, user: dict[str, Any], *, config: dict[str, Any] | None = None) -> Any:
    product_config = config or hooks.load_product_config()
    hooks.ensure_hermes_home()
    stable_user_id = hooks._user_id(user)
    existing = hooks.load_runtime_record(stable_user_id, config=product_config) or hooks._migrate_legacy_runtime(user, product_config, stable_user_id)
    runtime_root = hooks._runtime_root(product_config, stable_user_id)
    hermes_home = hooks._hermes_home(product_config, stable_user_id)
    workspace_root = hooks._workspace_root(product_config, stable_user_id)
    for path in (hooks._product_storage_root(product_config), hooks._product_users_root(product_config), runtime_root):
        path.mkdir(parents=True, exist_ok=True)
        hooks._secure_runtime_dir(path)
    for path in (hermes_home, hermes_home / "memories", workspace_root):
        path.mkdir(parents=True, exist_ok=True)
        hooks._secure_runtime_writable_dir(path)

    soul_path = hermes_home / "SOUL.md"
    hooks._write_runtime_text_if_changed(soul_path, hooks.render_product_soul(product_config))
    hooks._secure_container_readable_file(soul_path)

    launch_settings = hooks._resolve_runtime_launch_settings(product_config)
    hooks._write_runtime_cli_config(product_config, stable_user_id, base_url=launch_settings.base_url, model=launch_settings.model)
    session_id = existing.session_id if existing is not None else hooks.product_runtime_session_id(stable_user_id)
    runtime_port = existing.runtime_port if existing is not None else hooks._resolve_runtime_port(product_config, stable_user_id)
    stable_runtime_key = existing.runtime_key if existing is not None and existing.runtime_key else hooks._runtime_key(stable_user_id)
    container_name = existing.container_name if existing is not None else f"hermes-product-runtime-{stable_runtime_key}"
    auth_token = existing.auth_token if existing is not None and existing.auth_token else secrets.token_urlsafe(32)

    env_path = hooks._env_path(product_config, stable_user_id)
    hooks._write_runtime_env_file(
        env_path,
        hooks._runtime_environment(
            launch_settings,
            session_id=session_id,
            auth_token=auth_token,
            internal_port=hooks._runtime_internal_port(product_config),
        ),
    )

    record = hooks.ProductRuntimeRecord(
        user_id=stable_user_id,
        runtime_key=stable_runtime_key,
        display_name=str(user.get("name") or user.get("preferred_username") or "").strip() or None,
        session_id=session_id,
        container_name=container_name,
        runtime=hooks._runtime_binary(product_config),
        runtime_port=runtime_port,
        runtime_root=str(runtime_root),
        hermes_home=str(hermes_home),
        workspace_root=str(workspace_root),
        env_file=str(env_path),
        manifest_file=str(hooks._manifest_path(product_config, stable_user_id)),
        auth_token=auth_token,
        status="staged",
    )
    hooks._write_runtime_record(record)
    return record
