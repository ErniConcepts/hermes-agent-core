from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from hermes_cli.product_runtime import (
    get_product_runtime_session,
    stop_product_runtime_turn,
    stream_product_runtime_turn,
)


def get_product_chat_session(user: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return get_product_runtime_session(user, config=config)


def stream_product_chat_turn(
    user: dict[str, Any],
    user_message: str,
    *,
    config: dict[str, Any] | None = None,
) -> Iterator[str]:
    yield from stream_product_runtime_turn(user, user_message, config=config)


def stop_product_chat_turn(user: dict[str, Any], *, config: dict[str, Any] | None = None) -> bool:
    return stop_product_runtime_turn(user, config=config)
