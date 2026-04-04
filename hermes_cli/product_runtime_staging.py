from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from hermes_cli.config import ensure_hermes_home, get_env_value, get_hermes_home, load_config
from hermes_cli.product_config import (
    load_product_config,
    resolve_hermes_model_config,
    resolve_hermes_runtime_toolsets,
    runtime_host_access_host,
)
from hermes_cli.product_runtime_common import (
    ProductRuntimeLaunchSettings,
    ProductRuntimeRecord,
    _RUNTIME_WORKSPACE_PATH,
    secure_container_readable_file,
    secure_runtime_dir,
    secure_runtime_file,
    secure_runtime_writable_dir,
)
from hermes_cli.product_runtime_template import (
    runtime_profile_name,
    runtime_template_root,
    stage_runtime_template,
)
from hermes_cli.runtime_provider import format_runtime_provider_error, resolve_runtime_provider

try:
    import fcntl
except Exception:
    fcntl = None
try:
    import msvcrt
except Exception:
    msvcrt = None

_MAX_RUNTIME_ENV_VALUE_LENGTH = 8192
_RUNTIME_LOCK_TIMEOUT_SECONDS = 15.0


def user_id(user: dict[str, object]) -> str:
    stable_id = str(user.get("sub") or user.get("id") or "").strip()
    if not stable_id:
        raise ValueError("Signed-in user is missing a stable user identifier")
    return stable_id


def legacy_user_ids(user: dict[str, object]) -> list[str]:
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


def product_storage_root(config: dict[str, object]) -> Path:
    return get_hermes_home() / str(config.get("storage", {}).get("root", "product"))


def product_users_root(config: dict[str, object]) -> Path:
    return get_hermes_home() / str(config.get("storage", {}).get("users_root", "product/users"))


def runtime_lock_path(config: dict[str, object]) -> Path:
    return product_storage_root(config) / ".runtime-staging.lock"


def runtime_root(config: dict[str, object], stable_user_id: str) -> Path:
    return product_users_root(config) / runtime_key(stable_user_id) / "runtime"


def user_install_root(config: dict[str, object], stable_user_id: str) -> Path:
    return product_users_root(config) / runtime_key(stable_user_id) / "install"


def user_storage_root(config: dict[str, object], stable_user_id: str) -> Path:
    return product_users_root(config) / runtime_key(stable_user_id)


def workspace_root(config: dict[str, object], stable_user_id: str) -> Path:
    return product_users_root(config) / runtime_key(stable_user_id) / "workspace"


def hermes_home(config: dict[str, object], stable_user_id: str) -> Path:
    return user_install_root(config, stable_user_id) / "hermes-home"


def profile_root(config: dict[str, object], stable_user_id: str) -> Path:
    return hermes_home(config, stable_user_id) / "profiles" / runtime_profile_name()


def manifest_path(config: dict[str, object], stable_user_id: str) -> Path:
    return runtime_root(config, stable_user_id) / "launch-spec.json"


def env_path(config: dict[str, object], stable_user_id: str) -> Path:
    return runtime_root(config, stable_user_id) / "runtime.env"


def runtime_config_path(config: dict[str, object], stable_user_id: str) -> Path:
    return hermes_home(config, stable_user_id) / "config.yaml"


@contextmanager
def runtime_staging_lock(config: dict[str, object]):
    lock_path = runtime_lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None and msvcrt is None:
        raise RuntimeError("Runtime staging lock is not available on this platform")
    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    started = time.monotonic()
    with lock_path.open("r+" if msvcrt else "a+") as lock_file:
        while True:
            try:
                if fcntl:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() - started >= _RUNTIME_LOCK_TIMEOUT_SECONDS:
                    raise RuntimeError("Timed out waiting for the runtime staging lock")
                time.sleep(0.05)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def runtime_toolsets(config: dict[str, object]) -> list[str]:
    _ = config
    try:
        return resolve_hermes_runtime_toolsets()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def runtime_port_range(config: dict[str, object]) -> tuple[int, int]:
    runtime_config = config.get("runtime", {})
    start = int(runtime_config.get("host_port_start", 18091))
    end = int(runtime_config.get("host_port_end", 18150))
    return start, end


