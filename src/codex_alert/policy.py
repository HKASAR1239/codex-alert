"""Local notification scheduling. No network, task content or persistent data."""

from __future__ import annotations

from datetime import datetime
import re


TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def quiet_config(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Invalid quiet hours.")
    enabled = value.get("enabled", False)
    start, end = value.get("start", "22:00"), value.get("end", "08:00")
    if (not isinstance(enabled, bool) or not isinstance(start, str)
            or not isinstance(end, str) or not TIME_PATTERN.fullmatch(start)
            or not TIME_PATTERN.fullmatch(end) or (enabled and start == end)):
        raise ValueError("Invalid quiet hours: use different HH:MM start and end times.")
    return {"enabled": enabled, "start": start, "end": end}


def is_quiet(config: dict, now: float) -> bool:
    quiet = config.get("quiet_hours", {})
    if not quiet.get("enabled", False):
        return False
    current = datetime.fromtimestamp(now).strftime("%H:%M")
    start, end = quiet["start"], quiet["end"]
    return start <= current < end if start < end else current >= start or current < end


def is_paused(config: dict, now: float) -> bool:
    return now < config.get("paused_until", 0)


def ready_groups(pending: list[dict], config: dict, now: float) -> list[list[dict]]:
    """Batch completions when the oldest wait expires; urgent types stay separate.

    Quiet hours defer all alerts. Pausing is handled by the caller and drops
    queued alerts instead of replaying them after the pause. Retry delays are
    respected; at most five completion names are placed in one message.
    """
    if is_paused(config, now) or is_quiet(config, now):
        return []
    ready = [item for item in pending if now >= item.get("retry_at", 0)]
    urgent = [[item] for item in ready if item.get("kind", "completed") != "completed"]
    completed = [item for item in ready if item.get("kind", "completed") == "completed"]
    if completed and min(item.get("deliver_after", 0) for item in completed) <= now:
        size = 5 if config.get("group_seconds", 0) else 1
        urgent.extend(completed[i:i + size] for i in range(0, len(completed), size))
    return urgent
