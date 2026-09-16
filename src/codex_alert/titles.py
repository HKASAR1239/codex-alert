"""Read display names from Codex metadata without reading conversation content."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import unicodedata


MAX_TITLE_CHARS = 120
MAX_TITLE_INPUT_CHARS = 4096
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_INDEX_RECORD_BYTES = 64 * 1024


def sanitize_title(value: object) -> str | None:
    """Return a short, single-line Unicode display name, or None.

    Keep emoji joiners, but remove other invisible formatting and control
    characters, including directional overrides. Never coerce other objects.
    """
    if not isinstance(value, str):
        return None
    clean = []
    for char in value[:MAX_TITLE_INPUT_CHARS]:
        if char.isspace():
            clean.append(" ")
        elif (unicodedata.category(char) not in ("Cc", "Cf", "Cs")
              or char in ("\u200c", "\u200d")):
            clean.append(char)
    title = " ".join("".join(clean).split())
    if not title:
        return None
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS - 1].rstrip() + "…"
    return title


def _updated_at(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


class TitleResolver:
    """Prefer explicit database names, then the latest session-index name.

    Database ``title``, ``first_user_message`` and transcript content are never
    used: those may contain a raw prompt instead of a human-visible name.
    Names are kept in memory only. A fresh read-only database connection sees
    committed WAL renames; the bounded index cache is invalidated on changes.
    """

    def __init__(self, codex_home: Path):
        self.home = Path(codex_home)
        self._index_signature = None
        self._index_names: dict[str, str] = {}

    def resolve(self, thread_id: str) -> str | None:
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 512:
            return None
        name = self._database_name(thread_id)
        if name:
            return name
        self._refresh_index()
        return self._index_names.get(thread_id)

    def _database_name(self, thread_id: str) -> str | None:
        connection = None
        try:
            # mode=ro refuses to create an absent database. Do not use
            # immutable=1: it would miss current names in the WAL.
            uri = (self.home / "state_5.sqlite").resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=0.05)
            row = connection.execute(
                "SELECT substr(name, 1, ?) FROM threads WHERE id = ? LIMIT 1",
                (MAX_TITLE_INPUT_CHARS, thread_id),
            ).fetchone()
            return sanitize_title(row[0]) if row else None
        except (sqlite3.Error, OSError, ValueError):
            return None
        finally:
            if connection is not None:
                connection.close()

    def _refresh_index(self) -> None:
        path = self.home / "session_index.jsonl"
        try:
            stat = path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_size,
                         stat.st_mtime_ns, stat.st_ctime_ns)
            if signature == self._index_signature:
                return
            # Inspect at most the newest 8 MiB. A corrupted/huge record cannot
            # trigger an unbounded read, allocation, or JSON parse.
            start = max(0, stat.st_size - MAX_INDEX_BYTES)
            with path.open("rb") as stream:
                stream.seek(start)
                data = stream.read(MAX_INDEX_BYTES)
            if start:
                # Seeking may land in the middle of an existing JSON record.
                first_newline = data.find(b"\n")
                data = data[first_newline + 1:] if first_newline >= 0 else b""
            records = data.split(b"\n")[:-1]  # Ignore a partial final record.
            names = {}
            timestamps = {}
            for record in records:
                if not record or len(record) > MAX_INDEX_RECORD_BYTES:
                    continue
                try:
                    entry = json.loads(record)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if not isinstance(entry, dict):
                    continue
                thread_id = entry.get("id")
                if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 512:
                    continue
                name = sanitize_title(entry.get("thread_name"))
                if not name:
                    continue
                timestamp = _updated_at(entry.get("updated_at"))
                previous = timestamps.get(thread_id)
                # The index is append-only. Use append order when timestamps
                # are absent/equal, but do not replace a newer dated name.
                if (timestamp is not None and previous is not None
                        and timestamp < previous):
                    continue
                names[thread_id] = name
                timestamps[thread_id] = timestamp
            self._index_names = names
            self._index_signature = signature
        except (OSError, ValueError):
            # Never return a stale cached name after the metadata disappears.
            self._index_names = {}
            self._index_signature = None
