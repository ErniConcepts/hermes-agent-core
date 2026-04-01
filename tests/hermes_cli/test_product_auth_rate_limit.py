import sqlite3

import pytest

from hermes_cli.product_auth_rate_limit import (
    ProductAuthRateLimitExceeded,
    enforce_product_auth_rate_limit,
)


def test_product_auth_rate_limit_blocks_after_max_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    for now in (1000, 1001):
        enforce_product_auth_rate_limit("100.64.0.1", "login", max_requests=2, window_seconds=300, now=now)

    with pytest.raises(ProductAuthRateLimitExceeded):
        enforce_product_auth_rate_limit("100.64.0.1", "login", max_requests=2, window_seconds=300, now=1002)


def test_product_auth_rate_limit_prunes_expired_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    enforce_product_auth_rate_limit("100.64.0.1", "login", max_requests=1, window_seconds=300, now=1000)
    enforce_product_auth_rate_limit("100.64.0.1", "login", max_requests=1, window_seconds=300, now=1401)


def test_product_auth_rate_limit_persists_state_in_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    enforce_product_auth_rate_limit("100.64.0.1", "callback", max_requests=5, window_seconds=300, now=1000)

    from hermes_cli.product_auth_rate_limit import _rate_limit_db_path

    path = _rate_limit_db_path()
    with sqlite3.connect(path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM auth_rate_limits").fetchone()[0]

    assert row_count == 1

