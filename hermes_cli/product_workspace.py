from __future__ import annotations

import math
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel

from hermes_cli.product_config import load_product_config
from hermes_cli.product_runtime import _workspace_root, _user_id

_HIDDEN_WORKSPACE_NAMES = frozenset({".tmp"})


class ProductWorkspaceEntry(BaseModel):
    name: str
    path: str
    kind: str
    size_bytes: int


class ProductWorkspaceState(BaseModel):
    current_path: str
    entries: list[ProductWorkspaceEntry]
    used_bytes: int
    limit_bytes: int


class ProductWorkspaceQuotaError(ValueError):
    pass


def workspace_limit_megabytes(config: dict[str, Any] | None = None) -> int:
    product_config = config or load_product_config()
    raw_value = product_config.get("storage", {}).get("user_workspace_limit_mb", 2048)
    try:
        limit_mb = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError("product storage.user_workspace_limit_mb must be an integer")
    if limit_mb < 1:
        raise ValueError("product storage.user_workspace_limit_mb must be greater than zero")
    return limit_mb


def workspace_limit_bytes(config: dict[str, Any] | None = None) -> int:
    return workspace_limit_megabytes(config) * 1024 * 1024


def humanize_bytes(value: int) -> str:
    amount = max(0, int(value))
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    size = float(amount)
    while size >= 1024.0 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    if size >= 10 or math.isclose(size, round(size)):
        return f"{size:.0f} {units[index]}"
    return f"{size:.1f} {units[index]}"