def runtime_image(config: dict[str, object]) -> str:
    runtime_config = config.get("runtime", {})
    image = str(runtime_config.get("image", "")).strip()
    if not image:
        raise RuntimeError("product runtime.image must be configured")
    return image


def runtime_binary(config: dict[str, object]) -> str:
    runtime_config = config.get("runtime", {})
    runtime = str(runtime_config.get("isolation_runtime", "")).strip()
    if not runtime:
        raise RuntimeError("product runtime.isolation_runtime must be configured")
    return runtime


def runtime_internal_port(config: dict[str, object]) -> int:
    runtime_port = config.get("runtime", {}).get("internal_port")
    if runtime_port is None:
        raise RuntimeError("product runtime.internal_port must be configured")
    return int(runtime_port)


def resolve_runtime_model_base_url(config: dict[str, object], base_url: str) -> str:
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


def load_runtime_record(user_id: str, *, config: dict[str, object] | None = None) -> ProductRuntimeRecord | None:
    product_config = config or load_product_config()
    path = manifest_path(product_config, user_id)
    if not path.exists():
        return None
    return ProductRuntimeRecord.model_validate_json(path.read_text(encoding="utf-8"))


def resolve_runtime_port(config: dict[str, object], stable_user_id: str) -> int:
    existing = load_runtime_record(stable_user_id, config=config)
    if existing is not None:
        return existing.runtime_port
    used_ports: set[int] = set()
    users_root = product_users_root(config)
    if users_root.exists():
        for candidate in users_root.glob("*/runtime/launch-spec.json"):
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                used_ports.add(int(payload["runtime_port"]))
            except Exception:
                continue
    start, end = runtime_port_range(config)
    for port in range(start, end + 1):
        if port not in used_ports:
            return port
    raise RuntimeError("No runtime ports are available in the configured product range")


