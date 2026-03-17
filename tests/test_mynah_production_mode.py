import importlib

import pytest
import yaml

import hermes_cli.plugins as plugins_mod
import model_tools
from hermes_cli.plugins import PluginManager


def _write_plugin(base, name: str, tool_name: str) -> None:
    plugin_dir = base / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(yaml.dump({"name": name}))
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        "    ctx.register_tool(\n"
        f"        name={tool_name!r},\n"
        f"        toolset='plugin_{name}',\n"
        "        schema={\n"
        f"            'name': {tool_name!r},\n"
        "            'description': 'Visible',\n"
        "            'parameters': {'type': 'object', 'properties': {}},\n"
        "        },\n"
        "        handler=lambda args, **kw: 'ok',\n"
        "    )\n"
    )


def test_mynah_production_mode_rejects_non_mynah_toolsets(monkeypatch):
    monkeypatch.setenv("MYNAH_PRODUCTION_MODE", "1")
    reloaded = importlib.reload(model_tools)

    with pytest.raises(RuntimeError, match="not allowed in MYNAH production mode"):
        reloaded.get_tool_definitions(enabled_toolsets=["terminal"], quiet_mode=True)


def test_mynah_production_mode_requires_explicit_toolsets(monkeypatch):
    monkeypatch.setenv("MYNAH_PRODUCTION_MODE", "1")
    reloaded = importlib.reload(model_tools)

    with pytest.raises(RuntimeError, match="requires explicit enabled_toolsets"):
        reloaded.get_tool_definitions(quiet_mode=True)


def test_mynah_production_mode_excludes_plugin_tools(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "hermes_test" / "plugins"
    _write_plugin(plugins_dir, "mynah_lockdown", "plugin_echo")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_test"))
    monkeypatch.setenv("MYNAH_PRODUCTION_MODE", "1")
    monkeypatch.setattr(plugins_mod, "_plugin_manager", None)

    mgr = PluginManager()
    mgr.discover_and_load()
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)

    reloaded = importlib.reload(model_tools)
    tools = reloaded.get_tool_definitions(enabled_toolsets=["mynah-tier1"], quiet_mode=True)
    tool_names = [tool["function"]["name"] for tool in tools]

    assert "memory" in tool_names
    assert "session_search" in tool_names
    assert "plugin_echo" not in tool_names
