import importlib
import json
import os
import pathlib
import subprocess
import sys


def run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    print(f"$ {' '.join(command)}")
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def assert_file(path: pathlib.Path, expected: str) -> None:
    if not path.exists():
        raise AssertionError(f"Missing expected file: {path}")
    content = path.read_text(encoding="utf-8")
    if expected not in content:
        raise AssertionError(f"Expected to find {expected!r} in {path}")


def main() -> None:
    print(json.dumps(
        {
            "python": sys.version,
            "cwd": os.getcwd(),
            "hermes_home": os.environ.get("HERMES_HOME"),
            "terminal_env": os.environ.get("TERMINAL_ENV"),
        },
        indent=2,
    ))

    for module_name in ("run_agent", "tools.terminal_tool", "tools.file_tools", "hermes_cli.main"):
        importlib.import_module(module_name)
        print(f"Imported {module_name}")

    run(["hermes", "--help"])
    importlib.import_module("tools.environments.docker")
    print("Imported tools.environments.docker")

    hermes_home = pathlib.Path(os.environ["HERMES_HOME"])
    hermes_home.mkdir(parents=True, exist_ok=True)
    memory_path = hermes_home / "MEMORY.md"
    user_path = hermes_home / "USER.md"
    memory_path.write_text("# Memory\n\n- smoke test memory\n", encoding="utf-8")
    user_path.write_text("# User\n\n- smoke test user\n", encoding="utf-8")

    assert_file(memory_path, "smoke test memory")
    assert_file(user_path, "smoke test user")
    print("Smoke test completed")


if __name__ == "__main__":
    main()
