"""CLI settings, atomic validation, and private setup without real delivery."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_alert import alert


TOPIC = "codex-" + "c" * 32


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.sessions = self.home / "synthetic-sessions"
        self.sessions.mkdir()
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        self.config = alert.validate_config({"phone": {"provider": "ntfy", "topic": TOPIC}})
        alert.save_json(self.home / "config.json", self.config)
        self.send_patch = mock.patch.object(alert, "send_phone")
        self.send = self.send_patch.start()
        self.addCleanup(self.send_patch.stop)

    def command(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = alert.main(["--home", str(self.home), "--sessions", str(self.sessions), *args])
        self.assertNotIn(TOPIC, output.getvalue())
        self.assertNotIn(TOPIC, errors.getvalue())
        return result, output.getvalue(), errors.getvalue()

    def test_valid_settings_change_together_and_preserve_private_subscription(self):
        result, _, errors = self.command("settings", "--set", "flash=false", "--set", "include_task_name=false",
                                         "--set", "keep_awake=always", "--set", "min_seconds=90",
                                         "--set", "group_seconds=15", "--set", "quiet_hours.enabled=true",
                                         "--set", "quiet_hours.start=10:00", "--set", "quiet_hours.end=18:00")
        self.assertEqual((result, errors), (0, ""))
        saved = alert.config_for(self.home)
        self.assertFalse(saved["flash"])
        self.assertFalse(saved["include_task_name"])
        self.assertEqual(saved["keep_awake"], "always")
        self.assertEqual(saved["min_seconds"], 90)
        self.assertEqual(saved["group_seconds"], 15)
        self.assertEqual(saved["quiet_hours"], {"enabled": True, "start": "10:00", "end": "18:00"})
        self.assertEqual(saved["phone"], self.config["phone"])
        self.assertEqual(stat.S_IMODE((self.home / "config.json").stat().st_mode), 0o600)
        self.send.assert_not_called()

    def test_invalid_setting_rolls_back_entire_multi_setting_operation(self):
        original = (self.home / "config.json").read_bytes()
        cases = ("flash=yes", "include_task_name=1", "group_seconds=-1", "group_seconds=301",
                 "group_seconds=nan", "group_seconds=inf", "min_seconds=86401", "keep_awake=invalid",
                 "quiet_hours.start=24:00", "quiet_hours.end=8:00", "phone.topic=private", "broken")
        for invalid in cases:
            with self.subTest(invalid=invalid):
                result, _, errors = self.command("settings", "--set", "flash=false", "--set", invalid)
                self.assertEqual(result, 1)
                self.assertTrue(errors)
                self.assertEqual((self.home / "config.json").read_bytes(), original)

    def test_invalid_quiet_period_does_not_leave_partial_configuration(self):
        original = (self.home / "config.json").read_bytes()
        result, _, _ = self.command("settings", "--set", "quiet_hours.enabled=true",
                                    "--set", "quiet_hours.start=08:00", "--set", "quiet_hours.end=08:00")
        self.assertEqual(result, 1)
        self.assertEqual((self.home / "config.json").read_bytes(), original)

    def test_atomic_replacement_failure_keeps_previous_configuration_and_hides_details(self):
        original = (self.home / "config.json").read_bytes()
        with mock.patch.object(alert.os, "replace", side_effect=OSError("private filesystem detail")):
            result, _, errors = self.command("settings", "--set", "flash=false")
        self.assertEqual(result, 1)
        self.assertNotIn("private filesystem detail", errors)
        self.assertEqual((self.home / "config.json").read_bytes(), original)
        self.assertEqual(list(self.home.glob(".config.json*")), [])

    def test_setup_phone_is_idempotent_and_does_not_print_or_send_subscription(self):
        (self.home / "config.json").unlink()
        with mock.patch.object(alert.secrets, "token_hex", return_value="d" * 32) as generate:
            first, output, errors = self.command("setup-phone")
            original = (self.home / "config.json").read_bytes()
            second, second_output, second_errors = self.command("setup-phone")
        self.assertEqual((first, second, errors, second_errors), (0, 0, "", ""))
        generate.assert_called_once_with(16)
        self.assertEqual((self.home / "config.json").read_bytes(), original)
        topic = alert.config_for(self.home)["phone"]["topic"]
        self.assertEqual(topic, "codex-" + "d" * 32)
        self.assertNotIn(topic, output + second_output)
        self.send.assert_not_called()

    def test_private_setup_repairs_bad_phone_and_discards_obsolete_credentials(self):
        alert.save_json(self.home / "config.json", {"phone": {"provider": "unsupported",
                                                              "api_key": "obsolete-private-value"}})
        result, output, errors = self.command("setup-phone")
        self.assertEqual((result, errors), (0, ""))
        saved = (self.home / "config.json").read_text()
        self.assertNotIn("obsolete-private-value", saved + output)
        self.assertEqual(alert.config_for(self.home)["phone"]["provider"], "ntfy")
        self.send.assert_not_called()

    def test_pause_duration_validation_and_resume_preserve_other_preferences(self):
        with mock.patch.object(alert.time, "time", return_value=1000):
            self.assertEqual(self.command("pause", "--minutes", "60")[0], 0)
        paused = alert.config_for(self.home)
        self.assertEqual(paused["paused_until"], 4600)
        self.assertEqual(paused["phone"], self.config["phone"])
        original = (self.home / "config.json").read_bytes()
        for invalid in ("-1", "1441", "nan", "inf"):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.command("pause", "--minutes", invalid)[0], 1)
                self.assertEqual((self.home / "config.json").read_bytes(), original)
        self.assertEqual(self.command("resume")[0], 0)
        self.assertEqual(alert.config_for(self.home)["paused_until"], 0)

    def test_status_json_contains_operational_fields_without_private_data(self):
        alert.save_json(self.home / "state.json", {
            "heartbeat_at": 1000, "active_tasks": 2, "keep_awake_active": True,
            "pending": [{"thread_id": "private-thread-identifier", "private_name": "private task name"}],
            "last_notification_at": 999, "last_notification_kind": "failed", "last_delivery_status": "accepted",
            "unknown_private_field": "private content",
        })
        with mock.patch.object(alert, "watcher_running", return_value=True), \
                mock.patch.object(alert.time, "time", return_value=1001):
            result, output, errors = self.command("status", "--json")
        self.assertEqual((result, errors), (0, ""))
        status = json.loads(output)
        self.assertEqual(status["watcher"], "active")
        self.assertEqual(status["active_tasks"], 2)
        self.assertTrue(status["keep_awake_active"])
        self.assertEqual(status["pending_alerts"], 1)
        self.assertEqual(status["phone"], "ntfy")
        self.assertEqual(status["last_notification_kind"], "failed")
        self.assertEqual(status["last_delivery_status"], "accepted")
        self.assertNotIn("private", output)
        self.assertNotIn("topic", output)
        self.assertNotIn(str(self.home), output)

    def test_stale_status_does_not_claim_active_tasks_or_awake_protection(self):
        alert.save_json(self.home / "state.json", {"heartbeat_at": 900, "active_tasks": 2,
                                                  "keep_awake_active": True})
        with mock.patch.object(alert, "watcher_running", return_value=True), \
                mock.patch.object(alert.time, "time", return_value=1001):
            _, output, _ = self.command("status", "--json")
        status = json.loads(output)
        self.assertEqual(status["active_tasks"], 0)
        self.assertFalse(status["keep_awake_active"])
        self.assertNotEqual(status["watcher"], "active")


if __name__ == "__main__":
    unittest.main()
