import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_alert import titles
from codex_alert.titles import TitleResolver, sanitize_title


class TitleSanitizerTests(unittest.TestCase):
    def test_unicode_and_emoji_are_preserved(self):
        self.assertEqual(sanitize_title("  Réviser\nle\tprojet 👩‍💻 東京  "),
                         "Réviser le projet 👩‍💻 東京")

    def test_controls_directional_overrides_and_surrogates_are_removed(self):
        self.assertEqual(sanitize_title("A\x00\x1b\u202eB\u202c\u200b\ud800"), "AB")

    def test_long_names_are_bounded(self):
        result = sanitize_title("é" * 500)
        self.assertEqual(len(result), 120)
        self.assertTrue(result.endswith("…"))

    def test_empty_or_non_string_values_are_not_names(self):
        for value in (None, 42, True, [], {}, b"name", "", "\n\t\x00\u202e"):
            with self.subTest(value=value):
                self.assertIsNone(sanitize_title(value))


class TitleResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "state_5.sqlite"
        self.index = self.root / "session_index.jsonl"
        self.resolver = TitleResolver(self.root)

    def database(self, name="Database name", title="PRIVATE PROMPT", wal=False):
        connection = sqlite3.connect(self.db)
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT)")
        connection.execute("INSERT INTO threads VALUES (?, ?, ?)", ("thread", name, title))
        connection.commit()
        self.addCleanup(connection.close)
        return connection

    def write_index(self, *rows):
        self.index.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))

    def row(self, name="Index name", timestamp="2026-09-16T12:00:00Z", thread="thread"):
        return {"id": thread, "thread_name": name, "updated_at": timestamp}

    def test_explicit_database_name_has_priority(self):
        self.database()
        self.write_index(self.row())
        self.assertEqual(self.resolver.resolve("thread"), "Database name")

    def test_fallback_to_index_for_blank_or_missing_database_name(self):
        self.database(name="\n \x00")
        self.write_index(self.row(), self.row("Second", thread="other"))
        self.assertEqual(self.resolver.resolve("thread"), "Index name")
        self.assertEqual(self.resolver.resolve("other"), "Second")

    def test_raw_title_and_other_content_are_never_used_as_fallback(self):
        self.database(name=None)
        self.write_index({"id": "thread", "title": "PRIVATE PROMPT", "prompt": "PRIVATE PROMPT"})
        self.assertIsNone(self.resolver.resolve("thread"))

    def test_no_metadata_never_creates_files(self):
        self.assertIsNone(self.resolver.resolve("thread"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_schema_changes_and_corrupt_database_use_index(self):
        self.write_index(self.row())
        self.db.write_bytes(b"not a database")
        self.assertEqual(self.resolver.resolve("thread"), "Index name")
        self.db.unlink()
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE threads (id TEXT, title TEXT)")
            connection.execute("INSERT INTO threads VALUES ('thread', 'PRIVATE PROMPT')")
        self.assertEqual(self.resolver.resolve("thread"), "Index name")

    def test_locked_database_returns_quickly_with_index_fallback(self):
        connection = self.database()
        self.write_index(self.row())
        connection.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        self.assertEqual(self.resolver.resolve("thread"), "Index name")
        self.assertLess(time.monotonic() - started, 0.5)
        connection.rollback()

    def test_committed_wal_rename_is_seen_without_cache_staleness(self):
        connection = self.database(wal=True)
        self.assertEqual(self.resolver.resolve("thread"), "Database name")
        connection.execute("UPDATE threads SET name = ? WHERE id = ?", ("Renamed tâche 🎉", "thread"))
        connection.commit()
        self.assertEqual(self.resolver.resolve("thread"), "Renamed tâche 🎉")

    def test_database_is_unchanged_by_resolution(self):
        connection = self.database()
        connection.close()
        before = {path.name: (path.stat().st_mtime_ns, path.read_bytes())
                  for path in self.root.iterdir()}
        self.assertEqual(self.resolver.resolve("thread"), "Database name")
        after = {path.name: (path.stat().st_mtime_ns, path.read_bytes())
                 for path in self.root.iterdir()}
        self.assertEqual(after, before)

    def test_database_lookup_is_parameterized(self):
        self.database()
        self.assertIsNone(self.resolver.resolve("' OR 1=1 --"))

    def test_index_uses_newest_timestamp_then_append_order(self):
        self.write_index(self.row("Newest", "2026-09-16T12:00:00Z"),
                         self.row("Older", "2026-09-15T12:00:00Z"),
                         self.row("Same time", "2026-09-16T12:00:00Z"))
        self.assertEqual(self.resolver.resolve("thread"), "Same time")

    def test_missing_or_invalid_timestamp_uses_append_order(self):
        self.write_index(self.row("First", None), self.row("Last", "broken"))
        self.assertEqual(self.resolver.resolve("thread"), "Last")

    def test_index_cache_handles_append_replace_truncate_and_delete(self):
        self.write_index(self.row())
        self.assertEqual(self.resolver.resolve("thread"), "Index name")
        with patch.object(Path, "open", side_effect=AssertionError("unchanged index reopened")):
            self.assertEqual(self.resolver.resolve("thread"), "Index name")
        with self.index.open("ab") as stream:
            stream.write(json.dumps(self.row("Append rename")).encode() + b"\n")
        self.assertEqual(self.resolver.resolve("thread"), "Append rename")
        replacement = self.root / "replacement"
        replacement.write_bytes(json.dumps(self.row("Replacement")).encode() + b"\n")
        replacement.replace(self.index)
        self.assertEqual(self.resolver.resolve("thread"), "Replacement")
        self.index.write_bytes(b"")
        self.assertIsNone(self.resolver.resolve("thread"))
        self.write_index(self.row())
        self.assertEqual(self.resolver.resolve("thread"), "Index name")
        self.index.unlink()
        self.assertIsNone(self.resolver.resolve("thread"))

    def test_partial_invalid_non_object_and_oversized_records_are_skipped(self):
        partial = json.dumps(self.row("Incomplete rename")).encode()
        self.index.write_bytes(b"{broken}\n[]\n\xff\n" + b"x" * (titles.MAX_INDEX_RECORD_BYTES + 1)
                               + b"\n" + json.dumps(self.row()).encode() + b"\n" + partial)
        self.assertEqual(self.resolver.resolve("thread"), "Index name")
        with self.index.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(self.resolver.resolve("thread"), "Incomplete rename")

    def test_bounded_index_tail_ignores_partial_first_record(self):
        self.index.write_bytes(b"x" * 2000 + b"\n" + json.dumps(self.row()).encode() + b"\n")
        with patch.object(titles, "MAX_INDEX_BYTES", 512):
            self.assertEqual(self.resolver.resolve("thread"), "Index name")

    def test_unavailable_index_does_not_return_stale_cache(self):
        self.write_index(self.row())
        self.assertEqual(self.resolver.resolve("thread"), "Index name")
        with patch.object(Path, "stat", side_effect=PermissionError):
            self.assertIsNone(self.resolver.resolve("thread"))

    def test_names_are_sanitized_at_both_sources(self):
        connection = self.database(name="Réviser\nla\ttâche\u202e 👩‍💻")
        self.assertEqual(self.resolver.resolve("thread"), "Réviser la tâche 👩‍💻")
        connection.execute("UPDATE threads SET name = NULL")
        connection.commit()
        self.write_index(self.row("A" * 130))
        self.assertEqual(len(self.resolver.resolve("thread")), 120)


if __name__ == "__main__":
    unittest.main()
