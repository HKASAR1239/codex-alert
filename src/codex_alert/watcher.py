"""Read Codex completion events without changing Codex's configuration or logs.

``state`` is JSON serializable and is updated in place. The caller must persist it
atomically together with any notification outbox after every poll. Keep the same
``activated_at`` across restarts. No conversation text is kept in state.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any


MAX_RECORD_BYTES = 2 * 1024 * 1024
RECENT_FILE_SECONDS = 48 * 60 * 60
ANCHOR_BYTES = 128
# A silent shell command may legitimately run for a long time. Release sleep
# protection after two hours without a complete Codex record, so an abandoned
# turn cannot keep a laptop awake indefinitely. New activity restores it.
ACTIVE_TASK_IDLE_SECONDS = 2 * 60 * 60
# A record can be appended after poll captures its clock snapshot but before
# the reader reaches EOF. Retain this bounded metadata; has_active_tasks still
# waits for wall-clock time to reach it before considering it active.
ACTIVITY_READ_AHEAD_SECONDS = 30
ACTIVITY_VERSION = 1


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _timestamp(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _signature(stat: Any) -> list[int]:
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _anchor(stream: Any, offset: int) -> str:
    stream.seek(max(0, offset - ANCHOR_BYTES))
    return hashlib.sha256(stream.read(min(offset, ANCHOR_BYTES))).hexdigest()


class Watcher:
    """Incremental watcher for top-level task completions strictly over a limit.

    Unchanged files are only stat'ed. Initially, untouched files older than 48
    hours are baselined without opening them. Changed files are streamed with a
    2 MiB record memory limit; oversized records are discarded. Retained state
    contains file cursors, activity timestamps, task IDs and start times since
    activation. IDs deliberately remain retained to prevent duplicate alerts
    after a log is copied, rotated, or replayed.
    """

    def __init__(
        self,
        session_root: Path,
        state: dict,
        activated_at: float,
        min_seconds: float = 120,
    ) -> None:
        self.session_root = Path(session_root)
        self.state = state
        self.activated_at = float(activated_at)
        self.min_seconds = float(min_seconds)
        if not math.isfinite(self.activated_at):
            raise ValueError("activated_at must be finite")
        if not math.isfinite(self.min_seconds) or self.min_seconds < 0:
            raise ValueError("min_seconds must be finite and nonnegative")
        self.files = state.setdefault("files", {})
        self.started = state.setdefault("started", {})
        self.terminal = state.setdefault("terminal", {})
        self._present_files: set[str] = set()

    def poll(self) -> list[dict]:
        """Return each eligible completion once, including across saved restarts."""
        records: list[dict] = []
        now = time.time()
        recent_cutoff = now - RECENT_FILE_SECONDS
        present_files = set()
        for path in sorted(self.session_root.rglob("*.jsonl")):
            try:
                stat = path.stat()
            except OSError:
                continue
            key = str(path)
            present_files.add(key)
            previous = self.files.get(key)
            signature = _signature(stat)
            if (previous and previous.get("signature") == signature
                    and (previous.get("baseline_only")
                         or previous.get("activity_version") == ACTIVITY_VERSION)):
                continue
            if previous is None and stat.st_mtime < recent_cutoff:
                self.files[key] = {"signature": signature, "baseline_only": True}
                continue
            try:
                updated, new_records = self._read_file(path, previous, now)
            except OSError:
                # Missing, replaced, or temporarily unreadable files can be
                # retried on the next poll, with the previous cursor intact.
                continue
            self.files[key] = updated
            records.extend(new_records)
        self._present_files = present_files

        # Starts from one file may correspond to a completion in another.
        # Sorting metadata-only records avoids depending on filename order.
        records.sort(key=lambda item: (item["time"], item["type"] != "task_started"))
        alerts = []
        for record in records:
            identity = record["id"]
            kind = record["type"]
            if kind == "task_started":
                if identity not in self.terminal:
                    old = self.started.get(identity)
                    self.started[identity] = min(old, record["time"]) if old is not None else record["time"]
                continue

            start = self.started.pop(identity, None)
            completed_at = record["time"]
            if completed_at < self.activated_at or identity in self.terminal:
                continue
            self.terminal[identity] = completed_at
            if kind == "turn_aborted":
                continue

            duration = record.get("duration")
            if duration is None:
                start = record.get("started_at", start)
                if start is None:
                    continue
                duration = completed_at - start
            if duration <= self.min_seconds:
                continue
            alerts.append({
                "id": identity,
                "thread_id": record["thread_id"],
                "turn_id": record["turn_id"],
                "seconds": duration,
                "completed_at": completed_at,
            })
        return alerts

    def has_active_tasks(self, now: float | None = None) -> bool:
        """Whether a current top-level turn warrants preventing idle sleep.

        Call after ``poll``. The newest lifecycle event in each thread must
        belong to an unfinished turn with activity in the last two hours.
        Completed, aborted, deleted, future-dated and stale sessions do not
        qualify. This grace also bounds protection for a silent shell command;
        it never discards start times needed to compute notification duration.
        """
        now = time.time() if now is None else _number(now)
        if now is None:
            return False
        threads: dict[str, dict] = {}
        for path in self._present_files:
            cursor = self.files.get(path, {})
            thread_id = cursor.get("thread_id")
            latest = cursor.get("latest_task")
            activity = _number(cursor.get("last_activity"))
            if (cursor.get("ignored") or not isinstance(thread_id, str)
                    or activity is None or activity > now):
                continue
            thread = threads.setdefault(thread_id, {"activity": activity, "latest": None})
            thread["activity"] = max(thread["activity"], activity)
            if not isinstance(latest, dict):
                continue
            latest_time = _number(latest.get("time"))
            if latest_time is None or latest_time > now:
                continue
            if (thread["latest"] is None
                    or self._task_order(latest) > self._task_order(thread["latest"])):
                thread["latest"] = latest
        for thread in threads.values():
            if thread["latest"] is None:
                continue
            start = _number(self.started.get(thread["latest"].get("id")))
            if (start is not None and start <= now
                    and start <= thread["activity"]
                    and now - thread["activity"] <= ACTIVE_TASK_IDLE_SECONDS):
                return True
        return False

    @staticmethod
    def _task_order(record: dict) -> tuple:
        # A terminal record takes precedence over a start at the same instant.
        return (record["time"], record.get("type") != "task_started", record["id"])

    def _read_file(self, path: Path, previous: dict | None, now: float) -> tuple[dict, list[dict]]:
        records = []
        with path.open("rb") as stream:
            import os

            signature = _signature(os.fstat(stream.fileno()))
            cursor = dict(previous or {})
            offset = cursor.get("offset", 0)
            old_signature = cursor.get("signature", [])
            reset = (
                cursor.get("baseline_only")
                or cursor.get("activity_version") != ACTIVITY_VERSION
                or old_signature[:2] != signature[:2]
                or offset > signature[2]
            )
            if not reset and offset:
                # An anchor detects truncate-and-regrow even when the new file
                # is already larger than the previously committed offset.
                reset = cursor.get("anchor") != _anchor(stream, offset)
            if reset:
                cursor = {}
                offset = 0
            stream.seek(offset)

            while True:
                record_start = stream.tell()
                line = stream.readline(MAX_RECORD_BYTES + 1)
                if not line:
                    break
                oversized = len(line) > MAX_RECORD_BYTES
                complete = line.endswith(b"\n")
                while not complete:
                    tail = stream.readline(MAX_RECORD_BYTES + 1)
                    if not tail:
                        break
                    oversized = True
                    complete = tail.endswith(b"\n")
                if not complete:
                    # The writer may finish this record later. Commit only
                    # complete newline-terminated records, also across restart.
                    offset = record_start
                    break
                offset = stream.tell()
                if oversized:
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if not isinstance(message, dict):
                    continue
                payload = message.get("payload")
                if not isinstance(payload, dict):
                    continue
                if message.get("type") == "session_meta":
                    if not cursor.get("metadata_seen"):
                        cursor["metadata_seen"] = True
                        source = payload.get("source")
                        cursor["ignored"] = (
                            isinstance(source, dict) and "subagent" in source
                        ) or (isinstance(source, str) and source.startswith("subagent"))
                        thread_id = payload.get("id")
                        cursor["thread_id"] = thread_id if isinstance(thread_id, str) else None
                    continue
                if cursor.get("ignored") or not cursor.get("thread_id"):
                    continue
                activity = _timestamp(message.get("timestamp"))
                if activity is not None and activity <= now + ACTIVITY_READ_AHEAD_SECONDS:
                    cursor["last_activity"] = max(cursor.get("last_activity", activity), activity)
                if message.get("type") != "event_msg":
                    continue
                record = self._event(cursor["thread_id"], message, payload)
                if record is not None:
                    records.append(record)
                    if record["time"] <= now + ACTIVITY_READ_AHEAD_SECONDS:
                        cursor["last_activity"] = max(
                            cursor.get("last_activity", record["time"]), record["time"])
                        latest = cursor.get("latest_task")
                        if latest is None or self._task_order(record) > self._task_order(latest):
                            cursor["latest_task"] = {
                                key: record[key] for key in ("id", "type", "time")
                            }

            cursor.update(signature=signature, offset=offset, anchor=_anchor(stream, offset),
                          activity_version=ACTIVITY_VERSION)
        return cursor, records

    @staticmethod
    def _event(thread_id: str, message: dict, payload: dict) -> dict | None:
        kind = payload.get("type")
        if kind not in ("task_started", "task_complete", "turn_aborted"):
            return None
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return None
        timestamp_field = "started_at" if kind == "task_started" else "completed_at"
        timestamp = _timestamp(payload.get(timestamp_field))
        if timestamp is None:
            timestamp = _timestamp(message.get("timestamp"))
        if timestamp is None:
            return None
        result = {
            "type": kind,
            "id": f"{thread_id}:{turn_id}",
            "thread_id": thread_id,
            "turn_id": turn_id,
            "time": timestamp,
        }
        started_at = _timestamp(payload.get("started_at"))
        if started_at is not None:
            result["started_at"] = started_at
        duration_ms = _number(payload.get("duration_ms"))
        if duration_ms is not None and duration_ms >= 0:
            result["duration"] = duration_ms / 1000
        return result
