import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_alert import watcher as watcher_module
Watcher = watcher_module.Watcher


def meta(thread="thread", source="vscode"):
    return {"type": "session_meta", "payload": {"id": thread, "source": source}}


def event(kind, turn="turn", timestamp=2000, **fields):
    return {"type": "event_msg", "timestamp": timestamp,
            "payload": {"type": kind, "turn_id": turn, **fields}}


def encode(*records):
    return b"".join(json.dumps(record).encode() + b"\n" for record in records)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "session.jsonl"
        self.state = {}
        self.watcher = Watcher(self.root, self.state, activated_at=1000)

    def write(self, *records, path=None):
        (path or self.path).write_bytes(encode(*records))

    def append(self, *records, path=None):
        with (path or self.path).open("ab") as stream:
            stream.write(encode(*records))

    def complete(self, turn="turn", duration_ms=121000, timestamp=2000, **fields):
        return event("task_complete", turn, timestamp, completed_at=timestamp,
                     duration_ms=duration_ms, **fields)

    def test_strict_threshold_uses_millisecond_duration(self):
        self.write(meta(), self.complete("short", 119999),
                   self.complete("boundary", 120000),
                   self.complete("long", 120001))
        alerts = self.watcher.poll()
        self.assertEqual([a["id"] for a in alerts], ["thread:long"])
        self.assertEqual(alerts[0]["seconds"], 120.001)
        self.assertEqual(self.watcher.poll(), [])

    def test_aborted_turn_never_alerts(self):
        self.write(meta(), event("task_started", timestamp=1100),
                   event("turn_aborted", timestamp=1400, completed_at=1400),
                   self.complete(timestamp=1500))
        self.assertEqual(self.watcher.poll(), [])
        self.assertNotIn("thread:turn", self.state["started"])

    def test_duplicate_across_files_and_json_restart(self):
        content = (meta(), self.complete())
        self.write(*content)
        self.write(*content, path=self.root / "copied.jsonl")
        self.assertEqual(len(self.watcher.poll()), 1)
        resumed_state = json.loads(json.dumps(self.state))
        resumed = Watcher(self.root, resumed_state, activated_at=1000)
        self.write(*content, path=self.root / "another-copy.jsonl")
        self.assertEqual(resumed.poll(), [])

    def test_first_subagent_metadata_cannot_be_overridden_by_parent(self):
        self.write(meta("child", {"subagent": {"thread_spawn": {"parent_thread_id": "thread"}}}),
                   meta("thread"), self.complete())
        self.assertEqual(self.watcher.poll(), [])
        cursor = self.state["files"][str(self.path)]
        self.assertTrue(cursor["ignored"])
        self.assertEqual(cursor["thread_id"], "child")

    def test_first_root_identity_is_stable(self):
        self.write(meta("first"), meta("second"), self.complete())
        self.assertEqual(self.watcher.poll()[0]["id"], "first:turn")

    def test_partial_record_cursor_is_not_committed_across_restart(self):
        prefix = encode(meta(), event("task_started", timestamp=1100))
        completion = encode(event("task_complete", timestamp=1221))
        split = len(completion) // 2
        self.path.write_bytes(prefix + completion[:split])
        self.assertEqual(self.watcher.poll(), [])
        self.assertEqual(self.state["files"][str(self.path)]["offset"], len(prefix))
        resumed = Watcher(self.root, json.loads(json.dumps(self.state)), activated_at=1000)
        with self.path.open("ab") as stream:
            stream.write(completion[split:])
        alerts = resumed.poll()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["seconds"], 121)

    def test_initial_scan_recovers_active_start_but_no_historical_alerts(self):
        self.write(meta(), self.complete("old", timestamp=999),
                   event("task_started", "active", timestamp=900))
        self.assertEqual(self.watcher.poll(), [])
        self.append(event("task_complete", "active", timestamp=1100))
        self.assertEqual(self.watcher.poll()[0]["seconds"], 200)

    def test_unchanged_files_are_not_opened(self):
        self.write(meta(), self.complete())
        self.watcher.poll()
        with patch.object(Path, "open", side_effect=AssertionError("unchanged file opened")):
            self.assertEqual(self.watcher.poll(), [])

    def test_old_untouched_files_baselined_without_open_then_recovered_when_changed(self):
        self.write(meta(), event("task_started", timestamp=900))
        old = time.time() - 49 * 3600
        os.utime(self.path, (old, old))
        with patch.object(Path, "open", side_effect=AssertionError("old file opened")):
            self.assertEqual(self.watcher.poll(), [])
        self.append(event("task_complete", timestamp=1100))
        self.assertEqual(len(self.watcher.poll()), 1)

    def test_rotation_and_truncation_do_not_replay_alerts(self):
        self.write(meta(), self.complete("first"))
        self.assertEqual(len(self.watcher.poll()), 1)
        self.path.rename(self.root / "rotated.jsonl")
        self.write(meta(), self.complete("second"))
        self.assertEqual([a["turn_id"] for a in self.watcher.poll()], ["second"])
        self.write(meta(), self.complete("third", note="x" * 1000))
        self.assertEqual([a["turn_id"] for a in self.watcher.poll()], ["third"])
        self.write(meta(), self.complete("last"))
        self.assertEqual([a["turn_id"] for a in self.watcher.poll()], ["last"])

    def test_simultaneous_tasks_have_independent_start_times(self):
        self.write(meta("thread-a"), event("task_started", timestamp=1001))
        second = self.root / "b.jsonl"
        self.write(meta("thread-b"), event("task_started", timestamp=1050), path=second)
        self.watcher.poll()
        self.append(event("task_complete", timestamp=1200))
        self.append(event("task_complete", timestamp=1200), path=second)
        alerts = self.watcher.poll()
        self.assertEqual({a["thread_id"]: a["seconds"] for a in alerts},
                         {"thread-a": 199, "thread-b": 150})

    def test_cross_file_starts_are_processed_by_time(self):
        self.write(meta(), event("task_complete", timestamp=1400), path=self.root / "a.jsonl")
        self.write(meta(), event("task_started", timestamp=1000), path=self.root / "z.jsonl")
        self.assertEqual(self.watcher.poll()[0]["seconds"], 400)

    def test_malformed_and_oversized_records_do_not_block_following_events(self):
        self.path.write_bytes(encode(meta()) + b"{broken}\n[]\n" +
                              b"x" * (watcher_module.MAX_RECORD_BYTES + 20) + b"\n" +
                              encode(self.complete()))
        self.assertEqual(len(self.watcher.poll()), 1)

    def test_iso_timestamp_fallback_and_invalid_durations(self):
        self.write(meta(), event("task_started", timestamp="1970-01-01T00:20:00Z"),
                   event("task_complete", timestamp="1970-01-01T00:22:00.001+00:00", duration_ms=True))
        self.assertAlmostEqual(self.watcher.poll()[0]["seconds"], 120.001)


if __name__ == "__main__":
    unittest.main()
