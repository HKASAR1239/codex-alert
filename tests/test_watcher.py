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

    def poll_at(self, now):
        with patch.object(watcher_module.time, "time", return_value=now):
            return self.watcher.poll()

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

    def test_active_task_survives_silence_until_bounded_grace_expires(self):
        self.write(meta(), event("task_started", timestamp=2000))
        self.poll_at(2000)
        grace = watcher_module.ACTIVE_TASK_IDLE_SECONDS
        self.assertTrue(self.watcher.has_active_tasks(2000))
        self.assertTrue(self.watcher.has_active_tasks(2000 + grace))
        self.assertFalse(self.watcher.has_active_tasks(2001 + grace))
        self.assertEqual(self.state["started"]["thread:turn"], 2000)

    def test_complete_record_activity_restores_protection_without_retaining_text(self):
        self.write(meta(), event("task_started", timestamp=2000))
        self.poll_at(2000)
        later = 2001 + watcher_module.ACTIVE_TASK_IDLE_SECONDS
        self.assertFalse(self.watcher.has_active_tasks(later))
        self.append({"type": "response_item", "timestamp": later,
                     "payload": {"type": "message", "content": "private conversation text"}})
        self.poll_at(later)
        self.assertTrue(self.watcher.has_active_tasks(later))
        self.assertNotIn("private conversation text", json.dumps(self.state))
        self.append(event("task_complete", timestamp=later + 1))
        self.assertEqual(self.poll_at(later + 1)[0]["seconds"], later + 1 - 2000)
        self.assertFalse(self.watcher.has_active_tasks(later + 1))

    def test_aborted_and_completed_tasks_release_protection(self):
        for kind in ("task_complete", "turn_aborted"):
            with self.subTest(kind=kind):
                turn = kind
                self.write(meta(), event("task_started", turn, timestamp=2000))
                self.poll_at(2000)
                self.assertTrue(self.watcher.has_active_tasks(2000))
                self.append(event(kind, turn, timestamp=2001))
                self.poll_at(2001)
                self.assertFalse(self.watcher.has_active_tasks(2001))

    def test_concurrent_threads_keep_protection_until_all_finish(self):
        second = self.root / "second.jsonl"
        self.write(meta("thread-a"), event("task_started", timestamp=2000))
        self.write(meta("thread-b"), event("task_started", timestamp=2000), path=second)
        self.poll_at(2000)
        self.append(event("task_complete", timestamp=2200))
        self.poll_at(2200)
        self.assertTrue(self.watcher.has_active_tasks(2200))
        self.append(event("turn_aborted", timestamp=2201), path=second)
        self.poll_at(2201)
        self.assertFalse(self.watcher.has_active_tasks(2201))

    def test_newer_completed_turn_does_not_revive_abandoned_start(self):
        self.write(meta(), event("task_started", "abandoned", timestamp=2000),
                   event("task_started", "new", timestamp=2100))
        self.poll_at(2100)
        self.assertTrue(self.watcher.has_active_tasks(2100))
        self.append(event("task_complete", "new", timestamp=2200))
        self.poll_at(2200)
        self.assertIn("thread:abandoned", self.state["started"])
        self.assertFalse(self.watcher.has_active_tasks(2200))
        self.append(event("token_count", timestamp=2300))
        self.poll_at(2300)
        self.assertFalse(self.watcher.has_active_tasks(2300))

    def test_newest_lifecycle_event_is_selected_across_rotated_files(self):
        self.write(meta(), event("task_started", "old", timestamp=2000),
                   path=self.root / "z.jsonl")
        self.write(meta(), event("task_started", "new", timestamp=2100),
                   event("task_complete", "new", timestamp=2200), path=self.root / "a.jsonl")
        self.poll_at(2200)
        self.assertFalse(self.watcher.has_active_tasks(2200))

    def test_continuation_file_activity_counts_for_the_same_thread(self):
        self.write(meta(), event("task_started", timestamp=2000))
        self.poll_at(2000)
        later = 2001 + watcher_module.ACTIVE_TASK_IDLE_SECONDS
        self.write(meta(), event("token_count", timestamp=later), path=self.root / "continuation.jsonl")
        self.poll_at(later)
        self.assertTrue(self.watcher.has_active_tasks(later))

    def test_stale_history_does_not_become_active_when_file_is_touched(self):
        self.write(meta(), event("task_started", timestamp=2000))
        later = 2001 + watcher_module.ACTIVE_TASK_IDLE_SECONDS
        self.poll_at(later)
        self.assertFalse(self.watcher.has_active_tasks(later))
        os.utime(self.path, (later + 1, later + 1))
        self.poll_at(later + 1)
        self.assertFalse(self.watcher.has_active_tasks(later + 1))

    def test_recent_turn_started_before_activation_can_be_active(self):
        self.write(meta(), event("task_started", timestamp=900),
                   event("token_count", timestamp=1100))
        self.poll_at(1100)
        self.assertTrue(self.watcher.has_active_tasks(1100))

    def test_subagent_activity_never_keeps_computer_awake(self):
        self.write(meta("child", {"subagent": {"thread_spawn": {"parent_thread_id": "thread"}}}),
                   event("task_started", timestamp=2000), event("token_count", timestamp=2100))
        self.poll_at(2100)
        self.assertFalse(self.watcher.has_active_tasks(2100))

    def test_future_timestamps_do_not_keep_computer_awake_or_poison_activity(self):
        self.write(meta(), event("task_started", timestamp=2000),
                   event("token_count", timestamp=9999999))
        self.poll_at(2000)
        self.assertTrue(self.watcher.has_active_tasks(2000))
        self.assertFalse(self.watcher.has_active_tasks(2001 + watcher_module.ACTIVE_TASK_IDLE_SECONDS))
        self.write(meta("future"), event("task_started", timestamp=9999999))
        self.poll_at(2000)
        self.assertFalse(self.watcher.has_active_tasks(2000))

    def test_start_appended_during_poll_becomes_active_without_rereading_file(self):
        self.write(meta(), event("task_started", timestamp=2000.05))
        self.poll_at(2000)
        self.assertFalse(self.watcher.has_active_tasks(2000))
        with patch.object(Path, "open", side_effect=AssertionError("unchanged file opened")):
            self.poll_at(2000.1)
        self.assertTrue(self.watcher.has_active_tasks(2000.1))

    def test_read_ahead_window_does_not_accept_far_future_activity(self):
        self.write(meta(), event("task_started", timestamp=2000),
                   event("token_count", timestamp=3000))
        self.poll_at(2000)
        self.assertTrue(self.watcher.has_active_tasks(2000))
        self.assertEqual(self.state["files"][str(self.path)]["last_activity"], 2000)
        self.assertFalse(self.watcher.has_active_tasks(2001 + watcher_module.ACTIVE_TASK_IDLE_SECONDS))
        self.write(meta("future"), event("task_started", timestamp=3000))
        self.poll_at(2000)
        self.assertNotIn("latest_task", self.state["files"][str(self.path)])
        self.assertFalse(self.watcher.has_active_tasks(2000))

    def test_partial_and_malformed_records_do_not_extend_activity(self):
        self.write(meta(), event("task_started", timestamp=2000))
        self.poll_at(2000)
        later = 2001 + watcher_module.ACTIVE_TASK_IDLE_SECONDS
        unfinished = encode(event("token_count", timestamp=later))[:-1]
        with self.path.open("ab") as stream:
            stream.write(b"{invalid}\n" + unfinished)
        self.poll_at(later)
        self.assertFalse(self.watcher.has_active_tasks(later))
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        self.poll_at(later)
        self.assertTrue(self.watcher.has_active_tasks(later))

    def test_activity_survives_saved_restart_without_rereading_unchanged_file(self):
        self.write(meta(), event("task_started", timestamp=2000))
        self.poll_at(2000)
        self.watcher = Watcher(self.root, json.loads(json.dumps(self.state)), activated_at=1000)
        with patch.object(Path, "open", side_effect=AssertionError("unchanged file opened")):
            self.poll_at(2100)
        self.assertTrue(self.watcher.has_active_tasks(2100))

    def test_legacy_saved_cursors_are_migrated_without_duplicate_alerts(self):
        self.write(meta(), event("task_started", "done", timestamp=2000),
                   event("task_complete", "done", timestamp=2200),
                   event("task_started", "active", timestamp=2300))
        self.assertEqual(len(self.poll_at(2300)), 1)
        state = json.loads(json.dumps(self.state))
        for field in ("activity_version", "last_activity", "latest_task"):
            state["files"][str(self.path)].pop(field)
        self.watcher = Watcher(self.root, state, activated_at=1000)
        self.assertEqual(self.poll_at(2400), [])
        self.assertTrue(self.watcher.has_active_tasks(2400))

    def test_deleted_files_release_protection_without_losing_start_duration(self):
        self.write(meta(), event("task_started", timestamp=2000))
        self.poll_at(2000)
        self.path.unlink()
        self.poll_at(2100)
        self.assertFalse(self.watcher.has_active_tasks(2100))
        self.assertEqual(self.state["started"]["thread:turn"], 2000)


if __name__ == "__main__":
    unittest.main()
