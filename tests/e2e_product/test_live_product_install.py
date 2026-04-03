from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e_product.live_product_support import (
    E2E_ARTIFACTS_DIR,
    E2E_BIN_HOME,
    E2E_HOME,
    E2E_INSTALL_DIR,
    healthcheck,
    load_live_product_state,
    prepare_live_product_install,
    run_curl_reinstall,
    run_product_uninstall,
    _run_wsl_bash,
)


pytestmark = pytest.mark.e2e


def test_live_product_repo_source_install_creates_isolated_layout(live_product_install_state: dict[str, object]) -> None:
    payload = json.loads(
        _run_wsl_bash(
            f"""
            set -euo pipefail
            test -x "{E2E_BIN_HOME}/hermes-core"
            test -x "{E2E_BIN_HOME}/hermes"
            test -d "{E2E_HOME}"
            test -d "{E2E_INSTALL_DIR}"
            python3 - <<'PY'
            import json
            from pathlib import Path
            payload = {{
                "has_product_config": (Path("{E2E_HOME}") / "product.yaml").exists(),
                "has_env_file": (Path("{E2E_HOME}") / ".env").exists(),
                "has_bootstrap_users": (Path("{E2E_HOME}") / "product" / "bootstrap" / "users.json").exists(),
            }}
            print(json.dumps(payload))
            PY
            """
        )
    )
    assert payload["has_product_config"] is True
    assert payload["has_env_file"] is True
    assert payload["has_bootstrap_users"] is True


def test_live_product_install_prepares_bootstrap_and_health(live_product_install_state: dict[str, object]) -> None:
    prepared = dict(live_product_install_state["prepared"])
    state = live_product_install_state["state"]

    assert prepared["bootstrap_url"]
    assert prepared["app_base_url"] == state.app_base_url
    healthcheck(state.local_app_base_url or state.app_base_url)


def test_live_product_uninstall_then_curl_reinstall_recovers_product_stack(
    live_product_install_state: dict[str, object],
) -> None:
    run_product_uninstall()
    run_curl_reinstall()
    prepared = prepare_live_product_install()
    state = load_live_product_state()
    healthcheck(state.local_app_base_url or state.app_base_url)

    assert prepared["bootstrap_url"]
    assert Path(E2E_ARTIFACTS_DIR).parent.exists()
