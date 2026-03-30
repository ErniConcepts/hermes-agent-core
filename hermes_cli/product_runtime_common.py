from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

_RUNTIME_HEALTH_TTL_SECONDS = 10.0
_RUNTIME_HEALTH_CACHE: dict[str, float] = {}
_RUNTIME_WORKSPACE_PATH = "/workspace"
_RUNTIME_ENV_MATCH_KEYS = {
    "HERMES_PRODUCT_PROVIDER",
    "HERMES_PRODUCT_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "HERMES_PRODUCT_TOOLSETS",
    "HERMES_PRODUCT_API_MODE",
    "HERMES_PRODUCT_RUNTIME_MODE",
}


@dataclass(frozen=True)
class ProductRuntimeLaunchSettings:
    model: str
    provider: str
    base_url: str
    api_mode: str
    api_key: str
    toolsets: list[str]


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


def secure_runtime_dir(path) -> None:
    try:
        path.chmod(0o755)
    except (OSError, NotImplementedError):
        pass


def secure_runtime_writable_dir(path) -> None:
    try:
        path.chmod(0o777)
    except (OSError, NotImplementedError):
        pass


def secure_runtime_file(path) -> None:
    try:
        if path.exists():
            path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass


def secure_container_readable_file(path) -> None:
    try:
        if path.exists():
            path.chmod(0o644)
    except (OSError, NotImplementedError):
        pass
