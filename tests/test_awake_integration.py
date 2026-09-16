"""Power coordination with the alert loop; native effects are always mocked."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_alert import alert


class AwakeIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.phone = {"provider": "ntfy", "topic": "codex-" + "a" * 32}
        self.config = dict(alert.DEFAULT_CONFIG, phone=self.phone, flash=False,
                           keep_awake="always", poll_seconds=60, group_seconds=0)
        alert.save_json(self.home / "config.json", self.config)

    def test_last_notification_is_attempted_before_releasing_power(self):
        sequence = []
        power = mock.Mock(active=False)

        def update(active, mode):
            self.assertEqual(mode, "always")
            if active != power.active:
                sequence.append("awake" if active else "released")
            power.active = active

        power.update.side_effect = update
        monitor = mock.Mock()
        monitor.active_task_count.side_effect = [1, 0]
        monitor.poll.side_effect = [[], [{"thread_id": "synthetic", "seconds": 125}]]
        monitor.has_active_tasks.side_effect = [True, False]

        def send(*_):
            self.assertTrue(power.active)
            sequence.append("sent")
            return True

        with mock.patch.object(alert, "Watcher", return_value=monitor), \
                mock.patch.object(alert, "KeepAwake", return_value=power), \
                mock.patch.object(alert, "TitleResolver"), \
                mock.patch.object(alert, "send_phone", side_effect=send), \
                mock.patch.object(alert.logging, "basicConfig"), \
                mock.patch.object(alert, "RotatingFileHandler"), \
                mock.patch.object(alert.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleep:
            alert.watch(self.home, self.home / "sessions")
        self.assertEqual(sequence, ["awake", "sent", "released"])
        self.assertEqual(sleep.call_args_list, [mock.call(15), mock.call(15)])
        power.close.assert_called_once_with()
        state = alert.read_json(self.home / "state.json", {})
        self.assertFalse(state["keep_awake_active"])
        self.assertEqual(state["pending"], [])

    def test_watcher_error_and_exit_release_assertion(self):
        power = mock.Mock(active=False)
        monitor = mock.Mock()
        monitor.active_task_count.return_value = 0
        monitor.poll.side_effect = [RuntimeError("synthetic failure"), KeyboardInterrupt]
        with mock.patch.object(alert, "Watcher", return_value=monitor), \
                mock.patch.object(alert, "KeepAwake", return_value=power), \
                mock.patch.object(alert, "TitleResolver"), \
                mock.patch.object(alert.logging, "basicConfig"), \
                mock.patch.object(alert, "RotatingFileHandler"), \
                mock.patch.object(alert.time, "sleep"), \
                self.assertLogs(level="ERROR"):
            alert.watch(self.home, self.home / "sessions")
        self.assertEqual(power.close.call_count, 2)
        self.assertFalse(alert.read_json(self.home / "state.json", {})["keep_awake_active"])

    def test_draining_many_alerts_refreshes_lease_between_deliveries(self):
        state = {}
        alert.enqueue(state, [{"seconds": 125} for _ in range(6)], self.config)
        order = []
        with mock.patch.object(alert, "send_phone", side_effect=lambda *_: order.append("sent")):
            alert.drain(self.home, state, self.config, refresh=lambda: order.append("refresh"))
        self.assertEqual(order, ["refresh", "sent"] * 6)
        self.assertEqual(state["pending"], [])

    def test_explicit_off_releases_before_draining(self):
        alert.save_json(self.home / "config.json", dict(self.config, keep_awake="off"))
        power = mock.Mock(active=True)
        power.update.side_effect = lambda *_: setattr(power, "active", False)
        monitor = mock.Mock()
        monitor.active_task_count.return_value = 0
        monitor.poll.return_value = []
        monitor.has_active_tasks.return_value = False

        def drain(*args, **kwargs):
            self.assertFalse(power.active)

        with mock.patch.object(alert, "Watcher", return_value=monitor), \
                mock.patch.object(alert, "KeepAwake", return_value=power), \
                mock.patch.object(alert, "TitleResolver"), \
                mock.patch.object(alert, "drain", side_effect=drain), \
                mock.patch.object(alert.logging, "basicConfig"), \
                mock.patch.object(alert, "RotatingFileHandler"), \
                mock.patch.object(alert.time, "sleep", side_effect=KeyboardInterrupt):
            alert.watch(self.home, self.home / "sessions")
        self.assertEqual(power.update.call_args_list[0], mock.call(False, "off"))

    def test_power_mode_updates_privately_without_changing_subscription(self):
        for mode in ("always", "off", "plugged_in"):
            with self.subTest(mode=mode), contextlib.redirect_stdout(io.StringIO()) as output:
                code = alert.main(["--home", str(self.home), "power-mode", "--mode", mode])
            self.assertEqual(code, 0)
            config = json.loads((self.home / "config.json").read_text())
            self.assertEqual(config["phone"], self.phone)
            self.assertEqual(config["keep_awake"], mode)
            self.assertNotIn(self.phone["topic"], output.getvalue())

    def test_mode_default_and_invalid_values(self):
        (self.home / "config.json").unlink()
        self.assertEqual(alert.config_for(self.home)["keep_awake"], "plugged_in")
        for value in (True, None, [], "invalid"):
            alert.save_json(self.home / "config.json", {"keep_awake": value})
            with self.subTest(value=value), self.assertRaises(alert.AlertError):
                alert.config_for(self.home)

    def test_inactive_watcher_does_not_report_stale_power_assertion(self):
        alert.save_json(self.home / "state.json", {"keep_awake_active": True, "heartbeat_at": 100})
        with mock.patch.object(alert, "watcher_running", return_value=False):
            self.assertFalse(alert.status_for(self.home, self.home)["keep_awake_active"])


if __name__ == "__main__":
    unittest.main()
