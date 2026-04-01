from __future__ import annotations

from pathlib import Path

from hermes_cli.default_soul import DEFAULT_SOUL_MD
from hermes_cli.product_config import load_product_config, resolve_hermes_runtime_toolsets
from toolsets import resolve_toolset


def default_product_soul(config: dict | None = None) -> str:
    product_config = config or load_product_config()
    product_name = str(product_config.get("product", {}).get("brand", {}).get("name", "Hermes Core")).strip() or "Hermes Core"
    runtime_intro = f"""# {product_name} Runtime Identity

You are a Hermes Agent running in a {product_name} user runtime.

This is a per-user Hermes Core product runtime.

- Your persistent user-visible working area is `/workspace`.
- Create, edit, and organize user-facing files inside `/workspace`.
- You also have internal temporary storage at `/workspace/.tmp`.
- You may use `/workspace/.tmp` for scratch files, intermediate outputs, caches, or transient work.
- Treat `/workspace/.tmp` as runtime-internal space, not as part of the normal user-facing workspace.
- If something should be visible or useful to the user, write or move it into `/workspace`.
- Do not assume you can write outside `/workspace` and `/workspace/.tmp`.
"""
    return runtime_intro.strip() + "\n\n" + DEFAULT_SOUL_MD.strip() + "\n"


def _runtime_tools_from_toolsets(toolsets: list[str]) -> list[str]:
    resolved: list[str] = []
    for toolset in toolsets:
        for tool_name in resolve_toolset(toolset):
            if tool_name not in resolved:
                resolved.append(tool_name)
    return resolved


def _runtime_capability_overlay(config: dict | None = None) -> str:
    product_config = config or load_product_config()
    normalized = resolve_hermes_runtime_toolsets()
    rendered_toolsets = ", ".join(normalized)
    runtime_tools = _runtime_tools_from_toolsets(normalized)
    rendered_tools = ", ".join(runtime_tools) if runtime_tools else "none"
    return (
        "\n## Product Runtime Contract\n\n"
        "You are running inside a Hermes Core product runtime.\n\n"
        f"Your currently enabled Hermes toolsets are: {rendered_toolsets}.\n\n"
        f"The concrete tools currently available in this runtime are: {rendered_tools}.\n\n"
        "If someone asks what tools or capabilities you have, answer only from the concrete tools enabled in this runtime.\n"
        "Do not describe the full Hermes tool universe unless those tools are actually enabled here.\n"
        "If a capability is not in that concrete tool list, say you do not have it in this runtime.\n"
        "Admin permissions in the web app do not grant extra runtime tools.\n"
    )


def resolve_product_soul_template_path(config: dict | None = None) -> Path | None:
    product_config = config or load_product_config()
    raw_path = (
        str(product_config.get("product", {}).get("agent", {}).get("soul_template_path", "")).strip()
    )
    if not raw_path:
        return None
    return Path(raw_path).expanduser().resolve()


def render_product_soul(config: dict | None = None) -> str:
    product_config = config or load_product_config()
    template_path = resolve_product_soul_template_path(config)
    if template_path is None:
        return default_product_soul(product_config).rstrip() + _runtime_capability_overlay(product_config).rstrip() + "\n"
    if not template_path.exists():
        raise FileNotFoundError(f"Configured SOUL.md template was not found: {template_path}")
    content = template_path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Configured SOUL.md template is empty: {template_path}")
    return content + _runtime_capability_overlay(product_config).rstrip() + "\n"
