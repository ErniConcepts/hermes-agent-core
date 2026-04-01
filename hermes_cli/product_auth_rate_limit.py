from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from hermes_cli.config import _secure_dir, _secure_file
from hermes_cli.product_config import get_product_storage_root


class ProductAuthRateLimitExceeded(RuntimeError):
    pass


def _rate_limit_db_path() -> Path:
    return get_product_storage_root() / "bootstrap" / "auth-rate-limit.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _rate_limit_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(path.parent)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_rate_limits (
            client_ip TEXT NOT NULL,
            route_key TEXT NOT NULL,
            observed_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_rate_limits_lookup "
        "ON auth_rate_limits (client_ip, route_key, observed_at)"
    )
    connection.commit()
    if path.exists():
        _secure_file(path)
    return connection


def enforce_product_auth_rate_limit(
    client_ip: str,
    route_key: str,
    *,
    max_requests: int,
    window_seconds: int,
    now: int | None = None,
) -> None:
    observed_at = int(now if now is not None else time.time())
    cutoff = observed_at - int(window_seconds)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM auth_rate_limits WHERE observed_at < ?", (cutoff,))
        current_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM auth_rate_limits WHERE client_ip = ? AND route_key = ?",
                (client_ip, route_key),
            ).fetchone()[0]
        )
        if current_count >= int(max_requests):
            connection.commit()
            raise ProductAuthRateLimitExceeded("Too many authentication requests")
        connection.execute(
            "INSERT INTO auth_rate_limits (client_ip, route_key, observed_at) VALUES (?, ?, ?)",
            (client_ip, route_key, observed_at),
        )
        connection.commit()

