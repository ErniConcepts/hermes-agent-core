from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass
class SessionResetPolicy:
    """Shared session reset policy used by gateway and product runtimes."""

    mode: str = "both"
    at_hour: int = 4
    idle_minutes: int = 1440
    notify: bool = True
    notify_exclude_platforms: tuple[str, ...] = ("api_server", "webhook")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "at_hour": self.at_hour,
            "idle_minutes": self.idle_minutes,
            "notify": self.notify,
            "notify_exclude_platforms": list(self.notify_exclude_platforms),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SessionResetPolicy":
        payload = data if isinstance(data, dict) else {}
        policy = cls(
            mode=payload.get("mode") if payload.get("mode") is not None else "both",
            at_hour=payload.get("at_hour") if payload.get("at_hour") is not None else 4,
            idle_minutes=payload.get("idle_minutes") if payload.get("idle_minutes") is not None else 1440,
            notify=payload.get("notify") if payload.get("notify") is not None else True,
            notify_exclude_platforms=tuple(payload.get("notify_exclude_platforms") or ("api_server", "webhook")),
        )
        return normalize_session_reset_policy(policy)


def normalize_session_reset_policy(policy: SessionResetPolicy) -> SessionResetPolicy:
    mode = str(policy.mode or "both").strip().lower()
    if mode not in {"daily", "idle", "both", "none"}:
        mode = "both"
    policy.mode = mode

    try:
        policy.at_hour = int(policy.at_hour)
    except (TypeError, ValueError):
        policy.at_hour = 4
    if not (0 <= policy.at_hour <= 23):
        policy.at_hour = 4

    try:
        policy.idle_minutes = int(policy.idle_minutes)
    except (TypeError, ValueError):
        policy.idle_minutes = 1440
    if policy.idle_minutes <= 0:
        policy.idle_minutes = 1440

    policy.notify = bool(policy.notify)
    policy.notify_exclude_platforms = tuple(str(item) for item in (policy.notify_exclude_platforms or ("api_server", "webhook")))
    return policy


def session_reset_reason(
    *,
    last_activity: datetime,
    policy: SessionResetPolicy,
    now: datetime | None = None,
) -> str | None:
    if policy.mode == "none":
        return None

    current = now or datetime.now()

    if policy.mode in {"idle", "both"}:
        idle_deadline = last_activity + timedelta(minutes=policy.idle_minutes)
        if current > idle_deadline:
            return "idle"

    if policy.mode in {"daily", "both"}:
        today_reset = current.replace(hour=policy.at_hour, minute=0, second=0, microsecond=0)
        if current.hour < policy.at_hour:
            today_reset -= timedelta(days=1)
        if last_activity < today_reset:
            return "daily"

    return None


def is_session_expired(
    *,
    last_activity: datetime,
    policy: SessionResetPolicy,
    now: datetime | None = None,
) -> bool:
    return session_reset_reason(last_activity=last_activity, policy=policy, now=now) is not None
