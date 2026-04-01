from __future__ import annotations

from typing import Any


def build_runtime_cli_config(
    *,
    base_url: str,
    model: str,
    model_cfg: dict[str, Any] | None,
    root_config: dict[str, Any] | None,
) -> dict[str, object]:
    """Derive the minimal config.yaml subset needed inside a staged runtime."""
    model_cfg = model_cfg or {}
    root_config = root_config or {}

    context_length = model_cfg.get("context_length")
    try:
        normalized_context_length = int(context_length) if context_length is not None else None
    except (TypeError, ValueError):
        normalized_context_length = None

    runtime_config: dict[str, object] = {}
    if normalized_context_length is not None and normalized_context_length > 0:
        runtime_config["model"] = {
            "default": model,
            "base_url": base_url,
            "provider": str(model_cfg.get("provider") or "").strip() or "custom",
            "context_length": normalized_context_length,
        }

    session_reset = root_config.get("session_reset")
    if isinstance(session_reset, dict) and session_reset:
        runtime_config["session_reset"] = dict(session_reset)

    return runtime_config
