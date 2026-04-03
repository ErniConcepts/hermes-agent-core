from __future__ import annotations

import pytest

from tests.e2e_product.live_product_support import (
    _wsl_available,
    ensure_clean_e2e_home,
    healthcheck,
    load_live_product_state,
    prepare_live_product_install,
    run_repo_source_install,
)


@pytest.fixture(scope="session")
def live_product_install_state() -> dict[str, object]:
    if not _wsl_available():
        pytest.skip("WSL is not available on this machine")
    ensure_clean_e2e_home()
    run_repo_source_install()
    prepared = prepare_live_product_install()
    state = load_live_product_state()
    healthcheck(state.local_app_base_url or state.app_base_url)
    return {
        "prepared": prepared,
        "state": state,
    }