def write_runtime_record(record: ProductRuntimeRecord) -> None:
    path = Path(record.manifest_file)
    path.write_text(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    secure_runtime_file(path)


def write_runtime_text_if_changed(path: Path, content: str) -> bool:
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    if existing == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def ensure_runtime_install(
    config: dict[str, object],
    stable_user_id: str,
    template_payload: dict[str, object],
) -> Path:
    install_root = user_install_root(config, stable_user_id)
    runtime_home = hermes_home(config, stable_user_id)
    runtime_profile_root = profile_root(config, stable_user_id)
    template_root = runtime_template_root(config)
    template_profile_root = template_root / "profiles" / runtime_profile_name()

    for path in (
        install_root,
        runtime_home,
        runtime_home / "profiles",
        runtime_profile_root,
        runtime_home / "memories",
        runtime_home / "sessions",
    ):
        path.mkdir(parents=True, exist_ok=True)
        secure_runtime_writable_dir(path)

    soul_text = (template_root / "SOUL.md").read_text(encoding="utf-8")
    write_runtime_text_if_changed(runtime_home / "SOUL.md", soul_text)
    secure_container_readable_file(runtime_home / "SOUL.md")
    write_runtime_text_if_changed(runtime_profile_root / "SOUL.md", soul_text)
    secure_container_readable_file(runtime_profile_root / "SOUL.md")

    template_config_path = template_root / "config.yaml"
    runtime_config_target = runtime_home / "config.yaml"
    profile_config_target = runtime_profile_root / "config.yaml"
    if template_config_path.exists():
        config_text = template_config_path.read_text(encoding="utf-8")
        write_runtime_text_if_changed(runtime_config_target, config_text)
        secure_container_readable_file(runtime_config_target)
        write_runtime_text_if_changed(profile_config_target, config_text)
        secure_container_readable_file(profile_config_target)
    else:
        for path in (runtime_config_target, profile_config_target):
            if path.exists():
                path.unlink()

    manifest_target = install_root / "template.json"
    write_runtime_text_if_changed(manifest_target, json.dumps(template_payload, indent=2, sort_keys=True) + "\n")
    secure_container_readable_file(manifest_target)
    return install_root


def write_runtime_cli_config(config: dict[str, object], stable_user_id: str, *, base_url: str, model: str) -> None:
    _ = (config, stable_user_id, base_url, model)
    return None


def resolve_runtime_api_key(model_cfg: dict[str, object]) -> str:
    direct_key = str(model_cfg.get("api_key") or model_cfg.get("api") or "").strip()
    if direct_key:
        return direct_key

    configured_provider = str(model_cfg.get("provider") or "").strip().lower()
    configured_base_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
    if configured_provider == "custom" and configured_base_url:
        custom_providers = load_product_config().get("custom_providers")
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

    return str(get_env_value("OPENAI_API_KEY") or "").strip()


def resolve_runtime_launch_settings(product_config: dict[str, object]) -> ProductRuntimeLaunchSettings:
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
    api_key = str(route.get("api_key") or "").strip() or resolve_runtime_api_key(model_cfg)
    if not api_key:
        api_key = "product-runtime"
    if not model:
        raise RuntimeError("Hermes model.default must be configured. Run 'hermes setup model'.")
    if not base_url:
        raise RuntimeError("Hermes runtime base URL is not available. Run 'hermes setup model'.")

    return ProductRuntimeLaunchSettings(
        model=model,
        provider=provider,
        base_url=resolve_runtime_model_base_url(product_config, base_url),
        api_mode=api_mode,
        api_key=api_key,
        toolsets=runtime_toolsets(product_config),
    )


def runtime_environment(
    settings: ProductRuntimeLaunchSettings,
    *,
    session_id: str,
    auth_token: str,
    internal_port: int,
    profile_name: str,
    template_version: str,
) -> dict[str, str]:
    return {
        "HERMES_HOME": "/srv/hermes",
        "PYTHONPATH": "/app",
        "HERMES_WRITE_SAFE_ROOT": _RUNTIME_WORKSPACE_PATH,
        "TERMINAL_CWD": _RUNTIME_WORKSPACE_PATH,
        "TMPDIR": f"{_RUNTIME_WORKSPACE_PATH}/.tmp",
        "TEMP": f"{_RUNTIME_WORKSPACE_PATH}/.tmp",
        "TMP": f"{_RUNTIME_WORKSPACE_PATH}/.tmp",
        "OPENAI_BASE_URL": settings.base_url,
        "OPENAI_API_KEY": settings.api_key,
        "HERMES_PRODUCT_RUNTIME_MODE": "product",
        "TIRITH_FAIL_OPEN": "false",
        "HERMES_RUNTIME_HOST": "0.0.0.0",
        "HERMES_RUNTIME_PORT": str(internal_port),
        "HERMES_PRODUCT_SESSION_ID": session_id,
        "HERMES_PRODUCT_TOOLSETS": ",".join(settings.toolsets),
        "HERMES_PRODUCT_PROVIDER": settings.provider,
        "HERMES_PRODUCT_API_MODE": settings.api_mode,
        "HERMES_PRODUCT_MODEL": settings.model,
        "HERMES_PRODUCT_PROFILE": profile_name,
        "HERMES_PRODUCT_TEMPLATE_VERSION": template_version,
        "HERMES_PRODUCT_RUNTIME_TOKEN": auth_token,
    }


def write_runtime_env_file(path: Path, env: dict[str, str]) -> None:
    invalid_keys = [key for key, value in env.items() if any(char in value for char in ("\n", "\r", "\x00"))]
    if invalid_keys:
        joined = ", ".join(sorted(invalid_keys))
        raise RuntimeError(f"Runtime env contains unsupported newline or NUL characters: {joined}")
    oversized_keys = [key for key, value in env.items() if len(value) > _MAX_RUNTIME_ENV_VALUE_LENGTH]
    if oversized_keys:
        joined = ", ".join(sorted(oversized_keys))
        raise RuntimeError(
            f"Runtime env contains values longer than {_MAX_RUNTIME_ENV_VALUE_LENGTH} characters: {joined}"
        )
    path.write_text("".join(f"{key}={value}\n" for key, value in sorted(env.items())), encoding="utf-8")
    secure_runtime_file(path)


def migrate_legacy_runtime(user: dict[str, object], product_config: dict[str, object], stable_user_id: str) -> ProductRuntimeRecord | None:
    from hermes_cli.product_runtime_container import remove_container_if_exists

    stable_root = user_storage_root(product_config, stable_user_id)
    if stable_root.exists():
        return None
    for legacy_user_id in legacy_user_ids(user):
        if legacy_user_id == stable_user_id:
            continue
        legacy_record = load_runtime_record(legacy_user_id, config=product_config)
        if legacy_record is None:
            continue
        legacy_root = user_storage_root(product_config, legacy_user_id)
        if not legacy_root.exists():
            continue
        remove_container_if_exists(legacy_record.container_name)
        stable_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_root), str(stable_root))
        migrated = legacy_record.model_copy(
            update={
                "user_id": stable_user_id,
                "runtime_key": runtime_key(stable_user_id),
                "container_name": f"hermes-product-runtime-{runtime_key(stable_user_id)}",
                "runtime_root": str(runtime_root(product_config, stable_user_id)),
                "hermes_home": str(hermes_home(product_config, stable_user_id)),
                "workspace_root": str(workspace_root(product_config, stable_user_id)),
                "env_file": str(env_path(product_config, stable_user_id)),
                "manifest_file": str(manifest_path(product_config, stable_user_id)),
                "display_name": str(user.get("name") or user.get("preferred_username") or "").strip() or legacy_record.display_name,
                "status": "staged",
            }
        )
        write_runtime_record(migrated)
        return migrated
    return None


