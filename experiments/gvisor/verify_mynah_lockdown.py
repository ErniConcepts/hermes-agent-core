#!/usr/bin/env python3
import os
from pathlib import Path


def main() -> None:
    home = Path(os.environ["HERMES_HOME"])
    plugin_dir = home / "plugins" / "blocked_plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text("name: blocked_plugin\n")
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        "    ctx.register_tool(\n"
        "        name='plugin_echo',\n"
        "        toolset='plugin_blocked',\n"
        "        schema={'name': 'plugin_echo', 'description': 'Echo', 'parameters': {'type': 'object', 'properties': {}}},\n"
        "        handler=lambda args, **kw: 'ok',\n"
        "    )\n"
    )

    from model_tools import get_tool_definitions
    from run_agent import AIAgent

    try:
        get_tool_definitions(quiet_mode=True)
        raise AssertionError("expected explicit toolset failure")
    except RuntimeError as exc:
        assert "requires explicit enabled_toolsets" in str(exc)

    try:
        get_tool_definitions(enabled_toolsets=["terminal"], quiet_mode=True)
        raise AssertionError("expected non-MYNAH toolset failure")
    except RuntimeError as exc:
        assert "not allowed in MYNAH production mode" in str(exc)

    tools = get_tool_definitions(enabled_toolsets=["mynah-tier1"], quiet_mode=True)
    tool_names = sorted(tool["function"]["name"] for tool in tools)
    assert tool_names == ["memory", "session_search"], tool_names
    assert "plugin_echo" not in tool_names, tool_names

    agent = AIAgent(
        base_url=os.environ["MYNAH_TEST_BASE_URL"],
        api_key=os.environ.get("MYNAH_TEST_API_KEY", "dummy"),
        model=os.environ["MYNAH_TEST_MODEL"],
        max_iterations=1,
        enabled_toolsets=["mynah-tier1"],
    )
    result = agent.run_conversation("Answer with exactly: fork ok")
    text = result["final_response"].strip()
    print(text)
    assert "fork ok" in text.lower(), text


if __name__ == "__main__":
    main()
