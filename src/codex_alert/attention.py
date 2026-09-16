"""Optional, non-deciding Codex lifecycle hooks for approval notifications.

Only opaque IDs, timestamps, and a one-way tool-call fingerprint reach disk.
Commands, tool arguments, questions, and approval decisions are never retained.
The hook always leaves Codex's normal approval flow unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time


MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_STATE_BYTES = 512 * 1024
MAX_PENDING = 256
EXPIRY_SECONDS = 10 * 60
EVENTS = frozenset(("PermissionRequest", "PostToolUse", "Stop", "Interrupt"))
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_ATTENTION_ID = re.compile(r"attention:[a-f0-9]{64}\Z")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _identifier(value):
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _load(home: Path, now: float) -> dict:
    """Validate our own small sidecar; never propagate arbitrary JSON fields."""
    try:
        with (home / "attention.json").open("rb") as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            return {}
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("pending"), dict):
        return {}
    pending = {}
    for identity, item in value["pending"].items():
        if (not isinstance(identity, str) or not _ATTENTION_ID.fullmatch(identity)
                or not isinstance(item, dict)):
            continue
        thread = _identifier(item.get("thread_id"))
        turn = _identifier(item.get("turn_id"))
        created = _number(item.get("created_at"))
        if not thread or not turn or created is None or not 0 <= now - created <= EXPIRY_SECONDS:
            continue
        pending[identity] = {"thread_id": thread, "turn_id": turn, "created_at": created}
        if len(pending) == MAX_PENDING:
            break
    return pending


@contextmanager
def _locked(home: Path):
    # Hooks must not hold up a turn if another process is writing. A skipped
    # notification is preferable to changing or delaying approval behavior.
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(home / "attention.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _save(home: Path, pending: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".attention-", dir=str(home))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pending": pending}, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, home / "attention.json")
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def capture(home: Path, event: str, stream, *, now: float | None = None) -> bool:
    """Consume one hook's JSON stdin and update local pending approval metadata.

    ``False`` means an unsupported, invalid, oversized or unavailable input.
    Callers must still exit zero without writing a Codex hook decision.
    """
    now = time.time() if now is None else _number(now)
    if event not in EVENTS or now is None:
        return False
    try:
        raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            return False
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("hook_event_name") != event:
            return False
        thread, turn = _identifier(payload.get("session_id")), _identifier(payload.get("turn_id"))
        if not thread or not turn:
            return False
        identity = None
        if event in ("PermissionRequest", "PostToolUse"):
            tool = payload.get("tool_name")
            if not isinstance(tool, str) or not tool or len(tool) > 256 or "tool_input" not in payload:
                return False
            fingerprint = json.dumps([thread, turn, tool, payload["tool_input"]],
                                     sort_keys=True, separators=(",", ":"), allow_nan=False)
            identity = "attention:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        home = Path(home)
        with _locked(home):
            pending = _load(home, now)
            if event == "PermissionRequest":
                if identity not in pending:
                    while len(pending) >= MAX_PENDING:
                        del pending[min(pending, key=lambda key: pending[key]["created_at"])]
                    pending[identity] = {"thread_id": thread, "turn_id": turn, "created_at": now}
            elif event == "PostToolUse":
                pending.pop(identity, None)
            else:
                pending = {key: item for key, item in pending.items()
                           if (item["thread_id"], item["turn_id"]) != (thread, turn)}
            _save(home, pending)
        return True
    except (OSError, ValueError, UnicodeError, RecursionError, TypeError):
        return False


def poll(home: Path, state: dict, *, now: float | None = None) -> list[dict]:
    """Return new approval requests once; persist caller state with its outbox.

    ``state`` is the daemon's top-level state, including ``activated_at`` and
    ``watcher.terminal``. Completed/aborted turns cancel unsent requests.
    """
    now = time.time() if now is None else _number(now)
    if now is None or not (Path(home) / "attention.json").is_file():
        return []
    seen = state.setdefault("attention_seen", {})
    if not isinstance(seen, dict):
        seen = state["attention_seen"] = {}
    for key, timestamp in list(seen.items()):
        value = _number(timestamp)
        if value is None or not 0 <= now - value <= EXPIRY_SECONDS:
            del seen[key]
    activated = _number(state.get("activated_at"))
    terminal = state.get("watcher", {}).get("terminal", {})
    pending = _load(Path(home), now)
    if any(f'{item["thread_id"]}:{item["turn_id"]}' in terminal for item in pending.values()):
        try:
            with _locked(Path(home)):
                pending = _load(Path(home), now)
                pending = {key: item for key, item in pending.items()
                           if f'{item["thread_id"]}:{item["turn_id"]}' not in terminal}
                _save(Path(home), pending)
        except OSError:
            return []
    alerts = []
    for identity, item in sorted(pending.items(), key=lambda entry: entry[1]["created_at"]):
        created = item["created_at"]
        if identity in seen or (activated is not None and created < activated):
            continue
        seen[identity] = created
        alerts.append({"id": identity, "kind": "attention", "thread_id": item["thread_id"],
                       "turn_id": item["turn_id"], "seconds": 0, "completed_at": created})
    return alerts


def is_pending(home: Path, item: dict, *, now: float | None = None) -> bool:
    """Recheck immediately before sending; false after finish or ten minutes.

    Codex has no approval-accepted hook. A granted long-running command clears
    at PostToolUse, so wording must say "requested approval", not "is blocked".
    """
    now = time.time() if now is None else _number(now)
    if now is None or not isinstance(item, dict):
        return False
    current = _load(Path(home), now).get(item.get("id"))
    return bool(current and current["thread_id"] == item.get("thread_id")
                and current["turn_id"] == item.get("turn_id"))
