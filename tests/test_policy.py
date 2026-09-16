"""Scheduling and delivery behavior with synthetic tasks and no external IO."""

from datetime import datetime
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_alert import alert, attention, policy
from codex_alert.watcher import Watcher


PHONE = {"provider": "ntfy", "topic": "codex-" + "b" * 32}


def local_time(hour, minute=0):
    return datetime(2026, 6, 16, hour, minute).timestamp()


def task(number=1, kind="completed"):
    return {"id": f"thread-{number}:turn", "thread_id": f"thread-{number}",
            "turn_id": "turn", "kind": kind, "seconds": 125, "completed_at": 1000}


class ScheduleTests(unittest.TestCase):
    def test_overnight_quiet_hours_include_start_and_exclude_end(self):
        config = {"quiet_hours": {"enabled": True, "start": "22:00", "end": "08:00"}}
        for hour, minute, expected in ((21, 59, False), (22, 0, True), (23, 59, True),
                                       (0, 0, True), (7, 59, True), (8, 0, False)):
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(policy.is_quiet(config, local_time(hour, minute)), expected)

    def test_daytime_quiet_hours_and_disabled_schedule(self):
        config = {"quiet_hours": {"enabled": True, "start": "09:30", "end": "17:00"}}
        for hour, minute, expected in ((9, 29, False), (9, 30, True), (16, 59, True),
                                       (17, 0, False), (23, 0, False)):
            self.assertEqual(policy.is_quiet(config, local_time(hour, minute)), expected)
        config["quiet_hours"]["enabled"] = False
        self.assertFalse(policy.is_quiet(config, local_time(12)))

    def test_oldest_batch_window_releases_completions_but_not_future_retries(self):
        pending = [dict(task(1), deliver_after=1010), dict(task(2), deliver_after=1015),
                   dict(task(3), deliver_after=1000, retry_at=1020)]
        config = {"group_seconds": 10}
        self.assertEqual(policy.ready_groups(pending, config, 1009), [])
        self.assertEqual(policy.ready_groups(pending, config, 1010), [pending[:2]])
        self.assertEqual(policy.ready_groups(pending, config, 1020), [pending])

    def test_batches_are_capped_at_five_and_grouping_can_be_disabled(self):
        pending = [dict(task(i), deliver_after=1000) for i in range(12)]
        self.assertEqual([len(group) for group in policy.ready_groups(pending, {"group_seconds": 10}, 1000)],
                         [5, 5, 2])
        self.assertEqual([len(group) for group in policy.ready_groups(pending, {"group_seconds": 0}, 1000)],
                         [1] * 12)

    def test_failure_and_attention_are_immediate_individual_alerts(self):
        pending = [dict(task(1), deliver_after=2000), task(2, "failed"), task(3, "attention")]
        self.assertEqual(policy.ready_groups(pending, {"group_seconds": 10}, 1000),
                         [[pending[1]], [pending[2]]])
        self.assertEqual(policy.ready_groups(pending, {"paused_until": 1001}, 1000), [])
        config = {"quiet_hours": {"enabled": True, "start": "22:00", "end": "08:00"}}
        self.assertEqual(policy.ready_groups(pending, config, local_time(23)), [])


class ScheduledDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = alert.validate_config({"phone": PHONE, "group_seconds": 10})
        self.state = {"activated_at": 900}
        self.clock = mock.patch.object(alert.time, "time", return_value=1000).start()
        self.addCleanup(mock.patch.stopall)
        self.send = mock.patch.object(alert, "send_phone", return_value=True).start()
        self.flash = mock.patch.object(alert, "flash").start()
        self.titles = mock.Mock()
        self.titles.resolve.side_effect = lambda thread: "Name " + thread

    def drain(self, now=1000, refresh=None):
        self.clock.return_value = now
        alert.drain(self.home, self.state, self.config, self.titles, refresh=refresh)

    def control(self, command, now, *extra):
        """Persist settings as the app would, with the watcher not polling."""
        self.clock.return_value = now
        alert.save_json(self.home / "config.json", self.config)
        old_umask = os.umask(0o077)
        try:
            with redirect_stdout(io.StringIO()):
                result = alert.main(["--home", str(self.home), command, *extra])
        finally:
            os.umask(old_umask)
        self.assertEqual(result, 0)
        self.config = alert.config_for(self.home)

    def test_completions_wait_then_send_one_summary_and_one_flash(self):
        alert.enqueue(self.state, [task(1), task(2)], self.config)
        self.drain(1009)
        self.send.assert_not_called()
        self.flash.assert_not_called()
        self.drain(1010)
        self.send.assert_called_once()
        self.flash.assert_called_once_with(self.home)
        call = self.send.call_args
        self.assertEqual(call.kwargs, {"kind": "completed", "count": 2})
        self.assertIn("Name thread-1", call.args[1])
        self.assertIn("Name thread-2", call.args[1])
        self.assertEqual(self.state["pending"], [])
        saved = (self.home / "state.json").read_text()
        self.assertNotIn("Name thread-", saved)
        self.assertNotIn(PHONE["topic"], saved)

    def test_batch_retry_respects_delay_and_never_repeats_flash(self):
        alert.enqueue(self.state, [task(1), task(2)], self.config)
        self.send.side_effect = [alert.AlertError("unavailable"), True]
        with self.assertLogs(level="WARNING"):
            self.drain(1010)
        self.assertEqual([item["retry_at"] for item in self.state["pending"]], [1070, 1070])
        self.drain(1069)
        self.assertEqual(self.send.call_count, 1)
        self.drain(1070)
        self.assertEqual(self.send.call_count, 2)
        self.flash.assert_called_once()
        self.assertEqual(self.state["last_delivery_status"], "accepted")
        self.assertEqual(self.state["pending"], [])

    def test_quiet_hours_defer_both_channels_then_release_summary(self):
        self.config["quiet_hours"] = {"enabled": True, "start": "09:00", "end": "17:00"}
        self.clock.return_value = local_time(16)
        alert.enqueue(self.state, [task(1), task(2)], self.config)
        self.drain(local_time(16, 59))
        self.send.assert_not_called()
        self.flash.assert_not_called()
        self.assertEqual(len(self.state["pending"]), 2)
        self.drain(local_time(17))
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.kwargs["count"], 2)
        self.flash.assert_called_once()

    def test_pause_drops_queued_and_new_events_without_replaying_watcher_history(self):
        alert.enqueue(self.state, [task(1)], self.config)
        self.config["paused_until"] = 2000
        sessions = self.home / "sessions"
        sessions.mkdir()
        records = [
            {"type": "session_meta", "payload": {"id": "other", "source": "vscode"}},
            {"type": "event_msg", "timestamp": 1000,
             "payload": {"type": "task_complete", "turn_id": "turn", "duration_ms": 121000}},
        ]
        (sessions / "synthetic.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
        watcher = Watcher(sessions, self.state.setdefault("watcher", {}), activated_at=900)
        events = watcher.poll()
        self.assertEqual(len(events), 1)
        alert.enqueue(self.state, events, self.config)
        self.drain(1000)
        self.assertEqual(self.state["pending"], [])
        self.config["paused_until"] = 0
        alert.enqueue(self.state, watcher.poll(), self.config)
        self.drain(2000)
        self.send.assert_not_called()
        self.flash.assert_not_called()
        self.assertEqual(self.state["pending"], [])

    def test_completion_during_offline_pause_is_suppressed_when_polled_after_resume(self):
        self.control("pause", 1000, "--minutes", "60")
        self.control("resume", 1100)
        sessions = self.home / "sessions"
        sessions.mkdir()
        records = [
            {"type": "session_meta", "payload": {"id": "offline", "source": "vscode"}},
            {"type": "event_msg", "timestamp": 1050,
             "payload": {"type": "task_complete", "turn_id": "turn", "duration_ms": 121000}},
        ]
        (sessions / "synthetic.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
        self.clock.return_value = 1200
        watcher = Watcher(sessions, self.state.setdefault("watcher", {}), activated_at=900)
        events = watcher.poll()
        self.assertEqual(len(events), 1)
        alert.enqueue(self.state, events, self.config)
        self.drain(1210)
        self.assertEqual(self.state["pending"], [])
        self.send.assert_not_called()
        self.flash.assert_not_called()
        self.assertEqual(watcher.poll(), [])

    def test_queue_is_discarded_when_pause_and_resume_happen_between_polls(self):
        alert.enqueue(self.state, [task(1), task(2)], self.config)
        # A saved legacy item may have only queued_at, without completed_at.
        self.state["pending"][1].pop("completed_at")
        saved_state = json.loads(json.dumps(self.state))
        self.control("pause", 1001, "--minutes", "60")
        self.control("resume", 1002)
        self.state = saved_state
        self.drain(1020)
        self.assertEqual(self.state["pending"], [])
        self.send.assert_not_called()
        self.flash.assert_not_called()

    def test_task_finishing_after_early_resume_is_delivered(self):
        self.control("pause", 1000, "--minutes", "60")
        self.control("resume", 1100)
        self.assertEqual(self.config["discard_before"], 1100)
        self.assertEqual(self.config["paused_until"], 0)
        self.clock.return_value = 1101
        alert.enqueue(self.state, [dict(task(1), completed_at=1101)], self.config)
        self.drain(1111)
        self.send.assert_called_once()
        self.assertIn("Name thread-1", self.send.call_args.args[1])
        self.assertEqual(self.state["pending"], [])

    def test_natural_pause_expiry_discards_paused_history_but_allows_later_completion(self):
        self.control("pause", 1000, "--minutes", "1")
        self.assertEqual(self.config["discard_before"], 1060)
        self.clock.return_value = 1070
        alert.enqueue(self.state, [dict(task(1), completed_at=1059),
                                   dict(task(2), completed_at=1060),
                                   dict(task(3), completed_at=1061)], self.config)
        self.drain(1080)
        self.send.assert_called_once()
        message = self.send.call_args.args[1]
        self.assertIn("Name thread-3", message)
        self.assertNotIn("Name thread-1", message)
        self.assertNotIn("Name thread-2", message)
        self.assertEqual(self.state["pending"], [])

    def test_redundant_resume_preserves_notifications_deferred_by_quiet_hours(self):
        self.config["quiet_hours"] = {"enabled": True, "start": "09:00", "end": "17:00"}
        self.config["discard_before"] = local_time(15)
        self.clock.return_value = local_time(16)
        alert.enqueue(self.state, [dict(task(1), completed_at=local_time(16))], self.config)
        self.control("resume", local_time(16, 30))
        self.assertEqual(self.config["discard_before"], local_time(15))
        self.drain(local_time(16, 31))
        self.send.assert_not_called()
        self.assertEqual(len(self.state["pending"]), 1)
        self.drain(local_time(17))
        self.send.assert_called_once()
        self.assertEqual(self.state["pending"], [])

    def test_failed_task_bypasses_batching_and_has_its_own_message(self):
        alert.enqueue(self.state, [task(1), task(2, "failed")], self.config)
        self.drain(1000)
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.kwargs, {"kind": "failed", "count": 1})
        self.assertIn("Task failed. Open Codex", self.send.call_args.args[1])
        self.assertNotIn("Completed in", self.send.call_args.args[1])
        self.assertEqual([item["id"] for item in self.state["pending"]], [task(1)["id"]])

    def test_resolved_attention_is_discarded_before_delivery(self):
        payload = {"hook_event_name": "PermissionRequest", "session_id": "thread", "turn_id": "turn",
                   "tool_name": "Bash", "tool_input": {"command": "private command"}}
        attention.capture(self.home, "PermissionRequest", io.StringIO(json.dumps(payload)), now=1000)
        alert.enqueue(self.state, attention.poll(self.home, self.state, now=1000), self.config)
        payload["hook_event_name"] = "PostToolUse"
        attention.capture(self.home, "PostToolUse", io.StringIO(json.dumps(payload)), now=1001)
        self.drain(1001)
        self.send.assert_not_called()
        self.flash.assert_not_called()
        self.assertEqual(self.state["pending"], [])

    def test_attention_resolution_during_refresh_prevents_a_stale_notification(self):
        alert.enqueue(self.state, [task(1, "attention")], self.config)
        refresh = mock.Mock()
        with mock.patch.object(attention, "is_pending", side_effect=[True, False]):
            self.drain(refresh=refresh)
        refresh.assert_called_once()
        self.send.assert_not_called()
        self.flash.assert_not_called()


class NotificationPriorityTests(unittest.TestCase):
    def test_failures_and_approval_requests_use_ntfy_high_priority(self):
        with mock.patch.object(alert.urllib.request, "build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.read.return_value = json.dumps({"event": "message", "id": "synthetic",
                                                     "topic": PHONE["topic"], "message": "Synthetic"}).encode()
            for kind, priority in (("completed", "3"), ("failed", "4"), ("attention", "4")):
                with self.subTest(kind=kind):
                    alert.send_phone(PHONE, "Synthetic", kind=kind)
                    request = build.return_value.open.call_args.args[0]
                    self.assertEqual(request.get_header("Priority"), priority)
                    self.assertNotIn(PHONE["topic"], str(request.headers))


if __name__ == "__main__":
    unittest.main()