def _workspace_root_for_user(user: dict[str, Any], config: dict[str, Any]) -> Path:
    user_id = _user_id(user)
    root = _workspace_root(config, user_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _normalize_relative_path(relative_path: str | None) -> str:
    raw_path = str(relative_path or "").strip().replace("\\", "/")
    if not raw_path or raw_path == ".":
        return ""
    path = PurePosixPath(raw_path)
    if path.is_absolute():
        raise ValueError("Workspace path must be relative")
    normalized_parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("Workspace path must stay inside the user workspace")
        normalized_parts.append(part)
    return PurePosixPath(*normalized_parts).as_posix() if normalized_parts else ""


def _resolve_workspace_path(root: Path, relative_path: str | None) -> tuple[Path, str]:
    normalized = _normalize_relative_path(relative_path)
    candidate = root / Path(normalized)
    current = root
    for part in Path(normalized).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinks are not permitted in workspace paths")
    target = candidate.resolve()
    if target != root and root not in target.parents:
        raise ValueError("Workspace path must stay inside the user workspace")
    return target, normalized


def resolve_workspace_file(
    user: dict[str, Any],
    *,
    path: str,
    config: dict[str, Any] | None = None,
) -> tuple[Path, str]:
    product_config = config or load_product_config()
    root = _workspace_root_for_user(user, product_config)
    target, normalized = _resolve_workspace_path(root, path)
    if not normalized:
        raise ValueError("Workspace download path must not be empty")
    if not target.exists():
        raise ValueError("Workspace path does not exist")
    if not target.is_file():
        raise ValueError("Workspace download path must refer to a file")
    return target, normalized


def _workspace_usage_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file():
            total += path.stat().st_size
    return total


def get_workspace_state(
    user: dict[str, Any],
    *,
    path: str | None = None,
    config: dict[str, Any] | None = None,
) -> ProductWorkspaceState:
    product_config = config or load_product_config()
    root = _workspace_root_for_user(user, product_config)
    target, normalized = _resolve_workspace_path(root, path)
    if not target.exists():
        raise ValueError("Workspace path does not exist")
    if not target.is_dir():
        raise ValueError("Workspace path must refer to a folder")

    entries: list[ProductWorkspaceEntry] = []
    for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if child.name in _HIDDEN_WORKSPACE_NAMES:
            continue
        if child.is_symlink():
            continue
        relative = child.relative_to(root).as_posix()
        entries.append(
            ProductWorkspaceEntry(
                name=child.name,
                path=relative,
                kind="folder" if child.is_dir() else "file",
                size_bytes=0 if child.is_dir() else child.stat().st_size,
            )
        )

    return ProductWorkspaceState(
        current_path=normalized,
        entries=entries,
        used_bytes=_workspace_usage_bytes(root),
        limit_bytes=workspace_limit_bytes(product_config),
    )


def create_workspace_folder(
    user: dict[str, Any],
    *,
    parent_path: str | None,
    folder_name: str,
    config: dict[str, Any] | None = None,
) -> ProductWorkspaceState:
    product_config = config or load_product_config()
    root = _workspace_root_for_user(user, product_config)
    parent, normalized_parent = _resolve_workspace_path(root, parent_path)
    if not parent.exists() or not parent.is_dir():
        raise ValueError("Target folder does not exist")

    name = str(folder_name or "").strip()
    if not name:
        raise ValueError("Folder name must not be empty")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("Folder name must be a single path segment")

    target = parent / name
    if target.exists():
        raise ValueError("A file or folder with that name already exists")
    target.mkdir(parents=False, exist_ok=False)
    return get_workspace_state(user, path=normalized_parent, config=product_config)


def store_workspace_file(
    user: dict[str, Any],
    *,
    parent_path: str | None,
    filename: str,
    content: bytes,
    config: dict[str, Any] | None = None,
) -> ProductWorkspaceState:
    product_config = config or load_product_config()
    root = _workspace_root_for_user(user, product_config)
    parent, normalized_parent = _resolve_workspace_path(root, parent_path)
    if not parent.exists() or not parent.is_dir():
        raise ValueError("Target folder does not exist")

    name = PurePosixPath(str(filename or "").replace("\\", "/")).name.strip()
    if not name:
        raise ValueError("Uploaded file must have a filename")
    if name in {".", ".."}:
        raise ValueError("Uploaded file must have a valid filename")

    target = parent / name
    existing_size = target.stat().st_size if target.exists() and target.is_file() else 0
    current_usage = _workspace_usage_bytes(root)
    projected_usage = current_usage - existing_size + len(content)
    limit = workspace_limit_bytes(product_config)
    if projected_usage > limit:
        raise ProductWorkspaceQuotaError(
            f"Workspace storage limit exceeded ({humanize_bytes(projected_usage)} / {humanize_bytes(limit)})"
        )
    target.write_bytes(content)
    return get_workspace_state(user, path=normalized_parent, config=product_config)


def delete_workspace_path(
    user: dict[str, Any],
    *,
    path: str,
    config: dict[str, Any] | None = None,
) -> ProductWorkspaceState:
    product_config = config or load_product_config()
    root = _workspace_root_for_user(user, product_config)
    target, normalized = _resolve_workspace_path(root, path)
    if not normalized:
        raise ValueError("Workspace delete path must not be empty")
    if not target.exists():
        raise ValueError("Workspace path does not exist")
    parent_normalized = str(PurePosixPath(normalized).parent)
    if parent_normalized == ".":
        parent_normalized = ""
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return get_workspace_state(user, path=parent_normalized, config=product_config)


def move_workspace_path(
    user: dict[str, Any],
    *,
    source_path: str,
    destination_parent_path: str | None,
    config: dict[str, Any] | None = None,
) -> ProductWorkspaceState:
    product_config = config or load_product_config()
    root = _workspace_root_for_user(user, product_config)
    source, normalized_source = _resolve_workspace_path(root, source_path)
    destination_parent, normalized_destination_parent = _resolve_workspace_path(root, destination_parent_path)
    if not normalized_source:
        raise ValueError("Workspace source path must not be empty")
    if not source.exists():
        raise ValueError("Workspace source path does not exist")
    if not destination_parent.exists() or not destination_parent.is_dir():
        raise ValueError("Workspace destination folder does not exist")
    destination = destination_parent / source.name
    if destination == source:
        return get_workspace_state(user, path=normalized_destination_parent, config=product_config)
    if destination.exists():
        raise ValueError("A file or folder with that name already exists in the destination")
    if source.is_dir():
        try:
            destination_parent.relative_to(source)
        except ValueError:
            pass
        else:
            raise ValueError("Cannot move a folder into itself")
    source.rename(destination)
    return get_workspace_state(user, path=normalized_destination_parent, config=product_config)