def stage_product_runtime(user: dict[str, object], *, config: dict[str, object] | None = None) -> ProductRuntimeRecord:
    product_config = config or load_product_config()
    ensure_hermes_home()
    stable_user_id = user_id(user)
    existing = load_runtime_record(stable_user_id, config=product_config) or migrate_legacy_runtime(user, product_config, stable_user_id)
    staged_runtime_root = runtime_root(product_config, stable_user_id)
    staged_hermes_home = hermes_home(product_config, stable_user_id)
    staged_workspace_root = workspace_root(product_config, stable_user_id)
    staged_install_root = user_install_root(product_config, stable_user_id)
    for path in (product_storage_root(product_config), product_users_root(product_config), staged_runtime_root):
        path.mkdir(parents=True, exist_ok=True)
        secure_runtime_dir(path)
    for path in (staged_install_root, staged_workspace_root, staged_workspace_root / ".tmp"):
        path.mkdir(parents=True, exist_ok=True)
        secure_runtime_writable_dir(path)

    launch_settings = resolve_runtime_launch_settings(product_config)
    root_config = load_config()
    model_cfg = resolve_hermes_model_config()
    session_id = existing.session_id if existing is not None else product_runtime_session_id(stable_user_id)
    with runtime_staging_lock(product_config):
        template_payload = stage_runtime_template(
            launch_settings,
            config=product_config,
            root_config=root_config,
            model_cfg=model_cfg,
        )
        ensure_runtime_install(product_config, stable_user_id, template_payload)
        runtime_port = existing.runtime_port if existing is not None else resolve_runtime_port(product_config, stable_user_id)
        stable_runtime_key = existing.runtime_key if existing is not None and existing.runtime_key else runtime_key(stable_user_id)
        container_name = existing.container_name if existing is not None else f"hermes-product-runtime-{stable_runtime_key}"
        auth_token = existing.auth_token if existing is not None and existing.auth_token else secrets.token_urlsafe(32)

        staged_env_path = env_path(product_config, stable_user_id)
        write_runtime_env_file(
            staged_env_path,
            runtime_environment(
                launch_settings,
                session_id=session_id,
                auth_token=auth_token,
                internal_port=runtime_internal_port(product_config),
                profile_name=str(template_payload.get("profile_name") or runtime_profile_name()),
                template_version=str(template_payload.get("template_version") or ""),
            ),
        )

        record = ProductRuntimeRecord(
            user_id=stable_user_id,
            runtime_key=stable_runtime_key,
            display_name=str(user.get("name") or user.get("preferred_username") or "").strip() or None,
            session_id=session_id,
            profile_name=str(template_payload.get("profile_name") or runtime_profile_name()),
            template_root=str(runtime_template_root(product_config)),
            template_version=str(template_payload.get("template_version") or ""),
            install_root=str(staged_install_root),
            container_name=container_name,
            runtime=runtime_binary(product_config),
            runtime_port=runtime_port,
            runtime_root=str(staged_runtime_root),
            hermes_home=str(staged_hermes_home),
            workspace_root=str(staged_workspace_root),
            env_file=str(staged_env_path),
            manifest_file=str(manifest_path(product_config, stable_user_id)),
            auth_token=auth_token,
            status="staged",
        )
        write_runtime_record(record)
    secure_runtime_dir(staged_runtime_root)
    return record
